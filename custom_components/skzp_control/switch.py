"""Przełączniki parametrów i głównego sterowania."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity
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
    resolve_parameter_write_key,
)

if TYPE_CHECKING:
    from .coordinator import SkzpCoordinator

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class ToggleSwitchDescription:
    """Definicja przełącznika."""

    data_key: str
    unique_suffix: str
    icon: str
    on_value: str
    off_value: str



SWITCH_DESCRIPTIONS = (
    ToggleSwitchDescription(
        "DHWPriority",
        "dhw_priority",
        "mdi:priority-high",
        "On",
        "Off",
    ),
    ToggleSwitchDescription(
        "DHWCAlwaysON",
        "dhwc_always_on",
        "mdi:water-sync",
        "On",
        "Off",
    ),
    ToggleSwitchDescription(
        "C020",
        "ch1_mixer",
        "mdi:valve",
        "1",
        "0",
    ),
    ToggleSwitchDescription(
        "C120",
        "ch2_mixer",
        "mdi:valve",
        "1",
        "0",
    ),
    ToggleSwitchDescription(
        "C220",
        "ch3_mixer",
        "mdi:valve",
        "1",
        "0",
    ),
    ToggleSwitchDescription(
        "CH1MixActive",
        "ch1_mixer",
        "mdi:valve",
        "On",
        "Off",
    ),
)

SWITCH_KEYS = frozenset(
    description.data_key for description in SWITCH_DESCRIPTIONS
)
def get_switch_descriptions(
    model: str,
) -> tuple[ToggleSwitchDescription, ...]:
    """Zwraca przełączniki do sprawdzenia dla rozpoznanego modelu."""
    if model == DEFAULT_DEVICE_MODEL:
        return ()
    return SWITCH_DESCRIPTIONS


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Tworzy przełączniki obsługiwane przez wykryty sterownik."""
    client = hass.data[DOMAIN][config_entry.entry_id]
    entry_id = config_entry.entry_id

    supported_descriptions = [
        description
        for description in get_switch_descriptions(client.model)
        if is_parameter_definition_supported(
            client.data,
            description.data_key,
            SWITCH_KEYS,
        )
    ]
    # Główne sterowanie nie jest częścią formularza wyboru encji.
    entities = [
        SkzpDevicePowerSwitch(client, entry_id),
        *[
            SkzpToggleSwitch(client, entry_id, description)
            for description in supported_descriptions
            if is_entity_enabled(
                config_entry,
                "switch",
                description.data_key,
            )
        ],
    ]

    async_add_entities(entities)
    _LOGGER.debug(
        "[SKZP Control] %s:%s — Added %d switches for model %s.",
        client.host,
        client.port,
        len(entities),
        client.model,
    )
class SkzpSwitchBase(PendingChangeMixin, SwitchEntity):
    """Wspólna obsługa przełączników."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        client: SkzpCoordinator,
        entry_id: str,
        unique_suffix: str,
        icon: str,
        entity_category: EntityCategory | None = None,
    ) -> None:
        self._client = client
        self._attr_unique_id = f"{entry_id}_{unique_suffix}"
        self._attr_translation_key = unique_suffix
        self._attr_icon = icon
        self._attr_entity_category = entity_category
        self._attr_is_on = None

        self._has_valid_state = False
        self._init_pending_change()
        self._confirmed_state: bool | None = None
        self._last_controller_state: bool | None = None
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
        """Aktualizuje stan przełącznika."""
        raise NotImplementedError

    def _update_availability(self, has_valid_state: bool) -> bool:
        """Aktualizuje dostępność i wykrywa jej zmianę."""
        self._has_valid_state = has_valid_state
        available = self.available
        changed = available != self._last_published_available
        self._last_published_available = available
        return changed


    def _apply_optimistic_state(self, new_state: bool) -> None:
        """Pokazuje zmianę i pomija starsze ramki."""
        self._attr_is_on = new_state
        self.async_write_ha_state()

    def _process_controller_state(
        self,
        new_state: bool,
        availability_changed: bool,
    ) -> None:
        """Przetwarza prawidłowy stan odebrany ze sterownika."""
        self._last_controller_state = new_state
        if self._confirm_pending_change(new_state):
            self._confirmed_state = new_state

        if self._is_stale_update(new_state):
            if availability_changed:
                self.async_write_ha_state()
            return

        self._confirmed_state = new_state
        state_changed = self._attr_is_on != new_state
        if state_changed:
            self._attr_is_on = new_state
        if state_changed or availability_changed:
            self.async_write_ha_state()

    async def _send_optimistic_command(
        self,
        new_state: bool,
        command: dict[str, str],
    ) -> None:
        """Wysyła polecenie i czeka na potwierdzenie."""
        state_text = "on" if new_state else "off"
        previous_state = (
            self._confirmed_state
            if self._confirmed_state is not None
            else self._attr_is_on
        )

        generation, confirmation_event = self._begin_pending_change(
            new_state, self._client.command_pending_timeout
        )

        self._apply_optimistic_state(new_state)

        try:
            async def send_attempt() -> None:
                try:
                    await self._client.send_command(command)
                except HomeAssistantError:
                    if generation == self._send_generation:
                        self._restore_controller_state(previous_state)
                    raise

            def log_retry(retry_number: int) -> None:
                self._client.log_command_retry(self._client.translate(f'entity.switch.{self._attr_translation_key}.name', language='en'), state_text, retry_number)

            if await self._client.command_manager.execute(
                send=send_attempt,
                wait=lambda: self._async_wait_for_confirmation(confirmation_event, generation, new_state),
                is_current=lambda: generation == self._send_generation,
                retry_count=self._client.command_retry_count,
                retry_delay=self._client.command_retry_delay,
                on_retry=log_retry,
            ):
                if generation == self._send_generation:
                    self._confirmation_event = None
                return

            if generation != self._send_generation:
                return

            self._restore_controller_state(previous_state)
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="command_not_confirmed",
                translation_placeholders={
                    "parameter": (self.name or self._attr_translation_key),
                    "value": self._client.translate(f"common.state_{state_text}"),
                },
            )
        except asyncio.CancelledError:
            if generation == self._send_generation:
                self._restore_controller_state(previous_state)
            raise
        finally:
            if generation == self._send_generation:
                self._clear_pending_change()

    async def _async_wait_for_confirmation(
        self,
        confirmation_event: asyncio.Event,
        generation: int,
        target_state: bool,
    ) -> bool:
        """Czeka na ramkę potwierdzającą stan przełącznika."""
        return await self._client.command_manager.wait_for_confirmation(
            confirmation_event,
            self._client.command_confirmation_timeout,
            lambda: generation == self._send_generation and self._last_controller_state is target_state,
        )

    def _restore_controller_state(
        self,
        fallback_state: bool | None,
    ) -> None:
        """Przywraca ostatni stan odebrany ze sterownika."""
        restored_state = (
            self._last_controller_state
            if self._last_controller_state is not None
            else fallback_state
        )
        self._attr_is_on = restored_state
        self._confirmed_state = restored_state
        self._clear_pending_change()
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Sprawdza dostępność bieżącego stanu."""
        return self._client.available and self._has_valid_state

    @property
    def device_info(self) -> DeviceInfo:
        return build_device_info(self._client)


