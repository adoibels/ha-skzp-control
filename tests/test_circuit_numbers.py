"""Testy wspólnych definicji parametrów obiegów."""

import ast
from unittest import TestCase

from support import ROOT


def load_definitions():
    tree = ast.parse((ROOT / "number.py").read_text(encoding="utf-8-sig"))
    functions = {"_temperature_number", "_number", "_build_circuit_numbers"}
    constants = {"_FUEL_CALORIC_NUMBER", "_CIRCUIT_NUMBER_TEMPLATES", "NUMBER_DESCRIPTIONS"}
    nodes = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in functions:
            nodes.append(node)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            if isinstance(target, ast.Name) and target.id in constants:
                nodes.append(node)
    env = {"Any": object}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "number.py", "exec"), env)
    return env["NUMBER_DESCRIPTIONS"]


class CircuitNumberTests(TestCase):
    def test_circuit_metadata_and_order(self):
        descriptions = load_definitions()
        codes = ("22", "23", "24", "25", "27", "28", "29", "46", "13", "14", "15", "18", "40")
        expected = [f"C{circuit}{code}" for circuit in range(3) for code in codes]
        self.assertEqual([key for key in descriptions if key in expected], expected)
        for code in codes:
            first = descriptions[f"C0{code}"]
            for circuit in (2, 3):
                other = descriptions[f"C{circuit - 1}{code}"]
                self.assertEqual(other, {**first, "unique_suffix": first["unique_suffix"].replace("ch1_", f"ch{circuit}_", 1)})
                self.assertIsNot(first, other)

    def test_alternative_keys_and_identifiers(self):
        descriptions = load_definitions()
        alternatives = {
            "CH1MixValueMin": ("C022", "ch1_mixer_opening_min"),
            "CH1MixValueMax": ("C023", "ch1_mixer_opening_max"),
            "CH1MixGain": ("C024", "ch1_mixer_gain"),
            "CH1MixPeriod": ("C025", "ch1_mixer_stabilization_time"),
            "CH1MixTempBase": ("C027", "ch1_mixer_temp_setpoint"),
            "CH1MixTempMin": ("C028", "ch1_mixer_temp_min"),
            "CH1MixTempMax": ("C029", "ch1_mixer_temp_max"),
            "CH1ReturnTempCmd": ("C030", "ch1_return_temp_min"),
            "CH1RoomTempCom": ("C013", "ch1_room_comfort_temp"),
            "CH1RoomTempEco": ("C014", "ch1_room_eco_temp"),
            "CH1RoomHist": ("C015", "ch1_room_hysteresis"),
            "WeaCorr": ("C018", "weather_correction"),
            "WeaTempStopCH1": ("C040", "weather_stop_temp_ch1"),
            "WeaTempStopCH2": ("C140", "weather_stop_temp_ch2"),
        }
        self.assertEqual([key for key in descriptions if key.startswith(("CH", "Wea"))], list(alternatives))
        for key, (source, suffix) in alternatives.items():
            self.assertEqual(descriptions[key], {**descriptions[source], "unique_suffix": suffix})
            self.assertIsNot(descriptions[key], descriptions[source])

    def test_metadata_is_independent(self):
        descriptions = load_definitions()
        descriptions["C022"]["max"] = 42
        self.assertEqual(descriptions["C122"]["max"], 90)
        self.assertEqual(descriptions["CH1MixValueMin"]["max"], 90)
