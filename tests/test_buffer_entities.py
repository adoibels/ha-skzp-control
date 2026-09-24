import ast
import logging
import sys
import types
import unittest
from unittest.mock import Mock, patch
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


def load_functions(filename, names, env):
    tree=ast.parse((ROOT/filename).read_text(encoding='utf-8-sig'))
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
    future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
    module=ast.fix_missing_locations(ast.Module(body=[future,*nodes],type_ignores=[]))
    exec(compile(module,filename,'exec'),env)


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
            'switch':dict(SWITCH_KEYS={},get_switch_descriptions=lambda _: [])}
        for name,values in attrs.items():
            mod=types.ModuleType('skzp_control.'+name);mod.__dict__.update(values);modules[mod.__name__]=mod
        self.modules=modules

    async def check(self, version, retained, expected_sensors, expected_numbers, disabled=()):
        self.registered.update(('sensor','skzp_control',f'entry_{key.lower()}') for key in retained)
        data=dict(DevType=f'SKZP-05S_V{version}',D203=60,D204=5)
        client=SimpleNamespace(data=data,model='SKZP-05S',host='host',port=1)
        hass=SimpleNamespace(data={'skzp_control':{'entry':client}})
        entry=SimpleNamespace(entry_id='entry',options={'disabled_entities':list(disabled)})
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

    async def test_new_570_numbers_only(self):
        await self.check('5.70',[],[],['D203','D204'])

    async def test_upgrade_preserves_both_sensors(self):
        await self.check('5.70',['D203','D204'],['D203','D204'],['D203','D204'])

    async def test_upgrade_preserves_only_registered_sensor(self):
        await self.check('5.70',['D203'],['D203'],['D203','D204'])

    async def test_old_firmware_sensors_only(self):
        await self.check('5.65',[],['D203','D204'],[])

    async def test_disabled_sensor_remains_selectable(self):
        await self.check('5.70',['D203'],['D203'],['D203','D204'],['sensor:D203'])

    async def test_other_entry_does_not_enable_legacy_sensors(self):
        self.registered.add(('sensor','skzp_control','other_d203'))
        await self.check('5.70',[],[],['D203','D204'])

    def test_setup_does_not_delete_registry_entries(self):
        source=(ROOT/'__init__.py').read_text(encoding='utf-8')
        self.assertNotIn('async_remove',source)
        self.assertNotIn('_remove_stale_buffer_setting_entities',source)

    def test_options_pass_existing_registry_sensors(self):
        # Sprawdź inicjalizację opcji, a nie tylko tworzenie listy wyboru.
        tree=ast.parse((ROOT/'config_flow.py').read_text(encoding='utf-8'))
        cls=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='SkzpOptionsFlow')
        method=next(n for n in cls.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_async_ensure_state')
        calls=[n for n in ast.walk(method) if isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id=='existing_buffer_sensor_keys']
        self.assertEqual(len(calls),1)
