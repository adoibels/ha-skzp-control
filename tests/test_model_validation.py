"""Nieznane modele nie udostępniają sterowania w konfiguracji ani opcjach."""
import ast
import unittest
from types import SimpleNamespace
import support
from support import ROOT
from skzp_control.device import SUPPORTED_DEVICE_MODELS


def load(filename, name):
    tree = ast.parse((ROOT / filename).read_text(encoding='utf-8-sig'))
    node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    module = ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[]))
    env = {'SUPPORTED_DEVICE_MODELS': SUPPORTED_DEVICE_MODELS, 'DOMAIN': 'skzp_control'}
    exec(compile(module, filename, 'exec'), env)
    return env[name]


class ModelValidationTests(unittest.IsolatedAsyncioTestCase):
    def test_number_rejects_all_unknown_models(self):
        descriptions = load('number.py', 'get_number_descriptions')
        for model in ('SKZP', 'unknown', '', None):
            with self.subTest(model=model):
                self.assertEqual(descriptions(model, 'SKZP-05S_V5.70'), {})

    def test_choices_reject_unknown_model_before_platform_imports(self):
        choices = load('config_entity_selection.py', 'build_entity_choices')
        self.assertEqual(choices('unknown', {'DevType': 'unknown', 'D203': 60}), [])
        self.assertEqual(choices('SKZP', {'D203': 60}), [])
        self.assertEqual(choices('SKZP-05S', None), [])

    async def test_options_validate_live_client_model(self):
        context = load('config_flow.py', '_async_get_device_context')
        for model in ('SKZP', 'unknown', 'SKZP-05S'):
            data = {'DevType': model, 'D203': 60}
            client = SimpleNamespace(model=model, data=data)
            flow = SimpleNamespace(hass=SimpleNamespace(data={'skzp_control': {'entry': client}}), config_entry=SimpleNamespace(entry_id='entry'))
            actual_model, actual_data = await context(flow)
            self.assertEqual(actual_model, model)
            if model in SUPPORTED_DEVICE_MODELS:
                self.assertEqual(actual_data, data)
                self.assertIsNot(actual_data, data)
            else:
                self.assertIsNone(actual_data)
