"""Liczbowe parametry konfiguracji sterownika."""

from __future__ import annotations

import asyncio
import logging
import math
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .pending_change import PendingChangeMixin
from .const import (
    CONF_BOILER_TEMP_MAX,
    DEFAULT_BOILER_TEMP_MAX,
    MIN_BOILER_TEMP_MAX,
    MAX_BOILER_TEMP_MAX,
    CONF_NUMBER_SEND_DELAY,
    DEFAULT_NUMBER_SEND_DELAY,
    DOMAIN,
    MAX_NUMBER_SEND_DELAY,
    MIN_NUMBER_SEND_DELAY,
)
from .device import (
    BUFFER_SETTING_KEYS,
    SUPPORTED_DEVICE_MODELS,
    SKZP02T_MODEL,
    build_device_info,
    supports_buffer_setting_writes,
    supports_fuel_caloric_write,
)
from .entity_layout import is_entity_enabled
from .parameter_resolver import (
    get_parameter_value,
    is_parameter_definition_supported,
    resolve_parameter_write_key,
)

if TYPE_CHECKING:
    from .coordinator import SkzpCoordinator

_LOGGER = logging.getLogger(__name__)


def _decimals_for_step(step: float) -> int:
    """Zwraca liczbę miejsc po przecinku wynikającą z kroku."""
    exponent = Decimal(str(step)).normalize().as_tuple().exponent
    return max(0, -exponent)


def _temperature_number(
    unique_suffix: str,
    icon: str,
    min_value: float,
    max_value: float,
    step: float,
) -> dict[str, Any]:
    """Tworzy definicję parametru temperatury."""
    return {
        "unique_suffix": unique_suffix,
        "unit": "°C",
        "min": min_value,
        "max": max_value,
        "step": step,
        "icon": icon,
        "divider": 100,
    }


def _number(
    unique_suffix: str,
    icon: str,
    unit: str | None,
    min_value: float,
    max_value: float,
    step: float,
    divider: int | None = None,
) -> dict[str, Any]:
    """Tworzy definicję parametru liczbowego."""
    meta: dict[str, Any] = {
        "unique_suffix": unique_suffix,
        "unit": unit,
        "min": min_value,
        "max": max_value,
        "step": step,
        "icon": icon,
    }
    if divider is not None:
        meta["divider"] = divider
    return meta


_FUEL_CALORIC_NUMBER = _number(
    "fuel_caloric",
    "mdi:fire",
    "kWh/kg",
    1,
    20,
    0.1,
    divider=10,
)


