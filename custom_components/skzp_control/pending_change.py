"""Wspólna obsługa lokalnej zmiany oczekującej na potwierdzenie."""

import asyncio
import time
from typing import Any

from homeassistant.exceptions import HomeAssistantError

from .const import DOMAIN


class PendingChangeMixin:
    """Chroni lokalny stan encji przed starszymi odczytami sterownika."""

    def _init_pending_change(self) -> None:
        self._send_generation = 0
        self._pending_target: Any = None
        self._pending_until: float | None = None
        self._confirmation_event: asyncio.Event | None = None

    def _normalize_pending_value(self, value: Any) -> Any:
        """Pozwala encji ujednolicić wartości przed porównaniem."""
        return value

    def _begin_pending_change(
        self, target: Any, timeout: float
    ) -> tuple[int, asyncio.Event]:
        """Zastępuje poprzednią zmianę i budzi jej oczekujące zadanie."""
        client = getattr(self, "_client", None)
        if client is not None:
            if getattr(client, "profile_import_active", False):
                raise HomeAssistantError(translation_domain=DOMAIN, translation_key="profile_busy")
            if not hasattr(client, "pending_entities"):
                client.pending_entities = set()
            client.pending_entities.add(id(self))
        self._send_generation += 1
        if self._confirmation_event is not None:
            self._confirmation_event.set()
        self._confirmation_event = asyncio.Event()
        self._pending_target = target
        self._pending_until = time.monotonic() + timeout
        return self._send_generation, self._confirmation_event

    def _confirm_pending_change(self, value: Any) -> bool:
        """Sygnalizuje odczyt zgodny z oczekiwaną wartością."""
        matches = self._pending_target is not None and (
            self._normalize_pending_value(value)
            == self._normalize_pending_value(self._pending_target)
        )
        if matches and self._confirmation_event is not None:
            self._confirmation_event.set()
        return matches

    def _is_stale_update(self, value: Any) -> bool:
        """Pomija stare odczyty podczas aktywnej zmiany lub okresu ochronnego."""
        if self._pending_until is None or self._pending_target is None:
            return False
        if self._confirmation_event is not None or time.monotonic() < self._pending_until:
            return self._normalize_pending_value(value) != self._normalize_pending_value(self._pending_target)
        self._pending_target = None
        self._pending_until = None
        return False

    def _clear_pending_change(self) -> None:
        """Kończy oczekiwanie bez zmiany wartości prezentowanej przez encję."""
        client = getattr(self, "_client", None)
        if client is not None:
            getattr(client, "pending_entities", set()).discard(id(self))
        self._pending_target = None
        self._pending_until = None
        self._confirmation_event = None
