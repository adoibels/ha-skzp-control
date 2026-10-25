"""Testy metadanych dekodera sensorów i przeliczania wartości."""
import math
import json
import re
from string import Formatter
import unittest
from helpers import load_function, definition_modules, translation_strings
from types import SimpleNamespace
from unittest.mock import patch

import support
from skzp_control import localization
from skzp_control.value_decoder import (
    decode_alarms, decode_devstatus_fan, decode_devstatus_mode,
    decode_devstatus_power, get_active_alarms,
)


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
        convert = load_function('sensor.py', '_apply_divider', {'math': math, 'Any': object})
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
        SENSOR_MAP = definition_modules()['skzp_control.sensors_map'].SENSOR_MAP
        self.assertIs(SENSOR_MAP['Alarms']['decoder'], decode_alarms)
        self.assertIs(SENSOR_MAP['DevStatus_Mode']['decoder'], decode_devstatus_mode)
        self.assertIs(SENSOR_MAP['DevStatus_Power']['decoder'], decode_devstatus_power)
        self.assertIs(SENSOR_MAP['DevStatus_Fan']['decoder'], decode_devstatus_fan)
        self.assertIsNone(SENSOR_MAP['BoilerTempAct'].get('decoder'))
        for key, translation_key in {
                'AN01': 'oxygen', 'K003': 'boiler_reduced_setpoint',
                'DevStatus_Power': 'boiler_power_current',
                'B060': 'fuel_capacity', 'B062': 'fuel_remaining',
                'D201': 'buffer_temp_top', 'D202': 'buffer_temp_bottom',
                'D203': 'buffer_temp_setpoint', 'D204': 'buffer_hysteresis'}.items():
            with self.subTest(key=key):
                self.assertEqual(SENSOR_MAP[key]['translation_key'], translation_key)


