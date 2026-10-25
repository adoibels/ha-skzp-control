"""Wspólne atrapy, dane i funkcje ładowania używane przez testy."""

import ast
import asyncio
import json
import re
import sys
import types
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import support
from support import ROOT
from skzp_control import const, device, entity_layout, entity_support, parameter_resolver, profiles, value_decoder
from skzp_control.client.client import SkzpClient
from skzp_control.client.command_manager import CommandManager
from skzp_control.coordinator import SkzpCoordinator
from skzp_control.parameter_resolver import get_parameter_value
from skzp_control.localization import entity_translation

FLOW_SOURCE = ROOT / "config_flow.py"
ENTITY_SELECTION_SOURCE = ROOT / "config_entity_selection.py"


def translation_strings(language):
    """Nakłada tłumaczenie na angielską bazę, tak jak pamięć tłumaczeń HA."""
    def overlay(base, localized):
        for key, value in localized.items():
            if isinstance(value, dict):
                overlay(base.setdefault(key, {}), value)
            else:
                base[key] = value
        return base

    english = json.loads((ROOT / "translations/en.json").read_text(encoding="utf-8"))
    if language == "en":
        return english
    localized = json.loads((ROOT / f"translations/{language}.json").read_text(encoding="utf-8"))
    return overlay(english, localized)


def load_function(filename, name, env):
    tree = ast.parse((ROOT / filename).read_text(encoding='utf-8-sig'))
    node = next(n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    future = ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0)
    exec(compile(ast.fix_missing_locations(ast.Module(body=[future, node], type_ignores=[])), filename, 'exec'), env)
    return env[name]


def load_functions(filename, names, env):
    tree=ast.parse((ROOT/filename).read_text(encoding='utf-8-sig'))
    nodes=[n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name in names]
    future=ast.ImportFrom(module='__future__',names=[ast.alias(name='annotations')],level=0)
    module=ast.fix_missing_locations(ast.Module(body=[future,*nodes],type_ignores=[]))
    exec(compile(module,filename,'exec'),env)


def load_definitions():
    tree = ast.parse((ROOT / "number.py").read_text(encoding="utf-8-sig"))
    functions = {"_temperature_number", "_number", "_build_circuit_numbers"}
    constants = {"_CIRCUIT_NUMBER_TEMPLATES", "NUMBER_DESCRIPTIONS"}
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
    env["initialize_entity_selection"] = Mock()
    env["ProfileFlowMixin"] = type("ProfileFlowMixin", (), {})
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


def definition_modules():
    """Pomija wyłącznie klasy encji HA, zachowując pełne definicje platform."""
    result = {}
    for name in ("number", "select", "switch", "sensors_map"):
        tree = ast.parse((support.ROOT / f"{name}.py").read_text(encoding="utf-8"))
        tree.body = [n for n in tree.body if not (
            isinstance(n, ast.ImportFrom) and (n.level or (n.module or "").startswith("homeassistant"))
            or isinstance(n, ast.ClassDef) and n.name.startswith("Skzp")
        )]
        module = types.ModuleType(f"skzp_control.{name}")
        for dependency in (const, device, parameter_resolver, value_decoder):
            module.__dict__.update({k: v for k, v in vars(dependency).items() if not k.startswith("_")})
        if name == "sensors_map":
            module.__dict__.update(
                SensorDeviceClass=SimpleNamespace(ENUM="enum", TEMPERATURE="temperature", WEIGHT="weight"),
                SensorStateClass=SimpleNamespace(MEASUREMENT="measurement", TOTAL_INCREASING="total_increasing"),
                PERCENTAGE="%", UnitOfMass=SimpleNamespace(KILOGRAMS="kg"),
                UnitOfTemperature=SimpleNamespace(CELSIUS="°C"))
        with patch.dict(sys.modules, {module.__name__: module}):
            exec(compile(tree, name, "exec"), module.__dict__)
        result[module.__name__] = module
    return result


class Bus:
    def __init__(self):
        self.listeners = []

    def async_listen(self, event, handler):
        self.listeners.append(handler)
        return lambda: self.listeners.remove(handler)

    def fire(self):
        for handler in list(self.listeners):
            handler(None)


def client(data=None):
    return SimpleNamespace(
        data={"DevType": "SKZP-05S_V5.71", "BoilerTempCmd": "6000",
              "C030": "3000", "C031": "0", "D200": "0", **(data or {})},
        model="SKZP-05S", available=True, host="test", port=1,
        hass=SimpleNamespace(bus=Bus()), update_event="update",
        command_manager=CommandManager(), command_retry_count=1,
        command_retry_delay=0, command_confirmation_timeout=0.005,
        log_command_sent=Mock(), translate=lambda key, **kwargs: key,
    )


class ProfileFixture:
    """Przygotowuje rzeczywiste definicje i atrapę klienta."""

    def setUp(self):
        self.patch = patch.dict(sys.modules, definition_modules())
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.client = client()

    def profile(self, params):
        return profiles.create_profile(self.client, "Próba", params)


