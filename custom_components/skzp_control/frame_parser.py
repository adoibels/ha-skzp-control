"""Odczyt ograniczonych rozmiarem ramek JSON ze strumienia TCP."""

import json
import logging
from collections.abc import Iterator
from typing import Any

_LOGGER = logging.getLogger(__name__)


class FrameParser:
    """Składa obiekty JSON niezależnie od granic pakietów TCP."""

    def __init__(self, max_frame_bytes: int) -> None:
        if max_frame_bytes < 2:
            raise ValueError("Frame size limit must be at least 2 bytes")
        self._limit = max_frame_bytes
        self._buffer = bytearray()
        self._depth = 0
        self._in_string = False
        self._escaped = False

    @property
    def buffered_bytes(self) -> int:
        """Zwraca rozmiar oczekującej, niepełnej ramki."""
        return len(self._buffer)

    def feed(self, chunk: bytes) -> Iterator[dict[str, Any]]:
        """Zwraca pełne ramki; błędna lub zbyt duża ramka przerywa odbiór."""
        for byte in chunk:
            if not self._buffer:
                # Pomijamy separatory i dane przed początkiem obiektu.
                if byte != 123:
                    continue
                self._depth = 1
                self._buffer.append(byte)
                continue

            if len(self._buffer) >= self._limit:
                _LOGGER.debug("Frame rejected: JSON size limit exceeded (%d bytes).", self._limit)
                raise ValueError("Received JSON frame exceeds the size limit")
            self._buffer.append(byte)
            if self._in_string:
                if self._escaped:
                    self._escaped = False
                elif byte == 92:
                    self._escaped = True
                elif byte == 34:
                    self._in_string = False
            elif byte == 34:
                self._in_string = True
            elif byte == 123:
                self._depth += 1
            elif byte == 125:
                self._depth -= 1
                if self._depth == 0:
                    try:
                        frame = json.loads(self._buffer.decode("utf-8"))
                    except (ValueError, RecursionError):
                        _LOGGER.debug("Frame rejected: invalid JSON or UTF-8 (%d bytes).", len(self._buffer))
                        # Nie umieszczamy zawartości ramki w komunikacie błędu.
                        raise ValueError("Received invalid JSON frame") from None
                    self._buffer.clear()
                    yield frame
