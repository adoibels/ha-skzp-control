"""Tłumaczenie tekstów generowanych przez integrację."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import (
    async_get_cached_translations,
    async_get_translations,
)

from .const import DOMAIN


async def async_load_localizations(hass: HomeAssistant) -> None:
    """Ładuje tłumaczenia HA przed uruchomieniem komunikacji."""
    for language in dict.fromkeys(("en", hass.config.language)):
        for category in ("common", "entity"):
            await async_get_translations(hass, language, category, {DOMAIN})


def translate(
    hass: HomeAssistant,
    key: str,
    *,
    language: str | None = None,
    **placeholders: object,
) -> str:
    """Zwraca tekst z pamięci HA, z angielskim językiem zapasowym."""
    category = key.split(".", 1)[0]
    full_key = f"component.{DOMAIN}.{key}"
    language = language or hass.config.language
    resources = async_get_cached_translations(hass, language, category, DOMAIN)
    text = resources.get(full_key)
    if text is None:
        english = async_get_cached_translations(hass, "en", category, DOMAIN)
        text = english.get(full_key, key)
    return text.format(**placeholders) if placeholders else text