class MemoryStore:
    data = {}

    def __init__(self, hass, version, key):
        self.key = key

    async def async_load(self):
        return self.data.get(self.key)

    async def async_save(self, value):
        self.data[self.key] = dict(value)


def load_profile_flow():
    tree = ast.parse((support.ROOT / "profile_flow.py").read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and (node.level or (node.module or "").startswith("homeassistant"))
        or isinstance(node, ast.Import) and any(a.name == "voluptuous" for a in node.names))]
    def translated(hass, key, **kwargs):
        if key.startswith("entity."):
            parts = key.split(".", 3)
            parts[2], placeholders = entity_translation(parts[2])
            kwargs = {**placeholders, **kwargs}
            key = ".".join(parts)
        strings = translation_strings("pl")
        value = strings
        for part in key.split("."):
            if not isinstance(value, dict) or part not in value:
                return key
            value = value[part]
        return value.format(**kwargs) if kwargs else value

    env = {key: value for key, value in vars(profiles).items() if not key.startswith("_")}
    env.update(__name__=__name__, __package__="skzp_control", DOMAIN="skzp_control", Store=MemoryStore,
               translate=translated, PAGE_ORDER=entity_layout.PAGE_ORDER,
               entity_location=entity_layout.entity_location,
               section=lambda schema, options: SimpleNamespace(schema=schema.schema, options=options),
               callback=lambda f: f, get_parameter_value=get_parameter_value,
               vol=SimpleNamespace(Schema=Schema, Required=Field, Optional=Field),
               FileSelector=Selector, FileSelectorConfig=dict,
               TextSelector=Selector, TextSelectorConfig=dict,
               NumberSelector=Selector, NumberSelectorConfig=dict,
               NumberSelectorMode=SimpleNamespace(BOX="box"),
               dt_util=SimpleNamespace(parse_datetime=datetime.fromisoformat,
                                       as_local=lambda value: value.astimezone(timezone.utc)),
               SelectSelector=Selector, SelectSelectorConfig=dict,
               SelectSelectorMode=SimpleNamespace(LIST="list", DROPDOWN="dropdown"),
               async_create_frame_download=Mock(return_value=("/signed/link", Mock())))
    env["profile_page_title"] = load_function(
        "localization.py", "profile_page_title", {"translate": translated, "re": re})
    exec(compile(tree, "profile_flow.py", "exec"), env)
    return env


class ProfileFlowFixture(ProfileFixture):
    """Przygotowuje formularz i udostępnia wspólne działania testowe."""

    def setUp(self):
        super().setUp()
        MemoryStore.data.clear()
        self.env = load_profile_flow()
        class Boundary(FlowBoundary):
            def async_show_progress(self, **kwargs):
                return {"type": "progress", **kwargs}
            def async_show_progress_done(self, **kwargs):
                return {"type": "progress_done", **kwargs}
            def async_remove(self):
                pass
        cls = type("Flow", (self.env["ProfileFlowMixin"], Boundary), {})
        self.flow = cls()
        self.client.send_command = AsyncMock()
        self.flow.hass = SimpleNamespace(data={"skzp_control": {"entry": self.client}},
            async_create_task=asyncio.create_task,
            async_add_executor_job=AsyncMock(side_effect=lambda fn, *args: fn(*args)))
        self.client.translate = lambda key, **kwargs: self.env["translate"](
            self.flow.hass, key, **{k: v for k, v in kwargs.items() if k != "language"})
        self.flow.config_entry = SimpleNamespace(entry_id="entry", options={})

    async def add_profile(self, params):
        await self.flow._profile_store().add(profiles.create_profile(self.client, "Test", params))
        return next(iter(await self.flow._profile_store().list()))

    async def pick_profile(self, profile_id):
        menu = await self.flow.async_step_profile_manage()
        stored = await self.flow._profile_store().list()
        ordered = sorted(stored, key=lambda key: stored[key]["_meta"]["name"].casefold())
        step = "profile_pick_" + str(ordered.index(profile_id))
        self.assertIn(step, menu["menu_options"])
        return await getattr(self.flow, "async_step_" + step)()

    async def load_profile(self, profile_id):
        await self.pick_profile(profile_id)
        return await self.flow.async_step_profile_load()

    async def save_profile(self, values):
        await self.flow.async_step_profile_save_new()
        result = await self.flow.async_step_profile_name(values)
        if result["step_id"] == "profile_duplicate":
            result = await self.flow.async_step_profile_duplicate_continue()
        if result["step_id"] != "profile_settings":
            return result
        selected = list(self.flow._profile_source["params"])
        result = await self.flow.async_step_profile_settings(self.selection(selected))
        if result["step_id"] != "profile_create_confirm":
            return result
        return await self.flow.async_step_profile_write_commit()

    async def import_file(self):
        await self.flow.async_step_profile_import_new()
        return await self.flow.async_step_profile_file({"file": "id"})

    async def finish_import(self, values):
        result = await self.flow.async_step_profile_import_review(values)
        if result["step_id"] == "profile_duplicate":
            result = await self.flow.async_step_profile_duplicate_continue()
        await self.flow.async_step_profile_settings(
            self.selection(list(self.flow._profile_source["params"])))
        return await self.flow.async_step_profile_write_commit()

    @staticmethod
    def field(result, name):
        return next((key, value) for key, value in result["data_schema"].schema.items()
                    if key.name == name)

    def selection(self, keys):
        return {
            page: {field: key in keys for field, key in fields.items()}
            for page, fields in self.flow._profile_fields_by_page.items()
        }

    def defaults(self, result):
        return {
            self.flow._profile_fields_by_page[page.name][field.name]: field.options["default"]
            for page, group in result["data_schema"].schema.items()
            for field in group.schema
        }

    def assert_translated_screen(self, screen):
        """Sprawdza opisy i dostępność działań w obu tłumaczeniach."""
        for language in ("pl", "en"):
            with self.subTest(language=language, step=screen["step_id"]):
                strings = translation_strings(language)
                step = strings["options"]["step"][screen["step_id"]]
                placeholders = screen.get("description_placeholders", {})
                step["title"].format(**placeholders)
                if "description" in step:
                    step["description"].format(**placeholders)
                options = screen.get("menu_options", [])
                for option in options:
                    if isinstance(options, dict):
                        self.assertIsInstance(options[option], str)
                    else:
                        self.assertIn(option, step["menu_options"])
                    if option != "init":
                        self.assertTrue(callable(getattr(self.flow, "async_step_" + option)))

    def assert_profile_table(self, html):
        """Sprawdza wspólny układ tabel bez wiązania testu z kolejnością tagów."""
        structure = TableStructure()
        structure.feed(html)
        self.assertEqual(structure.maximum, 1)
        self.assertEqual(structure.depth, 0)
        self.assertTrue(structure.widths)
        self.assertEqual(structure.widths, ["100%"] * len(structure.widths))
        self.assertEqual(structure.columns, ["65%", "35%"] * len(structure.widths))
        self.assertTrue(structure.rows)
        self.assertEqual(structure.rows, [2] * len(structure.rows))
        self.assertEqual(structure.alignments, ["left"] * (2 * len(structure.widths)))
        self.assertNotIn("<details open", html)
        self.assertNotIn("<strong>", html)
        self.assertNotIn("<h3>", html)
        return structure


