"""Obsługa integracji i komunikacji TCP."""

import asyncio
import logging
import time

from homeassistant.components import persistent_notification
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    HomeAssistantError,
)

from .const import (
    COMMAND_WRITE_TIMEOUT_SECONDS,
    CONNECTION_CLOSE_TIMEOUT_SECONDS,
    CONF_COMMAND_CONFIRM_TIMEOUT,
    CONF_COMMAND_RETRY_COUNT,
    CONF_COMMAND_RETRY_DELAY,
    CONF_NO_DATA_TIMEOUT,
    CONF_NOTIFY_CONNECTION_LOST,
    CONF_NOTIFY_CONNECTION_RESTORED,
    CONF_RECONNECT_DELAY,
    DEFAULT_COMMAND_CONFIRM_TIMEOUT,
    DEFAULT_COMMAND_RETRY_COUNT,
    DEFAULT_COMMAND_RETRY_DELAY,
    DEFAULT_NO_DATA_TIMEOUT,
    DEFAULT_NOTIFY_CONNECTION_LOST,
    DEFAULT_NOTIFY_CONNECTION_RESTORED,
    DEFAULT_RECONNECT_DELAY,
    DOMAIN,
    MAX_COMMAND_CONFIRM_TIMEOUT,
    MAX_COMMAND_RETRY_COUNT,
    MAX_COMMAND_RETRY_DELAY,
    MAX_NO_DATA_TIMEOUT,
    MAX_FRAME_BYTES,
    MAX_RECONNECT_DELAY,
    MIN_COMMAND_CONFIRM_TIMEOUT,
    MIN_COMMAND_RETRY_COUNT,
    MIN_COMMAND_RETRY_DELAY,
    MIN_NO_DATA_TIMEOUT,
    MIN_RECONNECT_DELAY,
    WATCHDOG_INTERVAL_SECONDS,
)
from .device import (
    detect_device_model,
)

from .localization import async_load_localizations, translate
from .value_decoder import get_active_alarms
from .client.client import SkzpClient
from .client.exceptions import MissingCredentialsError, NotConnectedError

_LOGGER = logging.getLogger(__name__)

def format_communication_error(error: Exception) -> str:
    """Zwraca opis błędu, także gdy wyjątek ma pusty komunikat."""
    if isinstance(error, TimeoutError):
        return "Operation timed out"
    return type(error).__name__


