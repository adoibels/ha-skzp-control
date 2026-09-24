"""Definicje sensorów i ich metadanych."""

from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import PERCENTAGE, UnitOfMass, UnitOfTemperature

from .value_decoder import (
    DEV_STATUS_MODES,
    decode_alarms,
    decode_devstatus_fan,
    decode_devstatus_mode,
    decode_devstatus_power,
)


def _temperature_sensor(
    translation_key: str | None = None, *, icon: str = "mdi:thermometer"
) -> dict[str, Any]:
    """Tworzy definicję sensora temperatury."""
    meta = {
        "icon": icon,
        "unit": UnitOfTemperature.CELSIUS,
        "divider": 100,
        "device_class": SensorDeviceClass.TEMPERATURE,
        "state_class": SensorStateClass.MEASUREMENT,
    }
    if translation_key is not None:
        meta["translation_key"] = translation_key
    return meta


def _position_sensor(translation_key: str | None = None) -> dict[str, Any]:
    """Tworzy definicję sensora położenia w procentach."""
    meta = {
        "icon": "mdi:valve",
        "unit": PERCENTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
    }
    if translation_key is not None:
        meta["translation_key"] = translation_key
    return meta



DIAGNOSTIC_SENSORS: dict[str, dict[str, Any]] = {
    "Alarms": {
        "decoder": decode_alarms,
        "icon": "mdi:alert-outline",
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "AlarmsList": {
        "icon": "mdi:alert-outline",
    },
    "DevStatus_Mode": {
        "decoder": decode_devstatus_mode,
        "icon": "mdi:state-machine",
        "device_class": SensorDeviceClass.ENUM,
        "options": list(DEV_STATUS_MODES.values()),
    },
    "TimeStamp": {
        "icon": "mdi:clock-outline",
    },
    "UpTime": {
        "icon": "mdi:history",
    },
}


SENSOR_MAP: dict[str, dict[str, Any]] = {
    **DIAGNOSTIC_SENSORS,

    # Kocioł / Palnik
    "BoilerTempAct": _temperature_sensor(),
    "CH1ReturnTempAct": _temperature_sensor(),
    "BuTempAct": _temperature_sensor(),
    "ExhaustTempAct": _temperature_sensor(),
    "AN01": {
        "icon": "mdi:molecule",
        "unit": PERCENTAGE,
        "divider": 100,
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
        "suggested_display_precision": 1,
    },
    "BuPhotoAct": {
        "icon": "mdi:brightness-percent",
        "unit": PERCENTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "K003": _temperature_sensor(icon="mdi:thermometer-minus"),
    "DevStatus_Power": {
        "decoder": decode_devstatus_power,
        "icon": "mdi:gauge",
        "unit": PERCENTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "DevStatus_Fan": {
        "decoder": decode_devstatus_fan,
        "icon": "mdi:fan",
        "unit": PERCENTAGE,
        "state_class": SensorStateClass.MEASUREMENT,
    },

    # Paliwo
    "B060": {
        "icon": "mdi:tray-full",
        "unit": UnitOfMass.KILOGRAMS,
        "divider": 1,
        "device_class": SensorDeviceClass.WEIGHT,
        "suggested_display_precision": 0,
    },
    "B062": {
        "icon": "mdi:tray",
        "unit": UnitOfMass.KILOGRAMS,
        "divider": 10,
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.MEASUREMENT,
        "suggested_display_precision": 1,
    },
    "BuTotalFuel": {
        "icon": "mdi:sack",
        "unit": UnitOfMass.KILOGRAMS,
        "divider": 100,
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.TOTAL_INCREASING,
    },
    "Bu24hFuel": {
        "icon": "mdi:sack",
        "unit": UnitOfMass.KILOGRAMS,
        "divider": 100,
        "device_class": SensorDeviceClass.WEIGHT,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "BuActualFuel": {
        "icon": "mdi:sack",
        "unit": "kg/h",
        "divider": 100,
        "state_class": SensorStateClass.MEASUREMENT,
    },
    "BuFuelCaloric": {
        "icon": "mdi:fire",
        "unit": "kWh/kg",
        "state_class": SensorStateClass.MEASUREMENT,
        "precision": 1,
        "suggested_display_precision": 1,
    },

    # CWU
    "DHWTempAct": _temperature_sensor(icon="mdi:water-thermometer"),

    # Regulator pogodowy
    "WeaTempAct": _temperature_sensor(icon="mdi:sun-thermometer-outline"),

    # Bufor
    "D201": _temperature_sensor(),
    "D202": _temperature_sensor(),
    "D203": {
        **_temperature_sensor(icon="mdi:thermometer-check"),
        "suggested_display_precision": 0,
    },
    "D204": {
        **_temperature_sensor(icon="mdi:thermometer-lines"),
        "suggested_display_precision": 0,
    },

    # Obieg CO1
    "C006": _temperature_sensor("ch1_mixer_supply_temp"),
    "CH1MixTempAct": _temperature_sensor("ch1_mixer_supply_temp"),
    "C007": _temperature_sensor(icon="mdi:thermometer-check"),

    # Obieg CO1 — mieszacz
    "C001": _position_sensor("ch1_mixer_opening"),
    "CH1MixValueAct": _position_sensor("ch1_mixer_opening"),

    # Obieg CO1 — regulator pokojowy
    "C008": _temperature_sensor("ch1_room_temp", icon="mdi:home-thermometer-outline"),
    "CH1RoomTempAct": _temperature_sensor("ch1_room_temp", icon="mdi:home-thermometer-outline"),
    "C009": _temperature_sensor("ch1_room_target_temp", icon="mdi:thermometer-check"),
    "CH1RoomTempCmd": _temperature_sensor("ch1_room_target_temp", icon="mdi:thermometer-check"),

    # Obieg CO2
    "C106": _temperature_sensor(),
    "C107": _temperature_sensor(icon="mdi:thermometer-check"),

    # Obieg CO2 — mieszacz
    "C101": _position_sensor(),

    # Obieg CO2 — regulator pokojowy
    "C108": _temperature_sensor(icon="mdi:home-thermometer-outline"),
    "C109": _temperature_sensor(icon="mdi:thermometer-check"),

    # Obieg CO3
    "C206": _temperature_sensor(),
    "C207": _temperature_sensor(icon="mdi:thermometer-check"),

    # Obieg CO3 — mieszacz
    "C201": _position_sensor(),

    # Obieg CO3 — regulator pokojowy
    "C208": _temperature_sensor(icon="mdi:home-thermometer-outline"),
    "C209": _temperature_sensor(icon="mdi:thermometer-check"),
}
