"""Listy wyboru trybów pracy sterownika."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .pending_change import PendingChangeMixin
from .const import DOMAIN
from .device import DEFAULT_DEVICE_MODEL, build_device_info
from .entity_layout import is_entity_enabled
from .parameter_resolver import (
    get_parameter_value,
    is_parameter_definition_supported,
    resolve_parameter_key,
    resolve_parameter_write_key,
)

if TYPE_CHECKING:
    from .coordinator import SkzpCoordinator

_LOGGER = logging.getLogger(__name__)

# Stałe klucze trybów i odpowiadające im wartości sterownika.

BOILER_MODES = {
    "constant_temp": "1",
    "timer": "2",
}

BURNER_MODES = {
    "automatic": "Auto",
    "interval": "Interwal",
}

DHW_MODES = {
    "off": "Stop",
    "automatic": "Still_On",
    "pump_always_on": "PumpStillOn",
    "timer": "Timer",
}

DHWC_MODES = CH_MODES_TEXT = {
    "off": "Stop",
    "active": "Active",
    "timer": "Timer",
}

CH_MODES_NUMERIC = {
    "off": "0",
    "active": "1",
    "timer": "2",
}

ROOM_MODES_NUMERIC = {
    "off": "0",
    "economy": "1",
    "comfort": "2",
    "timer": "3",
}

ROOM_MODES_TEXT = {
    "off": "Off",
    "economy": "Economy",
    "comfort": "Comfort",
    "timer": "Timer",
}

_MODE_MAP_BY_DATA_KEY = {
    "C012": CH_MODES_NUMERIC,
    "CH1Mode": CH_MODES_TEXT,
    "C112": CH_MODES_NUMERIC,
    "CH2Mode": CH_MODES_TEXT,
    "C016": ROOM_MODES_NUMERIC,
    "CH1RoomMode": ROOM_MODES_TEXT,
}

_MODE_ALIAS_FAMILIES = (
    ("C012", "CH1Mode"),
    ("C112", "CH2Mode"),
    ("C016", "CH1RoomMode"),
)


def _modes_for_value(
    requested_key: str,
    resolved_key: str,
    raw_value: object,
    default_modes: dict[str, str],
) -> dict[str, str]:
    """Dobiera opcje do nazwy i wartości używanej przez sterownik."""
    family = next(
        (keys for keys in _MODE_ALIAS_FAMILIES if requested_key in keys),
        None,
    )
    if family is None:
        return default_modes

    raw_text = str(raw_value).strip()
    for key in family:
        modes = _MODE_MAP_BY_DATA_KEY[key]
        if raw_text in modes.values():
            return modes

    return _MODE_MAP_BY_DATA_KEY.get(resolved_key, default_modes)


@dataclass(frozen=True)
class ModeSelectDescription:
    """Definicja encji wyboru."""

    data_key: str
    unique_suffix: str
    icon: str
    modes: dict[str, str]



SELECT_DESCRIPTIONS = [
    ModeSelectDescription(
        "BuMode",
        "burner_mode",
        "mdi:fire",
        BURNER_MODES,
    ),
    ModeSelectDescription(
        "DHWMode",
        "dhw_mode",
        "mdi:water",
        DHW_MODES,
    ),
    ModeSelectDescription(
        "DHWCMode",
        "dhwc_mode",
        "mdi:water-sync",
        DHWC_MODES,
    ),
    ModeSelectDescription(
        "K006",
        "boiler_mode",
        "mdi:water-boiler",
        BOILER_MODES,
    ),
    ModeSelectDescription(
        "C012",
        "ch1_mode",
        "mdi:pump",
        CH_MODES_NUMERIC,
    ),
    ModeSelectDescription(
        "C016",
        "ch1_room_mode",
        "mdi:home-thermometer-outline",
        ROOM_MODES_NUMERIC,
    ),
    ModeSelectDescription(
        "C112",
        "ch2_mode",
        "mdi:pump",
        CH_MODES_NUMERIC,
    ),
    ModeSelectDescription(
        "C116",
        "ch2_room_mode",
        "mdi:home-thermometer-outline",
        ROOM_MODES_NUMERIC,
    ),
    ModeSelectDescription(
        "C212",
        "ch3_mode",
        "mdi:pump",
        CH_MODES_NUMERIC,
    ),
    ModeSelectDescription(
        "C216",
        "ch3_room_mode",
        "mdi:home-thermometer-outline",
        ROOM_MODES_NUMERIC,
    ),
    ModeSelectDescription(
        "CH1Mode",
        "ch1_mode",
        "mdi:pump",
        CH_MODES_TEXT,
    ),
    ModeSelectDescription(
        "CH2Mode",
        "ch2_mode",
        "mdi:pump",
        CH_MODES_TEXT,
    ),
    ModeSelectDescription(
        "CH1RoomMode",
        "ch1_room_mode",
        "mdi:home-thermometer-outline",
        ROOM_MODES_TEXT,
    ),
]

SELECT_KEYS = frozenset(
    description.data_key for description in SELECT_DESCRIPTIONS
)
def get_select_descriptions(
    model: str,
) -> list[ModeSelectDescription]:
    """Zwraca listy wyboru do sprawdzenia dla rozpoznanego modelu."""
    if model == DEFAULT_DEVICE_MODEL:
        return []
    return SELECT_DESCRIPTIONS


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Tworzy listy wyboru obsługiwane przez wykryty sterownik."""
    client = hass.data[DOMAIN][config_entry.entry_id]
    entry_id = config_entry.entry_id

    supported_descriptions = [
        description
        for description in get_select_descriptions(client.model)
        if is_parameter_definition_supported(
            client.data,
            description.data_key,
            SELECT_KEYS,
        )
    ]
    entities = [
        SkzpModeSelect(client, entry_id, description)
        for description in supported_descriptions
        if is_entity_enabled(config_entry, "select", description.data_key)
    ]
    async_add_entities(entities)

    _LOGGER.debug(
        "[SKZP Control] %s:%s — Added %d select entities for model %s.",
        client.host, client.port,
        len(entities),
        client.model,
    )
