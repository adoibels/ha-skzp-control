"""Tymczasowe pobieranie ramki nieobsługiwanego sterownika."""

import json
import re
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any
from urllib.parse import quote

from aiohttp import web
from homeassistant.components.diagnostics import async_redact_data
from homeassistant.components.http import HomeAssistantView
from homeassistant.components.http.auth import async_sign_path
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN
from .diagnostics import TO_REDACT

DOWNLOAD_LIFETIME = timedelta(minutes=10)
_DATA_DOWNLOAD_VIEW = f"{DOMAIN}_frame_download"
_DOWNLOAD_PATH = f"/api/{DOMAIN}/frame"


def _profile_filename(name: str) -> str:
    """Zachowuje nazwę profilu, zastępując znaki niedozwolone w pliku."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "_", name).strip().rstrip(". ")
    if not name:
        name = "skzp_profile"
    if name.split(".", 1)[0].upper() in {
        "CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
        *(f"LPT{i}" for i in range(1, 10)),
    }:
        name = "_" + name
    return name if name.lower().endswith(".json") else name + ".json"


def _download_disposition(filename: str) -> str:
    """Przekazuje nazwę UTF-8 i zapasową nazwę ASCII przeglądarkom."""
    fallback = filename.encode("ascii", errors="replace").decode("ascii").replace("?", "_")
    return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename, safe="")}'


class FrameDownloadView(HomeAssistantView):
    """Udostępnia oczyszczone ramki przez uwierzytelnione odnośniki HA."""

    url = f"{_DOWNLOAD_PATH}/{{download_id}}"
    name = f"api:{DOMAIN}:frame"
    requires_auth = True

    def __init__(self) -> None:
        self.frames: dict[str, tuple[float, bytes, str]] = {}

    async def get(
        self, request: web.Request, download_id: str
    ) -> web.Response:
        """Zwraca plik JSON, dopóki ramka jest dostępna."""
        frame = self.frames.get(download_id)
        if frame is None:
            raise web.HTTPNotFound
        expires_at, payload, filename = frame
        if time.monotonic() >= expires_at:
            self.frames.pop(download_id, None)
            raise web.HTTPNotFound
        return web.Response(
            body=payload,
            content_type="application/json",
            headers={
                "Content-Disposition": _download_disposition(filename),
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


@callback
def async_create_frame_download(
    hass: HomeAssistant, frame: Mapping[str, Any], *, profile: bool = False
) -> tuple[str, Callable[[], None]]:
    """Zwraca podpisany odnośnik i funkcję usuwającą ramkę z pamięci."""
    view = hass.data.get(_DATA_DOWNLOAD_VIEW)
    if view is None:
        view = FrameDownloadView()
        hass.http.register_view(view)
        hass.data[_DATA_DOWNLOAD_VIEW] = view

    download_id = secrets.token_urlsafe(32)
    download_url = async_sign_path(
        hass, f"{_DOWNLOAD_PATH}/{download_id}", DOWNLOAD_LIFETIME
    )
    payload = (
        json.dumps(
            async_redact_data(dict(frame), TO_REDACT),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    ).encode("utf-8")
    lifetime = DOWNLOAD_LIFETIME.total_seconds()
    filename = _profile_filename(str(frame.get("_meta", {}).get("name", ""))) if profile else "skzp_frame.json"
    view.frames[download_id] = (time.monotonic() + lifetime, payload, filename)
    expiry_timer = hass.loop.call_later(
        lifetime, view.frames.pop, download_id, None
    )

    @callback
    def remove_download() -> None:
        """Unieważnia odnośnik przy zamknięciu formularza lub powrocie."""
        expiry_timer.cancel()
        view.frames.pop(download_id, None)

    return download_url, remove_download
