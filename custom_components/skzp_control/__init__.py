"""Uruchamianie i zamykanie integracji."""
import asyncio
import logging
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError, ConfigEntryNotReady
from .const import CONF_KNOWN_ENTITIES, DOMAIN
from .device import SUPPORTED_DEVICE_MODELS, detect_device_model
from .coordinator import SkzpCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS = ["sensor", "binary_sensor", "number", "select", "button", "switch"]

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Konfiguruje integrację na podstawie wpisu konfiguracyjnego."""
    host = entry.data[CONF_HOST]
    port = entry.data[CONF_PORT]

    skzp_client = SkzpCoordinator(
        hass,
        host,
        port,
        entry.entry_id,
        entry.options,
    )
    await skzp_client.start()

    # Model i pełna ramka danych są wymagane przed utworzeniem encji.
    data_ready = await skzp_client.wait_for_data(
        timeout=skzp_client.no_data_timeout
    )

    dev_type = skzp_client.data.get("DevType")
    if not data_ready or not isinstance(dev_type, str) or not dev_type.strip():
        skzp_client.create_no_data_notification()
        await skzp_client.stop(dismiss_notification=False)
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="initial_data_timeout",
            translation_placeholders={
                "host": host,
                "port": str(port),
                "seconds": f"{skzp_client.no_data_timeout:g}",
            },
        )

    if detect_device_model(dev_type) not in SUPPORTED_DEVICE_MODELS:
        await skzp_client.stop()
        raise ConfigEntryError(
            translation_domain=DOMAIN,
            translation_key="unsupported_model",
            translation_placeholders={"model": dev_type},
        )

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = skzp_client

    try:
        if CONF_KNOWN_ENTITIES not in entry.options:
            from .config_entity_selection import build_entity_choices
            from .entity_layout import (
                existing_buffer_sensor_keys,
                initialize_entity_selection,
            )

            choices = build_entity_choices(
                skzp_client.model,
                skzp_client.data,
                existing_buffer_sensor_keys(hass, entry.entry_id),
            )
            initialize_entity_selection(hass, entry, choices)
        skzp_client.platform_device_type = skzp_client.data.get("DevType")
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except (Exception, asyncio.CancelledError):
        hass.data[DOMAIN].pop(entry.entry_id, None)
        await skzp_client.stop()
        raise
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Wyładowuje integrację i zamyka jej połączenie TCP."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False

    client: "SkzpCoordinator | None" = hass.data.get(DOMAIN, {}).pop(
        entry.entry_id, None
    )
    if client:
        await client.stop()

    return True


