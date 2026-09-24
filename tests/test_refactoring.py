"""Testy parsera formularza, wyboru dekodera i oczekujących zmian."""
import ast
import asyncio
import json
import logging
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, patch

import support
from support import ROOT
from skzp_control.frame_parser import FrameParser
from skzp_control.pending_change import PendingChangeMixin
from skzp_control.device import detect_device_model, SUPPORTED_DEVICE_MODELS


def load_function(filename, name, env):
    tree = ast.parse((ROOT / filename).read_text(encoding='utf-8-sig'))
    node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[])), filename, 'exec'), env)
    return env[name]


class PendingTests(TestCase):
    def test_types_and_confirmation(self):
        for target, old in ((False, True), (True, False), (0, 1), ('stop', 'auto')):
            entity = PendingChangeMixin()
            entity._init_pending_change()
            generation, event = entity._begin_pending_change(target, 1)
            self.assertEqual(generation, 1)
            self.assertFalse(entity._confirm_pending_change(old))
            self.assertTrue(entity._is_stale_update(old))
            self.assertTrue(entity._confirm_pending_change(target))
            self.assertTrue(event.is_set())
            self.assertFalse(entity._is_stale_update(target))
            self.assertIsNone(entity._pending_target)
            entity._clear_pending_change()
            self.assertIsNone(entity._confirmation_event)

    def test_supersession(self):
        entity = PendingChangeMixin()
        entity._init_pending_change()
        first, event = entity._begin_pending_change(1, 1)
        second, current = entity._begin_pending_change(2, 1)
        self.assertGreater(second, first)
        self.assertTrue(event.is_set())
        self.assertFalse(current.is_set())
        self.assertTrue(entity._is_stale_update(1))

    def test_expiry_and_active_confirmation(self):
        entity = PendingChangeMixin()
        entity._init_pending_change()
        entity._begin_pending_change(2, -1)
        self.assertTrue(entity._is_stale_update(1))
        entity._confirmation_event = None
        self.assertFalse(entity._is_stale_update(1))
        self.assertIsNone(entity._pending_until)

    def test_number_precision(self):
        method = load_function('number.py', '_normalize_pending_value', {})
        class Number(PendingChangeMixin):
            _normalize_pending_value = method
            def _round(self, value):
                return round(value, 1)
        entity = Number()
        entity._init_pending_change()
        entity._begin_pending_change(1.2, 1)
        self.assertTrue(entity._confirm_pending_change(1.20000001))
        self.assertFalse(entity._is_stale_update(1.20000001))


class SetupParserTests(IsolatedAsyncioTestCase):
    async def run_setup(self, chunks, limit=65536):
        class CannotRead(Exception): pass
        env = dict(asyncio=asyncio, FrameParser=FrameParser, MAX_FRAME_BYTES=limit,
            CONNECTION_TIMEOUT_SECONDS=1, DEVICE_DATA_TIMEOUT_SECONDS=1,
            _LOGGER=logging.getLogger('test'), format_communication_error=str,
            CannotConnectError=ConnectionError, CannotReadDeviceError=CannotRead,
            UnsupportedDeviceError=ValueError, detect_device_model=detect_device_model,
            SUPPORTED_DEVICE_MODELS=SUPPORTED_DEVICE_MODELS)
        fn = load_function('config_flow.py', '_async_get_device_info', env)
        reader = SimpleNamespace(read=AsyncMock(side_effect=chunks))
        writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
        with patch('asyncio.open_connection', AsyncMock(return_value=(reader, writer))):
            try:
                return await fn('test', 80)
            except CannotRead:
                return 'cannot_read'
            finally:
                writer.wait_closed.assert_awaited_once()

    async def test_fragments_unicode_nested_and_multiple(self):
        raw = json.dumps({'FrameType':'SkzpData','DevType':'SKZP-05S', 'x':{'text':'żółć } {'}}, ensure_ascii=False).encode()
        model, data = await self.run_setup([b'{"Token":"test"}'] + [bytes([x]) for x in raw])
        self.assertEqual(model, 'SKZP-05S')
        self.assertEqual(data['x']['text'], 'żółć } {')
        self.assertEqual(data['Token'], 'test')

    async def test_invalid_and_large(self):
        self.assertEqual(await self.run_setup([b'{bad}']), 'cannot_read')
        self.assertEqual(await self.run_setup([b'{"x":"' + b'a' * 100], limit=32), 'cannot_read')

    async def test_eof_and_timeout(self):
        self.assertEqual(await self.run_setup([b'{"x":', b'']), 'cannot_read')
        self.assertEqual(await self.run_setup([TimeoutError()]), 'cannot_read')


class SensorDecoderTests(TestCase):
    def test_explicit_decoder_even_if_result_equals_raw(self):
        calls = []
        env = dict(get_parameter_value=lambda data, key:data[key], _raw_data_source_key=lambda key:key,
                   VALUE_FORMATTERS={}, VALUE_PROVIDERS={})
        fn = load_function('sensor.py', '_handle_data_update', env)
        entity = SimpleNamespace(_client=SimpleNamespace(data={'special':'7'},available=True),
            _key='special', _decoder=lambda raw:raw, _is_temperature=False, async_write_ha_state=lambda: calls.append('state'),
            _apply_divider=lambda raw: calls.append('divider'))
        fn(entity, None)
        self.assertEqual(entity._attr_native_value, '7')
        self.assertEqual(calls, ['state'])

    def test_plain_sensor_uses_divider(self):
        env = dict(get_parameter_value=lambda data, key:data[key], _raw_data_source_key=lambda key:key,
                   VALUE_FORMATTERS={}, VALUE_PROVIDERS={})
        fn = load_function('sensor.py', '_handle_data_update', env)
        entity = SimpleNamespace(_client=SimpleNamespace(data={'plain':'123'},available=True),
            _key='plain', _decoder=None, _is_temperature=False, async_write_ha_state=lambda: None,
            _apply_divider=lambda raw:float(raw)/10)
        fn(entity, None)
        self.assertEqual(entity._attr_native_value, 12.3)
