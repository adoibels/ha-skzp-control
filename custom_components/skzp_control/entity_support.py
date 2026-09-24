"""Centralne reguły dostępności encji dla konkretnego sterownika."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from .device import (
    BUFFER_SETTING_KEYS,
    supports_buffer_setting_reads,
    supports_buffer_setting_writes,
    supports_fuel_caloric_write,
)


@dataclass(frozen=True)
class EntitySupportContext:
    """Możliwości sterownika używane przy wyborze encji."""

    buffer_settings_as_sensors: bool
    fuel_caloric_as_sensor: bool

    @classmethod
    def from_device(
        cls,
        model: str,
        device_data: Mapping[str, Any],
    ) -> "EntitySupportContext":
        """Tworzy kontekst możliwości na podstawie modelu i danych urządzenia."""
        device_identity = device_data.get("DevType") or model
        buffer_settings_writable = supports_buffer_setting_writes(device_identity)
        return cls(
            buffer_settings_as_sensors=(
                supports_buffer_setting_reads(device_identity)
                and not buffer_settings_writable
            ),
            fuel_caloric_as_sensor=(
                not supports_fuel_caloric_write(device_identity)
            ),
        )


def should_create_sensor(
    key: str,
    context: EntitySupportContext,
    retained_buffer_sensors: frozenset[str] = frozenset(),
) -> bool:
    """Sprawdza, czy sensor powinien istnieć dla danego sterownika."""
    if (
        key in BUFFER_SETTING_KEYS
        and not context.buffer_settings_as_sensors
        and key not in retained_buffer_sensors
    ):
        return False
    return key != "BuFuelCaloric" or context.fuel_caloric_as_sensor
