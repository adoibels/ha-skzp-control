"""Testy nazw plików i odpowiedzi pobierania profilu."""

import ast
import asyncio
from email.message import Message
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import support


def load_download():
    tree = ast.parse((support.ROOT / "frame_download.py").read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and (
            node.level or (node.module or "").startswith(("homeassistant", "aiohttp"))))]
    env = dict(
        __name__=__name__, DOMAIN="skzp_control", TO_REDACT=[],
        callback=lambda fn: fn, HomeAssistant=object, HomeAssistantView=object,
        async_redact_data=lambda data, _: data,
        async_sign_path=lambda hass, path, lifetime: path + "?authSig=test",
        web=SimpleNamespace(Response=lambda **kwargs: kwargs, Request=object,
                            HTTPNotFound=LookupError),
    )
    exec(compile(tree, "frame_download.py", "exec"), env)
    return env


class FrameDownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_profile_name_roundtrip_and_original_frame_name(self):
        env = load_download()
        hass = SimpleNamespace(data={}, loop=asyncio.get_running_loop(),
                               http=SimpleNamespace(register_view=Mock()))
        for profile, name, expected in (
            (True, "Zima — Żółć", "Zima — Żółć.json"),
            (True, 'A/B\\C:D*"E<>|?', "A_B_C_D__E____.json"),
            (True, "CON", "_CON.json"),
            (True, "Nastawy.json", "Nastawy.json"),
            (True, " ", "skzp_profile.json"),
            (True, "A" * 80, "A" * 80 + ".json"),
            (True, "Zima\r\nHeader", "Zima__Header.json"),
            (False, "Zima", "skzp_frame.json"),
        ):
            with self.subTest(name=name, profile=profile):
                url, remove = env["async_create_frame_download"](
                    hass, {"_meta": {"name": name}, "params": {"K006": "1"}}, profile=profile)
                view = hass.data["skzp_control_frame_download"]
                download_id = url.split("?")[0].rsplit("/", 1)[1]
                response = await view.get(None, download_id)
                header = response["headers"]["Content-Disposition"]
                self.assertNotIn("\r", header)
                self.assertNotIn("\n", header)
                self.assertIn("filename*=UTF-8''", header)
                message = Message()
                message["Content-Disposition"] = header
                # Parser poczty preferuje nazwę zapasową; sprawdzamy osobno nazwę UTF-8.
                from urllib.parse import unquote
                self.assertEqual(unquote(header.split("filename*=UTF-8''", 1)[1]), expected)
                self.assertEqual(message.get_content_disposition(), "attachment")
                self.assertEqual(response["headers"]["Cache-Control"], "no-store")
                remove()
                with self.assertRaises(LookupError):
                    await view.get(None, download_id)
