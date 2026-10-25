"""Testy wspólnych definicji parametrów obiegów."""

from unittest import TestCase

from helpers import definition_modules, load_definitions


class CircuitNumberTests(TestCase):
    def test_min_max_pairs_cover_common_circuit_and_alternative_keys(self):
        module = definition_modules()["skzp_control.number"]
        expected = {("BuModulMin", "BuModulMax"), ("BuAirMin", "BuAirMax"),
                    ("CH1MixTempMin", "CH1MixTempMax"), ("CH1MixValueMin", "CH1MixValueMax")}
        for circuit in range(3):
            expected.update({(f"C{circuit}28", f"C{circuit}29"),
                             (f"C{circuit}22", f"C{circuit}23")})
        self.assertEqual(set(module.MIN_MAX_PAIRS), expected)
        self.assertEqual(len(module.MIN_MAX_PAIRS), len(expected))
        for low, high in module.MIN_MAX_PAIRS:
            self.assertEqual(module.NUMBER_DESCRIPTIONS[low]["paired_max"], high)
            self.assertIn(high, module.NUMBER_DESCRIPTIONS)
            self.assertEqual(module._MIN_TO_MAX[low], high)
            self.assertEqual(module._MAX_TO_MIN[high], low)

    def test_model_variants_and_fuel_definitions_do_not_change_defaults(self):
        module = definition_modules()["skzp_control.number"]
        keys = list(module.NUMBER_DESCRIPTIONS)
        self.assertEqual(keys[keys.index("BuFuelCorr") + 1], "BuFuelCaloric")
        for model in ("SKZP-02S", "SKZP-02T", "SKZP-04P", "SKZP-05S", "SKZP-05PW"):
            with self.subTest(model=model):
                descriptions = module.get_number_descriptions(model, model)
                feeder = descriptions["BuOptFdrEff"]
                self.assertEqual(
                    (feeder["unit"], feeder["min"], feeder["max"], feeder["step"], feeder["divider"]),
                    ("kg", 0.01, 50, 0.01, 100) if model == "SKZP-02T" else
                    ("kg/h", 2, 200, 0.05, 100))
                self.assertEqual(feeder["unique_suffix"], "feeder_efficiency")
                self.assertNotIn("model_overrides", feeder)
                caloric = model in {"SKZP-04P", "SKZP-05PW"}
                self.assertEqual("BuFuelCaloric" in descriptions, caloric)
                self.assertEqual("BuFuelCorr" in descriptions, not caloric)
                feeder["max"] = -1
                self.assertEqual(module.NUMBER_DESCRIPTIONS["BuOptFdrEff"]["max"], 200)
                self.assertNotEqual(module.get_number_descriptions(model, model)["BuOptFdrEff"]["max"], -1)

    def test_circuit_metadata_and_order(self):
        descriptions = load_definitions()
        codes = ("22", "23", "24", "25", "27", "28", "29", "46", "13", "14", "15", "18", "40")
        expected = [f"C{circuit}{code}" for circuit in range(3) for code in codes]
        self.assertEqual([key for key in descriptions if key in expected], expected)
        for code in codes:
            first = descriptions[f"C0{code}"]
            for circuit in (2, 3):
                other = descriptions[f"C{circuit - 1}{code}"]
                expected_meta = {**first, "unique_suffix": first["unique_suffix"].replace("ch1_", f"ch{circuit}_", 1)}
                if "paired_max" in first:
                    expected_meta["paired_max"] = first["paired_max"].replace("C0", f"C{circuit - 1}", 1)
                self.assertEqual(other, expected_meta)
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
            expected_meta = {**descriptions[source], "unique_suffix": suffix}
            if "paired_max" in expected_meta:
                expected_meta["paired_max"] = {"C023": "CH1MixValueMax", "C029": "CH1MixTempMax"}[
                    expected_meta["paired_max"]]
            self.assertEqual(descriptions[key], expected_meta)
            self.assertIsNot(descriptions[key], descriptions[source])

    def test_metadata_is_independent(self):
        descriptions = load_definitions()
        descriptions["C022"]["max"] = 42
        self.assertEqual(descriptions["C122"]["max"], 90)
        self.assertEqual(descriptions["CH1MixValueMin"]["max"], 90)
