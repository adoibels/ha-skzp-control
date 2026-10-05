"""Testy dostępności i wartości przełącznika pracy z buforem."""

import ast
from dataclasses import dataclass
from unittest import TestCase

from support import ROOT
from skzp_control import device
from skzp_control.entity_layout import entity_location
from skzp_control.parameter_resolver import (
    is_parameter_definition_supported,
    resolve_parameter_write_key,
    resolve_parameter_write_value,
)


class BufferSwitchTests(TestCase):
    def test_firmware_filter_and_wire_values(self):
        tree = ast.parse((ROOT / "switch.py").read_text(encoding="utf-8-sig"))
        nodes = [node for node in tree.body if (
            isinstance(node, ast.ClassDef) and node.name == "ToggleSwitchDescription"
            or isinstance(node, ast.FunctionDef) and node.name == "get_switch_descriptions"
            or isinstance(node, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == "SWITCH_DESCRIPTIONS"
                for target in node.targets)
        )]
        env = {**vars(device), "dataclass": dataclass}
        future = ast.ImportFrom(module="__future__",
            names=[ast.alias(name="annotations")], level=0)
        exec(compile(ast.fix_missing_locations(ast.Module(
            body=[future, *nodes], type_ignores=[])), "switch.py", "exec"), env)
        for identity, expected in (
            ("SKZP-05S_V5.65", False), ("SKZP-05S_V5.69", False),
            ("SKZP-05S_V5.70", True), ("SKZP-05S_V5.71", True),
            ("SKZP-05PW_V6.00", False), ("SKZP-02S_V5.71", False),
            ("SKZP-05S", False),
        ):
            with self.subTest(identity=identity):
                descriptions = env["get_switch_descriptions"](
                    device.detect_device_model(identity), identity)
                self.assertEqual(any(d.data_key == "D200" for d in descriptions), expected)
        description = next(d for d in env["SWITCH_DESCRIPTIONS"] if d.data_key == "D200")
        self.assertEqual((description.on_value, description.off_value), ("1", "0"))
        for value in ("0", "1"):
            self.assertEqual(resolve_parameter_write_key({"D200": value}, "D200"), "D200")
            self.assertEqual(resolve_parameter_write_value("D200", value), value)
        self.assertFalse(is_parameter_definition_supported({}, "D200", {"D200"}))
        self.assertTrue(is_parameter_definition_supported({"D200": "0"}, "D200", {"D200"}))
        self.assertEqual(entity_location("switch", "D200")[:3],
                         ("dhw_buffer", "configuration", "buffer"))
