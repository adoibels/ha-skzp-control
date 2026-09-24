"""Konfiguracja integracji i jej opcji."""

import asyncio
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.selector import (
    BooleanSelector,
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .const import (
    CONF_BOILER_TEMP_MAX,
    DEFAULT_BOILER_TEMP_MAX,
    MIN_BOILER_TEMP_MAX,
    MAX_BOILER_TEMP_MAX,
    CONF_COMMAND_CONFIRM_TIMEOUT,
    CONF_COMMAND_RETRY_COUNT,
    CONF_COMMAND_RETRY_DELAY,
    CONF_DEVICE_MODEL,
    CONF_DISABLED_ENTITIES,
    CONF_NO_DATA_TIMEOUT,
    CONF_NOTIFY_CONNECTION_LOST,
    CONF_NOTIFY_CONNECTION_RESTORED,
    CONF_NUMBER_SEND_DELAY,
    CONF_RECONNECT_DELAY,
    DEFAULT_COMMAND_CONFIRM_TIMEOUT,
    DEFAULT_COMMAND_RETRY_COUNT,
    DEFAULT_COMMAND_RETRY_DELAY,
    DEFAULT_NO_DATA_TIMEOUT,
    DEFAULT_NOTIFY_CONNECTION_LOST,
    DEFAULT_NOTIFY_CONNECTION_RESTORED,
    DEFAULT_NUMBER_SEND_DELAY,
    DEFAULT_RECONNECT_DELAY,
    DOMAIN,
    MAX_COMMAND_CONFIRM_TIMEOUT,
    MAX_COMMAND_RETRY_COUNT,
    MAX_COMMAND_RETRY_DELAY,
    MAX_NO_DATA_TIMEOUT,
    MAX_NUMBER_SEND_DELAY,
    MAX_RECONNECT_DELAY,
    MAX_FRAME_BYTES,
    MIN_COMMAND_CONFIRM_TIMEOUT,
    MIN_COMMAND_RETRY_COUNT,
    MIN_COMMAND_RETRY_DELAY,
    MIN_NO_DATA_TIMEOUT,
    MIN_NUMBER_SEND_DELAY,
    MIN_RECONNECT_DELAY,
)
from .config_entity_selection import (
    PAGE_DHW_ONLY,
    available_menu_pages,
    build_entity_choices,
    build_entity_page_schema,
    disabled_entities_from_page,
    selectable_entity_ids,
    selection_count_placeholders,
)
from .coordinator import format_communication_error
from .device import (
    DEFAULT_DEVICE_MODEL,
    SUPPORTED_DEVICE_MODELS,
    detect_device_model,
)
from .entity_layout import (
    PAGE_BOILER_BURNER,
    PAGE_CH1,
    PAGE_CH2,
    PAGE_CH3,
    PAGE_DHW_BUFFER,
    EntityChoice,
    existing_buffer_sensor_keys,
)
from .entity_recommendations import recommended_disabled_entities
from .frame_parser import FrameParser
from .frame_download import async_create_frame_download

_LOGGER = logging.getLogger(__name__)

CONNECTION_TIMEOUT_SECONDS = 5.0
DEVICE_DATA_TIMEOUT_SECONDS = 10.0


class CannotConnectError(Exception):
    """Nie udało się połączyć ze sterownikiem."""


class CannotReadDeviceError(Exception):
    """Sterownik nie przesłał modelu i kompletnej ramki danych."""


class UnsupportedDeviceError(Exception):
    """Odebrano ramkę modelu, którego integracja nie obsługuje."""

    def __init__(self, dev_type: str, frame: dict[str, Any]) -> None:
        super().__init__("Unsupported controller model")
        self.dev_type = dev_type
        self.frame = dict(frame)


def _build_connection_schema() -> vol.Schema:
    """Wspólny formularz adresu i tekstowego portu TCP."""
    return vol.Schema(
        {
            vol.Required(CONF_HOST): str,
            vol.Required(CONF_PORT): TextSelector(
                TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="off")
            ),
        }
    )


