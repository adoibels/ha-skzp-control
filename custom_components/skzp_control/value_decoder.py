"""Dekodowanie wartości otrzymywanych ze sterownika."""

from __future__ import annotations

import math
from typing import Any



UNAVAILABLE_TEMPERATURE_RAW_VALUE = 30000.0

DEV_STATUS_MODES: dict[str, str] = {
    "CZU": "standby",
    "CZY": "cleaning",
    "DOP": "afterburning",
    "DRE": "wood",
    "KON": "burner_start",
    "MOD": "modulation",
    "OFF": "off",
    "PAU": "pause",
    "PRA": "operating",
    "PRZ": "break",
    "ROZ": "igniting",
    "STA": "stabilizing",
    "STO": "stopped",
    "WYG": "extinguishing",
}

ALARM_CODES: dict[str, str] = {
    "A": "boiler_overheat",
    "B": "burner_overheat",
    "C": "flame_loss",
    "D": "boiler_sensor_fault",
    "E": "feeder_sensor_fault",
    "F": "flue_gas_sensor_fault",
    "G": "mixer_sensor_fault",
    "H": "dhw_sensor_fault",
    "O": "return_sensor_fault",
    "P": "room_sensor_fault",
    "R": "piston_blocked",
    "S": "piston_blocked",
    "T": "ignition_failed",
    "U": "low_fuel",
    "X": "high_boiler_temp",
    "7": "no_fuel",
}


def _normalize_text(raw_value: Any) -> str:
    """Normalizuje wartość tekstową."""
    return str(raw_value).strip() if raw_value is not None else ""


def is_unavailable_temperature(raw_value: Any) -> bool:
    """Sprawdza, czy wartość oznacza brak odczytu temperatury."""
    try:
        return float(raw_value) >= UNAVAILABLE_TEMPERATURE_RAW_VALUE
    except (TypeError, ValueError):
        return False


def is_unavailable_oxygen(raw_value: Any) -> bool:
    """Sprawdza, czy wartość oznacza brak odczytu analizatora tlenu."""
    try:
        value = float(raw_value)
    except (TypeError, ValueError):
        return True
    return not math.isfinite(value) or value == 0 or value >= 30000


def decode_devstatus_mode(raw_value: Any) -> str | None:
    """Dekoduje tryb pracy z wartości DevStatus."""
    raw_text = _normalize_text(raw_value)
    if len(raw_text) < 3:
        return None

    mode = raw_text[:3].upper()
    return DEV_STATUS_MODES.get(mode)


def decode_devstatus_power(raw_value: Any) -> int | None:
    """Dekoduje aktualną modulację z wartości DevStatus."""
    return decode_devstatus_percentage(raw_value, 3)


def decode_devstatus_fan(raw_value: Any) -> int | None:
    """Dekoduje aktualną moc wentylatora z wartości DevStatus."""
    return decode_devstatus_percentage(raw_value, 6)


def decode_devstatus_percentage(
    raw_value: Any,
    start: int,
) -> int | None:
    """Dekoduje trzycyfrową wartość procentową."""
    raw_text = _normalize_text(raw_value)
    end = start + 3
    if len(raw_text) < end:
        return None

    try:
        value = int(raw_text[start:end])
    except ValueError:
        return None

    return value if 0 <= value <= 100 else None


def decode_alarms(raw_value: Any) -> int | None:
    """Zwraca liczbę aktywnych alarmów."""
    if not _normalize_text(raw_value):
        return None
    return len(get_active_alarms(raw_value))


def get_active_alarms(raw_value: Any) -> list[str]:
    """Zwraca unikalne klucze aktywnych alarmów."""
    raw_text = _normalize_text(raw_value).upper()
    if not raw_text:
        return []

    alarm_keys = (
        alarm_key
        for code, alarm_key in ALARM_CODES.items()
        if code in raw_text
    )
    return list(dict.fromkeys(alarm_keys))

