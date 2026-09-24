"""Testy metadanych dekodera sensorów i przeliczania wartości."""
import ast
import math
import unittest
from types import SimpleNamespace

import support
from support import ROOT
from skzp_control.value_decoder import (
    decode_alarms, decode_devstatus_fan, decode_devstatus_mode,
    decode_devstatus_power, get_active_alarms,
)


def method(name):
    tree = ast.parse((ROOT / 'sensor.py').read_text(encoding='utf-8'))
    node = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
    scope = {'math': math, 'Any': object}
    exec(compile(ast.Module(body=[node], type_ignores=[]), 'sensor.py', 'exec'), scope)
    return scope[name]


class DecodingTests(unittest.TestCase):
    def test_special_decoders(self):
        self.assertEqual(decode_devstatus_mode('PRA050075'), 'operating')
        self.assertEqual(decode_devstatus_power('PRA050075'), 50)
        self.assertEqual(decode_devstatus_fan('PRA050075'), 75)
        self.assertIsNone(decode_devstatus_mode('XX'))
        self.assertIsNone(decode_devstatus_power('PRAxxx075'))
        self.assertIsNone(decode_devstatus_fan('PRA050101'))

    def test_alarms(self):
        self.assertEqual(get_active_alarms('ARS'), ['boiler_overheat', 'piston_blocked'])
        self.assertEqual(decode_alarms('ARS'), 2)
        self.assertEqual(decode_alarms(''), None)

    def test_numeric_dividers_and_text(self):
        convert = method('_apply_divider')
        for raw, divider, precision, numeric, expected in (
            ('1234', 100, 2, True, 12.34),
            ('125', 10, 1, True, 12.5),
            ('7', None, None, True, 7),
            ('ABC', None, None, False, 'ABC'),
        ):
            entity = SimpleNamespace(_divider=divider, _value_precision=precision, _is_numeric=numeric)
            self.assertEqual(convert(entity, raw), expected)
        entity = SimpleNamespace(_divider=10, _value_precision=1, _is_numeric=True)
        self.assertIsNone(convert(entity, 'invalid'))

    def test_metadata_bindings(self):
        import sys
        import types
        sensor_mod = types.ModuleType('homeassistant.components.sensor')
        sensor_mod.SensorDeviceClass = SimpleNamespace(TEMPERATURE='temperature', ENUM='enum', WEIGHT='weight')
        sensor_mod.SensorStateClass = SimpleNamespace(MEASUREMENT='measurement', TOTAL_INCREASING='total_increasing')
        sys.modules['homeassistant.components.sensor'] = sensor_mod
        sys.modules['homeassistant.const'].PERCENTAGE = '%'
        sys.modules['homeassistant.const'].UnitOfMass = SimpleNamespace(KILOGRAMS='kg')
        sys.modules['homeassistant.const'].UnitOfTemperature = SimpleNamespace(CELSIUS='C')
        from skzp_control.sensors_map import SENSOR_MAP
        self.assertIs(SENSOR_MAP['Alarms']['decoder'], decode_alarms)
        self.assertIs(SENSOR_MAP['DevStatus_Mode']['decoder'], decode_devstatus_mode)
        self.assertIs(SENSOR_MAP['DevStatus_Power']['decoder'], decode_devstatus_power)
        self.assertIs(SENSOR_MAP['DevStatus_Fan']['decoder'], decode_devstatus_fan)
        self.assertIsNone(SENSOR_MAP['BoilerTempAct'].get('decoder'))
