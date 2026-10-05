"""Testy odczytu i zapisu ochrony powrotu."""

import ast
from dataclasses import dataclass
from types import SimpleNamespace
from unittest import TestCase

import support
from support import ROOT
from test_circuit_numbers import load_definitions
from test_refactoring import load_function
from skzp_control import device
from skzp_control.parameter_resolver import (
    get_parameter_value,
    is_parameter_supported,
    is_parameter_definition_supported,
    resolve_parameter_write_key,
    resolve_parameter_write_value,
)


class ReturnProtectionTests(TestCase):
    def test_read_and_write_keys(self):
        data = {"C030": "3000", "C031": "0"}
        self.assertEqual(get_parameter_value(data, "C030"), "3000")
        self.assertTrue(is_parameter_supported(data, "C031"))
        self.assertEqual(resolve_parameter_write_key(data, "C030"), "CH1ReturnTempCmd")
        self.assertEqual(resolve_parameter_write_key(data, "C031"), "CH1ReturnProtAct")
        self.assertEqual(resolve_parameter_write_value("CH1ReturnProtAct", "1"), "On")
        self.assertEqual(resolve_parameter_write_value("CH1ReturnProtAct", "0"), "Off")
        self.assertEqual(resolve_parameter_write_value("CH1ReturnTempCmd", "3000"), "3000")

    def test_temperature_range_and_models(self):
        definitions = load_definitions()
        meta = definitions["C030"]
        self.assertEqual((meta["min"], meta["max"], meta["step"], meta["divider"]), (30, 80, 1, 100))
        env = {**vars(device), "NUMBER_DESCRIPTIONS": definitions, "Any": object,
               "_FUEL_CALORIC_NUMBER": {"unique_suffix": "fuel_caloric"}}
        get_numbers = load_function("number.py", "get_number_descriptions", env)
        for model in device.SUPPORTED_DEVICE_MODELS:
            numbers = get_numbers(model, model)
            self.assertEqual(numbers["C030"], numbers["CH1ReturnTempCmd"])
            self.assertIsNot(numbers["C030"], numbers["CH1ReturnTempCmd"])
            for data, expected in (
                ({"C030": "3000"}, ["C030"]),
                ({"CH1ReturnTempCmd": "3000"}, ["CH1ReturnTempCmd"]),
                ({"C030": "3000", "CH1ReturnTempCmd": "3000"}, ["C030"]),
                ({}, []),
            ):
                selected = [
                    key for key in ("C030", "CH1ReturnTempCmd")
                    if is_parameter_definition_supported(data, key, numbers)
                ]
                self.assertEqual(selected, expected)

    def test_switch_models_and_confirmation_values(self):
        tree = ast.parse((ROOT / "switch.py").read_text(encoding="utf-8-sig"))
        names = {"SWITCH_DESCRIPTIONS", "SWITCH_KEYS"}
        nodes = [
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "ToggleSwitchDescription"
            or isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id in names for target in node.targets)
        ]
        env = {**vars(device), "dataclass": dataclass}
        exec(compile(ast.Module(body=nodes, type_ignores=[]), "switch.py", "exec"), env)
        get_switches = load_function("switch.py", "get_switch_descriptions", env)
        for model in device.SUPPORTED_DEVICE_MODELS:
            keys = {description.data_key for description in get_switches(model)}
            for data, expected in (
                ({"C031": "0"}, ["C031"]),
                ({"CH1ReturnProtAct": "Off"}, ["CH1ReturnProtAct"]),
                ({"C031": "0", "CH1ReturnProtAct": "Off"}, ["C031"]),
                ({}, []),
            ):
                selected = [
                    key for key in ("C031", "CH1ReturnProtAct")
                    if is_parameter_definition_supported(data, key, keys)
                ]
                self.assertEqual(selected, expected)
        decode = load_function("switch.py", "_decode_state", {"Any": object})
        entity = SimpleNamespace(_data_key="C031", _on_value_normalized="1", _off_value_normalized="0")
        for raw, expected in (("1", True), ("0", False), ("bad", None)):
            self.assertIs(decode(entity, raw), expected)

    def test_text_parameters_are_not_converted(self):
        data = {"CH1ReturnTempCmd": "3000", "CH1ReturnProtAct": "On"}
        for key, value in data.items():
            self.assertEqual(get_parameter_value(data, key), value)
            self.assertEqual(resolve_parameter_write_key(data, key), key)
            self.assertEqual(resolve_parameter_write_value(key, value), value)
        decode = load_function("switch.py", "_decode_state", {"Any": object})
        entity = SimpleNamespace(_data_key="CH1ReturnProtAct", _on_value_normalized="on", _off_value_normalized="off")
        for raw, expected in (("On", True), ("Off", False), ("on", True), ("off", False), ("bad", None)):
            self.assertIs(decode(entity, raw), expected)