_CIRCUIT_NUMBER_TEMPLATES: tuple[dict[str, Any], ...] = (
    # Mieszacz
    {
        "keys": {
            1: ("C022", "CH1MixValueMin",),
            2: ("C122",),
            3: ("C222",),
        },
        "definition": _number(
            "mixer_opening_min",
            "mdi:valve-closed",
            "%",
            0,
            90,
            1,
        ),
    },
    {
        "keys": {
            1: ("C023", "CH1MixValueMax",),
            2: ("C123",),
            3: ("C223",),
        },
        "definition": _number(
            "mixer_opening_max",
            "mdi:valve-open",
            "%",
            10,
            100,
            1,
        ),
    },
    {
        "keys": {
            1: ("C024", "CH1MixGain",),
            2: ("C124",),
            3: ("C224",),
        },
        "definition": _number(
            "mixer_gain",
            "mdi:signal",
            None,
            2,
            15,
            1,
        ),
    },
    {
        "keys": {
            1: ("C025", "CH1MixPeriod",),
            2: ("C125",),
            3: ("C225",),
        },
        "definition": _number(
            "mixer_stabilization_time",
            "mdi:timer-sync-outline",
            "s",
            15,
            240,
            1,
        ),
    },

    # Temperatury
    {
        "keys": {
            1: ("C027", "CH1MixTempBase",),
            2: ("C127",),
            3: ("C227",),
        },
        "definition": _temperature_number(
            "mixer_temp_setpoint",
            "mdi:thermometer-check",
            15,
            75,
            1,
        ),
    },
    {
        "keys": {
            1: ("C028", "CH1MixTempMin",),
            2: ("C128",),
            3: ("C228",),
        },
        "definition": _temperature_number(
            "mixer_temp_min",
            "mdi:thermometer-chevron-down",
            12,
            50,
            1,
        ),
    },
    {
        "keys": {
            1: ("C029", "CH1MixTempMax",),
            2: ("C129",),
            3: ("C229",),
        },
        "definition": _temperature_number(
            "mixer_temp_max",
            "mdi:thermometer-chevron-up",
            20,
            80,
            1,
        ),
    },
    {
        "keys": {
            1: ("C046",),
            2: ("C146",),
            3: ("C246",),
        },
        "definition": _temperature_number(
            "mixer_temp_reduction",
            "mdi:thermometer-minus",
            0,
            20,
            1,
        ),
    },
    # Ochrona powrotu CO1
    {
        "keys": {
            1: ("C030", "CH1ReturnTempCmd",),
        },
        "definition": _temperature_number(
            "return_temp_min",
            "mdi:thermometer",
            30,
            80,
            1,
        ),
    },
    # Regulator pokojowy
    {
        "keys": {
            1: ("C013", "CH1RoomTempCom",),
            2: ("C113",),
            3: ("C213",),
        },
        "definition": _temperature_number(
            "room_comfort_temp",
            "mdi:thermometer-high",
            7,
            50,
            0.1,
        ),
    },
    {
        "keys": {
            1: ("C014", "CH1RoomTempEco",),
            2: ("C114",),
            3: ("C214",),
        },
        "definition": _temperature_number(
            "room_eco_temp",
            "mdi:thermometer-low",
            7,
            30,
            0.1,
        ),
    },
    {
        "keys": {
            1: ("C015", "CH1RoomHist",),
            2: ("C115",),
            3: ("C215",),
        },
        "definition": _temperature_number(
            "room_hysteresis",
            "mdi:thermometer-lines",
            0.1,
            2.0,
            0.1,
        ),
    },

    # Regulator pogodowy
    {
        "keys": {
            1: ("C018", "WeaCorr",),
            2: ("C118",),
            3: ("C218",),
        },
        "definition": _number(
            "weather_correction",
            "mdi:tune-variant",
            "",
            0,
            9,
            1,
        ),
        "suffix_overrides": {"WeaCorr": "weather_correction"},
    },
    {
        "keys": {
            1: ("C040", "WeaTempStopCH1",),
            2: ("C140", "WeaTempStopCH2",),
            3: ("C240",),
        },
        "definition": _temperature_number(
            "weather_stop_temp",
            "mdi:thermometer-off",
            5,
            40,
            1,
        ),
        "suffix_overrides": {
            "WeaTempStopCH1": "weather_stop_temp_ch1",
            "WeaTempStopCH2": "weather_stop_temp_ch2",
        },
    },
)


def _build_circuit_numbers() -> dict[str, dict[str, Any]]:
    """Rozwija jawne klucze, zachowując kolejność obiegów i ich odpowiedników."""
    descriptions = {}
    alternatives = {}
    for circuit in range(1, 4):
        for template in _CIRCUIT_NUMBER_TEMPLATES:
            keys = template["keys"].get(circuit, ())
            meta = template["definition"]
            overrides = template.get("suffix_overrides", {})
            for index, key in enumerate(keys):
                target = descriptions if index == 0 else alternatives
                target[key] = {
                    **meta,
                    "unique_suffix": overrides.get(key, f"ch{circuit}_{meta['unique_suffix']}"),
                }
    return {**descriptions, **alternatives}


