import ast
import asyncio
import logging
import sys
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch
from types import SimpleNamespace
from collections.abc import Mapping
from typing import Any
import support
from support import ROOT
from skzp_control.entity_layout import existing_buffer_sensor_keys
from skzp_control.device import SUPPORTED_DEVICE_MODELS, BUFFER_SETTING_KEYS, supports_buffer_setting_reads, supports_buffer_setting_writes, supports_fuel_caloric_write
from skzp_control.entity_layout import EntityChoice, entity_location, is_entity_enabled
from skzp_control.entity_support import (
    EntitySupportContext,
    should_create_sensor,
)


from helpers import load_functions, load_function, load_flow
from dataclasses import dataclass
from unittest import TestCase
from skzp_control import device
from skzp_control.parameter_resolver import is_parameter_definition_supported, resolve_parameter_write_key, resolve_parameter_write_value


class BufferTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry=Mock()
        self.registered=set()
        self.registry.async_get_entity_id.side_effect=lambda domain,platform,uid: ('sensor.renamed' if (domain,platform,uid) in self.registered else None)
        self.patch=patch('skzp_control.entity_layout.er.async_get',return_value=self.registry,create=True)
        self.patch.start();self.addCleanup(self.patch.stop)
        self.env=dict(__package__='skzp_control',SUPPORTED_DEVICE_MODELS=SUPPORTED_DEVICE_MODELS,BUFFER_SETTING_KEYS=BUFFER_SETTING_KEYS,
            supports_buffer_setting_reads=supports_buffer_setting_reads,
            supports_buffer_setting_writes=supports_buffer_setting_writes,
            supports_fuel_caloric_write=supports_fuel_caloric_write,
            existing_buffer_sensor_keys=existing_buffer_sensor_keys,
            DOMAIN='skzp_control',DIAGNOSTIC_KEYS=set(),
            get_sensor_descriptions=lambda _: {'D203':{},'D204':{}},
            is_sensor_supported=lambda data,key:key in data,
            is_entity_enabled=is_entity_enabled,
            SkzpSensor=lambda client,entry,key,meta:key,
            _LOGGER=logging.getLogger('buffer-test'),EntityChoice=EntityChoice,
            entity_location=entity_location,Mapping=Mapping,Any=Any,
            EntitySupportContext=EntitySupportContext,
            should_create_sensor=should_create_sensor,
            is_parameter_definition_supported=lambda data,key,descriptions:key in data)
        load_functions('sensor.py',{'async_setup_entry'},self.env)
        load_functions('config_entity_selection.py',{'build_entity_choices'},self.env)
        modules={}
        attrs={
            'sensor':{name:self.env[name] for name in ('DIAGNOSTIC_KEYS','get_sensor_descriptions','is_sensor_supported')},
            'binary_sensor':dict(get_output_descriptions=lambda _: {},is_output_supported=lambda *_:False),
            'number':dict(get_number_descriptions=lambda model,dev: {'D203':{'unique_suffix':'buffer_temp_setpoint'},'D204':{'unique_suffix':'buffer_hysteresis'}} if supports_buffer_setting_writes(dev) else {}),
            'select':dict(SELECT_KEYS={},get_select_descriptions=lambda _: []),
            'switch':dict(SWITCH_KEYS={},get_switch_descriptions=lambda *_: [])}
        for name,values in attrs.items():
            mod=types.ModuleType('skzp_control.'+name);mod.__dict__.update(values);modules[mod.__name__]=mod
        self.modules=modules

    async def check(self, version, retained, expected_sensors, expected_numbers, disabled=()):
        self.registered.update(('sensor','skzp_control',f'entry_{key.lower()}') for key in retained)
        data=dict(DevType=f'SKZP-05S_V{version}',D203=60,D204=5)
        client=SimpleNamespace(data=data,model='SKZP-05S',host='host',port=1)
        hass=SimpleNamespace(data={'skzp_control':{'entry':client}})
        entry=SimpleNamespace(entry_id='entry',options={
            'disabled_entities':list(disabled),
            'known_entities':[f'{platform}:{key}' for platform,keys in (
                ('sensor',expected_sensors),('number',expected_numbers)) for key in keys],
        })
        kept=existing_buffer_sensor_keys(hass,'entry')
        entities=[]
        await self.env['async_setup_entry'](hass,entry,entities.extend)
        self.assertEqual(set(entities),set(expected_sensors)-{s.split(':')[1] for s in disabled if s.startswith('sensor:')})
        with patch.dict(sys.modules,self.modules):
            choices=self.env['build_entity_choices']('SKZP-05S',data,kept)
        self.assertEqual({c.key for c in choices if c.platform=='sensor'},set(expected_sensors))
        self.assertEqual({c.key for c in choices if c.platform=='number'},set(expected_numbers))
        for c in choices:
            self.assertEqual(c.section,'sensors' if c.platform=='sensor' else 'configuration')
        self.registry.async_remove.assert_not_called()

    async def test_sensor_and_number_variants(self):
        cases = (
            ("nowy wpis", "5.70", [], [], ["D203", "D204"], [], []),
            ("zachowane oba sensory", "5.70", ["D203", "D204"], ["D203", "D204"], ["D203", "D204"], [], []),
            ("zachowany jeden sensor", "5.70", ["D203"], ["D203"], ["D203", "D204"], [], []),
            ("starsze oprogramowanie", "5.65", [], ["D203", "D204"], [], [], []),
            ("wyłączony sensor", "5.70", ["D203"], ["D203"], ["D203", "D204"], ["sensor:D203"], []),
            ("sensor innego wpisu", "5.70", [], [], ["D203", "D204"], [], [("sensor", "skzp_control", "other_d203")]),
        )
        for name, version, retained, sensors, numbers, disabled, other_entries in cases:
            with self.subTest(case=name):
                self.registered.clear()
                self.registered.update(other_entries)
                self.registry.reset_mock()
                await self.check(version, retained, sensors, numbers, disabled)

    async def test_setup_does_not_delete_registry_entries(self):
        for version in ("5.65", "5.70"):
            with self.subTest(version=version):
                client = SimpleNamespace(
                    start=AsyncMock(), wait_for_data=AsyncMock(return_value=True), stop=AsyncMock(),
                    no_data_timeout=30, model="SKZP-05S",
                    data={"DevType": f"SKZP-05S_V{version}", "D203": "6000", "D204": "500"})
                env = {**vars(device), "asyncio": asyncio, "DOMAIN": "skzp_control",
                       "CONF_HOST": "host", "CONF_PORT": "port", "CONF_KNOWN_ENTITIES": "known_entities",
                       "SkzpCoordinator": Mock(return_value=client)}
                setup = load_function("__init__.py", "async_setup_entry", env)
                hass = SimpleNamespace(data={}, config_entries=SimpleNamespace(
                    async_forward_entry_setups=AsyncMock()))
                entry = SimpleNamespace(entry_id="entry", data={"host": "host", "port": 1},
                    options={"known_entities": ["sensor:D203", "sensor:D204"]})
                env["PLATFORMS"] = ["sensor", "number", "switch"]
                registry_entries = {"sensor.buffer": SimpleNamespace(unique_id="entry_d203")}
                self.registry.entities = registry_entries.copy()
                with patch("homeassistant.helpers.entity_registry.async_get", return_value=self.registry, create=True):
                    self.assertTrue(await setup(hass, entry))
                self.registry.async_remove.assert_not_called()
                self.assertEqual(self.registry.entities, registry_entries)
                hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(entry, env["PLATFORMS"])
                self.assertIs(hass.data["skzp_control"]["entry"], client)
                client.stop.assert_not_awaited()

    async def test_options_pass_existing_registry_sensors(self):
        module, env = load_flow()
        retained = frozenset({"D203"})
        env["existing_buffer_sensor_keys"] = existing = Mock(return_value=retained)
        env["build_entity_choices"] = choices = Mock(return_value=[])
        flow = module.SkzpOptionsFlow()
        flow.hass = SimpleNamespace(data={})
        flow.config_entry = SimpleNamespace(entry_id="entry", options={})
        data = {"DevType": "SKZP-05S_V5.70", "D203": "6000", "D204": "500"}
        flow._async_get_device_context = AsyncMock(return_value=("SKZP-05S", data))
        await flow.async_step_init()
        existing.assert_called_once_with(flow.hass, "entry")
        choices.assert_called_once_with("SKZP-05S", data, retained)


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
