"""Obsługa zamiennych nazw parametrów sterownika."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


# Nazwy używane zamiennie w ramkach różnych wersji sterowników.
PARAMETER_ALIAS_GROUPS = (
    ("BuTempAct", "B016"),
    ("ExhaustTempAct", "B017"),
    ("C001", "CH1MixValueAct"),
    ("C006", "CH1MixTempAct"),
    ("C007", "CH1MixTempCmd"),
    ("C008", "CH1RoomTempAct"),
    ("C009", "CH1RoomTempCmd"),
    ("C012", "CH1Mode"),
    ("C112", "CH2Mode"),
    ("C013", "CH1RoomTempCom"),
    ("C014", "CH1RoomTempEco"),
    ("C015", "CH1RoomHist"),
    ("C016", "CH1RoomMode"),
    ("C018", "WeaCorr"),
    ("C020", "CH1MixActive"),
    ("C022", "CH1MixValueMin"),
    ("C023", "CH1MixValueMax"),
    ("C024", "CH1MixGain"),
    ("C025", "CH1MixPeriod"),
    ("C027", "CH1MixTempBase"),
    ("C028", "CH1MixTempMin"),
    ("C029", "CH1MixTempMax"),
    ("C040", "WeaTempStopCH1"),
    ("C140", "WeaTempStopCH2"),
)

_CANDIDATES_BY_KEY = {
    key: (key, *(candidate for candidate in group if candidate != key))
    for group in PARAMETER_ALIAS_GROUPS
    for key in group
}
_ALIAS_GROUP_BY_KEY = {
    key: group for group in PARAMETER_ALIAS_GROUPS for key in group
}

_CH1_MODE_ALIAS_VALUES = {"active", "stop", "timer"}


def parameter_candidates(key: str) -> tuple[str, ...]:
    """Zwraca nazwę parametru i jej znane zamienniki."""
    return _CANDIDATES_BY_KEY.get(key, (key,))


def is_parameter_supported(data: Mapping[str, Any], key: str) -> bool:
    """Sprawdza, czy ramka zawiera parametr lub jego zamiennik."""
    return any(
        candidate in data and data[candidate] is not None
        for candidate in parameter_candidates(key)
    )


def is_parameter_definition_supported(
    data: Mapping[str, Any],
    key: str,
    definition_keys: Mapping[str, Any] | set[str] | frozenset[str],
) -> bool:
    """Wybiera jedną definicję, gdy ramka używa zamiennej nazwy parametru."""
    group = _ALIAS_GROUP_BY_KEY.get(key)
    if group is None:
        return is_parameter_supported(data, key)

    available_definitions = [
        candidate for candidate in group if candidate in definition_keys
    ]
    if len(available_definitions) <= 1:
        return is_parameter_supported(data, key)

    selected_key = next(
        (
            candidate
            for candidate in available_definitions
            if candidate in data and data[candidate] is not None
        ),
        None,
    )
    return key == selected_key


def resolve_parameter_key(data: Mapping[str, Any], key: str) -> str:
    """Wybiera nazwę parametru obecną w bieżącej ramce."""
    for candidate in parameter_candidates(key):
        if candidate in data and data[candidate] is not None:
            return candidate
    return key


def resolve_parameter_write_key(data: Mapping[str, Any], key: str) -> str:
    """Wybiera nazwę parametru przyjmowaną przez sterownik przy zapisie."""
    if key in parameter_candidates("C012"):
        value = data.get("C012")
        if (
            value is not None
            and str(value).strip().casefold() in _CH1_MODE_ALIAS_VALUES
        ):
            return "CH1Mode"
    return resolve_parameter_key(data, key)


def get_parameter_value(data: Mapping[str, Any], key: str) -> Any:
    """Odczytuje wartość parametru z uwzględnieniem zamienników."""
    return data.get(resolve_parameter_key(data, key))
