"""Sensory danych odczytywanych ze sterownika."""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .device import (
    DEFAULT_DEVICE_MODEL,
    build_device_info,
)
from .entity_layout import existing_buffer_sensor_keys, is_entity_enabled
from .entity_support import EntitySupportContext, should_create_sensor
from .parameter_resolver import (
    get_parameter_value,
    is_parameter_definition_supported,
    is_parameter_supported,
)
from .sensors_map import DIAGNOSTIC_SENSORS, SENSOR_MAP
from .value_decoder import (
    get_active_alarms,
    is_unavailable_oxygen,
    is_unavailable_temperature,
)

if TYPE_CHECKING:
    from .coordinator import SkzpCoordinator

_LOGGER = logging.getLogger(__name__)

DIAGNOSTIC_KEYS = {
    "Alarms",
    "AlarmsList",
    "DevStatus_Mode",
    "TimeStamp",
    "UpTime",
}
DISABLED_BY_DEFAULT_KEYS = {"TimeStamp", "UpTime"}

_UPTIME_RE = re.compile(
    r"(?:(?P<d>\d+)d)?(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+)s)?"
)


def _format_timestamp(raw: Any) -> str:
    """Formatuje czas z postaci „GG-MM-SS” do „GG:MM:SS”."""
    return str(raw).replace("-", ":")


def _format_uptime(raw: Any) -> str:
    """Dodaje odstępy do wartości czasu pracy zapisanej jako „NdNhNmNs”."""
    match = _UPTIME_RE.fullmatch(str(raw).strip())
    if not match or not any(match.groups()):
        return str(raw)
    return " ".join(
        f"{value}{unit}"
        for unit, value in zip("dhms", match.groups())
        if value is not None
    )


def _active_alarm_descriptions(client: SkzpCoordinator, raw: Any) -> list[str]:
    """Tłumaczy opisy alarmów według głównego języka HA."""
    return [
        client.translate(f"common.alarm_{key}")
        for key in get_active_alarms(raw)
    ]


def _format_active_alarms(client: SkzpCoordinator, raw: Any) -> str:
    """Zwraca listę alarmów mieszczącą się w limicie stanu HA."""
    active = _active_alarm_descriptions(client, raw)
    if not active:
        return client.translate("common.no_alarms")
    text = ", ".join(active)
    if len(text) > 255:
        return client.translate("common.active_alarm_count", count=len(active))
    return text


def _alarm_attributes(client: SkzpCoordinator) -> dict[str, Any]:
    """Zwraca pełną listę alarmów, również przy skróconym stanie."""
    return {
        "aktywne_alarmy": _active_alarm_descriptions(
            client, client.data.get("Alarms")
        )
    }


def _fuel_caloric_value(client: SkzpCoordinator, raw: Any) -> float | None:
    """Przelicza kaloryczność paliwa z uwzględnieniem korekty dawki."""
    try:
        value = float(raw) / 10
        correction_raw = get_parameter_value(client.data, "BuFuelCorr")
        if correction_raw is not None:
            denominator = 100 + float(correction_raw)
            if denominator <= 0:
                return None
            value *= 100 / denominator
    except (TypeError, ValueError, ZeroDivisionError):
        return None

    return round(value, 1) if math.isfinite(value) else None


def _oxygen_value(_client: SkzpCoordinator, raw: Any) -> float | None:
    """Przelicza odczyt analizatora tlenu i odrzuca brak pomiaru."""
    if is_unavailable_oxygen(raw):
        return None
    return round(float(raw) / 100, 1)


VALUE_FORMATTERS: dict[str, Callable[[Any], str]] = {
    "TimeStamp": _format_timestamp,
    "UpTime": _format_uptime,
}

VALUE_PROVIDERS: dict[
    str, Callable[[SkzpCoordinator, Any], int | float | str | None]
] = {
    "AlarmsList": _format_active_alarms,
    "AN01": _oxygen_value,
    "BuFuelCaloric": _fuel_caloric_value,
}

EXTRA_ATTRIBUTE_PROVIDERS: dict[
    str, Callable[[SkzpCoordinator], dict[str, Any]]
] = {
    "Alarms": _alarm_attributes,
    "AlarmsList": _alarm_attributes,
}

VIRTUAL_KEY_SOURCES = {
    "AlarmsList": "Alarms",
}

DEV_STATUS_MIN_LENGTHS = {
    "DevStatus_Mode": 3,
    "DevStatus_Power": 6,
    "DevStatus_Fan": 9,
}


def get_sensor_descriptions(
    model: str,
) -> dict[str, dict[str, Any]]:
    """Zwraca sensory do sprawdzenia dla rozpoznanego modelu."""
    if model == DEFAULT_DEVICE_MODEL:
        return DIAGNOSTIC_SENSORS
    return SENSOR_MAP


def _raw_data_source_key(key: str) -> str:
    """Zwraca źródłowy klucz danych sensora."""
    if key.startswith("DevStatus_"):
        return "DevStatus"
    return VIRTUAL_KEY_SOURCES.get(key, key)


def is_sensor_supported(data: Mapping[str, Any], key: str) -> bool:
    """Sprawdza, czy ramka zawiera dane wymagane przez sensor."""
    source_key = _raw_data_source_key(key)
    source_supported = (
        is_parameter_supported(data, source_key)
        if source_key != key
        else is_parameter_definition_supported(data, key, SENSOR_MAP)
    )
    if not source_supported:
        return False

    min_length = DEV_STATUS_MIN_LENGTHS.get(key)
    if min_length is None:
        return True

    raw_value = get_parameter_value(data, source_key)
    return isinstance(raw_value, str) and len(raw_value) >= min_length


