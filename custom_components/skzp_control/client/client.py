"""Klient łączący obsługę TCP, ramek i protokołu SKZP."""
import asyncio
import logging
from ..frame_parser import FrameParser
from ..parameter_resolver import resolve_parameter_write_key
from .transport import TcpTransport
from .command_manager import CommandManager
from .protocol import SkzpProtocol
from .exceptions import NotConnectedError

_LOGGER = logging.getLogger(__name__)

class SkzpClient:
    def __init__(self, host, port, *, max_frame_bytes=65536, transport=None):
        self.transport = transport if transport is not None else TcpTransport(host, port)
        self.protocol = SkzpProtocol()
        self.command_manager = CommandManager()
        self.data = {}
        self._max_frame_bytes = max_frame_bytes
        self._parser = FrameParser(max_frame_bytes)
        # Blokada obejmuje sprawdzenie połączenia, dane autoryzacji i zapis komendy.
        self._command_lock = asyncio.Lock()

    @property
    def connected(self):
        return self.transport.connected

    @property
    def available(self):
        return self.connected

    async def connect(self):
        await self.transport.connect()
        self._parser = FrameParser(self._max_frame_bytes)
        for key in ("DevId", "DevPin", "Token"):
            self.data.pop(key, None)

    async def disconnect(self):
        if self._parser.buffered_bytes:
            _LOGGER.debug(
                "Incomplete JSON frame discarded on disconnect (%d bytes).",
                self._parser.buffered_bytes,
            )
        await self.transport.disconnect()
        self._parser = FrameParser(self._max_frame_bytes)

    async def receive(self):
        chunk = await self.transport.read()
        if not chunk:
            raise ConnectionError("TCP connection closed")
        return self._decode_chunk(chunk)

    def _decode_chunk(self, chunk):
        for frame in self._parser.feed(chunk):
            message = self.protocol.decode_message(frame)
            self.data.update(message.fields)
            yield message

    async def send_command(self, parameters):
        parameters = dict(parameters)
        async with self._command_lock:
            if not self.available:
                raise NotConnectedError("Not connected")
            await self.transport.send(self.protocol.encode_command(parameters, self.data))

    async def set_parameter(self, parameter, value):
        """Wysyła parametr z uwzględnieniem aliasów, w jednostkach sterownika."""
        await self.send_command({resolve_parameter_write_key(self.data, parameter): value})

    async def execute_command(self, command):
        await self.send_command({"CommandToDo": command})
