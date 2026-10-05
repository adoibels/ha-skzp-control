"""Testy zachowania wyboru encji po aktualizacji integracji."""

from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

import support
from skzp_control.const import CONF_DISABLED_ENTITIES, CONF_KNOWN_ENTITIES
from skzp_control.entity_layout import (
    EntityChoice,
    disabled_entity_ids,
    entity_selection_options,
    initialize_entity_selection,
    is_entity_enabled,
)


def choice(platform, key, suffix=None):
    return EntityChoice(platform, key, suffix or key.lower(), "ch1",
                        "configuration", "sample", (0, 0))


class EntitySelectionTests(TestCase):
    def test_upgrade_preserves_selection_and_blocks_new_entities(self):
        choices = [choice("number", "C027"), choice("switch", "C020"),
                   choice("number", "C030")]
        options = {CONF_KNOWN_ENTITIES: ["number:C027", "switch:C020"],
                   CONF_DISABLED_ENTITIES: ["switch:C020"]}
        entry = SimpleNamespace(options=options)
        self.assertTrue(is_entity_enabled(entry, "number", "C027"))
        self.assertFalse(is_entity_enabled(entry, "switch", "C020"))
        self.assertFalse(is_entity_enabled(entry, "number", "C030"))
        self.assertEqual(disabled_entity_ids(options, choices),
                         {"switch:C020", "number:C030"})
        self.assertNotIn("number:C030", options[CONF_KNOWN_ENTITIES])

    def test_saving_unchanged_then_enabling_new_entity(self):
        choices = [choice("number", "C030")]
        options = {CONF_KNOWN_ENTITIES: [], "number_send_delay": 2}
        saved = entity_selection_options(
            options, choices, disabled_entity_ids(options, choices)
        )
        entry = SimpleNamespace(options=saved)
        self.assertFalse(is_entity_enabled(entry, "number", "C030"))
        self.assertEqual(saved[CONF_KNOWN_ENTITIES], ["number:C030"])
        self.assertEqual(saved["number_send_delay"], 2)
        entry.options = entity_selection_options(saved, choices, set())
        self.assertTrue(is_entity_enabled(entry, "number", "C030"))

    def test_temporarily_missing_entities_keep_their_choices(self):
        options = {CONF_KNOWN_ENTITIES: ["sensor:D203", "sensor:D204"],
                   CONF_DISABLED_ENTITIES: ["sensor:D204"]}
        saved = entity_selection_options(options, [], disabled_entity_ids(options, []))
        self.assertEqual(saved, options)
        entry = SimpleNamespace(options=saved)
        self.assertTrue(is_entity_enabled(entry, "sensor", "D203"))
        self.assertFalse(is_entity_enabled(entry, "sensor", "D204"))
        self.assertFalse(is_entity_enabled(entry, "number", "D203"))

    def test_first_configuration_uses_explicit_choice(self):
        choices = [choice("number", "C030"), choice("switch", "C031")]
        options = entity_selection_options({}, choices, {"switch:C031"})
        entry = SimpleNamespace(options=options)
        self.assertTrue(is_entity_enabled(entry, "number", "C030"))
        self.assertFalse(is_entity_enabled(entry, "switch", "C031"))

    def test_initial_selection_uses_registry_and_runs_once(self):
        choices = [choice("number", "C027"), choice("switch", "C020"),
                   choice("number", "C030", "ch1_return_temp_min"),
                   choice("switch", "C031", "ch1_return_protection")]
        entry = SimpleNamespace(entry_id="entry", options={
            CONF_DISABLED_ENTITIES: ["switch:C020"], "number_send_delay": 2,
        })
        registry = Mock()
        registry.async_get_entity_id.side_effect = lambda domain, integration, unique_id: (
            "existing.entity" if unique_id in {"entry_c027", "entry_c020"} else None
        )
        hass = SimpleNamespace(config_entries=Mock())
        hass.config_entries.async_update_entry.side_effect = (
            lambda target, options: setattr(target, "options", options)
        )
        with patch("skzp_control.entity_layout.er.async_get", return_value=registry,
                   create=True):
            initialize_entity_selection(hass, entry, choices)
            initialize_entity_selection(hass, entry, choices)
        hass.config_entries.async_update_entry.assert_called_once()
        self.assertTrue(is_entity_enabled(entry, "number", "C027"))
        self.assertFalse(is_entity_enabled(entry, "switch", "C020"))
        self.assertFalse(is_entity_enabled(entry, "number", "C030"))
        self.assertFalse(is_entity_enabled(entry, "switch", "C031"))
        self.assertEqual(entry.options["number_send_delay"], 2)
        registry.async_update_entity.assert_not_called()
        registry.async_remove.assert_not_called()

    def test_missing_snapshot_does_not_implicitly_enable_parameters(self):
        entry = SimpleNamespace(options={})
        for platform, key in (("number", "C030"), ("number", "CH1ReturnTempCmd"),
                              ("switch", "C031"), ("switch", "CH1ReturnProtAct")):
            self.assertFalse(is_entity_enabled(entry, platform, key))
        self.assertFalse(is_entity_enabled(entry, "number", "CH1MixTempBase"))