class SkzpDevicePowerSwitch(SkzpSwitchBase):
    """Główne sterowanie urządzeniem."""

    def __init__(
        self,
        client: SkzpCoordinator,
        entry_id: str,
    ) -> None:
        super().__init__(
            client,
            entry_id,
            "device_power",
            "mdi:power",
        )

    @callback
    def _handle_data_update(self, _event: Event | None) -> None:
        """Aktualizuje stan na podstawie DevStatus."""
        raw_status = self._client.data.get("DevStatus")
        dev_status = (
            str(raw_status).strip().upper()
            if self._client.available and raw_status is not None
            else ""
        )
        has_valid_state = len(dev_status) >= 3
        availability_changed = self._update_availability(has_valid_state)

        if not has_valid_state:
            if availability_changed:
                self.async_write_ha_state()
            return

        new_state = not dev_status.startswith("OFF")
        self._process_controller_state(new_state, availability_changed)

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._send_optimistic_command(
            True,
            {"CommandToDo": "Cmd_DeviceOn"},
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._send_optimistic_command(
            False,
            {"CommandToDo": "Cmd_DeviceOff"},
        )


class SkzpToggleSwitch(SkzpSwitchBase):
    """Przełącznik parametru sterownika."""

    def __init__(
        self,
        client: SkzpCoordinator,
        entry_id: str,
        description: ToggleSwitchDescription,
    ) -> None:
        super().__init__(
            client,
            entry_id,
            description.unique_suffix,
            description.icon,
            entity_category=EntityCategory.CONFIG,
        )
        self._data_key = description.data_key
        self._on_value = description.on_value
        self._off_value = description.off_value
        self._on_value_normalized = description.on_value.casefold()
        self._off_value_normalized = description.off_value.casefold()

    @callback
    def _handle_data_update(self, _event: Event | None) -> None:
        """Aktualizuje stan na podstawie danych sterownika."""
        raw_value = get_parameter_value(self._client.data, self._data_key)
        new_state = (
            self._decode_state(raw_value)
            if self._client.available and raw_value is not None
            else None
        )
        has_valid_state = new_state is not None
        availability_changed = self._update_availability(has_valid_state)

        if new_state is None:
            if availability_changed:
                self.async_write_ha_state()
            return

        self._process_controller_state(new_state, availability_changed)

    def _decode_state(self, raw_value: Any) -> bool | None:
        """Dekoduje stan bez rozróżniania wielkości liter."""
        normalized = str(raw_value).strip().casefold()
        if normalized == self._on_value_normalized:
            return True
        if normalized == self._off_value_normalized:
            return False
        return None

    async def async_turn_on(self, **kwargs: Any) -> None:
        command_key = resolve_parameter_write_key(
            self._client.data,
            self._data_key,
        )
        await self._send_optimistic_command(
            True,
            {command_key: self._on_value},
        )

    async def async_turn_off(self, **kwargs: Any) -> None:
        command_key = resolve_parameter_write_key(
            self._client.data,
            self._data_key,
        )
        await self._send_optimistic_command(
            False,
            {command_key: self._off_value},
        )