NUMBER_DESCRIPTIONS = {
    # Kocioł
    "BoilerTempCmd": _temperature_number(
        "boiler_temp_setpoint",
        "mdi:thermometer-check",
        40,
        90,
        1,
    ),
    "BuIntHist": _temperature_number(
        "boiler_hysteresis",
        "mdi:thermometer-lines",
        2,
        16,
        1,
    ),
    "K007": _temperature_number(
        "boiler_temp_reduction",
        "mdi:thermometer-minus",
        0,
        30,
        1,
    ),

    # Palnik
    "BuOptNoFireTime": _number(
        "no_fire_time",
        "mdi:timer-alert-outline",
        "min",
        3,
        600,
        1,
    ),
    "BuOptClrPeriod": _number(
        "clean_period",
        "mdi:timer-sync-outline",
        "min",
        0,
        180,
        5,
    ),
    "BuOptClrTime": _number(
        "clean_time",
        "mdi:timer-outline",
        "s",
        1,
        40,
        1,
    ),

    # Modulacja
    "BuModulMin": _number(
        "modulation_min",
        "mdi:gauge-low",
        "%",
        5,
        100,
        1,
    ),
    "BuModulMax": _number(
        "modulation_max",
        "mdi:gauge-full",
        "%",
        5,
        100,
        1,
    ),
    "B026": _number(
        "modulation_dynamics",
        "mdi:speedometer",
        "",
        0,
        7,
        1,
    ),
    "BuTimeAboveMax": _number(
        "overtemp_time",
        "mdi:timer-alert-outline",
        "min",
        0,
        600,
        1,
    ),

    # Paliwo
    "BuFuelCorr": _number(
        "fuel_correction",
        "mdi:plus-minus-variant",
        "%",
        -50,
        200,
        1,
    ),

    # Spaliny
    "ExhaustTempMax": _temperature_number(
        "exhaust_temp_max",
        "mdi:thermometer-alert",
        90,
        300,
        1,
    ),

    # Czuwanie
    "BuSbyFeedTime": _number(
        "standby_feed_time",
        "mdi:timer-play-outline",
        "s",
        0,
        60,
        1,
    ),
    "BuSbyPeriod": _number(
        "standby_period",
        "mdi:timer-pause-outline",
        "min",
        1,
        60,
        1,
    ),
    "BuSbyAirPwr": _number(
        "standby_air",
        "mdi:fan",
        "%",
        5,
        100,
        1,
    ),
    "BuSbyAirTime": _number(
        "standby_air_time",
        "mdi:fan-clock",
        "s",
        10,
        120,
        1,
    ),
    "B059": _number(
        "standby_exit_time",
        "mdi:timer-outline",
        "s",
        0,
        300,
        5,
    ),

    # Powietrze
    "BuAirMin": _number(
        "air_min",
        "mdi:fan-chevron-down",
        "%",
        1,
        100,
        1,
    ),
    "BuAirMax": _number(
        "air_max",
        "mdi:fan-chevron-up",
        "%",
        10,
        100,
        1,
    ),
    "BuAditAir": _number(
        "feed_air_boost",
        "mdi:fan-plus",
        "%",
        0,
        50,
        1,
    ),

    # Interwał
    "BuIntFeedTime": _number(
        "interval_feed_time",
        "mdi:timer-play-outline",
        "s",
        1,
        60,
        0.1,
        divider=10,
    ),
    "BuIntBreakTime": _number(
        "interval_break_time",
        "mdi:timer-pause-outline",
        "s",
        1,
        120,
        0.1,
        divider=10,
    ),
    "BuIntAirPwr": _number(
        "interval_air",
        "mdi:fan",
        "%",
        1,
        100,
        1,
    ),

    # Podajnik
    "BuOptFdrEff": _number(
        "feeder_efficiency",
        "mdi:scale",
        "kg/h",
        2,
        200,
        0.05,
        divider=100,
    ),
    "BuOptFdrFTime": _number(
        "max_feed_time",
        "mdi:timer-play-outline",
        "s",
        2,
        30,
        1,
    ),

    # Moc kotła
    "BuOptPwr": _number(
        "boiler_power",
        "mdi:lightning-bolt-outline",
        "kW",
        8,
        150,
        1,
    ),

    # CWU
    "DHWTempCmd": _temperature_number(
        "dhw_temp_setpoint",
        "mdi:thermometer-water",
        20,
        85,
        1,
    ),
    "DHWHist": _temperature_number(
        "dhw_hysteresis",
        "mdi:thermometer-lines",
        1,
        25,
        1,
    ),
    "DHWOverH": _temperature_number(
        "dhw_overshoot",
        "mdi:thermometer-plus",
        0,
        20,
        1,
    ),

    # Cyrkulacja CWU
    "DHWCTempON": _temperature_number(
        "dhwc_temp_on",
        "mdi:thermometer-water",
        30,
        60,
        1,
    ),
    "DHWCWork": _number(
        "dhwc_work_time",
        "mdi:timer-play-outline",
        "min",
        1,
        20,
        1,
    ),
    "DHWCBrake": _number(
        "dhwc_break_time",
        "mdi:timer-pause-outline",
        "min",
        0,
        30,
        1,
    ),
    # Bufor
    "D203": _temperature_number(
        "buffer_temp_setpoint",
        "mdi:thermometer-check",
        10,
        90,
        1,
    ),
    "D204": _temperature_number(
        "buffer_hysteresis",
        "mdi:thermometer-lines",
        2,
        40,
        1,
    ),

    **_build_circuit_numbers(),
}


