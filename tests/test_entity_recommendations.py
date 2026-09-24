"""Regresje domyślnego wyboru encji na podstawie danych sterownika."""

import ast
import unittest
from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

import support  # noqa: F401  # Udostępnia ścieżkę integracji i atrapy HA.
from support import ROOT
from skzp_control.parameter_resolver import get_parameter_value
from skzp_control.value_decoder import (
    is_unavailable_oxygen,
    is_unavailable_temperature,
)


def load_recommendations():
    """Ładuje rzeczywiste reguły bez zależności od interfejsu HA."""
    path = ROOT / "entity_recommendations.py"
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    names = {
        "_has_unavailable_temperature",
        "_value_is",
        "recommended_disabled_entities",
    }
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    future = ast.ImportFrom(
        module="__future__",
        names=[ast.alias(name="annotations")],
        level=0,
    )
    scope = {
        "Mapping": Mapping,
        "Any": Any,
        "EntityChoice": object,
        "SensorDeviceClass": SimpleNamespace(TEMPERATURE="temperature"),
        "SENSOR_MAP": {"BoilerTempAct": {"device_class": "temperature"}},
        "get_parameter_value": get_parameter_value,
        "is_unavailable_oxygen": is_unavailable_oxygen,
        "is_unavailable_temperature": is_unavailable_temperature,
    }
    module = ast.fix_missing_locations(
        ast.Module(body=[future, *functions], type_ignores=[])
    )
    exec(compile(module, str(path), "exec"), scope)
    return scope["recommended_disabled_entities"]


def choice(platform, key):
    return SimpleNamespace(platform=platform, key=key, selection_id=f"{platform}:{key}")


class RecommendationTests(unittest.TestCase):
    def test_missing_temperatures_disable_related_entities(self):
        recommend = load_recommendations()
        choices = [
            choice("sensor", "BoilerTempAct"),
            choice("binary_sensor", "DevStatus_outBuffer"),
            choice("sensor", "D201"),
            choice("number", "D203"),
            choice("number", "DHWTempCmd"),
            choice("select", "DHWMode"),
        ]
        data = {
            "BoilerTempAct": 30000,
            "D201": 30000,
            "D202": 30000,
            "DHWTempAct": 30000,
        }
        disabled = recommend(choices, data)
        self.assertIn("sensor:BoilerTempAct", disabled)
        self.assertIn("binary_sensor:DevStatus_outBuffer", disabled)
        self.assertIn("number:D203", disabled)
        self.assertIn("number:DHWTempCmd", disabled)
        self.assertNotIn("select:DHWMode", disabled)

    def test_burner_mode_selects_matching_parameter_group(self):
        recommend = load_recommendations()
        choices = [
            choice("number", "BuIntFeedTime"),
            choice("number", "BuModulMin"),
            choice("number", "BuOptFdrFTime"),
        ]
        automatic = recommend(choices, {"BuMode": "Auto"})
        interval = recommend(choices, {"BuMode": "Interwal"})
        self.assertIn("number:BuIntFeedTime", automatic)
        self.assertNotIn("number:BuModulMin", automatic)
        self.assertNotIn("number:BuIntFeedTime", interval)
        self.assertIn("number:BuModulMin", interval)
        self.assertIn("number:BuOptFdrFTime", interval)
