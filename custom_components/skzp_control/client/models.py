"""Struktury danych odebranych ze sterownika."""
from dataclasses import dataclass, field
from typing import Any

@dataclass(frozen=True)
class Message:
    fields: dict[str, Any] = field(repr=False)
    is_data: bool