def get_number_descriptions(
    model: str,
    dev_type: Any,
) -> dict[str, dict[str, Any]]:
    """Zwraca parametry liczbowe odpowiednie dla modelu i firmware."""
    if model not in SUPPORTED_DEVICE_MODELS:
        return {}

    descriptions = dict(NUMBER_DESCRIPTIONS)
    device_identity = dev_type or model

    if not supports_buffer_setting_writes(device_identity):
        for key in BUFFER_SETTING_KEYS:
            descriptions.pop(key, None)

    if model == SKZP02T_MODEL:
        descriptions["BuOptFdrEff"] = {
            **descriptions["BuOptFdrEff"],
            "unit": "kg",
            "min": 0.01,
            "max": 50,
            "step": 0.01,
        }

    if supports_fuel_caloric_write(device_identity):
        descriptions.pop("BuFuelCorr", None)
        descriptions["BuFuelCaloric"] = _FUEL_CALORIC_NUMBER
    return descriptions


# Pary parametrów, które muszą zachować minimum ≤ maksimum.
MIN_MAX_PAIRS = (
    ("BuModulMin", "BuModulMax"),
    ("BuAirMin", "BuAirMax"),
    ("C028", "C029"),  # CO1 Mieszacz Temp. min./maks.
    ("C022", "C023"),  # CO1 Mieszacz Min./Maks. otwarcie
    ("C128", "C129"),  # CO2 Mieszacz Temp. min./maks.
    ("C122", "C123"),  # CO2 Mieszacz Min./Maks. otwarcie
    ("C228", "C229"),  # CO3 Mieszacz Temp. min./maks.
    ("C222", "C223"),  # CO3 Mieszacz Min./Maks. otwarcie
    ("CH1MixTempMin", "CH1MixTempMax"),
    ("CH1MixValueMin", "CH1MixValueMax"),
)

_MIN_TO_MAX = dict(MIN_MAX_PAIRS)
_MAX_TO_MIN = {max_key: min_key for min_key, max_key in MIN_MAX_PAIRS}