class TableStructure(HTMLParser):
    """Odczytuje strukturę tabel i poziomy rozwijanych sekcji."""

    def __init__(self):
        super().__init__()
        self.depth = self.maximum = self.cells = 0
        self.rows = []
        self.alignments = []
        self.widths = []
        self.columns = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "details":
            self.depth += 1
            self.maximum = max(self.maximum, self.depth)
        elif tag == "table":
            self.widths.append(attrs.get("width"))
        elif tag == "col":
            self.columns.append(attrs.get("width"))
        elif tag == "tr":
            self.cells = 0
        elif tag in ("th", "td"):
            self.cells += 1
            if tag == "th":
                self.alignments.append(attrs.get("align"))

    def handle_endtag(self, tag):
        if tag == "details":
            self.depth -= 1
        elif tag == "tr":
            self.rows.append(self.cells)


class Writer:
    def __init__(self, *, stall=False, fail_at=None, close_stall=False):
        self.reader = asyncio.StreamReader()
        self.transport = self
        self.frames = []
        self.active = self.closed = self.aborted = False
        self.stall, self.fail_at, self.close_stall = stall, fail_at, close_stall
        self.started = asyncio.Event()
    def is_closing(self): return self.closed or self.aborted
    def write(self, payload):
        assert not self.active, 'overlapping writes'
        self.active = True
        self.frames.append(payload)
    async def drain(self):
        self.started.set()
        try:
            if self.stall: await asyncio.Event().wait()
            await asyncio.sleep(0)
            if len(self.frames) == self.fail_at: raise OSError('secret must not be logged')
        finally: self.active = False
    def close(self): self.closed = True; self.reader.feed_eof()
    def abort(self): self.aborted = True; self.reader.feed_eof()
    async def wait_closed(self):
        if self.close_stall: await asyncio.Event().wait()

def attach(t, w): t._reader, t._writer = w.reader, w

def make_client(w, ha=False):
    if ha:
        c = SkzpCoordinator(SimpleNamespace(bus=SimpleNamespace(async_fire=lambda *_: None), config=SimpleNamespace(language='en')), 'host', 1, 'id', {})
        c._set_connected(True)
        c._data_ready_event.set()
        import time
        c._last_data_received = time.monotonic()
    else: c = SkzpClient('host', 1)
    attach(c.transport, w)
    c.transport.write_timeout = c.transport.close_timeout = .02
    c.data.update(DevId=1, DevPin=2, Token='secret')
    return c
