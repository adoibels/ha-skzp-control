"""Domyślny wybór encji w profilu „Zalecane”."""

from collections.abc import Mapping
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass

from .entity_layout import EntityChoice
from .parameter_resolver import get_parameter_value
from .sensors_map import SENSOR_MAP
from .value_decoder import is_unavailable_oxygen, is_unavailable_temperature


def _has_unavailable_temperature(
    device_data: Mapping[str, Any], key: str
) -> bool:
    """Sprawdza, czy wskazany odczyt temperatury jest niedostępny."""
    return is_unavailable_temperature(get_parameter_value(device_data, key))


def _value_is(
    device_data: Mapping[str, Any], key: str, *values: str
) -> bool:
    """Porównuje tekstową wartość parametru bez rozróżniania wielkości liter."""
    raw_value = get_parameter_value(device_data, key)
    if raw_value is None:
        return False
    actual = str(raw_value).strip().casefold()
    return actual in {value.casefold() for value in values}


def recommended_disabled_entities(
    choices: list[EntityChoice], device_data: Mapping[str, Any]
) -> set[str]:
    """Wyłącza domyślnie zbędne encje i grupy niewykryte w ramce."""
    disabled_keys: set[str] = {
        # Encje domyślnie wyłączone w profilu zalecanym.
        "K003",
        "K006",
        "K007",
        "BuMode",
        "BuTimeAboveMax",
        "ExhaustTempMax",
        "BuSbyFeedTime",
        "BuSbyPeriod",
        "BuSbyAirPwr",
        "BuSbyAirTime",
        "B059",
        "BuAditAir",
        "BuOptNoFireTime",
        "BuOptClrPeriod",
        "BuOptClrTime",
        "BuOptPwr",
    }

    def disable(*keys: str) -> None:
        disabled_keys.update(keys)

    for choice in choices:
        metadata = SENSOR_MAP.get(choice.key)
        if (
            choice.platform == "sensor"
            and metadata is not None
            and metadata.get("device_class") == SensorDeviceClass.TEMPERATURE
            and _has_unavailable_temperature(device_data, choice.key)
        ):
            disable(choice.key)

    if is_unavailable_oxygen(get_parameter_value(device_data, "AN01")):
        disable("AN01")

    if _value_is(device_data, "C031", "0", "off") or _has_unavailable_temperature(
        device_data, "CH1ReturnTempAct"
    ):
        disable("C030", "C031", "CH1ReturnTempCmd", "CH1ReturnProtAct")

    buffer_top_missing = _has_unavailable_temperature(device_data, "D201")
    buffer_bottom_missing = _has_unavailable_temperature(device_data, "D202")
    if buffer_top_missing and buffer_bottom_missing:
        disable("DevStatus_outBuffer", "D200", "D203", "D204")

    if _has_unavailable_temperature(device_data, "WeaTempAct"):
        disable(
            "C018",
            "C040",
            "C118",
            "C140",
            "C218",
            "C240",
            "WeaCorr",
            "WeaTempStopCH1",
            "WeaTempStopCH2",
        )

    if _value_is(device_data, "BuMode", "auto"):
        disable("BuIntFeedTime", "BuIntBreakTime", "BuIntAirPwr")
    elif _value_is(device_data, "BuMode", "interwal"):
        disable(
            "BuModulMin",
            "BuModulMax",
            "B026",
            "BuTimeAboveMax",
            "BuAirMin",
            "BuAirMax",
            "BuAditAir",
            "BuOptFdrFTime",
        )

    supply_rules = (
        (
            "C006",
            (
                "C007",
                "C001",
                "C020",
                "C022",
                "C023",
                "C024",
                "C025",
                "C027",
                "C028",
                "C029",
                "C046",
            ),
        ),
        (
            "C106",
            (
                "C107",
                "C101",
                "C120",
                "C122",
                "C123",
                "C124",
                "C125",
                "C127",
                "C128",
                "C129",
                "C146",
            ),
        ),
        (
            "C206",
            (
                "C207",
                "C201",
                "C220",
                "C222",
                "C223",
                "C224",
                "C225",
                "C227",
                "C228",
                "C229",
                "C246",
            ),
        ),
        (
            "CH1MixTempAct",
            (
                "CH1MixValueAct",
                "CH1MixActive",
                "CH1MixValueMin",
                "CH1MixValueMax",
                "CH1MixGain",
                "CH1MixPeriod",
                "CH1MixTempBase",
                "CH1MixTempMin",
                "CH1MixTempMax",
            ),
        ),
    )
    for temperature_key, group_keys in supply_rules:
        if _has_unavailable_temperature(device_data, temperature_key):
            disable(*group_keys)

    room_rules = (
        ("C008", ("C009", "C016", "C013", "C014", "C015")),
        ("C108", ("C109", "C116", "C113", "C114", "C115")),
        ("C208", ("C209", "C216", "C213", "C214", "C215")),
        (
            "CH1RoomTempAct",
            (
                "CH1RoomTempCmd",
                "CH1RoomMode",
                "CH1RoomTempCom",
                "CH1RoomTempEco",
                "CH1RoomHist",
            ),
        ),
    )
    for temperature_key, group_keys in room_rules:
        if _has_unavailable_temperature(device_data, temperature_key):
            disable(*group_keys)

    if _has_unavailable_temperature(device_data, "DHWTempAct"):
        disable(
            "DHWTempCmd",
            "DHWHist",
            "DHWOverH",
            "DHWPriority",
            "DHWCTempON",
            "DHWCWork",
            "DHWCBrake",
            "DHWCAlwaysON",
        )

    disabled = {
        choice.selection_id for choice in choices if choice.key in disabled_keys
    }
    return disabled
