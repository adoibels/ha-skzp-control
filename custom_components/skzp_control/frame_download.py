"""Tymczasowe pobieranie ramki nieobsługiwanego sterownika."""

import json
import secrets
import time
from collections.abc import Callable, Mapping
from datetime import timedelta
from typing import Any

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


class FrameDownloadView(HomeAssistantView):
    """Udostępnia oczyszczone ramki przez uwierzytelnione odnośniki HA."""

    url = f"{_DOWNLOAD_PATH}/{{download_id}}"
    name = f"api:{DOMAIN}:frame"
    requires_auth = True

    def __init__(self) -> None:
        self.frames: dict[str, tuple[float, bytes]] = {}

    async def get(
        self, request: web.Request, download_id: str
    ) -> web.Response:
        """Zwraca plik JSON, dopóki ramka jest dostępna."""
        frame = self.frames.get(download_id)
        if frame is None:
            raise web.HTTPNotFound
        expires_at, payload = frame
        if time.monotonic() >= expires_at:
            self.frames.pop(download_id, None)
            raise web.HTTPNotFound
        return web.Response(
            body=payload,
            content_type="application/json",
            headers={
                "Content-Disposition": 'attachment; filename="skzp_frame.json"',
                "Cache-Control": "no-store",
                "X-Content-Type-Options": "nosniff",
            },
        )


@callback
def async_create_frame_download(
    hass: HomeAssistant, frame: Mapping[str, Any]
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
    view.frames[download_id] = (time.monotonic() + lifetime, payload)
    expiry_timer = hass.loop.call_later(
        lifetime, view.frames.pop, download_id, None
    )

    @callback
    def remove_download() -> None:
        """Unieważnia odnośnik przy zamknięciu formularza lub powrocie."""
        expiry_timer.cancel()
        view.frames.pop(download_id, None)

    return download_url, remove_download