def _read_paired_value(client: SkzpCoordinator, key: str) -> float | None:
    """Odczytuje przeliczoną wartość powiązanego parametru ze sterownika."""
    meta = NUMBER_DESCRIPTIONS.get(key)
    if meta is None:
        return None
    raw = get_parameter_value(client.data, key)
    if raw is None:
        return None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    divider = meta.get("divider")
    return value / divider if divider else value


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Tworzy encje liczbowe obsługiwane przez wykryty sterownik."""
    client = hass.data[DOMAIN][config_entry.entry_id]
    descriptions = get_number_descriptions(
        client.model,
        client.data.get("DevType"),
    )
    supported_descriptions = {
        key: meta
        for key, meta in descriptions.items()
        if is_parameter_definition_supported(client.data, key, descriptions)
    }
    entities = [
        SkzpNumber(client, config_entry, key, meta)
        for key, meta in supported_descriptions.items()
        if is_entity_enabled(config_entry, "number", key)
    ]

    async_add_entities(entities)
    _LOGGER.debug(
        "[SKZP Control] %s:%s — Added %d number entities for model %s.",
        client.host, client.port,
        len(entities),
        client.model,
    )
class SkzpNumber(PendingChangeMixin, RestoreNumber):
    """Parametr liczbowy sterownika."""

    _attr_should_poll = False
    _attr_has_entity_name = True
    _attr_mode = NumberMode.BOX

    def __init__(
        self,
        client: SkzpCoordinator,
        config_entry: ConfigEntry,
        key: str,
        meta: dict[str, Any],
    ) -> None:
        self._client = client
        self._config_entry = config_entry
        self._key = key
        self._meta = meta
        self._decimals = _decimals_for_step(meta["step"])

        self._attr_translation_key = meta["unique_suffix"]
        self._attr_unique_id = f"{config_entry.entry_id}_{meta['unique_suffix']}"
        self._attr_native_unit_of_measurement = meta.get("unit")
        self._attr_native_min_value = meta["min"]
        self._attr_native_max_value = meta["max"]
        self._attr_native_step = meta["step"]
        self._attr_icon = meta["icon"]
        self._attr_entity_category = EntityCategory.CONFIG

        self._attr_native_value = None
        self._has_valid_value = False
        self._init_pending_change()
        self._confirmed_value: int | float | None = None
        self._last_controller_value: int | float | None = None

    async def async_added_to_hass(self) -> None:
        """Przywraca stan i rozpoczyna nasłuchiwanie danych."""
        await super().async_added_to_hass()

        last_number_data = await self.async_get_last_number_data()
        if last_number_data and last_number_data.native_value is not None:
            self._attr_native_value = self._round(last_number_data.native_value)

        self.async_on_remove(
            self._client.hass.bus.async_listen(
                self._client.update_event,
                self._handle_data_update,
            )
        )
        self._handle_data_update(None)

    @callback
    def _handle_data_update(self, _event: Event | None) -> None:
        """Aktualizuje wartość na podstawie danych ze sterownika."""
        if not self._client.available:
            self.async_write_ha_state()
            return

        raw_val = get_parameter_value(self._client.data, self._key)
        if raw_val is None:
            self._has_valid_value = False
            self.async_write_ha_state()
            return

        try:
            val = float(raw_val)
        except (TypeError, ValueError):
            self._has_valid_value = False
            self.async_write_ha_state()
            return
        if not math.isfinite(val):
            self._has_valid_value = False
            self.async_write_ha_state()
            return

        if (divider := self._meta.get("divider")) is not None:
            val /= divider

        rounded = self._round(val)
        self._has_valid_value = True
        self._last_controller_value = rounded

        if self._confirm_pending_change(rounded):
            self._confirmed_value = rounded

        if self._is_stale_update(val):
            return

        self._confirmed_value = rounded
        self._attr_native_value = rounded
        self.async_write_ha_state()

    def _round(self, val: float) -> int | float:
        """Zaokrągla wartość zgodnie z krokiem encji."""
        rounded = round(float(val), self._decimals)
        return int(rounded) if self._decimals == 0 else float(rounded)

    @property
    def native_max_value(self) -> float:
        """Zwraca maksimum parametru z uwzględnieniem limitu kotła."""
        if self._key != "BoilerTempCmd":
            return self._attr_native_max_value
        try:
            limit = int(self._config_entry.options.get(
                CONF_BOILER_TEMP_MAX, DEFAULT_BOILER_TEMP_MAX
            ))
        except (TypeError, ValueError, OverflowError):
            limit = DEFAULT_BOILER_TEMP_MAX
        return min(self._attr_native_max_value, max(
            MIN_BOILER_TEMP_MAX, min(limit, MAX_BOILER_TEMP_MAX)
        ))

    def _command_value(self, value: float) -> str:
        """Koduje wartość w formacie oczekiwanym przez sterownik."""
        if (divider := self._meta.get("divider")) is not None:
            return str(int(round(value * divider)))

        rounded = self._round(value)
        if isinstance(rounded, float) and rounded.is_integer():
            return str(int(rounded))
        return str(rounded)

    def _format_value(self, value: float) -> str:
        """Formatuje wartość z jednostką do logów i komunikatów."""
        text = f"{self._round(value):.{self._decimals}f}"
        unit = self._attr_native_unit_of_measurement
        return f"{text} {unit}" if unit else text

    def _normalize_pending_value(self, value: Any) -> int | float:
        """Porównuje nastawę i odczyt z dokładnością encji."""
        return self._round(value)

    async def async_set_native_value(self, value: float) -> None:
        """Ustawia nową wartość i czeka na potwierdzenie sterownika."""
        self._validate_min_max_pair(value)

        send_val = self._command_value(value)
        previous_value = (
            self._confirmed_value
            if self._confirmed_value is not None
            else self._attr_native_value
        )
        send_delay = self._number_send_delay
        send_generation, confirmation_event = self._begin_pending_change(
            value, send_delay + self._client.command_pending_timeout
        )

        # Pokaż zmianę od razu i ignoruj starsze ramki do czasu potwierdzenia.
        self._attr_native_value = self._round(value)
        self.async_write_ha_state()

        try:
            # W czasie opóźnienia nowsza zmiana zastępuje poprzednią.
            if send_delay > 0:
                await asyncio.sleep(send_delay)
            if send_generation != self._send_generation:
                return

            try:
                async def send_attempt() -> None:
                    command_key = resolve_parameter_write_key(self._client.data, self._key)
                    await self._client.send_command({command_key: send_val})
                    _LOGGER.debug("[SKZP Control] %s:%s — Command sent: %s = %s.", self._client.host, self._client.port, self._client.translate(f'entity.number.{self._attr_translation_key}.name', language='en'), self._format_value(value))

                def log_retry(retry_number: int) -> None:
                    self._client.log_command_retry(self._client.translate(f'entity.number.{self._attr_translation_key}.name', language='en'), self._format_value(value), retry_number)

                if await self._client.command_manager.execute(
                    send=send_attempt,
                    wait=lambda: self._async_wait_for_confirmation(confirmation_event, send_generation, value),
                    is_current=lambda: send_generation == self._send_generation,
                    retry_count=self._client.command_retry_count,
                    retry_delay=self._client.command_retry_delay,
                    on_retry=log_retry,
                    is_confirmed=lambda: confirmation_event.is_set()
                    and self._last_controller_value is not None
                    and self._round(self._last_controller_value) == self._round(value),
                ):
                    if send_generation == self._send_generation:
                        self._confirmation_event = None
                        _LOGGER.debug("[SKZP Control] %s:%s — Change confirmed: %s = %s.", self._client.host, self._client.port, self._client.translate(f'entity.number.{self._attr_translation_key}.name', language='en'), self._format_value(value))
                    return
            except HomeAssistantError:
                if send_generation == self._send_generation:
                    self._restore_controller_value(previous_value)
                raise

            if send_generation != self._send_generation:
                return

            self._restore_controller_value(previous_value)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_not_confirmed",
                translation_placeholders={
                    "parameter": (self.name or self._key),
                    "value": self._format_value(value),
                },
            )
        except asyncio.CancelledError:
            if send_generation == self._send_generation:
                self._restore_controller_value(previous_value)
            raise
        finally:
            if send_generation == self._send_generation:
                self._clear_pending_change()

    async def _async_wait_for_confirmation(
        self,
        confirmation_event: asyncio.Event,
        send_generation: int,
        target_value: float,
    ) -> bool:
        """Czeka na ramkę zawierającą wartość wysłaną do sterownika."""
        return await self._client.command_manager.wait_for_confirmation(
            confirmation_event,
            self._client.command_confirmation_timeout,
            lambda: send_generation == self._send_generation and self._last_controller_value is not None and (self._round(self._last_controller_value) == self._round(target_value)),
        )

    def _restore_controller_value(
        self,
        fallback_value: int | float | None,
    ) -> None:
        """Przywraca ostatnią wartość odebraną ze sterownika."""
        restored_value = (
            self._last_controller_value
            if self._last_controller_value is not None
            else fallback_value
        )
        self._attr_native_value = restored_value
        _LOGGER.debug("[SKZP Control] %s:%s — State restored: %s = %s.", self._client.host, self._client.port, self._client.translate(f'entity.number.{self._attr_translation_key}.name', language='en'), self._format_value(restored_value) if restored_value is not None else "unknown")
        self._confirmed_value = restored_value
        self._clear_pending_change()
        self.async_write_ha_state()

    @property
    def _number_send_delay(self) -> float:
        """Zwraca opóźnienie wysyłania liczb ustawione w opcjach integracji."""
        try:
            delay = float(
                self._config_entry.options.get(
                    CONF_NUMBER_SEND_DELAY,
                    DEFAULT_NUMBER_SEND_DELAY,
                )
            )
        except (TypeError, ValueError):
            delay = DEFAULT_NUMBER_SEND_DELAY
        return max(MIN_NUMBER_SEND_DELAY, min(delay, MAX_NUMBER_SEND_DELAY))

    def _validate_min_max_pair(self, value: float) -> None:
        """Sprawdza zależność między wartością minimalną i maksymalną."""
        paired_max_key = _MIN_TO_MAX.get(self._key)
        if paired_max_key:
            max_value = _read_paired_value(self._client, paired_max_key)
            if max_value is not None and value > max_value:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="minimum_above_maximum",
                    translation_placeholders={
                        "value": self._format_value(value),
                        "maximum": self._format_value(max_value),
                    },
                )
            return

        paired_min_key = _MAX_TO_MIN.get(self._key)
        if paired_min_key:
            min_value = _read_paired_value(self._client, paired_min_key)
            if min_value is not None and value < min_value:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="maximum_below_minimum",
                    translation_placeholders={
                        "value": self._format_value(value),
                        "minimum": self._format_value(min_value),
                    },
                )

    @property
    def available(self) -> bool:
        """Sprawdza dostępność bieżącej wartości parametru."""
        return self._client.available and self._has_valid_value

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self._client)
