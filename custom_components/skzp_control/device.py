"""Rozpoznawanie modelu i informacje o urządzeniu."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN

if TYPE_CHECKING:
    from .coordinator import SkzpCoordinator

SKZP05S_MODEL = "SKZP-05S"
SKZP05PW_MODEL = "SKZP-05PW"
SKZP04P_MODEL = "SKZP-04P"
SKZP02S_MODEL = "SKZP-02S"
SKZP02T_MODEL = "SKZP-02T"
SUPPORTED_DEVICE_MODELS = (
    SKZP02S_MODEL,
    SKZP02T_MODEL,
    SKZP04P_MODEL,
    SKZP05S_MODEL,
    SKZP05PW_MODEL,
)
DEFAULT_DEVICE_MODEL = "SKZP"

_FIRMWARE_RE = re.compile(
    r"[_\s]V(?P<version>\d+(?:\.\d+)*)[_\s](?P<date>\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
_FIRMWARE_VERSION_RE = re.compile(
    r"(?:^|[_\s])V(?P<version>\d+(?:\.\d+)+)(?=$|[_\s])",
    re.IGNORECASE,
)

BUFFER_SETTING_KEYS = frozenset({"D203", "D204"})


def detect_device_model(dev_type: Any) -> str:
    """Rozpoznaje obsługiwany model z DevType."""
    normalized = str(dev_type).strip().upper()
    return next(
        (
            model
            for model in SUPPORTED_DEVICE_MODELS
            if normalized == model
            or normalized.startswith((f"{model}_", f"{model} "))
        ),
        DEFAULT_DEVICE_MODEL,
    )


def detect_firmware_version(dev_type: Any) -> str | None:
    """Zwraca wersję i datę oprogramowania z DevType."""
    match = _FIRMWARE_RE.search(str(dev_type))
    if not match:
        return None
    return f"{match['version']} ({match['date']})"


def _firmware_version_parts(dev_type: Any) -> tuple[int, ...] | None:
    """Zwraca numeryczne części wersji oprogramowania."""
    match = _FIRMWARE_VERSION_RE.search(str(dev_type))
    if not match:
        return None
    return tuple(int(part) for part in match["version"].split("."))


def supports_fuel_caloric_write(dev_type: Any) -> bool:
    """Sprawdza, czy kaloryczność paliwa jest parametrem zapisywalnym."""
    model = detect_device_model(dev_type)
    return model in {SKZP04P_MODEL, SKZP05PW_MODEL}


def supports_buffer_setting_reads(dev_type: Any) -> bool:
    """Sprawdza, czy wersja udostępnia ustawienia bufora do odczytu."""
    model = detect_device_model(dev_type)
    version = _firmware_version_parts(dev_type)
    if version is None:
        return False
    return (model == SKZP05S_MODEL and version[0] == 5) or (
        model == SKZP05PW_MODEL and version[0] == 6
    )


def supports_buffer_setting_writes(dev_type: Any) -> bool:
    """Sprawdza, czy wersja pozwala zmieniać ustawienia bufora."""
    if not supports_buffer_setting_reads(dev_type):
        return False

    version = _firmware_version_parts(dev_type)
    return (
        version is not None
        and version[0] == 5
        and len(version) >= 2
        and 70 <= version[1] <= 99
    )


def build_device_info(client: SkzpCoordinator) -> DeviceInfo:
    """Zwraca informacje o urządzeniu dla encji."""
    dev_type = client.data.get("DevType", DEFAULT_DEVICE_MODEL)
    model = detect_device_model(dev_type)
    displayed_model = client.translate("common.controller_name", model=model)

    info: DeviceInfo = {
        "identifiers": {(DOMAIN, client.entry_id)},
        "name": displayed_model,
        "manufacturer": "Timel",
        "model": displayed_model,
    }

    firmware_version = detect_firmware_version(dev_type)
    if firmware_version:
        info["sw_version"] = firmware_version

    return info