class SkzpModeSelect(PendingChangeMixin, SelectEntity):
    """Tryb pracy sterownika."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        client: SkzpCoordinator,
        entry_id: str,
        description: ModeSelectDescription,
    ) -> None:
        self._client = client
        self._data_key = description.data_key
        self._default_modes: dict[str, str] = description.modes
        self._modes: dict[str, str] = description.modes
        self._modes_inverse: dict[str, str] = {
            value: name for name, value in description.modes.items()
        }

        self._attr_unique_id = f"{entry_id}_{description.unique_suffix}"
        self._attr_translation_key = description.unique_suffix
        self._attr_icon = description.icon
        self._attr_options = list(description.modes.keys())
        self._attr_current_option = None
        self._attr_entity_category = EntityCategory.CONFIG

        self._has_valid_value = False
        self._init_pending_change()
        self._confirmed_option: str | None = None
        self._last_controller_option: str | None = None
        self._last_published_available: bool | None = None

    async def async_added_to_hass(self) -> None:
        """Rozpoczyna nasłuchiwanie danych sterownika."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.hass.bus.async_listen(
                self._client.update_event,
                self._handle_data_update,
            )
        )
        self._handle_data_update(None)

    @callback
    def _handle_data_update(self, _event: Event | None) -> None:
        """Aktualizuje wybraną opcję na podstawie danych sterownika."""
        resolved_key = resolve_parameter_key(
            self._client.data,
            self._data_key,
        )
        raw_value = get_parameter_value(self._client.data, self._data_key)
        modes = _modes_for_value(
            self._data_key,
            resolved_key,
            raw_value,
            self._default_modes,
        )
        options_changed = modes != self._modes
        if options_changed:
            self._modes = modes
            self._modes_inverse = {
                value: name for name, value in modes.items()
            }
            self._attr_options = list(modes)
        new_value = (
            self._modes_inverse.get(str(raw_value).strip())
            if self._client.available and raw_value is not None
            else None
        )
        available = new_value is not None
        availability_changed = available != self._last_published_available
        self._has_valid_value = available
        self._last_published_available = available

        if not available:
            if availability_changed or options_changed:
                self.async_write_ha_state()
            return

        assert new_value is not None
        self._last_controller_option = new_value
        if self._confirm_pending_change(new_value):
            self._confirmed_option = new_value

        if self._is_stale_update(new_value):
            if availability_changed:
                self.async_write_ha_state()
            return

        self._confirmed_option = new_value
        option_changed = self._attr_current_option != new_value
        if option_changed:
            self._attr_current_option = new_value
        if option_changed or availability_changed or options_changed:
            self.async_write_ha_state()


    async def async_select_option(self, option: str) -> None:
        """Wysyła wybraną opcję do sterownika."""
        if option not in self._modes:
            return

        val_to_send = self._modes[option]
        previous_option = self._confirmed_option or self._attr_current_option

        generation, confirmation_event = self._begin_pending_change(
            option, self._client.command_pending_timeout
        )

        # Pokaż zmianę od razu i pomijaj starsze ramki.
        self._attr_current_option = option
        self.async_write_ha_state()

        try:
            async def send_attempt() -> None:
                try:
                    command_key = resolve_parameter_write_key(self._client.data, self._data_key)
                    command = {command_key: val_to_send}
                    await self._client.send_command(command)
                    _LOGGER.debug("[SKZP Control] %s:%s — Command sent: %s = %s.", self._client.host, self._client.port, self._client.translate(f'entity.select.{self._attr_translation_key}.name', language='en'), self._client.translate(f'entity.select.{self._attr_translation_key}.state.{option}', language='en'))
                except HomeAssistantError:
                    if generation == self._send_generation:
                        self._restore_controller_option(previous_option)
                    raise

            def log_retry(retry_number: int) -> None:
                self._client.log_command_retry(self._client.translate(f'entity.select.{self._attr_translation_key}.name', language='en'), self._client.translate(f'entity.select.{self._attr_translation_key}.state.{option}', language='en'), retry_number)

            if await self._client.command_manager.execute(
                send=send_attempt,
                wait=lambda: self._async_wait_for_confirmation(confirmation_event, generation, option),
                is_current=lambda: generation == self._send_generation,
                retry_count=self._client.command_retry_count,
                retry_delay=self._client.command_retry_delay,
                on_retry=log_retry,
                is_confirmed=lambda: confirmation_event.is_set()
                and self._last_controller_option == option,
            ):
                if generation == self._send_generation:
                    self._confirmation_event = None
                    _LOGGER.debug("[SKZP Control] %s:%s — Change confirmed: %s = %s.", self._client.host, self._client.port, self._client.translate(f'entity.select.{self._attr_translation_key}.name', language='en'), self._client.translate(f'entity.select.{self._attr_translation_key}.state.{option}', language='en'))
                return

            if generation != self._send_generation:
                return

            self._restore_controller_option(previous_option)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_not_confirmed",
                translation_placeholders={
                    "parameter": (self.name or self._data_key),
                    "value": self._client.translate(
                        f"entity.select.{self._attr_translation_key}.state.{option}"
                    ),
                },
            )
        except asyncio.CancelledError:
            if generation == self._send_generation:
                self._restore_controller_option(previous_option)
            raise
        finally:
            if generation == self._send_generation:
                self._clear_pending_change()

    async def _async_wait_for_confirmation(
        self,
        confirmation_event: asyncio.Event,
        generation: int,
        target_option: str,
    ) -> bool:
        """Czeka na ramkę potwierdzającą wybraną opcję."""
        return await self._client.command_manager.wait_for_confirmation(
            confirmation_event,
            self._client.command_confirmation_timeout,
            lambda: generation == self._send_generation and self._last_controller_option == target_option,
        )

    def _restore_controller_option(self, fallback_option: str | None) -> None:
        """Przywraca ostatnią opcję potwierdzoną przez sterownik."""
        restored_option = self._last_controller_option or fallback_option
        self._attr_current_option = restored_option
        _LOGGER.debug("[SKZP Control] %s:%s — State restored: %s = %s.", self._client.host, self._client.port, self._client.translate(f'entity.select.{self._attr_translation_key}.name', language='en'), self._client.translate(f'entity.select.{self._attr_translation_key}.state.{restored_option}', language='en') if restored_option is not None else "unknown")
        self._confirmed_option = restored_option
        self._clear_pending_change()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Sprawdza dostępność bieżącej wartości parametru."""
        return self._client.available and self._has_valid_value

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self._client)
