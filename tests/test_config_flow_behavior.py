"""Testy formularza z lekkimi atrapami interfejsu Home Assistanta.

Uruchamia klasy formularza i funkcje pomocnicze razem, zachowując dziedziczenie.
Podmienia tylko importy na granicy HA i integracji; nie wydziela metod.
"""

from copy import deepcopy
import json
import asyncio
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support
from skzp_control import entity_layout, localization
from skzp_control.frame_parser import FrameParser
from skzp_control.device import detect_device_model, SUPPORTED_DEVICE_MODELS
from helpers import definition_modules, load_flow, load_function, translation_strings


class AdvancedOptionsTests(unittest.TestCase):
    def setUp(self):
        self.flow, _ = load_flow()

    def test_defaults_and_form_contract(self):
        expected = {
            "boiler_temp_max": (80, 60, 90, 1, "°C"),
            "number_send_delay": (1.0, 0.0, 10.0, 0.1, "s"),
            "command_retry_count": (1, 0, 5, 1, None),
            "command_retry_delay": (1.0, 0.0, 10.0, 0.5, "s"),
            "command_confirm_timeout": (5.0, 3.0, 30.0, 1, "s"),
            "no_data_timeout": (30.0, 10.0, 300.0, 5, "s"),
            "reconnect_delay": (5.0, 1.0, 30.0, 1, "s"),
        }
        defaults = self.flow._default_advanced_options()
        schema = self.flow._build_advanced_settings_schema(defaults).schema
        self.assertEqual([field.name for field in schema],
                         [*expected, "notify_connection_lost", "notify_connection_restored"])
        for field, selector in schema.items():
            if field.name in expected:
                default, low, high, step, unit = expected[field.name]
                self.assertEqual(defaults[field.name], default)
                self.assertIs(type(defaults[field.name]), type(default))
                config = dict(min=low, max=high, step=step, mode="slider")
                if unit is not None:
                    config["unit_of_measurement"] = unit
                self.assertEqual(selector.config, config)
            else:
                self.assertIs(
                    defaults[field.name], field.name == "notify_connection_lost"
                )
            self.assertEqual(field.options["default"], defaults[field.name])

    def test_normalization_types_bounds_and_invalid_values(self):
        normalize = self.flow._normalize_advanced_options
        defaults = self.flow._default_advanced_options()
        self.assertEqual(normalize({}), defaults)
        for bad in (None, "invalid", [], {}):
            self.assertEqual(normalize(dict.fromkeys(defaults, bad)), defaults)
        result = normalize({"boiler_temp_max": "95", "command_retry_count": 2.9,
                            "number_send_delay": "2.5", "command_retry_delay": -4,
                            "command_confirm_timeout": 100, "no_data_timeout": 1,
                            "reconnect_delay": 100, "notify_connection_lost": False,
                            "notify_connection_restored": "false", "unknown": 7})
        self.assertEqual(result, dict(boiler_temp_max=90, command_retry_count=2,
                         number_send_delay=2.5, command_retry_delay=0.0,
                         command_confirm_timeout=30.0, no_data_timeout=10.0,
                         reconnect_delay=30.0, notify_connection_lost=False,
                         notify_connection_restored=False))
        self.assertIs(type(result["boiler_temp_max"]), int)
        self.assertIs(type(result["number_send_delay"]), float)

    def test_partial_form_preserves_previous_values_without_mutation(self):
        previous = {"boiler_temp_max": 90, "notify_connection_lost": False}
        result = self.flow._advanced_options_from_form({"number_send_delay": 2}, previous)
        self.assertEqual(result["boiler_temp_max"], 90)
        self.assertIs(result["notify_connection_lost"], False)
        self.assertEqual(result["number_send_delay"], 2.0)
        self.assertEqual(previous, {"boiler_temp_max": 90, "notify_connection_lost": False})


class FlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.module, self.env = load_flow()
        self.choices = [entity_layout.EntityChoice(
            platform="sensor", key="K", unique_suffix="k", page="ch1",
            section=entity_layout.SECTION_SENSORS, field="sample", order=(0, 0))]
        self.entry = SimpleNamespace(entry_id="entry", data={"host": "controller", "port": 80,
                                     "device_model": "SKZP-05S"}, options={})

    def create_flow(self, options=False, initialized=True):
        cls = self.module.SkzpOptionsFlow if options else self.module.SkzpConfigFlow
        flow = cls()
        flow.hass = SimpleNamespace(data={})
        flow.config_entry = self.entry
        if initialized:
            flow._choices = self.choices
            flow._disabled_entities = {"sensor:outside"}
            if options:
                flow._model = "SKZP-05S"
            else:
                flow._device_model = "SKZP-05S"
                flow._connection_data = dict(self.entry.data)
        return flow

    async def test_all_named_pages_and_dhw_alias_in_both_flows(self):
        for options in (False, True):
            flow = self.create_flow(options)
            for step in ("boiler_burner", "dhw_buffer", "dhw", "ch1", "ch2", "ch3"):
                with self.subTest(options=options, step=step):
                    result = await getattr(flow, "async_step_" + step)()
                    self.assertEqual(result["step_id"], "circuit" if step.startswith("ch") else step)
                    self.assertEqual(result["type"], "form")
                    if step.startswith("ch"):
                        self.assertEqual(result["description_placeholders"], {"number": step[-1]})
                        self.assertEqual(flow._entity_circuit, step)
                        for language in ("pl", "en"):
                            strings = json.loads((support.ROOT / f"translations/{language}.json").read_text(encoding="utf-8"))
                            category = "options" if options else "config"
                            text = strings[category]["step"]["circuit"]
                            self.assertIn(step[-1], text["title"].format(**result["description_placeholders"]))
                            text["description"].format(**result["description_placeholders"])
                    result = await getattr(flow, "async_step_" + result["step_id"])({})
                    self.assertEqual(result["step_id"], "entities")
            self.assertIn("sensor:outside", flow._disabled_entities)
        dhw_choice = entity_layout.EntityChoice("sensor", "DHWTempAct", "dhwtempact",
                        entity_layout.PAGE_DHW_BUFFER, entity_layout.SECTION_SENSORS,
                        "dhw", (0, 0))
        flow._choices = [dhw_choice]
        result = await flow.async_step_dhw()
        self.assertTrue(result["data_schema"].schema)

    async def test_page_save_and_bulk_selection_preserve_hidden_ids(self):
        for options in (False, True):
            flow = self.create_flow(options)
            await flow.async_step_ch1({})
            self.assertEqual(flow._disabled_entities, {"sensor:K", "sensor:outside"})
            await flow.async_step_ch1({entity_layout.SECTION_SENSORS: {"sample": ["k"]}})
            self.assertEqual(flow._disabled_entities, {"sensor:outside"})
            result = await flow.async_step_deselect_all()
            self.assertEqual(result["step_id"], "entities")
            self.assertEqual(flow._disabled_entities, {"sensor:K", "sensor:outside"})
            self.assertEqual(result["description_placeholders"]["selected"], "0")
            result = await flow.async_step_select_all()
            self.assertEqual(result["step_id"], "entities")
            self.assertEqual(flow._disabled_entities, {"sensor:outside"})
            self.assertEqual(result["description_placeholders"]["selected"], "1")

    async def test_entity_submenu_has_no_save_and_keeps_working_selection_in_both_flows(self):
        for options in (False, True):
            with self.subTest(options=options):
                flow = self.create_flow(options)
                flow.async_create_entry = Mock(wraps=flow.async_create_entry)
                self.env["_sync_entity_registry_selection"] = sync = Mock()
                before = deepcopy(self.entry.options)
                if options:
                    main = await flow.async_step_init()
                else:
                    flow._recommended_disabled = set(flow._disabled_entities)
                    main = await flow.async_step_advanced()
                expected = ["entities", "profiles", "settings", "finish"] if options else ["entities", "settings", "finish"]
                self.assertEqual(main["step_id"], "init")
                self.assertEqual(main["menu_options"], expected)
                self.assertFalse(main.get("description_placeholders"))
                menu = await flow.async_step_entities()
                self.assertEqual(menu["menu_options"],
                                 ["ch1", "select_all", "deselect_all", "restore_recommended", "init"])
                result = await flow.async_step_ch1({})
                self.assertEqual(result["step_id"], "entities")
                self.assertEqual(result["description_placeholders"], {"selected": "0", "total": "1"})
                await flow.async_step_init()
                settings = await flow.async_step_settings({"boiler_temp_max": 90})
                self.assertEqual(settings["step_id"], "init")
                menu = await flow.async_step_entities()
                self.assertEqual(menu["description_placeholders"]["selected"], "0")
                page = await flow.async_step_ch1()
                group = next(iter(page["data_schema"].schema.values()))[0].schema
                self.assertEqual(next(iter(group)).options["default"], [])
                self.assertEqual(self.entry.options, before)
                sync.assert_not_called()
                flow.async_create_entry.assert_not_called()
                result = await flow.async_step_finish()
                self.assertEqual(result["type"], "create_entry")
                saved = result["data"] if options else result["options"]
                self.assertEqual(saved["disabled_entities"], ["sensor:K", "sensor:outside"])
                self.assertEqual(saved["boiler_temp_max"], 90)
                if options:
                    sync.assert_called_once()
                else:
                    sync.assert_not_called()
                flow.async_create_entry.assert_called_once()

    async def test_counter_translations_and_dynamic_pages_in_both_flows(self):
        for options in (False, True):
            flow = self.create_flow(options)
            flow._choices = [entity_layout.EntityChoice(
                "sensor", f"K{index}", f"k{index}", "ch1", "sensors", "sample", (0, index))
                for index in range(132)]
            flow._disabled_entities = {f"sensor:K{index}" for index in range(52)} | {"sensor:hidden"}
            main = await flow.async_step_init()
            entities = await flow.async_step_entities()
            self.assertEqual(entities["description_placeholders"], {"selected": "80", "total": "132"})
            for language, expected in (("pl", "Wybrane encje: 80 / 132"),
                                       ("en", "Selected entities: 80 / 132")):
                with self.subTest(options=options, language=language):
                    strings = json.loads((support.ROOT / f"translations/{language}.json").read_text(encoding="utf-8"))
                    steps = strings["options" if options else "config"]["step"]
                    for screen in (main, entities):
                        texts = steps[screen["step_id"]]
                        placeholders = screen.get("description_placeholders", {})
                        texts["title"].format(**placeholders)
                        description = texts["description"].format(**placeholders)
                        if screen is entities:
                            self.assertTrue(description.startswith(expected))
                            self.assertNotIn("menu_option_descriptions", texts)
                        for option in screen["menu_options"]:
                            self.assertIn(option, texts["menu_options"])
                            if option != "profiles":
                                self.assertTrue(callable(getattr(flow, "async_step_" + option)))
                    self.assertEqual(set(steps["init"]["menu_option_descriptions"]), set(main["menu_options"]))
                    self.assertNotIn("finish", steps["entities"]["menu_options"])
                    self.assertNotIn("settings", steps["entities"]["menu_options"])
                    self.assertNotIn("{model}", steps["init"]["description"])
                    self.assertNotIn("{model}", steps["entities"]["description"])
            for buffer in (False, True):
                flow._choices = [entity_layout.EntityChoice(
                    "sensor", "D201" if buffer else "DHWTempAct", "dhw",
                    entity_layout.PAGE_DHW_BUFFER, "sensors", "buffer" if buffer else "dhw", (0, 0))]
                menu = await flow.async_step_entities()
                self.assertEqual(menu["menu_options"][0], "dhw_buffer" if buffer else "dhw")
            flow._choices = []
            menu = await flow.async_step_entities()
            self.assertEqual(menu["menu_options"], ["init"])
            self.assertEqual(menu["description_placeholders"], {"selected": "0", "total": "0"})

    async def test_settings_show_and_partial_save_in_both_flows(self):
        for options in (False, True):
            flow = self.create_flow(options)
            shown = await flow.async_step_settings()
            self.assertEqual(shown["step_id"], "settings")
            self.assertEqual(len(shown["data_schema"].schema), 9)
            flow._advanced_options["notify_connection_lost"] = False
            result = await flow.async_step_settings({"boiler_temp_max": 90})
            self.assertEqual(result["step_id"], "init")
            self.assertEqual(flow._advanced_options["boiler_temp_max"], 90)
            self.assertIs(flow._advanced_options["notify_connection_lost"], False)

    async def test_config_requires_device_for_page_and_settings(self):
        for step in ("init", "entities", "advanced", "ch1", "settings"):
            flow = self.create_flow(initialized=False)
            self.assertEqual(await getattr(flow, "async_step_" + step)(),
                             {"type": "abort", "reason": "cannot_read_device"})

    async def test_detected_controller_shows_firmware_without_date_when_available(self):
        self.env["translate"] = localization.translate
        self.env["async_get_translations"] = load_translations = AsyncMock()
        self.env["build_entity_choices"] = Mock(return_value=self.choices)
        self.env["recommended_disabled_entities"] = Mock(return_value=set())
        for language in ("pl", "en"):
            strings = translation_strings(language)
            cache = {"component.skzp_control.common.firmware_version":
                     strings["common"]["firmware_version"]}
            for suffix, version in (
                ("_V5.71_2026-10-04", "5.71"),
                ("_V5.71", "5.71"),
                ("_V5.05", "5.05"),
                ("", None),
                ("_Vunknown", None),
            ):
                with self.subTest(language=language, suffix=suffix):
                    flow = self.create_flow(initialized=False)
                    flow.hass.config = SimpleNamespace(language=language)
                    flow.async_set_unique_id = AsyncMock()
                    flow._abort_if_unique_id_configured = Mock()
                    self.env["_async_get_device_info"] = read = AsyncMock(
                        return_value=("SKZP-05S", {"DevType": "SKZP-05S" + suffix}))
                    load_translations.reset_mock()
                    with patch.object(localization, "async_get_cached_translations", return_value=cache):
                        result = await flow.async_step_user({"host": "controller", "port": "80"})
                    self.assertEqual(result["step_id"], "selection_method")
                    self.assertEqual(result["menu_options"], ["quick", "advanced"])
                    placeholders = result["description_placeholders"]
                    firmware = ("  \n" + strings["common"]["firmware_version"].format(version=version)
                                if version else "")
                    self.assertEqual(placeholders, {"model": "SKZP-05S", "firmware": firmware})
                    description = strings["config"]["step"]["selection_method"]["description"].format(**placeholders)
                    self.assertIn("SKZP-05S.", description)
                    self.assertNotIn("2026-10-04", description)
                    self.assertTrue(description.endswith("Wybierz sposób konfiguracji." if language == "pl"
                                                         else "Choose a setup method."))
                    if version:
                        load_translations.assert_awaited_once_with(flow.hass, language, "common", {"skzp_control"})
                    else:
                        load_translations.assert_not_awaited()
                        self.assertEqual(description.count("\n"), 2)
                    read.assert_awaited_once_with("controller", 80)
                    self.assertEqual(flow._connection_data,
                                     {"host": "controller", "port": 80, "device_model": "SKZP-05S"})

    async def test_options_direct_steps_load_state_once(self):
        for step in ("ch1", "settings", "select_all", "deselect_all"):
            with self.subTest(step=step):
                flow = self.create_flow(options=True, initialized=False)
                flow.config_entry.options = {"disabled_entities": ["sensor:outside"],
                                             "boiler_temp_max": 90}
                flow._async_get_device_context = AsyncMock(return_value=("SKZP-05S", {}))
                self.env["build_entity_choices"] = Mock(return_value=self.choices)
                await getattr(flow, "async_step_" + step)()
                await getattr(flow, "async_step_" + step)()
                flow._async_get_device_context.assert_awaited_once()
                self.assertEqual(flow._advanced_options["boiler_temp_max"], 90)
                self.assertIn("sensor:outside", flow._disabled_entities)

    async def test_finish_keeps_sorted_disabled_ids_and_options_sync(self):
        self.env["_sync_entity_registry_selection"] = sync = Mock()
        for options in (False, True):
            flow = self.create_flow(options)
            flow._disabled_entities = {"sensor:z", "sensor:a"}
            result = await flow.async_step_finish()
            saved = result["data"] if options else result["options"]
            self.assertEqual(saved["disabled_entities"], ["sensor:a", "sensor:z"])
            self.assertEqual(saved["known_entities"], ["sensor:K"])
        sync.assert_called_once()

    async def test_restore_uses_fresh_client_data_and_preserves_selection_on_errors(self):
        flow = self.create_flow(options=True)
        self.env["recommended_disabled_entities"] = recommend = Mock(return_value={"sensor:K"})
        flow.hass.data = {"skzp_control": {"entry": SimpleNamespace(model="SKZP-05S", data={"x": 1})}}
        self.env["_async_get_device_info"] = read = AsyncMock()
        result = await flow.async_step_restore_recommended()
        self.assertEqual(result["step_id"], "entities")
        recommend.assert_called_once_with(self.choices, {"x": 1})
        read.assert_not_awaited()
        flow.hass.data = {}
        for error in (self.module.CannotConnectError(), self.module.CannotReadDeviceError(),
                      self.module.UnsupportedDeviceError("unknown", {})):
            read.side_effect = error
            result = await flow.async_step_restore_recommended()
            self.assertEqual(result["step_id"], "entities")
            self.assertEqual(flow._disabled_entities, {"sensor:K", "sensor:outside"})
        self.assertEqual(recommend.call_count, 1)

    async def test_new_entity_is_unselected_until_saved_explicitly(self):
        flow = self.create_flow(options=True, initialized=False)
        self.entry.options = {"known_entities": ["sensor:outside"],
                              "disabled_entities": ["sensor:outside"]}
        flow._async_get_device_context = AsyncMock(return_value=("SKZP-05S", {}))
        self.env["build_entity_choices"] = Mock(return_value=self.choices)
        self.env["_sync_entity_registry_selection"] = Mock()
        await flow.async_step_init()
        self.assertEqual(flow._disabled_entities, {"sensor:outside", "sensor:K"})
        shown = await flow.async_step_ch1()
        groups = next(iter(shown["data_schema"].schema.values()))[0].schema
        self.assertEqual(next(iter(groups)).options["default"], [])
        saved = (await flow.async_step_finish())["data"]
        self.assertEqual(saved["known_entities"], ["sensor:K", "sensor:outside"])
        self.assertIn("sensor:K", saved["disabled_entities"])
        await flow.async_step_ch1({"sensors": {"sample": ["k"]}})
        saved = (await flow.async_step_finish())["data"]
        self.assertEqual(saved["disabled_entities"], ["sensor:outside"])

    async def test_shared_labels_do_not_link_selection_between_groups(self):
        sensors = definition_modules()["skzp_control.sensors_map"].SENSOR_MAP
        cases = (
            ("boiler_burner", (
                ("select", "K006", "boiler_mode", "configuration", "boiler"),
                ("select", "BuMode", "burner_mode", "configuration", "burner"),
            )),
            ("dhw_buffer", (
                ("sensor", "D203", "d203", "sensors", "buffer"),
                ("number", "D203", "buffer_temp_setpoint", "configuration", "buffer"),
                ("number", "DHWTempCmd", "dhw_temp_setpoint", "configuration", "dhw"),
            )),
            ("dhw_buffer", (
                ("binary_sensor", "DevStatus_outBuffer", "devstatus_outbuffer", "sensors", "buffer"),
                ("binary_sensor", "DevStatus_outDHW", "devstatus_outdhw", "sensors", "dhw"),
                ("binary_sensor", "DevStatus_outCirc", "devstatus_outcirc", "sensors", "dhwc"),
            )),
            ("ch1", (
                ("sensor", "C007", "c007", "sensors", "circuit"),
                ("sensor", "C009", "ch1_room_target_temp", "sensors", "room"),
            )),
        )
        for options in (False, True):
            for page, descriptions in cases:
                with self.subTest(options=options, page=page, keys=[item[1] for item in descriptions]):
                    flow = self.create_flow(options)
                    choices = [entity_layout.EntityChoice(
                        platform, key, suffix, page, section, field, (0, index),
                        translation_key=sensors[key].get("translation_key") if platform == "sensor" else None)
                        for index, (platform, key, suffix, section, field) in enumerate(descriptions)]
                    flow._choices = choices
                    self.assertEqual(len({choice.option_id for choice in choices}), 1)
                    for selected in (choices[0], choices[-1]):
                        await getattr(flow, "async_step_" + page)({
                            selected.section: {selected.field: [selected.option_id]}})
                        self.assertEqual(flow._disabled_entities, {"sensor:outside"} | {
                            choice.selection_id for choice in choices if choice is not selected})
                        screen = await getattr(flow, "async_step_" + page)()
                        defaults = {
                            (section.name, field.name): field.options["default"]
                            for section, (group, _options) in screen["data_schema"].schema.items()
                            for field in group.schema
                        }
                        for choice in choices:
                            self.assertEqual(defaults[choice.section, choice.field],
                                             [choice.option_id] if choice is selected else [])

    async def test_offline_settings_do_not_reset_selection(self):
        self.env["_sync_entity_registry_selection"] = Mock()
        self.env["build_entity_choices"] = Mock(return_value=[])
        for known in (None, ["sensor:K"]):
            flow = self.create_flow(options=True, initialized=False)
            self.entry.options = {"disabled_entities": ["sensor:outside"]}
            if known is not None:
                self.entry.options["known_entities"] = known
            flow._async_get_device_context = AsyncMock(return_value=("SKZP-05S", None))
            saved = (await flow.async_step_finish())["data"]
            self.assertEqual(saved.get("known_entities"), known)
            self.assertEqual(saved["disabled_entities"], ["sensor:outside"])

    async def test_firmware_change_reloads_unchanged_options_only(self):
        self.env["_sync_entity_registry_selection"] = Mock()
        for firmware_changed, options_changed in ((False, False), (True, False), (True, True)):
            flow = self.create_flow(options=True)
            self.entry.options = entity_layout.entity_selection_options(
                flow._advanced_options, self.choices, flow._disabled_entities
            )
            flow.hass.config_entries = Mock()
            flow.hass.data = {"skzp_control": {"entry": SimpleNamespace(
                requires_platform_reload=firmware_changed)}}
            if options_changed:
                flow._advanced_options["boiler_temp_max"] = 90
            await flow.async_step_finish()
            if firmware_changed and not options_changed:
                flow.hass.config_entries.async_schedule_reload.assert_called_once_with("entry")
            else:
                flow.hass.config_entries.async_schedule_reload.assert_not_called()

    async def test_firmware_upgrade_keeps_new_buffer_numbers_unselected(self):
        flow = self.create_flow(options=True, initialized=False)
        self.entry.options = {"known_entities": ["sensor:D203", "sensor:D204"],
                              "disabled_entities": []}
        self.env["build_entity_choices"] = Mock(return_value=[
            entity_layout.EntityChoice("number", key, suffix, "dhw_buffer",
                "configuration", "buffer", (0, index))
            for index, (key, suffix) in enumerate((
                ("D203", "buffer_temp_setpoint"), ("D204", "buffer_hysteresis")))
        ])
        flow.hass.data = {"skzp_control": {"entry": SimpleNamespace(
            model="SKZP-05S", data={"DevType": "SKZP-05S_V5.71_2026-10-04"})}}
        await flow.async_step_init()
        self.assertEqual(flow._disabled_entities, {"number:D203", "number:D204"})

    async def test_restore_falls_back_to_tcp_for_empty_client(self):
        flow = self.create_flow(options=True)
        flow.hass.data = {"skzp_control": {"entry": SimpleNamespace(model="SKZP-05S", data={})}}
        self.env["_async_get_device_info"] = read = AsyncMock(return_value=("SKZP-05S", {"x": 2}))
        self.env["recommended_disabled_entities"] = recommend = Mock(return_value=set())
        await flow.async_step_restore_recommended()
        read.assert_awaited_once_with("controller", 80)
        recommend.assert_called_once_with(self.choices, {"x": 2})

    async def test_quick_and_restore_use_copies_of_recommendations(self):
        flow = self.create_flow()
        flow._recommended_disabled = {"sensor:K"}
        result = await flow.async_step_quick()
        self.assertEqual(result["options"]["disabled_entities"], ["sensor:K"])
        flow._disabled_entities.clear()
        await flow.async_step_restore_recommended()
        self.assertEqual(flow._disabled_entities, {"sensor:K"})
        self.assertIsNot(flow._disabled_entities, flow._recommended_disabled)

    async def test_connection_forms_and_unchanged_reconfigure(self):
        flow = self.create_flow()
        flow._get_reconfigure_entry = Mock(return_value=self.entry)
        for step, data in (("user", None), ("reconfigure", None),
                           ("user", {"host": "new", "port": "bad"})):
            result = await getattr(flow, "async_step_" + step)(data)
            fields = {field.name: selector for field, selector in result["data_schema"].schema.items()}
            self.assertEqual(set(fields), {"host", "port"})
            self.assertEqual(fields["port"].config, {"type": "text", "autocomplete": "off"})
            self.assertTrue(all("default" not in field.options for field in result["data_schema"].schema))
            if step == "reconfigure":
                self.assertEqual(flow.suggested_values["port"], "80")
            elif data is None:
                self.assertEqual(flow.suggested_values, {})
            if data:
                self.assertEqual(result["errors"], {"port": "invalid_port"})
                self.assertEqual(flow.suggested_values, data)
        self.env["_async_get_device_info"] = read = AsyncMock()
        flow.async_update_reload_and_abort = Mock(return_value={"type": "abort"})
        await flow.async_step_reconfigure({"host": "controller", "port": "80"})
        read.assert_not_awaited()
        self.assertFalse(flow.async_update_reload_and_abort.call_args.kwargs["reload_even_if_entry_is_unchanged"])
        for language in ("pl", "en"):
            strings = translation_strings(language)["config"]
            user = strings["step"]["user"]
            self.assertEqual(user["data_description"], strings["step"]["reconfigure"]["data_description"])
            self.assertNotIn("192.168.1.100", str(user))
            self.assertEqual(strings["error"]["invalid_host"],
                             "Wprowadź poprawny adres IP lub nazwę hosta." if language == "pl"
                             else "Enter a valid IP address or hostname.")

    def test_host_format_accepts_ip_addresses_and_dns_names(self):
        valid = (
            "192.168.1.100", "172.25.25.10", "127.0.0.1", "0.0.0.0",
            "::1", "2001:db8::1", "fe80::1%eth0", "::ffff:192.0.2.1",
            "controller", "skzp.local", "SKZP-05S.local", "skzp.local.",
            "żółć.local", "xn--kda4b0koi.local", "a" * 63 + ".local",
            ".".join(["a" * 63] * 3 + ["b" * 61]),
        )
        invalid = (
            "", " ", "skzp local", "skzp\nlocal", "skzp\x00.local",
            "192.168.1.999", "192.168.1", "192.168.001.1", "123",
            "http://192.168.1.1", "https://skzp.local", "skzp.local/path",
            "192.168.1.1:1180", "skzp.local:1180", "192.168.1.1/24",
            "[::1]", "2001:db8::invalid", "::1%", "skzp@local",
            "-skzp.local", "skzp-.local", "skzp_local", ".local", "skzp..local", "skzp.local..",
            "a" * 64 + ".local", ".".join(["a" * 63] * 4),
        )
        for host in valid:
            with self.subTest(host=host):
                self.module._validate_host(host)
        for host in invalid:
            with self.subTest(host=host), self.assertRaises(ValueError):
                self.module._validate_host(host)

    async def test_invalid_connection_fields_do_not_connect_or_change_entry(self):
        original = deepcopy(self.entry.data)
        for step in ("user", "reconfigure"):
            for host, port, expected in (
                ("192.168.1.999", "80", {"host": "invalid_host"}),
                ("http://skzp.local", "1180", {"host": "invalid_host"}),
                ("", "80", {"host": "invalid_host"}),
                ("skzp.local", "0", {"port": "invalid_port"}),
                ("skzp.local", "65536", {"port": "invalid_port"}),
                ("skzp.local", "-80", {"port": "invalid_port"}),
                ("192.168.1.999", "bad", {"host": "invalid_host", "port": "invalid_port"}),
            ):
                with self.subTest(step=step, host=host, port=port):
                    flow = self.create_flow()
                    flow._get_reconfigure_entry = Mock(return_value=self.entry)
                    flow.async_update_reload_and_abort = Mock()
                    flow.async_set_unique_id = AsyncMock()
                    self.env["_async_get_device_info"] = read = AsyncMock()
                    self.env["_async_get_device_model"] = read_model = AsyncMock()
                    data = {"host": host, "port": port}
                    result = await getattr(flow, "async_step_" + step)(data)
                    self.assertEqual(result["step_id"], step)
                    self.assertEqual(result["errors"], expected)
                    self.assertEqual(flow.suggested_values, data)
                    read.assert_not_awaited()
                    read_model.assert_not_awaited()
                    flow.async_set_unique_id.assert_not_awaited()
                    flow.async_update_reload_and_abort.assert_not_called()
                    self.assertEqual(self.entry.data, original)

    async def test_valid_hosts_are_trimmed_and_reach_connection_check(self):
        for step in ("user", "reconfigure"):
            for host in ("192.168.1.100", "::1", "fe80::1%eth0", "skzp.local", "żółć.local"):
                with self.subTest(step=step, host=host):
                    flow = self.create_flow()
                    flow._get_reconfigure_entry = Mock(return_value=self.entry)
                    self.env["_async_get_device_info"] = read = AsyncMock(
                        side_effect=self.module.CannotConnectError())
                    self.env["_async_get_device_model"] = read_model = AsyncMock(
                        side_effect=self.module.CannotConnectError())
                    result = await getattr(flow, "async_step_" + step)(
                        {"host": "  " + host + "  ", "port": " 1180 "})
                    self.assertEqual(result["errors"], {"base": "cannot_connect"})
                    if step == "user":
                        read.assert_awaited_once_with(host, 1180)
                        read_model.assert_not_awaited()
                    else:
                        read_model.assert_awaited_once_with(host, 1180)
                        read.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()


