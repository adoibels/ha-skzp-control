"""Przyciski wysyłające polecenia do sterownika."""

from dataclasses import dataclass
import logging

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .device import build_device_info

_LOGGER = logging.getLogger(__name__)

@dataclass(frozen=True)
class CommandButtonDescription:
    """Opis przycisku wysyłającego pojedyncze polecenie do sterownika."""

    command: str
    unique_suffix: str
    icon: str
    entity_category: EntityCategory | None = None


COMMAND_BUTTONS = (
    CommandButtonDescription(
        "Cmd_BurnerStart",
        "burner_start",
        "mdi:fire",
    ),
    CommandButtonDescription(
        "Cmd_BurnerStop",
        "burner_stop",
        "mdi:fire-off",
    ),
    CommandButtonDescription(
        "Cmd_AlarmsReset",
        "alarms_reset",
        "mdi:alert-remove-outline",
        EntityCategory.DIAGNOSTIC,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Tworzy przyciski sterujące."""
    client = hass.data[DOMAIN][config_entry.entry_id]
    async_add_entities(
        SkzpCommandButton(client, config_entry.entry_id, description)
        for description in COMMAND_BUTTONS
    )


class SkzpCommandButton(ButtonEntity):
    """Przycisk wysyłający polecenie do sterownika przez TCP."""

    _attr_should_poll = False
    _attr_has_entity_name = True

    def __init__(
        self,
        client,
        entry_id: str,
        description: CommandButtonDescription,
    ) -> None:
        self._client = client
        self._command = description.command
        self._attr_translation_key = description.unique_suffix
        self._attr_icon = description.icon
        self._attr_unique_id = f"{entry_id}_{description.unique_suffix}"
        self._attr_entity_category = description.entity_category

    async def async_added_to_hass(self) -> None:
        """Nasłuchuje zmian dostępności sterownika."""
        await super().async_added_to_hass()
        self.async_on_remove(
            self._client.hass.bus.async_listen(
                self._client.update_event, self._handle_data_update
            )
        )
        self._handle_data_update(None)

    @callback
    def _handle_data_update(self, _event: Event | None) -> None:
        """Odświeża w HA dostępność przycisku."""
        self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Przycisk jest dostępny tylko przy aktualnych danych sterownika."""
        return self._client.available

    @property
    def device_info(self):
        return build_device_info(self._client)

    async def async_press(self) -> None:
        """Wysyła przypisane polecenie do sterownika."""
        await self._client.send_command({"CommandToDo": self._command})
        _LOGGER.debug(
            "[SKZP Control] %s:%s — Command sent: %s (%s).",
            self._client.host, self._client.port,
            self._client.translate(f"entity.button.{self._attr_translation_key}.name", language="en"),
            self._command,
        )