def _parse_port(value: Any) -> int:
    """Zwraca poprawny port TCP albo zgłasza błąd walidacji."""
    raw_value = str(value).strip()
    if not raw_value.isascii() or not raw_value.isdigit():
        raise ValueError

    port = int(raw_value)
    if not 1 <= port <= 65535:
        raise ValueError
    return port


async def _async_get_device_info(
    host: str, port: int
) -> tuple[str, dict[str, Any]]:
    """Odczytuje model i dane potrzebne do przygotowania formularza."""
    writer = None
    try:
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port),
                timeout=CONNECTION_TIMEOUT_SECONDS,
            )
        except (OSError, asyncio.TimeoutError) as err:
            _LOGGER.debug(
                "[SKZP Control] %s:%s — Connection error during setup (%g s limit): %s",
                host,
                port,
                CONNECTION_TIMEOUT_SECONDS,
                format_communication_error(err),
            )
            raise CannotConnectError from err

        parser = FrameParser(MAX_FRAME_BYTES)
        device_data: dict[str, Any] = {}
        loop = asyncio.get_running_loop()
        deadline = loop.time() + DEVICE_DATA_TIMEOUT_SECONDS

        while loop.time() < deadline:
            remaining = deadline - loop.time()
            try:
                chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
            except (OSError, asyncio.TimeoutError) as err:
                _LOGGER.debug(
                    "[SKZP Control] %s:%s — Error receiving setup data (%g s limit): %s",
                    host,
                    port,
                    DEVICE_DATA_TIMEOUT_SECONDS,
                    format_communication_error(err),
                )
                raise CannotReadDeviceError from err

            if not chunk:
                _LOGGER.debug(
                    "[SKZP Control] %s:%s — TCP connection closed before receiving complete "
                    "setup data.",
                    host,
                    port,
                )
                raise CannotReadDeviceError

            try:
                frames = list(parser.feed(chunk))
            except ValueError as err:
                _LOGGER.debug(
                    "[SKZP Control] %s:%s — Invalid setup data: %s",
                    host,
                    port,
                    format_communication_error(err),
                )
                raise CannotReadDeviceError from err

            for frame in frames:
                if not isinstance(frame, dict):
                    continue

                device_data.update(frame)
                dev_type = device_data.get("DevType")
                if (
                    frame.get("FrameType") != "SkzpData"
                    or not isinstance(dev_type, str)
                    or not dev_type.strip()
                ):
                    continue

                model = detect_device_model(dev_type)
                if model not in SUPPORTED_DEVICE_MODELS:
                    _LOGGER.debug(
                        "[SKZP Control] %s:%s — Detected unsupported controller model: %s",
                        host,
                        port,
                        dev_type,
                    )
                    raise UnsupportedDeviceError(dev_type, frame)
                return model, device_data

        _LOGGER.debug(
            "[SKZP Control] %s:%s — Timed out waiting for complete setup data (%g s limit).",
            host,
            port,
            DEVICE_DATA_TIMEOUT_SECONDS,
        )
        raise CannotReadDeviceError
    finally:
        if writer is not None:
            writer.close()
            try:
                await writer.wait_closed()
            except OSError:
                pass


async def _async_get_device_model(host: str, port: int) -> str:
    """Łączy się ze sterownikiem i zwraca wykryty model."""
    model, _device_data = await _async_get_device_info(host, port)
    return model


@dataclass(frozen=True)
class _NumberOption:
    """Wspólna definicja normalizacji i suwaka ustawienia."""

    default: int | float
    minimum: int | float
    maximum: int | float
    step: int | float
    unit: str | None = None


