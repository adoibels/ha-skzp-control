"""Sensory binarne stanu wyjść sterownika."""

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .device import DEFAULT_DEVICE_MODEL, build_device_info
from .entity_layout import is_entity_enabled

_LOGGER = logging.getLogger(__name__)

# Wyjścia binarne i ich pozycje w DevStatus.
OUTPUT_KEYS = {
    "DevStatus_outFeeder": 9,
    "DevStatus_outCH1": 10,
    "DevStatus_outCH2": 11,
    "DevStatus_outDHW": 12,
    "DevStatus_outCirc": 13,
    "DevStatus_outCH3": 14,
    "DevStatus_outBuffer": 15,
    "DevStatus_outHeater": 16,
}

def get_output_descriptions(
    model: str,
) -> dict[str, int]:
    """Zwraca wyjścia do sprawdzenia dla rozpoznanego modelu."""
    if model == DEFAULT_DEVICE_MODEL:
        return {}
    return OUTPUT_KEYS

def is_output_supported(data: Mapping[str, Any], index: int) -> bool:
    """Sprawdza, czy DevStatus zawiera wskazane wyjście."""
    devstatus = data.get("DevStatus")
    return isinstance(devstatus, str) and len(devstatus) > index


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Tworzy sensory binarne obsługiwane przez wykryty sterownik."""
    client = hass.data[DOMAIN][config_entry.entry_id]
    entry_id = config_entry.entry_id
    supported_descriptions = [
        (key, index)
        for key, index in get_output_descriptions(client.model).items()
        if is_output_supported(client.data, index)
    ]
    entities = [
        DevStatusBinarySensor(client, entry_id, key, index)
        for key, index in supported_descriptions
        if is_entity_enabled(config_entry, "binary_sensor", key)
    ]
    async_add_entities(entities)

    _LOGGER.debug(
        "[SKZP Control] %s:%s — Added %d binary sensors from DevStatus for model %s.",
        client.host,
        client.port,
        len(entities),
        client.model,
    )
class DevStatusBinarySensor(BinarySensorEntity):
    """Reprezentuje stan pojedynczego wyjścia zapisanego w DevStatus."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        client,
        entry_id: str,
        key: str,
        index: int,
    ) -> None:
        self._client = client
        self._key = key
        self._index = index
        self._attr_translation_key = key.lower()
        self._attr_unique_id = f"{entry_id}_{key.lower()}"
        self._attr_is_on = None
        self._attr_available = False

    async def async_added_to_hass(self) -> None:
        """Uruchamia nasłuchiwanie danych po dodaniu encji do HA."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.hass.bus.async_listen(
                self._client.update_event, self._handle_data_update
            )
        )
        self._handle_data_update(None)

    @callback
    def _handle_data_update(self, _event: Event | None) -> None:
        """Aktualizuje stan sensora na podstawie wartości DevStatus."""
        devstatus = self._client.data.get("DevStatus")

        # Brak pozycji w DevStatus oznacza niedostępny sygnał, a nie wyłączenie.
        state = (
            devstatus[self._index]
            if (
                self._client.available
                and isinstance(devstatus, str)
                and len(devstatus) > self._index
            )
            else None
        )
        self._attr_available = state in {"0", "1"}
        self._attr_is_on = (
            state == "1"
            if self._attr_available
            else None
        )

        self.async_write_ha_state()

    @property
    def device_info(self):
        return build_device_info(self._client)