def _precision_from_divider(divider: float | None) -> int | None:
    """Zwraca dokładność wartości wynikającą z dzielnika."""
    if divider is None:
        return None
    if divider <= 1:
        return 0
    return math.ceil(math.log10(divider))


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Tworzy sensory obsługiwane przez wykryty sterownik."""
    client = hass.data[DOMAIN][config_entry.entry_id]
    entry_id = config_entry.entry_id
    retained_buffer_sensors = existing_buffer_sensor_keys(hass, entry_id)
    support_context = EntitySupportContext.from_device(client.model, client.data)

    sensor_descriptions = get_sensor_descriptions(client.model)
    descriptions = [
        (key, meta)
        for key, meta in sensor_descriptions.items()
        if should_create_sensor(
            key,
            support_context,
            retained_buffer_sensors,
        )
    ]
    supported_descriptions = [
        (key, meta)
        for key, meta in descriptions
        if is_sensor_supported(client.data, key)
    ]
    entities = [
        SkzpSensor(client, entry_id, key, meta)
        for key, meta in supported_descriptions
        if (
            key in DIAGNOSTIC_KEYS
            or is_entity_enabled(config_entry, "sensor", key)
        )
    ]
    async_add_entities(entities)

    _LOGGER.debug(
        "[SKZP Control] %s:%s — Added %d sensors for model %s.",
        client.host, client.port,
        len(entities),
        client.model,
    )
class SkzpSensor(SensorEntity):
    """Wartość odczytana ze sterownika."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        client: SkzpCoordinator,
        entry_id: str,
        key: str,
        meta: dict[str, Any],
    ) -> None:
        self._client = client
        self._key = key
        self._decoder: Callable[[Any], str | int | None] | None = meta.get("decoder")
        self._divider: float | None = meta.get("divider")
        self._value_precision: int | None = meta.get(
            "precision", _precision_from_divider(self._divider)
        )
        self._is_numeric = bool(meta.get("state_class"))
        self._is_temperature = (
            meta.get("device_class") == SensorDeviceClass.TEMPERATURE
        )

        self._attr_translation_key = meta.get("translation_key", key.lower())
        self._attr_unique_id = f"{entry_id}_{key.lower()}"
        self._attr_native_unit_of_measurement = meta.get("unit")
        self._attr_icon = meta.get("icon")

        if meta.get("state_class"):
            self._attr_state_class = meta["state_class"]
        if meta.get("device_class"):
            self._attr_device_class = meta["device_class"]
        if "options" in meta:
            self._attr_options = meta["options"]
        if meta.get("suggested_display_precision") is not None:
            self._attr_suggested_display_precision = meta[
                "suggested_display_precision"
            ]
        if key in DIAGNOSTIC_KEYS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if key in DISABLED_BY_DEFAULT_KEYS:
            self._attr_entity_registry_enabled_default = False

    async def async_added_to_hass(self) -> None:
        """Uruchamia nasłuchiwanie danych po dodaniu encji do HA."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.hass.bus.async_listen(
                self._client.update_event,
                self._handle_data_update,
            )
        )
        self._handle_data_update(None)

    @callback
    def _handle_data_update(self, _event: Event | None) -> None:
        raw_value = get_parameter_value(
            self._client.data,
            _raw_data_source_key(self._key),
        )

        if not self._client.available or raw_value is None:
            self._mark_unavailable()
            return

        if self._is_temperature and is_unavailable_temperature(raw_value):
            self._mark_unavailable()
            return

        self._attr_available = True

        formatter = VALUE_FORMATTERS.get(self._key)
        if formatter is not None:
            self._attr_native_value = formatter(raw_value)
            self.async_write_ha_state()
            return

        provider = VALUE_PROVIDERS.get(self._key)
        if provider is not None:
            value = provider(self._client, raw_value)
            if value is None:
                self._mark_unavailable()
                return
            self._attr_native_value = value
            self.async_write_ha_state()
            return

        if self._decoder is not None:
            self._attr_native_value = self._decoder(raw_value)
            self.async_write_ha_state()
            return

        value = self._apply_divider(raw_value)
        if value is None:
            self._mark_unavailable()
            return

        self._attr_native_value = value
        self.async_write_ha_state()

    def _mark_unavailable(self) -> None:
        """Oznacza sensor jako niedostępny."""
        self._attr_available = False
        self._attr_native_value = None
        self.async_write_ha_state()

    def _apply_divider(self, raw_value: Any) -> int | float | str | None:
        """Przelicza surową wartość sensora."""
        if self._divider is None and not self._is_numeric:
            return str(raw_value)

        try:
            divider = self._divider if self._divider is not None else 1
            if divider <= 0:
                return None
            value = float(raw_value) / divider
        except (TypeError, ValueError, ZeroDivisionError):
            return None

        if not math.isfinite(value):
            return None
        if self._value_precision is None:
            return int(value) if value.is_integer() else value

        rounded = round(value, self._value_precision)
        return int(rounded) if self._value_precision == 0 else float(rounded)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        provider = EXTRA_ATTRIBUTE_PROVIDERS.get(self._key)
        return provider(self._client) if provider is not None else None

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self._client)