_NUMBER_OPTIONS = {
    CONF_BOILER_TEMP_MAX: _NumberOption(
        DEFAULT_BOILER_TEMP_MAX, MIN_BOILER_TEMP_MAX, MAX_BOILER_TEMP_MAX, 1, "°C"
    ),
    CONF_NUMBER_SEND_DELAY: _NumberOption(
        DEFAULT_NUMBER_SEND_DELAY, MIN_NUMBER_SEND_DELAY, MAX_NUMBER_SEND_DELAY, 0.1, "s"
    ),
    CONF_COMMAND_RETRY_COUNT: _NumberOption(
        DEFAULT_COMMAND_RETRY_COUNT, MIN_COMMAND_RETRY_COUNT, MAX_COMMAND_RETRY_COUNT, 1
    ),
    CONF_COMMAND_RETRY_DELAY: _NumberOption(
        DEFAULT_COMMAND_RETRY_DELAY, MIN_COMMAND_RETRY_DELAY, MAX_COMMAND_RETRY_DELAY,
        0.5, "s",
    ),
    CONF_COMMAND_CONFIRM_TIMEOUT: _NumberOption(
        DEFAULT_COMMAND_CONFIRM_TIMEOUT, MIN_COMMAND_CONFIRM_TIMEOUT,
        MAX_COMMAND_CONFIRM_TIMEOUT, 1, "s",
    ),
    CONF_NO_DATA_TIMEOUT: _NumberOption(
        DEFAULT_NO_DATA_TIMEOUT, MIN_NO_DATA_TIMEOUT, MAX_NO_DATA_TIMEOUT, 5, "s"
    ),
    CONF_RECONNECT_DELAY: _NumberOption(
        DEFAULT_RECONNECT_DELAY, MIN_RECONNECT_DELAY, MAX_RECONNECT_DELAY, 1, "s"
    ),
}
_BOOLEAN_OPTIONS = {
    CONF_NOTIFY_CONNECTION_LOST: DEFAULT_NOTIFY_CONNECTION_LOST,
    CONF_NOTIFY_CONNECTION_RESTORED: DEFAULT_NOTIFY_CONNECTION_RESTORED,
}


def _default_advanced_options() -> dict[str, Any]:
    """Zwraca domyślne ustawienia komunikacji i potwierdzania poleceń."""
    return {
        **{key: option.default for key, option in _NUMBER_OPTIONS.items()},
        **_BOOLEAN_OPTIONS,
    }


def _normalize_advanced_options(values: Mapping[str, Any]) -> dict[str, Any]:
    """Normalizuje typy i zakresy; typ liczbowy wynika z wartości domyślnej."""
    normalized: dict[str, Any] = {}
    for key, option in _NUMBER_OPTIONS.items():
        try:
            value = type(option.default)(values.get(key, option.default))
        except (TypeError, ValueError):
            value = option.default
        normalized[key] = max(option.minimum, min(value, option.maximum))
    for key, default in _BOOLEAN_OPTIONS.items():
        value = values.get(key, default)
        normalized[key] = value if isinstance(value, bool) else default
    return normalized


def _build_advanced_settings_schema(values: Mapping[str, Any]) -> vol.Schema:
    """Buduje suwaki oraz przełączniki z tych samych definicji co normalizator."""
    schema = {}
    for key, option in _NUMBER_OPTIONS.items():
        config = NumberSelectorConfig(
            min=option.minimum,
            max=option.maximum,
            step=option.step,
            mode=NumberSelectorMode.SLIDER,
        )
        if option.unit is not None:
            config["unit_of_measurement"] = option.unit
        schema[vol.Optional(key, default=values[key])] = NumberSelector(config)
    for key in _BOOLEAN_OPTIONS:
        schema[vol.Optional(key, default=values[key])] = BooleanSelector()
    return vol.Schema(schema)