class SetupParserTests(unittest.IsolatedAsyncioTestCase):
    async def run_setup(self, chunks, limit=65536):
        class CannotRead(Exception): pass
        env = dict(asyncio=asyncio, FrameParser=FrameParser, MAX_FRAME_BYTES=limit,
            CONNECTION_TIMEOUT_SECONDS=1, DEVICE_DATA_TIMEOUT_SECONDS=1,
            _LOGGER=logging.getLogger('test'), format_communication_error=str,
            CannotConnectError=ConnectionError, CannotReadDeviceError=CannotRead,
            UnsupportedDeviceError=ValueError, detect_device_model=detect_device_model,
            SUPPORTED_DEVICE_MODELS=SUPPORTED_DEVICE_MODELS)
        fn = load_function('config_flow.py', '_async_get_device_info', env)
        reader = SimpleNamespace(read=AsyncMock(side_effect=chunks))
        writer = SimpleNamespace(close=lambda: None, wait_closed=AsyncMock())
        with patch('asyncio.open_connection', AsyncMock(return_value=(reader, writer))):
            try:
                return await fn('test', 80)
            except CannotRead:
                return 'cannot_read'
            finally:
                writer.wait_closed.assert_awaited_once()

    async def test_fragments_unicode_nested_and_multiple(self):
        raw = json.dumps({'FrameType':'SkzpData','DevType':'SKZP-05S', 'x':{'text':'żółć } {'}}, ensure_ascii=False).encode()
        model, data = await self.run_setup([b'{"Token":"test"}'] + [bytes([x]) for x in raw])
        self.assertEqual(model, 'SKZP-05S')
        self.assertEqual(data['x']['text'], 'żółć } {')
        self.assertEqual(data['Token'], 'test')

    async def test_invalid_and_large(self):
        self.assertEqual(await self.run_setup([b'{bad}']), 'cannot_read')
        self.assertEqual(await self.run_setup([b'{"x":"' + b'a' * 100], limit=32), 'cannot_read')

    async def test_eof_and_timeout(self):
        self.assertEqual(await self.run_setup([b'{"x":', b'']), 'cannot_read')
        self.assertEqual(await self.run_setup([TimeoutError()]), 'cannot_read')