class TranslationTests(unittest.TestCase):
    def test_shared_names_keep_original_entity_ids_and_match_in_both_languages(self):
        definitions = definition_modules()
        env = dict(
            entity_translation=localization.entity_translation,
            _decimals_for_step=lambda _step: 0,
            _precision_from_divider=lambda _divider: 0,
            EntityCategory=SimpleNamespace(CONFIG='config', DIAGNOSTIC='diagnostic'),
            SensorDeviceClass=SimpleNamespace(TEMPERATURE='temperature'),
            DIAGNOSTIC_KEYS=set(), DISABLED_BY_DEFAULT_KEYS=set(),
        )
        initializers = {name: load_function(name + '.py', '__init__', dict(env))
                        for name in ('number', 'select', 'switch', 'sensor', 'binary_sensor')}
        client = SimpleNamespace()
        cases = []
        for key, meta in definitions['skzp_control.number'].NUMBER_DESCRIPTIONS.items():
            cases.append(('number', meta['unique_suffix'],
                          (client, SimpleNamespace(entry_id='entry'), key, meta)))
        for desc in definitions['skzp_control.select'].SELECT_DESCRIPTIONS:
            cases.append(('select', desc.unique_suffix, (client, 'entry', desc)))
        for desc in definitions['skzp_control.switch'].SWITCH_DESCRIPTIONS:
            cases.append(('switch', desc.unique_suffix, (client, 'entry', desc.unique_suffix, desc.icon)))
        for key, meta in definitions['skzp_control.sensors_map'].SENSOR_MAP.items():
            cases.append(('sensor', key.lower(), (client, 'entry', key, meta)))
        for output in ('CH1', 'CH2', 'CH3', 'Feeder', 'DHW', 'Circ', 'Buffer', 'Heater'):
            key = f'DevStatus_out{output}'
            cases.append(('binary_sensor', key.lower(), (client, 'entry', key, 10)))
        translations = {
            language: json.loads((support.ROOT / f'translations/{language}.json').read_text(encoding='utf-8'))
            for language in ('pl', 'en')}
        for platform, suffix, args in cases:
            with self.subTest(platform=platform, suffix=suffix):
                entity = SimpleNamespace(_init_pending_change=lambda: None)
                initializers[platform](entity, *args)
                self.assertEqual(entity._attr_unique_id, 'entry_' + suffix)
                for language, strings in translations.items():
                    record = strings['entity'][platform][entity._attr_translation_key]
                    name = record['name'].format(**entity._attr_translation_placeholders)
                    self.assertNotIn('{', name)
                    if platform != 'sensor' or suffix not in {
                            key.lower() for key in definitions['skzp_control.sensors_map'].DIAGNOSTIC_SENSORS}:
                        option = localization.selection_translation_key(entity._attr_translation_key)
                        self.assertIn(option, strings['selector']['entity_selection']['options'])
                    if 'number' in entity._attr_translation_placeholders:
                        prefix = 'CO' if language == 'pl' else 'CH'
                        self.assertTrue(name.startswith(prefix + entity._attr_translation_placeholders['number'] + ' '))

    def test_runtime_names_states_and_shared_errors_use_native_translation_cache(self):
        raw = {language: json.loads((support.ROOT / f'translations/{language}.json').read_text(encoding='utf-8'))
                   for language in ('pl', 'en')}
        strings = {language: translation_strings(language) for language in raw}
        self.assertEqual(len(raw['en']['selector']['entity_selection']['options']), 69)
        def flatten(value, prefix):
            result = {}
            for key, item in value.items():
                name = prefix + '.' + key
                if isinstance(item, dict):
                    result.update(flatten(item, name))
                else:
                    result[name] = item
            return result
        base = flatten(raw['en'], 'component.skzp_control')
        localized = flatten(raw['pl'], 'component.skzp_control')
        self.assertLess(set(localized), set(base))
        self.assertNotIn('profile_settings', raw['pl']['options']['step'])
        for key in base.keys() - localized.keys():
            remainder = re.sub(r'\{[A-Za-z_][A-Za-z_0-9]*\}', '', base[key])
            self.assertFalse(any(character.isalnum() for character in remainder), key)
        for key, text in localized.items():
            placeholders = lambda value: {field for _, field, _, _ in Formatter().parse(value) if field}
            self.assertEqual(placeholders(text), placeholders(base[key]), key)
        cache = {'en': base, 'pl': {**base, **localized}}
        hass = SimpleNamespace(config=SimpleNamespace(language='pl'))
        with patch.object(localization, 'async_get_cached_translations',
                          side_effect=lambda _hass, language, *_args: cache.get(language, {})):
            for circuit in (1, 2, 3):
                self.assertEqual(localization.translate(hass, f'entity.number.ch{circuit}_mixer_gain.name'),
                                 f'CO{circuit} Mieszacz Wzmocnienie')
                self.assertEqual(localization.translate(hass, f'entity.switch.ch{circuit}_mixer.name', language='en'),
                                 f'CH{circuit} Mixer')
            self.assertEqual(localization.translate(hass, 'entity.sensor.c109.name'),
                             'CO2 Reg. pokojowy Temp. docelowa')
            self.assertEqual(localization.translate(hass, 'entity.sensor.controller_status.state.off'),
                             'Wyłączony')
            self.assertEqual(localization.translate(hass, 'entity.sensor.active_alarms.name'),
                             'Aktywne alarmy')
            self.assertEqual(localization.translate(hass, 'entity.sensor.fuel_capacity.name'),
                             'Paliwo Pojemność zasobnika')
            self.assertEqual(localization.translate(hass, 'entity.sensor.boiler_power_current.name'),
                             'Kocioł Moc')
            self.assertEqual(localization.translate(hass, 'entity.sensor.boiler_power_current.name', language='en'),
                             'Boiler Power')
            self.assertEqual(localization.translate(hass, 'entity.binary_sensor.devstatus_outdhw.name'),
                             'CWU Pompa')
            self.assertEqual(localization.translate(hass, 'entity.binary_sensor.devstatus_outheater.name', language='en'),
                             'Heater')
            self.assertEqual(localization.translate(hass, 'entity.select.ch2_mode.state.active'), 'Aktywny')
            for circuit in (1, 2):
                self.assertEqual(localization.translate(hass, f'entity.number.weather_stop_temp_ch{circuit}.name'),
                                 f'CO{circuit} Reg. pogodowy Temp. wył. obwodu')
            for circuit in (1, 2, 3):
                self.assertEqual(localization.profile_page_title(hass, f'ch{circuit}'), f'Obieg CO{circuit}')
            message = localization.translate(hass, 'options.error.profile_invalid_pair')
            self.assertTrue(message.startswith('Wartość minimalna nie może być większa'))
            self.assertNotIn('profile_invalid_pair', strings['pl']['common'])
            for language, value in strings.items():
                self.assertNotIn('profile_table_edited', value['common'])
                self.assertNotIn('ch2', value['options']['step'])
                self.assertNotIn('profile_preview', value['options']['step'])
                self.assertIn('profile_settings', value['options']['step'])


class SensorDecoderTests(unittest.TestCase):
    def test_explicit_decoder_even_if_result_equals_raw(self):
        calls = []
        env = dict(get_parameter_value=lambda data, key:data[key], _raw_data_source_key=lambda key:key,
                   VALUE_FORMATTERS={}, VALUE_PROVIDERS={})
        fn = load_function('sensor.py', '_handle_data_update', env)
        entity = SimpleNamespace(_client=SimpleNamespace(data={'special':'7'},available=True),
            _key='special', _decoder=lambda raw:raw, _is_temperature=False, async_write_ha_state=lambda: calls.append('state'),
            _apply_divider=lambda raw: calls.append('divider'))
        fn(entity, None)
        self.assertEqual(entity._attr_native_value, '7')
        self.assertEqual(calls, ['state'])

    def test_plain_sensor_uses_divider(self):
        env = dict(get_parameter_value=lambda data, key:data[key], _raw_data_source_key=lambda key:key,
                   VALUE_FORMATTERS={}, VALUE_PROVIDERS={})
        fn = load_function('sensor.py', '_handle_data_update', env)
        entity = SimpleNamespace(_client=SimpleNamespace(data={'plain':'123'},available=True),
            _key='plain', _decoder=None, _is_temperature=False, async_write_ha_state=lambda: None,
            _apply_divider=lambda raw:float(raw)/10)
        fn(entity, None)
        self.assertEqual(entity._attr_native_value, 12.3)