def _advanced_options_from_form(
    user_input: Mapping[str, Any],
    previous_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalizuje zapis formularza, zachowując pominięte ustawienia."""
    return _normalize_advanced_options({**(previous_options or {}), **user_input})


def _sync_entity_registry_selection(
    hass: HomeAssistant,
    config_entry: config_entries.ConfigEntry,
    choices: list[EntityChoice],
    disabled_entities: set[str],
) -> None:
    """Synchronizuje wybór encji bez usuwania ich z rejestru."""
    selectable_registry_keys = {
        (choice.platform, f"{config_entry.entry_id}_{choice.unique_suffix}")
        for choice in choices
    }
    disabled_registry_keys = {
        (choice.platform, f"{config_entry.entry_id}_{choice.unique_suffix}")
        for choice in choices
        if choice.selection_id in disabled_entities
    }

    registry = er.async_get(hass)
    for registry_entry in er.async_entries_for_config_entry(
        registry, config_entry.entry_id
    ):
        domain = registry_entry.entity_id.split(".", 1)[0]
        registry_key = (domain, registry_entry.unique_id)
        if registry_key not in selectable_registry_keys:
            continue

        should_disable = registry_key in disabled_registry_keys
        if should_disable and registry_entry.disabled_by is None:
            registry.async_update_entity(
                registry_entry.entity_id,
                disabled_by=er.RegistryEntryDisabler.INTEGRATION,
            )
        elif (
            not should_disable
            and registry_entry.disabled_by
            == er.RegistryEntryDisabler.INTEGRATION
        ):
            registry.async_update_entity(
                registry_entry.entity_id,
                disabled_by=None,
            )


class _EntitySelectionFlowMixin:
    """Wspólne kroki wyboru encji i ustawień dla obu formularzy."""

    async def _async_prepare_selection(self) -> dict[str, Any] | None:
        """Przygotowuje stan lub zwraca wynik przerywający krok."""
        raise NotImplementedError

    async def _async_selection_menu(self) -> dict[str, Any]:
        """Wraca do menu właściwego formularza."""
        raise NotImplementedError

    async def _async_step_entity_page(
        self,
        page: str,
        user_input: dict[str, Any] | None,
        step_id: str | None = None,
    ) -> dict[str, Any]:
        """Wyświetla i zapisuje jeden ekran wyboru encji."""
        if (result := await self._async_prepare_selection()) is not None:
            return result
        if user_input is not None:
            self._disabled_entities = disabled_entities_from_page(
                user_input,
                page,
                self._choices,
                self._disabled_entities,
            )
            return await self._async_selection_menu()
        return self.async_show_form(
            step_id=step_id or page,
            data_schema=build_entity_page_schema(
                self._choices,
                page,
                self._disabled_entities,
            ),
        )

    async def async_step_boiler_burner(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Konfiguruje encje kotła i palnika."""
        return await self._async_step_entity_page(PAGE_BOILER_BURNER, user_input)

    async def async_step_dhw_buffer(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Konfiguruje encje CWU i bufora."""
        return await self._async_step_entity_page(PAGE_DHW_BUFFER, user_input)

    async def async_step_dhw(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Konfiguruje encje CWU w modelu bez obsługi bufora."""
        return await self._async_step_entity_page(
            PAGE_DHW_BUFFER,
            user_input,
            PAGE_DHW_ONLY,
        )

    async def async_step_ch1(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._async_step_entity_page(PAGE_CH1, user_input)

    async def async_step_ch2(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._async_step_entity_page(PAGE_CH2, user_input)

    async def async_step_ch3(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return await self._async_step_entity_page(PAGE_CH3, user_input)

    async def async_step_select_all(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Zaznacza wszystkie encje dostępne w formularzu."""
        if (result := await self._async_prepare_selection()) is not None:
            return result
        self._disabled_entities.difference_update(
            selectable_entity_ids(self._choices)
        )
        return await self._async_selection_menu()

    async def async_step_deselect_all(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Odznacza wszystkie opcjonalne encje w formularzu."""
        if (result := await self._async_prepare_selection()) is not None:
            return result
        self._disabled_entities.update(selectable_entity_ids(self._choices))
        return await self._async_selection_menu()

    async def async_step_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Konfiguruje czasy komunikacji, ponawianie i powiadomienia."""
        if (result := await self._async_prepare_selection()) is not None:
            return result
        if user_input is not None:
            self._advanced_options = _advanced_options_from_form(
                user_input,
                self._advanced_options,
            )
            return await self._async_selection_menu()
        return self.async_show_form(
            step_id="settings",
            data_schema=_build_advanced_settings_schema(self._advanced_options),
        )


class SkzpConfigFlow(_EntitySelectionFlowMixin, config_entries.ConfigFlow, domain=DOMAIN):
    """Obsługuje formularz konfiguracji integracji SKZP Control."""

    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._connection_data: dict[str, Any] | None = None
        self._device_model: str | None = None
        self._choices: list[EntityChoice] = []
        self._disabled_entities: set[str] = set()
        self._recommended_disabled: set[str] = set()
        self._advanced_options = _default_advanced_options()
        self._unsupported_placeholders: dict[str, str] = {}
        self._remove_frame_download: Callable[[], None] | None = None

    async def _async_prepare_selection(self) -> dict[str, Any] | None:
        """Wymaga poprawnego odczytu sterownika przed edycją wyboru."""
        if self._connection_data is None or self._device_model is None:
            return self.async_abort(reason="cannot_read_device")
        return None

    async def _async_selection_menu(self) -> dict[str, Any]:
        return await self.async_step_entities()

    @callback
    def _clear_frame_download(self) -> None:
        """Usuwa tymczasową ramkę i jej odnośnik."""
        if self._remove_frame_download is not None:
            self._remove_frame_download()
            self._remove_frame_download = None
        self._unsupported_placeholders = {}

    @callback
    def async_remove(self) -> None:
        """Zwalnia tymczasowe dane po zamknięciu formularza."""
        self._clear_frame_download()
        super().async_remove()

    @callback
    def _prepare_unsupported_device(self, error: UnsupportedDeviceError) -> None:
        """Przygotowuje pobranie ramki bez tworzenia wpisu integracji."""
        self._clear_frame_download()
        download_url, self._remove_frame_download = async_create_frame_download(
            self.hass, error.frame
        )
        self._unsupported_placeholders = {
            "dev_type": re.sub(r"([\\`*_{}\[\]()#+.!|<>~-])", r"\\\1", error.dev_type),
            "download_url": download_url,
        }

    async def async_step_unsupported_device(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Pokazuje nieobsługiwany model i odnośnik do pobrania ramki."""
        if not self._unsupported_placeholders:
            return self.async_abort(reason="cannot_read_device")
        return self.async_show_menu(
            step_id="unsupported_device",
            menu_options=["retry_connection"],
            description_placeholders=self._unsupported_placeholders,
        )

    async def async_step_retry_connection(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Wraca do formularza adresu IP i portu TCP."""
        self._clear_frame_download()
        if self.source == config_entries.SOURCE_RECONFIGURE:
            return await self.async_step_reconfigure()
        return await self.async_step_user()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> config_entries.OptionsFlow:
        """Zwraca formularz opcji integracji."""
        return SkzpOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Pobiera połączenie, wykrywa model i przechodzi do wyboru encji."""
        errors: dict[str, str] = {}

        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            try:
                port = _parse_port(user_input[CONF_PORT])
            except ValueError:
                errors[CONF_PORT] = "invalid_port"
            else:
                try:
                    model, device_data = await _async_get_device_info(host, port)
                except CannotConnectError:
                    errors["base"] = "cannot_connect"
                except CannotReadDeviceError:
                    errors["base"] = "cannot_read_device"
                except UnsupportedDeviceError as err:
                    self._prepare_unsupported_device(err)
                    return await self.async_step_unsupported_device()
                else:
                    # Identyfikator ustawiamy po prawidłowym wykryciu sterownika.
                    await self.async_set_unique_id(f"{host}:{port}")
                    self._abort_if_unique_id_configured()
                    self._device_model = model
                    self._connection_data = {
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_DEVICE_MODEL: model,
                    }
                    self._choices = build_entity_choices(model, device_data)
                    self._recommended_disabled = recommended_disabled_entities(
                        self._choices, device_data
                    )
                    return await self.async_step_selection_method()

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                _build_connection_schema(),
                user_input or {},
            ),
            errors=errors,
        )

    async def async_step_selection_method(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Pozwala wybrać konfigurację szybką lub zaawansowaną."""
        if self._connection_data is None or self._device_model is None:
            return self.async_abort(reason="cannot_read_device")
        return self.async_show_menu(
            step_id="selection_method",
            menu_options=["quick", "advanced"],
            description_placeholders={"model": self._device_model},
        )

    async def async_step_quick(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Stosuje wykryte ustawienia zalecane i domyślną komunikację."""
        self._disabled_entities = set(self._recommended_disabled)
        self._advanced_options = _default_advanced_options()
        return self._create_config_entry()

    async def async_step_advanced(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Rozpoczyna zaawansowany wybór encji na osobnych ekranach."""
        if self._connection_data is None or self._device_model is None:
            return self.async_abort(reason="cannot_read_device")

        self._disabled_entities = set(self._recommended_disabled)
        self._advanced_options = _default_advanced_options()
        return await self.async_step_entities()

    async def async_step_entities(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Wyświetla menu ekranów wyboru encji oraz ustawień komunikacji."""
        if self._connection_data is None or self._device_model is None:
            return self.async_abort(reason="cannot_read_device")
        placeholders = selection_count_placeholders(
            self._choices,
            self._disabled_entities,
        )
        placeholders["model"] = self._device_model
        return self.async_show_menu(
            step_id="entities",
            menu_options=[
                *available_menu_pages(self._choices),
                "select_all",
                "deselect_all",
                "restore_recommended",
                "settings",
                "finish",
            ],
            description_placeholders=placeholders,
        )

    async def async_step_restore_recommended(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Przywraca automatyczny wybór encji dla wykrytego sterownika."""
        self._disabled_entities = set(self._recommended_disabled)
        return await self.async_step_entities()

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Zapisuje konfigurację bez dodatkowego ekranu potwierdzenia."""
        return self._create_config_entry()

    def _create_config_entry(self) -> dict[str, Any]:
        """Zapisuje konfigurację bez dodatkowego ekranu potwierdzenia."""
        if self._connection_data is None:
            return self.async_abort(reason="cannot_read_device")
        host = self._connection_data[CONF_HOST]
        model = self._device_model or DEFAULT_DEVICE_MODEL
        return self.async_create_entry(
            title=f"{model} ({host})",
            data=self._connection_data,
            options={
                **self._advanced_options,
                CONF_DISABLED_ENTITIES: sorted(self._disabled_entities),
            },
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Zmienia adres IP i port istniejącego wpisu integracji."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            host = str(user_input[CONF_HOST]).strip()
            try:
                port = _parse_port(user_input[CONF_PORT])
            except ValueError:
                errors[CONF_PORT] = "invalid_port"
            else:
                current_host = str(entry.data[CONF_HOST]).strip()
                current_port = int(entry.data[CONF_PORT])
                if host == current_host and port == current_port:
                    model = str(
                        entry.data.get(CONF_DEVICE_MODEL, DEFAULT_DEVICE_MODEL)
                    )
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=f"{host}:{port}",
                        title=f"{model} ({host})",
                        data_updates={},
                        reload_even_if_entry_is_unchanged=False,
                    )

                try:
                    model = await _async_get_device_model(host, port)
                except CannotConnectError:
                    errors["base"] = "cannot_connect"
                except CannotReadDeviceError:
                    errors["base"] = "cannot_read_device"
                except UnsupportedDeviceError as err:
                    self._prepare_unsupported_device(err)
                    return await self.async_step_unsupported_device()
                else:
                    updated_data = {
                        CONF_HOST: host,
                        CONF_PORT: port,
                        CONF_DEVICE_MODEL: model,
                    }
                    self._async_abort_entries_match(updated_data)
                    return self.async_update_reload_and_abort(
                        entry,
                        unique_id=f"{host}:{port}",
                        title=f"{model} ({host})",
                        data_updates=updated_data,
                        reload_even_if_entry_is_unchanged=False,
                    )

        suggested_values = dict(user_input or entry.data)
        if CONF_PORT in suggested_values:
            suggested_values[CONF_PORT] = str(suggested_values[CONF_PORT])
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                _build_connection_schema(),
                suggested_values,
            ),
            errors=errors,
        )


class SkzpOptionsFlow(_EntitySelectionFlowMixin, config_entries.OptionsFlowWithReload):
    """Obsługuje zmianę encji i ustawień komunikacji integracji."""

    def __init__(self) -> None:
        super().__init__()
        self._model: str | None = None
        self._choices: list[EntityChoice] | None = None
        self._disabled_entities: set[str] = set()
        self._advanced_options = _default_advanced_options()

    async def _async_prepare_selection(self) -> None:
        """Ładuje zapisane opcje przed edycją wyboru."""
        await self._async_ensure_state()

    async def _async_selection_menu(self) -> dict[str, Any]:
        return await self.async_step_init()

    async def _async_get_device_context(
        self,
    ) -> tuple[str, Mapping[str, Any] | None]:
        """Pobiera model i dane używane do filtrowania formularza."""
        client = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if client is not None and client.data:
            model = str(client.model)
            if model in SUPPORTED_DEVICE_MODELS:
                return model, dict(client.data)
            return model, None

        try:
            return await _async_get_device_info(
                self.config_entry.data[CONF_HOST],
                self.config_entry.data[CONF_PORT],
            )
        except (
            CannotConnectError,
            CannotReadDeviceError,
            UnsupportedDeviceError,
        ):
            model = self.config_entry.data.get(
                CONF_DEVICE_MODEL,
                DEFAULT_DEVICE_MODEL,
            )
            return str(model), None

    async def _async_ensure_state(self) -> None:
        """Ładuje bieżące opcje przy pierwszym otwarciu formularza."""
        if self._choices is not None:
            return
        self._model, device_data = await self._async_get_device_context()
        retained_buffer_sensors = existing_buffer_sensor_keys(
            self.hass,
            self.config_entry.entry_id,
        )
        self._choices = build_entity_choices(
            self._model,
            device_data,
            retained_buffer_sensors,
        )
        self._disabled_entities = set(
            self.config_entry.options.get(CONF_DISABLED_ENTITIES, [])
        )
        self._advanced_options = _normalize_advanced_options(
            self.config_entry.options
        )

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Wyświetla menu ekranów opcji integracji."""
        await self._async_ensure_state()
        placeholders = selection_count_placeholders(
            self._choices,
            self._disabled_entities,
        )
        placeholders["model"] = self._model
        menu_options = [*available_menu_pages(self._choices)]
        if self._choices:
            menu_options.extend(
                ["select_all", "deselect_all", "restore_recommended"]
            )
        menu_options.extend(["settings", "finish"])
        return self.async_show_menu(
            step_id="init",
            menu_options=menu_options,
            description_placeholders=placeholders,
        )

    async def async_step_restore_recommended(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Przywraca automatyczny wybór na podstawie aktualnych danych."""
        await self._async_ensure_state()
        _model, device_data = await self._async_get_device_context()
        if device_data is None:
            # Nieudany odczyt nie zmienia bieżącego wyboru.
            return await self.async_step_init()
        self._disabled_entities = recommended_disabled_entities(
            self._choices,
            device_data,
        )
        return await self.async_step_init()

    async def async_step_finish(
        self, user_input: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Zapisuje wszystkie opcje bez dodatkowego potwierdzenia."""
        await self._async_ensure_state()
        _sync_entity_registry_selection(
            self.hass,
            self.config_entry,
            self._choices,
            self._disabled_entities,
        )
        return self.async_create_entry(
            data={
                **self._advanced_options,
                CONF_DISABLED_ENTITIES: sorted(self._disabled_entities),
            }
        )
