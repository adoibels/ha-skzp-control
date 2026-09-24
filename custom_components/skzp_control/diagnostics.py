"""Dane diagnostyczne integracji."""

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN

TO_REDACT = {"DevId", "DevPin", "Token"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Zwraca aktualne dane sterownika do pobrania jako plik diagnostyczny."""
    client = hass.data[DOMAIN][entry.entry_id]

    return {
        "model": client.model,
        "connected": client.connected,
        "controller_data": async_redact_data(dict(client.data), TO_REDACT),
    }
