"""Regresje centralnych reguł dostępności sensorów."""

import unittest

import support  # noqa: F401  # Instaluje minimalne atrapy Home Assistant.

from skzp_control.entity_support import (
    EntitySupportContext,
    should_create_sensor,
)
from skzp_control.device import supports_fuel_caloric_write


class EntitySupportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.context = EntitySupportContext(
            buffer_settings_as_sensors=True,
            fuel_caloric_as_sensor=False,
        )

    def test_regular_sensor_is_created(self) -> None:
        self.assertTrue(should_create_sensor("Supported", self.context))

    def test_context_uses_firmware_capabilities(self) -> None:
        readable = EntitySupportContext.from_device(
            "SKZP-05S",
            {"DevType": "SKZP-05S_V5.65"},
        )
        writable = EntitySupportContext.from_device(
            "SKZP-05S",
            {"DevType": "SKZP-05S_V5.70"},
        )
        self.assertTrue(readable.buffer_settings_as_sensors)
        self.assertFalse(writable.buffer_settings_as_sensors)

    def test_unrelated_sensor_is_not_affected_by_buffer_capability(self) -> None:
        unavailable_context = EntitySupportContext(
            buffer_settings_as_sensors=False,
            fuel_caloric_as_sensor=True,
        )
        self.assertTrue(should_create_sensor("BoilerTempAct", unavailable_context))

    def test_buffer_sensor_depends_on_capability_or_existing_entity(self) -> None:
        unavailable_context = EntitySupportContext(
            buffer_settings_as_sensors=False,
            fuel_caloric_as_sensor=True,
        )
        self.assertFalse(should_create_sensor("D203", unavailable_context))
        self.assertTrue(
            should_create_sensor(
                "D203",
                unavailable_context,
                frozenset({"D203"}),
            )
        )
        self.assertTrue(should_create_sensor("D203", self.context))

    def test_fuel_caloric_sensor_depends_on_write_capability(self) -> None:
        self.assertFalse(should_create_sensor("BuFuelCaloric", self.context))
        readable_context = EntitySupportContext(
            buffer_settings_as_sensors=True,
            fuel_caloric_as_sensor=True,
        )
        self.assertTrue(should_create_sensor("BuFuelCaloric", readable_context))

    def test_fuel_caloric_write_depends_on_controller_model(self) -> None:
        for dev_type in ("SKZP-04P_V3.08", "SKZP-05PW_V6.10"):
            with self.subTest(dev_type=dev_type):
                self.assertTrue(supports_fuel_caloric_write(dev_type))
                context = EntitySupportContext.from_device("SKZP", {"DevType": dev_type})
                self.assertFalse(should_create_sensor("BuFuelCaloric", context))

        for dev_type in ("SKZP-05S_V5.70", "SKZP-05S_V6.10", "SKZP-02S_V2.46"):
            with self.subTest(dev_type=dev_type):
                self.assertFalse(supports_fuel_caloric_write(dev_type))
                context = EntitySupportContext.from_device("SKZP", {"DevType": dev_type})
                self.assertTrue(should_create_sensor("BuFuelCaloric", context))
