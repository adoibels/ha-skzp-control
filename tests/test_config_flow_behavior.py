"""Testy formularza z lekkimi atrapami interfejsu Home Assistanta.

Uruchamia klasy formularza i funkcje pomocnicze razem, zachowując dziedziczenie.
Podmienia tylko importy na granicy HA i integracji; nie wydziela metod.
"""

import ast
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock

import support
from skzp_control import const, device, entity_layout, entity_support
from skzp_control import parameter_resolver


FLOW_SOURCE = support.ROOT / "config_flow.py"
ENTITY_SELECTION_SOURCE = support.ROOT / "config_entity_selection.py"


class Field:
    def __init__(self, name, **kwargs):
        self.name = name
        self.options = kwargs


class Schema:
    def __init__(self, schema):
        self.schema = schema


class Selector:
    def __init__(self, config=None):
        self.config = config or {}


class FlowBoundary:
    def __init_subclass__(cls, **kwargs):
        super().__init_subclass__()

    def async_show_form(self, **kwargs):
        return {"type": "form", **kwargs}

    def async_show_menu(self, **kwargs):
        return {"type": "menu", **kwargs}

    def async_abort(self, **kwargs):
        return {"type": "abort", **kwargs}

    def async_create_entry(self, **kwargs):
        return {"type": "create_entry", **kwargs}

    def add_suggested_values_to_schema(self, schema, values):
        self.suggested_values = values
        return schema


def load_flow(path: Path = None):
    """Ładuje cały formularz, zachowując importy biblioteki standardowej."""
    tree = ast.parse((path or FLOW_SOURCE).read_text(encoding="utf-8-sig"))
    tree.body = [
        node for node in tree.body
        if not (
            isinstance(node, ast.ImportFrom)
            and (node.level or (node.module or "").startswith("homeassistant"))
            or isinstance(node, ast.Import)
            and any(alias.name == "voluptuous" for alias in node.names)
        )
    ]
    env = {"__name__": __name__}
    for module in (const, device, entity_layout, entity_support, parameter_resolver):
        env.update({key: value for key, value in vars(module).items()
                    if not key.startswith("_")})
    env["existing_buffer_sensor_keys"] = lambda *_args: frozenset()
    env.update(
        config_entries=SimpleNamespace(ConfigFlow=FlowBoundary,
                                       OptionsFlowWithReload=FlowBoundary,
                                       ConfigEntry=object, OptionsFlow=object),
        vol=SimpleNamespace(Schema=Schema, Required=Field, Optional=Field),
        SensorDeviceClass=SimpleNamespace(TEMPERATURE="temperature"),
        CONF_HOST="host", CONF_PORT="port", HomeAssistant=object,
        callback=lambda function: function,
        section=lambda schema, options: (schema, options),
        NumberSelectorMode=SimpleNamespace(SLIDER="slider"),
        SelectSelectorMode=SimpleNamespace(LIST="list"),
        TextSelectorType=SimpleNamespace(TEXT="text"),
    )
    for name in ("Number", "Select", "Text"):
        env[name + "Selector"] = type(name + "Selector", (Selector,), {})
        env[name + "SelectorConfig"] = dict
    env["BooleanSelector"] = type("BooleanSelector", (Selector,), {})
    selection_tree = ast.parse(
        ENTITY_SELECTION_SOURCE.read_text(encoding="utf-8-sig")
    )
    selection_nodes = [
        node
        for node in selection_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    future = ast.ImportFrom(
        module="__future__",
        names=[ast.alias(name="annotations")],
        level=0,
    )
    exec(
        compile(
            ast.fix_missing_locations(
                ast.Module(body=[future, *selection_nodes], type_ignores=[])
            ),
            str(ENTITY_SELECTION_SOURCE),
            "exec",
        ),
        env,
    )
    env["PAGE_DHW_ONLY"] = "dhw"
    env["recommended_disabled_entities"] = lambda *_args: set()
    exec(compile(tree, str(path or FLOW_SOURCE), "exec"), env)
    return SimpleNamespace(**env), env


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
                    self.assertEqual(result["step_id"], step)
                    self.assertEqual(result["type"], "form")
                    result = await getattr(flow, "async_step_" + step)({})
                    self.assertEqual(result["step_id"], "init" if options else "entities")
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
            await flow.async_step_ch1({entity_layout.SECTION_SENSORS: {"sample": ["sensor_k"]}})
            self.assertEqual(flow._disabled_entities, {"sensor:outside"})
            await flow.async_step_deselect_all()
            self.assertEqual(flow._disabled_entities, {"sensor:K", "sensor:outside"})
            await flow.async_step_select_all()
            self.assertEqual(flow._disabled_entities, {"sensor:outside"})

    async def test_settings_show_and_partial_save_in_both_flows(self):
        for options in (False, True):
            flow = self.create_flow(options)
            shown = await flow.async_step_settings()
            self.assertEqual(shown["step_id"], "settings")
            self.assertEqual(len(shown["data_schema"].schema), 9)
            flow._advanced_options["notify_connection_lost"] = False
            result = await flow.async_step_settings({"boiler_temp_max": 90})
            self.assertEqual(result["step_id"], "init" if options else "entities")
            self.assertEqual(flow._advanced_options["boiler_temp_max"], 90)
            self.assertIs(flow._advanced_options["notify_connection_lost"], False)

    async def test_config_requires_device_for_page_and_settings(self):
        for step in ("ch1", "settings"):
            flow = self.create_flow(initialized=False)
            self.assertEqual(await getattr(flow, "async_step_" + step)(),
                             {"type": "abort", "reason": "cannot_read_device"})

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
        sync.assert_called_once()

    async def test_restore_uses_fresh_client_data_and_preserves_selection_on_errors(self):
        flow = self.create_flow(options=True)
        self.env["recommended_disabled_entities"] = recommend = Mock(return_value={"sensor:K"})
        flow.hass.data = {"skzp_control": {"entry": SimpleNamespace(model="SKZP-05S", data={"x": 1})}}
        self.env["_async_get_device_info"] = read = AsyncMock()
        await flow.async_step_restore_recommended()
        recommend.assert_called_once_with(self.choices, {"x": 1})
        read.assert_not_awaited()
        flow.hass.data = {}
        for error in (self.module.CannotConnectError(), self.module.CannotReadDeviceError(),
                      self.module.UnsupportedDeviceError("unknown", {})):
            read.side_effect = error
            await flow.async_step_restore_recommended()
            self.assertEqual(flow._disabled_entities, {"sensor:K"})
        self.assertEqual(recommend.call_count, 1)

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
            if step == "reconfigure":
                self.assertEqual(flow.suggested_values["port"], "80")
            if data:
                self.assertEqual(result["errors"], {"port": "invalid_port"})
                self.assertEqual(flow.suggested_values, data)
        self.env["_async_get_device_info"] = read = AsyncMock()
        flow.async_update_reload_and_abort = Mock(return_value={"type": "abort"})
        await flow.async_step_reconfigure({"host": "controller", "port": "80"})
        read.assert_not_awaited()
        self.assertFalse(flow.async_update_reload_and_abort.call_args.kwargs["reload_even_if_entry_is_unchanged"])


if __name__ == "__main__":
    unittest.main()