def _bounded_float(options, key, default, minimum, maximum) -> float:
    """Odczytuje liczbę z opcji i ogranicza ją do obsługiwanego zakresu."""
    try:
        value = float(options.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _bounded_int(options, key, default, minimum, maximum) -> int:
    """Odczytuje liczbę całkowitą z opcji i ogranicza jej zakres."""
    try:
        value = int(options.get(key, default))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


class SkzpCoordinator(SkzpClient):
    """Odbiera ramki JSON przez TCP i przekazuje aktualizacje encjom HA."""

    def __init__(
        self,
        hass: HomeAssistant,
        host: str,
        port: int,
        entry_id: str,
        options: dict,
    ) -> None:
        super().__init__(host, port, max_frame_bytes=MAX_FRAME_BYTES)
        self.transport.write_timeout = COMMAND_WRITE_TIMEOUT_SECONDS
        self.transport.close_timeout = CONNECTION_CLOSE_TIMEOUT_SECONDS
        self.hass = hass
        self.host = host
        self.port = port
        self.entry_id = entry_id
        self._logged_alarms: set[str] = set()
        self.update_event = f"{DOMAIN}_{entry_id}_data_update"
        self.command_retry_count = _bounded_int(
            options,
            CONF_COMMAND_RETRY_COUNT,
            DEFAULT_COMMAND_RETRY_COUNT,
            MIN_COMMAND_RETRY_COUNT,
            MAX_COMMAND_RETRY_COUNT,
        )
        self.command_retry_delay = _bounded_float(
            options,
            CONF_COMMAND_RETRY_DELAY,
            DEFAULT_COMMAND_RETRY_DELAY,
            MIN_COMMAND_RETRY_DELAY,
            MAX_COMMAND_RETRY_DELAY,
        )
        self.command_confirmation_timeout = _bounded_float(
            options,
            CONF_COMMAND_CONFIRM_TIMEOUT,
            DEFAULT_COMMAND_CONFIRM_TIMEOUT,
            MIN_COMMAND_CONFIRM_TIMEOUT,
            MAX_COMMAND_CONFIRM_TIMEOUT,
        )
        self.no_data_timeout = _bounded_float(
            options,
            CONF_NO_DATA_TIMEOUT,
            DEFAULT_NO_DATA_TIMEOUT,
            MIN_NO_DATA_TIMEOUT,
            MAX_NO_DATA_TIMEOUT,
        )
        self.reconnect_delay = _bounded_float(
            options,
            CONF_RECONNECT_DELAY,
            DEFAULT_RECONNECT_DELAY,
            MIN_RECONNECT_DELAY,
            MAX_RECONNECT_DELAY,
        )
        self.notify_connection_lost = bool(
            options.get(
                CONF_NOTIFY_CONNECTION_LOST,
                DEFAULT_NOTIFY_CONNECTION_LOST,
            )
        )
        self.notify_connection_restored = bool(
            options.get(
                CONF_NOTIFY_CONNECTION_RESTORED,
                DEFAULT_NOTIFY_CONNECTION_RESTORED,
            )
        )
        self._task: asyncio.Task | None = None
        self._watchdog_task: asyncio.Task | None = None
        self._connected = False
        self._connected_at: float | None = None
        self._data_ready_event = asyncio.Event()
        self._last_data_received: float | None = None
        self._no_data_notification_active = False
        self._recovery_notification_active = False
        self._notification_cleanup_pending = True
        self._outage_active = False
        self._connection_issue_logged = False

    def translate(
        self,
        key: str,
        *,
        language: str | None = None,
        **placeholders: object,
    ) -> str:
        """Tłumaczy tekst dla głównego języka HA lub wskazanego języka."""
        return translate(self.hass, key, language=language, **placeholders)

    @property
    def connected(self) -> bool:
        """Czy klient ma aktualnie aktywne połączenie TCP ze sterownikiem."""
        return self._connected and self.transport.connected

    @property
    def available(self) -> bool:
        """Czy połączenie działa i sterownik przesłał aktualne dane."""
        if (
            not self.connected
            or not self._data_ready_event.is_set()
            or self._last_data_received is None
        ):
            return False
        return (
            time.monotonic() - self._last_data_received
            < self.no_data_timeout
        )

    @property
    def command_pending_timeout(self) -> float:
        """Łączny czas ochrony wartości podczas wysyłania i ponowień."""
        return (
            self.command_confirmation_timeout * (self.command_retry_count + 1)
            + self.command_retry_delay * self.command_retry_count
        )

    def _notify_entities(self) -> None:
        """Powiadamia encje o zmianie danych lub dostępności sterownika."""
        self.hass.bus.async_fire(self.update_event)

    def _set_connected(self, connected: bool) -> None:
        """Aktualizuje stan TCP i natychmiast odświeża encje."""
        state_changed = self._connected != connected
        self._connected = connected

        if connected:
            if state_changed:
                self._connected_at = time.monotonic()
        else:
            self._connected_at = None
            self._data_ready_event.clear()

        if state_changed:
            self._notify_entities()

    def _log_connection_issue(self, message: str, *args) -> None:
        """Loguje pierwszą awarię jako warning, a kolejne próby jako debug."""
        level = logging.DEBUG if self._connection_issue_logged else logging.WARNING
        _LOGGER.log(
            level,
            "[SKZP Control] %s:%s — " + message,
            self.host, self.port,
            *args,
        )
        self._connection_issue_logged = True

    def log_command_retry(
        self, parameter: str, value: str, retry_number: int
    ) -> None:
        """Zapisuje wspólny komunikat ponowienia dla encji sterujących."""
        _LOGGER.warning(
            "[SKZP Control] %s:%s — Change of %s to %s not confirmed. Retry %d/%d in %g s.",
            self.host, self.port,
            parameter,
            value,
            retry_number,
            self.command_retry_count,
            self.command_retry_delay,
        )

    @property
    def model(self) -> str:
        """Zwraca rozpoznany model; SKZP oznacza model nierozpoznany."""
        return detect_device_model(self.data.get("DevType"))

    async def wait_for_data(self, timeout: float = 5.0) -> bool:
        """Oczekuje na pełną ramkę danych i informację o modelu."""
        try:
            await asyncio.wait_for(self._data_ready_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return True

    async def start(self) -> None:
        await async_load_localizations(self.hass)
        self._task = asyncio.create_task(self._read_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def stop(self, *, dismiss_notification: bool = True) -> None:
        self._set_connected(False)
        tasks = [task for task in (self._task, self._watchdog_task) if task]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as err:
                _LOGGER.debug(
                    "[SKZP Control] %s:%s — Error stopping task: %s",
                    self.host, self.port,
                    format_communication_error(err),
                )

        self._task = None
        self._watchdog_task = None

        await self._close_connection()

        if dismiss_notification:
            self._dismiss_no_data_notification(force=True)
            self._dismiss_recovery_notification()

    @property
    def _notification_id(self) -> str:
        """Stały identyfikator zapobiegający duplikowaniu powiadomień."""
        return f"{DOMAIN}_no_data_{self.entry_id}"

    @property
    def _recovery_notification_id(self) -> str:
        """Identyfikator powiadomienia o odzyskaniu komunikacji."""
        return f"{DOMAIN}_connection_restored_{self.entry_id}"

    def _mark_data_received(self) -> None:
        """Zeruje licznik braku danych i zgłasza odzyskanie komunikacji."""
        recovered = self._outage_active
        first_data = self._last_data_received is None
        self._last_data_received = time.monotonic()
        self._outage_active = False
        self._dismiss_no_data_notification()

        if recovered or self._connection_issue_logged:
            _LOGGER.info(
                "[SKZP Control] %s:%s — Communication restored. The controller is sending data "
                "again.",
                self.host, self.port,
            )
        elif first_data:
            _LOGGER.info(
                "[SKZP Control] %s:%s — Received the first controller data frame.",
                self.host, self.port,
            )
        self._connection_issue_logged = False

        if recovered and self.notify_connection_restored:
            persistent_notification.async_create(
                self.hass,
                self.translate(
                    "common.connection_restored_message",
                    host=self.host,
                    port=self.port,
                ),
                self.translate("common.connection_restored_title"),
                self._recovery_notification_id,
            )
            self._recovery_notification_active = True

    def _dismiss_no_data_notification(self, *, force: bool = False) -> None:
        """Usuwa ostrzeżenie po odzyskaniu komunikacji lub wyłączeniu integracji."""
        if not (
            force
            or self._no_data_notification_active
            or self._notification_cleanup_pending
        ):
            return
        persistent_notification.async_dismiss(self.hass, self._notification_id)
        self._no_data_notification_active = False
        self._notification_cleanup_pending = False

    def _dismiss_recovery_notification(self) -> None:
        """Usuwa poprzednią informację o odzyskaniu po rozpoczęciu nowej awarii."""
        if not self._recovery_notification_active:
            return
        persistent_notification.async_dismiss(
            self.hass, self._recovery_notification_id
        )
        self._recovery_notification_active = False

    def create_no_data_notification(self) -> None:
        """Oznacza awarię i opcjonalnie tworzy jedno powiadomienie."""
        if self._outage_active:
            return

        self._outage_active = True
        self._dismiss_recovery_notification()
        self._notify_entities()

        if not self.notify_connection_lost:
            return

        persistent_notification.async_create(
            self.hass,
            self.translate(
                "common.connection_lost_message",
                seconds=f"{self.no_data_timeout:g}",
                host=self.host,
                port=self.port,
            ),
            self.translate("common.connection_lost_title"),
            self._notification_id,
        )
        self._no_data_notification_active = True
        self._notification_cleanup_pending = False

    async def _watchdog_loop(self) -> None:
        """Oznacza awarię, gdy przez ustawiony czas nie odebrano danych."""
        while True:
            await asyncio.sleep(WATCHDOG_INTERVAL_SECONDS)

            activity_times = [
                value
                for value in (self._last_data_received, self._connected_at)
                if value is not None
            ]
            if not activity_times:
                continue
            activity_time = max(activity_times)

            silence_time = time.monotonic() - activity_time
            if silence_time < self.no_data_timeout:
                continue

            self.create_no_data_notification()
            self._log_connection_issue(
                "No data for at least %s s. Restarting the connection.",
                f"{self.no_data_timeout:g}",
            )
            self.transport.abort()

    async def _close_connection(self) -> None:
        """Zamyka strumień w ograniczonym czasie, także podczas anulowania."""
        await self.disconnect()

    async def _reconnect(self, reason: str, *args) -> None:
        self._log_connection_issue(reason, *args)
        self._set_connected(False)
        await self._close_connection()
        await asyncio.sleep(self.reconnect_delay)
        await self._connect()

    async def _connect(self) -> None:
        _LOGGER.debug(
            "[SKZP Control] %s:%s — Connecting via TCP.",
            self.host, self.port,
        )
        while True:
            try:
                await self.connect()
                self._data_ready_event.clear()
                self._set_connected(True)
                _LOGGER.debug(
                    "[SKZP Control] %s:%s — TCP connection established. Waiting for controller "
                    "data.",
                    self.host, self.port,
                )
                return
            except asyncio.CancelledError:
                raise
            except Exception as err:
                self._log_connection_issue(
                    "TCP connection error: %s. Retrying in %s s.",
                    format_communication_error(err),
                    f"{self.reconnect_delay:g}",
                )
                await asyncio.sleep(self.reconnect_delay)

    def _log_alarm_changes(self, fields: dict) -> None:
        """Loguje pojawienie i ustąpienie alarmów bez powtarzania wpisów."""
        raw = fields.get("Alarms")
        if raw is None or not str(raw).strip():
            return
        alarms = set(get_active_alarms(raw))
        if alarms == self._logged_alarms:
            return
        for key in sorted(alarms - self._logged_alarms):
            _LOGGER.warning(
                "[SKZP Control] %s:%s — Alarm active: %s.",
                self.host, self.port,
                self.translate(f"common.alarm_{key}", language="en"),
            )
        for key in sorted(self._logged_alarms - alarms):
            _LOGGER.info(
                "[SKZP Control] %s:%s — Alarm cleared: %s.",
                self.host, self.port,
                self.translate(f"common.alarm_{key}", language="en"),
            )
        self._logged_alarms = alarms

    async def _read_loop(self) -> None:
        await self._connect()
        while True:
            try:
                messages = await asyncio.wait_for(
                    self.receive(), timeout=self.no_data_timeout)
                for message in messages:
                    parsed = message.fields
                    if message.is_data:
                        self._log_alarm_changes(parsed)
                        self._mark_data_received()
                        if "DevType" in self.data:
                            self._data_ready_event.set()
                    self._notify_entities()

            except asyncio.TimeoutError:
                self.create_no_data_notification()
                await self._reconnect(
                    "No data for at least %s s. Reconnecting in %s s.",
                    f"{self.no_data_timeout:g}",
                    f"{self.reconnect_delay:g}",
                )
            except asyncio.CancelledError:
                break
            except Exception as err:
                await self._reconnect(
                    "Error receiving data: %s. Reconnecting in %s s.",
                    format_communication_error(err),
                    f"{self.reconnect_delay:g}",
                )

    async def send_command(self, parameters: dict) -> None:
        """Wysyła komendę i tłumaczy błędy komunikacji na błędy Home Assistant."""
        try:
            await super().send_command(parameters)
        except NotConnectedError as err:
            raise HomeAssistantError(translation_domain=DOMAIN,
                translation_key="not_connected") from err
        except MissingCredentialsError as err:
            raise HomeAssistantError(translation_domain=DOMAIN,
                translation_key="missing_command_credentials",
                translation_placeholders={"fields": ", ".join(err.fields)}) from err
        except asyncio.CancelledError:
            if not self.transport.connected:
                self._set_connected(False)
            raise
        except Exception as err:
            if not self.transport.connected:
                self._set_connected(False)
            self._log_connection_issue("Error sending command: %s", format_communication_error(err))
            raise HomeAssistantError(translation_domain=DOMAIN,
                translation_key="command_send_timeout" if isinstance(err, TimeoutError) else "command_send_failed",
                translation_placeholders={} if isinstance(err, TimeoutError) else {"error": format_communication_error(err)}) from err
