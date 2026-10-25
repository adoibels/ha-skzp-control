"""Tłumaczenie tekstów generowanych przez integrację."""

import re

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import (
    async_get_cached_translations,
    async_get_translations,
)

from .const import DOMAIN

_CIRCUIT_SENSOR_NAMES = {
    "01": "mixer_opening",
    "06": "mixer_supply_temp",
    "07": "mixer_target_temp",
    "08": "room_temp",
    "09": "room_target_temp",
}

_BINARY_SENSOR_NAMES = {
    "devstatus_outfeeder": "feeder",
    "devstatus_outdhw": "dhw_pump",
    "devstatus_outcirc": "dhwc_pump",
    "devstatus_outbuffer": "buffer_pump",
    "devstatus_outheater": "heater",
}

_SELECTION_LABELS = {
    source: target
    for target, sources in {
        "temp_setpoint": ("boiler_temp_setpoint", "dhw_temp_setpoint", "buffer_temp_setpoint"),
        "hysteresis": ("boiler_hysteresis", "dhw_hysteresis", "buffer_hysteresis", "room_hysteresis"),
        "feed_time": ("standby_feed_time", "interval_feed_time"),
        "break_time": ("standby_period", "interval_break_time", "dhwc_break_time"),
        "air": ("standby_air", "interval_air"),
        "enabled": ("buffer", "return_protection", "mixer"),
        "work_mode": ("burner_mode", "dhw_mode", "dhwc_mode", "boiler_mode", "mode", "room_mode"),
        "target_temperature": ("mixer_target_temp", "room_target_temp"),
        "pump_state": ("pump", "dhw_pump", "dhwc_pump", "buffer_pump"),
    }.items()
    for source in sources
}


def entity_translation(key: str) -> tuple[str, dict[str, str]]:
    """Zwraca czytelny klucz tłumaczenia i opcjonalny numer obiegu."""
    if match := re.fullmatch(r"ch([123])_(.+)", key):
        return f"ch_{match[2]}", {"number": match[1]}
    if match := re.fullmatch(r"c([012])(01|06|07|08|09)", key):
        return f"ch_{_CIRCUIT_SENSOR_NAMES[match[2]]}", {"number": str(int(match[1]) + 1)}
    if match := re.fullmatch(r"devstatus_outch([123])", key):
        return "ch_pump", {"number": match[1]}
    if match := re.fullmatch(r"weather_stop_temp_ch([12])", key):
        return "ch_weather_stop_temp", {"number": match[1]}
    return _BINARY_SENSOR_NAMES.get(key, key), {}


def selection_translation_key(key: str) -> str:
    """Zwraca wspólną etykietę opcji, niezależną od obiegu i platformy."""
    key, _ = entity_translation(key)
    key = key.removeprefix("ch_")
    return _SELECTION_LABELS.get(key, key)


def profile_page_title(hass: HomeAssistant, page: str) -> str:
    """Tłumaczy nazwę grupy nastaw, podstawiając numer obiegu."""
    if re.fullmatch(r"ch[123]", page):
        return translate(hass, "common.profile_page_circuit", number=page[-1])
    return translate(hass, f"common.profile_page_{page}")


async def async_load_localizations(hass: HomeAssistant) -> None:
    """Ładuje tłumaczenia HA przed uruchomieniem komunikacji."""
    for language in dict.fromkeys(("en", hass.config.language)):
        for category in ("common", "entity", "options"):
            await async_get_translations(hass, language, category, {DOMAIN})


def translate(
    hass: HomeAssistant,
    key: str,
    *,
    language: str | None = None,
    **placeholders: object,
) -> str:
    """Zwraca tekst z pamięci HA, z angielskim językiem zapasowym."""
    if key.startswith("entity."):
        parts = key.split(".", 3)
        if len(parts) == 4:
            parts[2], entity_placeholders = entity_translation(parts[2])
            placeholders = {**entity_placeholders, **placeholders}
            key = ".".join(parts)
    category = key.split(".", 1)[0]
    full_key = f"component.{DOMAIN}.{key}"
    language = language or hass.config.language
    resources = async_get_cached_translations(hass, language, category, DOMAIN)
    text = resources.get(full_key)
    if text is None:
        english = async_get_cached_translations(hass, "en", category, DOMAIN)
        text = english.get(full_key, key)
    return text.format(**placeholders) if placeholders else text
