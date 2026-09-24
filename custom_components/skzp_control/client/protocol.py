"""Kodowanie komend i odczyt ramek SKZP niezależne od TCP i Home Assistant."""
import json
from .exceptions import MissingCredentialsError
from .models import Message

class SkzpProtocol:
    def encode_command(self, parameters: dict, state: dict) -> bytes:
        credentials = {key: state.get(key) for key in ("DevId", "DevPin", "Token")}
        missing = [key for key, value in credentials.items() if value is None]
        if missing:
            raise MissingCredentialsError(missing)
        credentials.update(parameters)
        return (json.dumps(credentials) + "\n").encode("utf-8")

    def decode_message(self, frame: dict) -> Message:
        return Message(dict(frame), frame.get("FrameType") == "SkzpData")
