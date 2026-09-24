import ast
import asyncio
import time
import unittest
from types import SimpleNamespace
from support import ROOT as root, HAError
from skzp_control.client.command_manager import CommandManager
from skzp_control.pending_change import PendingChangeMixin

class FakeEntity(PendingChangeMixin, SimpleNamespace):
    pass

class EntityRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_error_and_cancel_restore_all_platforms(self):
        # Sprawdź obsługę błędu i anulowania na metodach rzeczywistych encji.
        for filename, method, argument, attrs in (
            ('number.py', 'async_set_native_value', 10, dict(_confirmed_value=1, _attr_native_value=1, _number_send_delay=0, _key='x', _validate_min_max_pair=lambda _:None, _command_value=lambda x:x, _round=lambda x:x)),
            ('select.py', 'async_select_option', 'on', dict(_modes={'on':1}, _confirmed_option='off', _attr_current_option='off', _data_key='x')),
            ('switch.py', '_send_optimistic_command', True, dict(_confirmed_state=False, _attr_is_on=False)),
        ):
            tree = ast.parse((root / filename).read_text(encoding='utf-8'))
            node = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == method)
            scope = dict(asyncio=asyncio, time=time, HomeAssistantError=HAError, resolve_parameter_write_key=lambda *_:'x')
            exec(compile(ast.Module(body=[node], type_ignores=[]), filename, 'exec'), scope)
            for cancellation in (False, True):
                async def send(_):
                    if cancellation: raise asyncio.CancelledError()
                    raise HAError(translation_key='command_send_timeout')
                entity = FakeEntity(**attrs, _send_generation=0, _confirmation_event=None,
                    _client=SimpleNamespace(command_manager=CommandManager(), command_retry_delay=0, command_pending_timeout=11, command_retry_count=1, data={}, send_command=send),
                    async_write_ha_state=lambda:None)
                restored = []
                for name in ('_restore_controller_value', '_restore_controller_option', '_restore_controller_state'):
                    setattr(entity, name, lambda x:restored.append(x))
                entity._apply_optimistic_state = lambda _:None
                args = (entity, argument, {'x':1}) if filename == 'switch.py' else (entity, argument)
                try: await scope[method](*args)
                except (HAError, asyncio.CancelledError): pass
                else: raise AssertionError('missing exception')
                assert len(restored) == 1 and entity._confirmation_event is None and entity._pending_target is None
