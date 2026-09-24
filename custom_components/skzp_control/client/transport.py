"""Obsługa TCP z limitami czasu i blokadą wysyłania, niezależna od protokołu SKZP."""
import asyncio
from .exceptions import NotConnectedError

class TcpTransport:
    def __init__(self, host, port, *, connect_timeout=10.0, write_timeout=10.0, close_timeout=3.0):
        self.host, self.port = host, port
        self.connect_timeout = connect_timeout
        self.write_timeout = write_timeout
        self.close_timeout = close_timeout
        self._reader = self._writer = None
        self._send_lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._generation = 0

    @property
    def connected(self):
        return self._writer is not None and not self._writer.is_closing()

    async def connect(self):
        async with self._lifecycle_lock:
            if self.connected:
                return
            generation = self._generation
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.connect_timeout)
            if generation != self._generation:
                writer.close()
                writer.transport.abort()
                raise NotConnectedError("Connection interrupted")
            self._reader, self._writer = reader, writer

    def abort(self):
        self._generation += 1
        writer = self._writer
        self._reader = self._writer = None
        if writer is not None:
            writer.transport.abort()

    async def disconnect(self):
        self._generation += 1
        writer = self._writer
        self._reader = self._writer = None
        if writer is None:
            return
        try:
            writer.close()
            await asyncio.wait_for(writer.wait_closed(), self.close_timeout)
        except asyncio.CancelledError:
            writer.transport.abort()
            raise
        except Exception:
            writer.transport.abort()

    async def read(self, size=4096):
        reader = self._reader
        if reader is None:
            raise NotConnectedError("Not connected")
        chunk = await reader.read(size)
        if self._reader is not reader:
            raise NotConnectedError("Connection changed during read")
        return chunk

    async def send(self, payload: bytes):
        generation = self._generation
        async with self._send_lock:
            if not self.connected or generation != self._generation:
                raise NotConnectedError("Not connected")
            writer = self._writer
            try:
                writer.write(payload)
                await asyncio.wait_for(writer.drain(), self.write_timeout)
            except (Exception, asyncio.CancelledError):
                if self._writer is writer:
                    self.abort()
                raise
