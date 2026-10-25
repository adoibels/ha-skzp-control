"""Walidacja, eksport i potwierdzany zapis profili nastaw."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
import json
import logging
from typing import Any

from homeassistant.exceptions import HomeAssistantError
from homeassistant.core import callback

from .const import (
    CONF_BOILER_TEMP_MAX, DEFAULT_BOILER_TEMP_MAX, MIN_BOILER_TEMP_MAX,
    MAX_BOILER_TEMP_MAX,
)
from .device import detect_device_model, SUPPORTED_DEVICE_MODELS
from .parameter_resolver import (
    get_parameter_value, is_parameter_definition_supported, parameter_candidates,
    resolve_parameter_key, resolve_parameter_write_key, resolve_parameter_write_value,
)

MAX_PROFILE_BYTES = 65536
MAX_PROFILES = 20
SCHEDULE_KEYS = frozenset({
    "C042", "C043", "C142", "C143", "C242", "C243",
    "DHWHours", "DHWDays", "DHWCDays", "DHWCHours", "CH2Days", "CH2Hours",
})
_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Setting:
    """Opis nastawy w jednostkach sterownika i interfejsu."""

    platform: str
    suffix: str
    meta: dict[str, Any]

    def decode(self, raw: Any) -> Decimal | str:
        if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
            raise ValueError("invalid_value")
        if self.platform == "number":
            try:
                value = Decimal(str(raw)) / Decimal(str(self.meta.get("divider", 1)))
            except (DecimalException, ValueError, ZeroDivisionError):
                raise ValueError("invalid_value") from None
            if not value.is_finite():
                raise ValueError("invalid_value")
            return value
        text = str(raw).strip()
        for option, wire in self.meta["modes"].items():
            if text.casefold() == wire.casefold():
                return option
        raise ValueError("invalid_value")

    def validate(self, raw: Any) -> Decimal | str:
        value = self.decode(raw)
        if self.platform == "number":
            low, high = (Decimal(str(self.meta[k])) for k in ("min", "max"))
            if not low <= value <= high:
                raise ValueError("out_of_range")
            # Odczyt może być dokładniejszy niż krok suwaka (np. pozostałe paliwo).
            divider = Decimal(str(self.meta.get("divider", 1)))
            if divider != 1 and value * divider != (value * divider).to_integral_value():
                raise ValueError("invalid_value")
            if divider == 1:
                precision = Decimal(str(self.meta["step"])).normalize().as_tuple().exponent
                quantum = Decimal(1).scaleb(precision)
                if value != value.quantize(quantum):
                    raise ValueError("invalid_value")
        return value

    def encode(self, value: Decimal | str) -> str:
        if self.platform != "number":
            return self.meta["modes"][value]
        raw = value * Decimal(str(self.meta.get("divider", 1)))
        return format(raw.normalize(), "f")

    def display(self, client, raw: Any, *, language=None) -> str:
        try:
            value = self.decode(raw)
        except ValueError:
            return "?"
        if self.platform == "number":
            unit = self.meta.get("unit") or ""
            return f"{format(value.normalize(), 'f')} {unit}".strip()
        if self.platform == "switch":
            return client.translate(f"common.profile_{value}", language=language)
        return client.translate(f"entity.select.{self.suffix}.state.{value}", language=language)

    def name(self, client, *, language=None) -> str:
        return client.translate(f"entity.{self.platform}.{self.suffix}.name", language=language)


def setting_catalog(client) -> dict[str, Setting]:
    """Korzysta z tych samych definicji i ograniczeń firmware co encje."""
    from .number import get_number_descriptions
    from .select import get_select_descriptions, SELECT_KEYS, _modes_for_value
    from .switch import get_switch_descriptions, SWITCH_KEYS

    data = client.data
    numbers = get_number_descriptions(client.model, data.get("DevType"))
    result = {}
    for key, source in numbers.items():
        if not is_parameter_definition_supported(data, key, numbers):
            continue
        meta = dict(source)
        result[key] = Setting("number", meta["unique_suffix"], meta)
    for desc in get_select_descriptions(client.model):
        key = desc.data_key
        if is_parameter_definition_supported(data, key, SELECT_KEYS):
            modes = _modes_for_value(key, resolve_parameter_key(data, key),
                                     get_parameter_value(data, key), desc.modes)
            result[key] = Setting("select", desc.unique_suffix, {"modes": modes})
    for desc in get_switch_descriptions(client.model, data.get("DevType")):
        key = desc.data_key
        if is_parameter_definition_supported(data, key, SWITCH_KEYS):
            result[key] = Setting("switch", desc.unique_suffix,
                                  {"modes": {"on": desc.on_value, "off": desc.off_value}})
    return result


def parse_profile(payload: str) -> dict:
    """Przyjmuje własny format lub eksport Timel, odrzucając wadliwy JSON."""
    if len(payload.encode("utf-8")) > MAX_PROFILE_BYTES:
        raise ValueError("invalid_profile")

    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("invalid_profile")
            result[key] = value
        return result

    try:
        profile = json.loads(payload, object_pairs_hook=unique_pairs,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        meta, params = profile["_meta"], profile["params"]
        if not isinstance(meta, dict) or not isinstance(params, dict) or not 1 <= len(params) <= 256:
            raise ValueError
        if meta.get("format") not in (None, "skzp_control"):
            raise ValueError
        if meta.get("format") == "skzp_control" and meta.get("version") != 1:
            raise ValueError
        identity = meta.get("devType")
        if not isinstance(identity, str) or len(identity) > 120:
            raise ValueError
        if detect_device_model(identity) not in SUPPORTED_DEVICE_MODELS:
            raise ValueError
        if any(len(key) > 80 for key in params):
            raise ValueError
    except (ValueError, TypeError, KeyError, RecursionError):
        raise ValueError("invalid_profile") from None
    return profile


def create_profile(client, name: str, params: dict) -> dict:
    """Tworzy przenośny profil bez danych autoryzacji i ustawień HA."""
    return {"_meta": {"format": "skzp_control", "version": 1,
                      "name": name.strip(), "devType": client.data["DevType"],
                      "date": datetime.now(timezone.utc).isoformat()},
            "params": dict(params)}


def order_profile_parameters(params: dict[str, Any]) -> dict[str, Any]:
    """Porządkuje nastawy według formularza, a nieznane klucze na końcu."""
    from .entity_layout import ENTITY_LAYOUT, SECTION_CONFIGURATION

    ordered = {
        key: params[key]
        for _, section_name, _, keys in ENTITY_LAYOUT
        if section_name == SECTION_CONFIGURATION
        for key in keys
        if key in params
    }
    ordered.update((key, params[key]) for key in sorted(params) if key not in ordered)
    return ordered


def export_parameters(client) -> tuple[dict, dict]:
    """Eksportuje wyłącznie odczytane i poprawne nastawy z białej listy."""
    params, skipped = {}, {}
    for key, setting in setting_catalog(client).items():
        raw = get_parameter_value(client.data, key)
        try:
            params[key] = setting.encode(setting.validate(raw))
        except ValueError as err:
            skipped[key] = str(err)
    return params, skipped


def prepare_profile(client, profile: dict) -> tuple[dict, dict, dict]:
    """Zwraca katalog, poprawne nastawy i powody pominięcia pozostałych."""
    if detect_device_model(profile["_meta"]["devType"]) != client.model:
        raise ValueError("profile_model_mismatch")
    catalog = setting_catalog(client)
    from .sensors_map import SENSOR_MAP
    from .select import ROOM_MODES_NUMERIC, ROOM_MODES_TEXT
    params, skipped, seen = {}, {}, set()
    for source, raw in profile["params"].items():
        key = next((candidate for candidate in parameter_candidates(source) if candidate in catalog), None)
        if key is None:
            skipped[source] = ("schedule" if source in SCHEDULE_KEYS else
                               "read_only" if source in SENSOR_MAP else "unsupported")
            continue
        if key in seen:
            params.pop(key, None)
            skipped[source] = "duplicate"
            skipped[key] = "duplicate"
            continue
        seen.add(key)
        try:
            # Dla aliasu przełącznika dopuszczamy odpowiednik tekstowy 0/1.
            if catalog[key].platform == "switch":
                normalized = str(raw).strip().casefold()
                if normalized in ("on", "off", "1", "0"):
                    option = "on" if normalized in ("on", "1") else "off"
                    raw = catalog[key].meta["modes"][option]
            elif (catalog[key].platform == "select" and
                  catalog[key].suffix in {"ch1_room_mode", "ch2_room_mode", "ch3_room_mode"}):
                normalized = str(raw).strip().casefold()
                for modes in (ROOM_MODES_NUMERIC, ROOM_MODES_TEXT):
                    option = next((name for name, wire in modes.items() if wire.casefold() == normalized), None)
                    if option is not None:
                        raw = catalog[key].meta["modes"][option]
                        break
            params[key] = catalog[key].encode(catalog[key].validate(raw))
        except ValueError as err:
            skipped[source] = str(err)
    return catalog, params, skipped


def profile_boiler_limit(catalog, options, selected) -> tuple[str, int] | None:
    """Zwraca wartość i limit wymagające osobnej zgody przed wysłaniem."""
    if "BoilerTempCmd" not in selected:
        return None
    setting = catalog["BoilerTempCmd"]
    value = setting.validate(selected["BoilerTempCmd"])
    limit = max(MIN_BOILER_TEMP_MAX, min(int(options.get(
        CONF_BOILER_TEMP_MAX, DEFAULT_BOILER_TEMP_MAX)), MAX_BOILER_TEMP_MAX))
    return (setting.encode(value), limit) if value > limit else None


def validate_profile_limit(catalog, options, selected, boiler_limit_override=None):
    """Nie pozwala wykorzystać jednorazowej zgody dla innej wartości lub limitu."""
    exceeded = profile_boiler_limit(catalog, options, selected)
    if exceeded is not None and exceeded != boiler_limit_override:
        raise ValueError("profile_boiler_limit")


def changed_parameters(catalog, params, baseline) -> dict:
    """Porównuje nastawy z profilem lub odczytem, uwzględniając aliasy i jednostki."""
    result = {}
    for key, raw in params.items():
        try:
            equal = catalog[key].decode(get_parameter_value(baseline, key)) == catalog[key].decode(raw)
        except ValueError:
            equal = False
        if not equal:
            result[key] = raw
    return result


def order_changes(client, catalog, params) -> list[str]:
    """Sprawdza docelowe pary min/max i ustala bezpieczną kolejność zapisu."""
    from .number import MIN_MAX_PAIRS

    values = {}
    for key, setting in catalog.items():
        if setting.platform != "number":
            continue
        try:
            values[key] = setting.decode(get_parameter_value(client.data, key))
        except ValueError:
            pass
    target = {**values, **{k: catalog[k].decode(v) for k, v in params.items()}}
    pairs = [(low, high) for low, high in MIN_MAX_PAIRS
             if low in catalog and high in catalog and (low in params or high in params)]
    for low, high in pairs:
        if low not in target or high not in target or target[low] > target[high]:
            raise ValueError("profile_invalid_pair")
    pending, ordered = list(params), []
    while pending:
        for key in pending:
            candidate = {**values, key: target[key]}
            if any(key in (low, high) and (
                low not in candidate or high not in candidate or candidate[low] > candidate[high]
            ) for low, high in pairs):
                continue
            values[key] = target[key]
            pending.remove(key)
            ordered.append(key)
            break
        else:
            raise ValueError("profile_invalid_pair")
    return ordered


async def apply_profile(client, options, profile: dict, selected: dict,
                        *, boiler_limit_override: tuple[str, int] | None = None) -> dict:
    """Wysyła pojedynczo, zatrzymując import po pierwszym niepotwierdzonym zapisie."""
    result = {"saved": [], "failed": [], "remaining": list(selected), "error": ""}
    if (getattr(client, "profile_import_active", False)
            or getattr(client, "pending_entities", set())
            or client.command_manager.active_count
            or (getattr(client, "_command_lock", None) is not None and client._command_lock.locked())):
        result["error"] = "profile_busy"
        _LOGGER.debug("[SKZP Control] %s:%s — Profile sending not started: another change is pending.",
                      client.host, client.port)
        return result
    client.profile_import_active = True
    in_flight = None
    cancelled = False
    _LOGGER.debug("[SKZP Control] %s:%s — Profile sending started: selected=%s.",
                  client.host, client.port, len(selected))
    try:
        if not client.available:
            raise ValueError("profile_offline")
        catalog, valid, _ = prepare_profile(client, profile)
        if any(valid.get(key) != raw for key, raw in selected.items()):
            raise ValueError("profile_changed")
        validate_profile_limit(catalog, options, selected, boiler_limit_override)
        ordered = order_changes(client, catalog, selected)
        identity = client.data.get("DevType")
        for index, key in enumerate(ordered):
            if not client.available or client.data.get("DevType") != identity:
                raise ValueError("profile_offline")
            # Po każdej ramce sprawdzamy ponownie zależności pozostałych nastaw.
            order_changes(client, catalog, {k: selected[k] for k in ordered[index:]})
            setting, raw = catalog[key], selected[key]
            log_name = setting.name(client, language="en")
            log_value = setting.display(client, raw, language="en")
            event = asyncio.Event()
            sent = False

            def matches():
                try:
                    return (client.available and client.data.get("DevType") == identity
                            and setting.decode(get_parameter_value(client.data, key)) == setting.decode(raw))
                except ValueError:
                    return False

            @callback
            def update(_event):
                if sent and matches():
                    event.set()

            remove = client.hass.bus.async_listen(client.update_event, update)
            try:
                async def send(retry_number):
                    nonlocal sent, in_flight
                    if not client.available or client.data.get("DevType") != identity:
                        raise ValueError("profile_offline")
                    validate_profile_limit(catalog, options, selected, boiler_limit_override)
                    event.clear()
                    sent = True
                    in_flight = key
                    write_key = resolve_parameter_write_key(client.data, key)
                    await client.send_command(
                        {write_key: resolve_parameter_write_value(write_key, raw)}, profile=True
                    )
                    client.log_command_sent(log_name, log_value, retry_number)

                confirmed = await client.command_manager.execute(
                    send=send,
                    wait=lambda: client.command_manager.wait_for_confirmation(
                        event, client.command_confirmation_timeout, matches),
                    is_current=lambda: True,
                    retry_count=client.command_retry_count,
                    retry_delay=client.command_retry_delay,
                    is_confirmed=lambda: event.is_set() and matches(),
                )
            finally:
                remove()
            if not confirmed:
                result["failed"].append(key)
                result["remaining"].remove(key)
                result["error"] = "profile_not_confirmed"
                break
            result["saved"].append(key)
            result["remaining"].remove(key)
            in_flight = None
            _LOGGER.debug("[SKZP Control] %s:%s — Profile change confirmed: %s = %s.",
                          client.host, client.port, log_name, log_value)
            if (index + 1) % 20 == 0 and index + 1 < len(ordered):
                await asyncio.sleep(5)
    except asyncio.CancelledError:
        cancelled = True
        raise
    except ValueError as err:
        result["error"] = str(err)
    except HomeAssistantError:
        result["error"] = "profile_send_failed"
        if in_flight is not None:
            result["failed"].append(in_flight)
            result["remaining"].remove(in_flight)
    except Exception:
        _LOGGER.exception("[SKZP Control] %s:%s — Unexpected error while sending profile settings.",
                          client.host, client.port)
        result["error"] = "profile_send_failed"
        if in_flight is not None and in_flight in result["remaining"]:
            result["failed"].append(in_flight)
            result["remaining"].remove(in_flight)
    finally:
        client.profile_import_active = False
        pending_confirmation = int(in_flight is not None and in_flight in result["remaining"])
        status = "cancelled" if cancelled else "stopped" if result["error"] else "finished"
        _LOGGER.debug("[SKZP Control] %s:%s — Profile sending %s: confirmed=%s, unconfirmed=%s, not_sent=%s.",
                      client.host, client.port, status, len(result["saved"]),
                      len(result["failed"]) + pending_confirmation,
                      len(result["remaining"]) - pending_confirmation)
    return result
