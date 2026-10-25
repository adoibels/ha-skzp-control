"""Testy tworzenia, aktualizacji, edycji i usuwania nastaw profili."""

from copy import deepcopy
import json
import unittest

import support
from helpers import ProfileFlowFixture, translation_strings
from skzp_control import profiles


class ProfileWorkflowTests(ProfileFlowFixture, unittest.IsolatedAsyncioTestCase):

    async def test_empty_store_skips_menus_and_existing_store_has_back(self):
        result = await self.flow.async_step_profile_save()
        self.assertEqual(result["step_id"], "profile_name")
        result = await self.flow.async_step_profile_import()
        self.assertEqual(result["step_id"], "profile_file")
        await self.add_profile({"BoilerTempCmd": "6500"})
        for step, expected in (
            ("profile_save", ["profile_save_new", "profile_save_update", "profiles"]),
            ("profile_import", ["profile_import_new", "profile_import_update", "profiles"]),
        ):
            result = await getattr(self.flow, "async_step_" + step)()
            self.assertEqual(result["type"], "menu")
            self.assertEqual(result["menu_options"], expected)
        self.client.send_command.assert_not_called()

    async def test_new_profile_saves_only_selected_snapshot_after_confirmation(self):
        view = await self.flow.async_step_profile_save()
        self.assertEqual(view["step_id"], "profile_name")
        view = await self.flow.async_step_profile_name({"name": "Nowy", "description": "Opis"})
        self.assertTrue(all(self.defaults(view).values()))
        self.assertFalse(await self.flow._profile_store().list())
        self.client.data["BoilerTempCmd"] = "7500"
        confirm = await self.flow.async_step_profile_write_select(self.selection(["BoilerTempCmd"]))
        self.assertEqual(confirm["step_id"], "profile_create_confirm")
        self.assertFalse(await self.flow._profile_store().list())
        back = await self.flow.async_step_profile_write_back()
        self.assertEqual(self.defaults(back)["BoilerTempCmd"], True)
        self.assertEqual(self.defaults(back)["C031"], False)
        await self.flow.async_step_profile_write_select(self.selection(["BoilerTempCmd"]))
        await self.flow.async_step_profile_write_commit()
        record = next(iter((await self.flow._profile_store().list()).values()))
        self.assertEqual(record["params"], {"BoilerTempCmd": "6000"})
        self.assertEqual(record["_meta"]["description"], "Opis")
        self.client.send_command.assert_not_called()

    async def test_controller_update_preserves_unchecked_and_metadata(self):
        record = profiles.create_profile(self.client, "Zima", {
            "BoilerTempCmd": "6500", "C030": "5000", "C031": "1", "Unsupported": "keep"})
        record["_meta"]["description"] = "Pełny opis"
        pid = await self.flow._profile_store().add(record)
        picker = await self.flow.async_step_profile_save_update()
        self.assertEqual(picker["step_id"], "profile_update_pick")
        self.assertIn("profile_save", picker["menu_options"])
        view = await self.flow.async_step_profile_pick_0()
        self.assertTrue(self.defaults(view)["BoilerTempCmd"])
        self.assertTrue(self.defaults(view)["C030"])
        self.assertIn("Sterownik:", view["description_placeholders"]["value_ch1_return_temp_min"])
        await self.flow.async_step_profile_write_select(self.selection(["BoilerTempCmd"]))
        await self.flow.async_step_profile_write_commit()
        records = await self.flow._profile_store().list()
        self.assertEqual(list(records), [pid])
        saved = records[pid]
        self.assertEqual(saved["params"], {**record["params"], "BoilerTempCmd": "6000"})
        self.assertEqual(saved["_meta"]["name"], "Zima")
        self.assertEqual(saved["_meta"]["description"], "Pełny opis")
        self.assertEqual(saved["_meta"]["date"], record["_meta"]["date"])
        self.assertIn("updated_at", saved["_meta"])
        menu = await self.flow.async_step_profile_menu()
        self.assertIn("Ostatnia aktualizacja:", menu["description_placeholders"]["updated"])
        self.client.send_command.assert_not_called()

    async def test_import_update_compares_profiles_and_preserves_missing(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000", "C030": "5000", "C031": "1"})
        self.client.data["BoilerTempCmd"] = "7000"
        self.env["_read_upload"] = lambda *_: profiles.create_profile(
            self.client, "Plik", {"BoilerTempCmd": "6000", "C031": "0", "Token": "secret"})
        await self.flow.async_step_profile_import_update()
        result = await self.flow.async_step_profile_file({"file": "id"})
        self.assertEqual(result["step_id"], "profile_update_pick")
        view = await self.flow.async_step_profile_pick_0()
        self.assertEqual(self.defaults(view), {"C031": True})
        self.assertNotIn("value_boiler_temp_setpoint", view["description_placeholders"])
        self.assertEqual(view["description_placeholders"]["value_ch1_return_protection"],
                         "Profil: **Włączony** · Plik: **Wyłączony**")
        self.assertIn("Plik:", view["description_placeholders"]["value_ch1_return_protection"])
        await self.flow.async_step_profile_write_select(self.selection(["C031"]))
        await self.flow.async_step_profile_write_commit()
        saved = (await self.flow._profile_store().list())[pid]
        self.assertEqual(saved["params"], {"BoilerTempCmd": "6000", "C030": "5000", "C031": "0"})
        self.assertEqual(saved["_meta"]["name"], "Test")
        self.client.send_command.assert_not_called()

    async def test_update_new_parameter_checked_and_alias_replaced_once(self):
        pid = await self.add_profile({"CH1ReturnTempCmd": "5000"})
        await self.flow.async_step_profile_save_update()
        view = await self.flow._profile_update_target(pid)
        self.assertTrue(self.defaults(view)["C031"])
        await self.flow.async_step_profile_write_select(self.selection(["C030", "C031"]))
        await self.flow.async_step_profile_write_commit()
        saved = (await self.flow._profile_store().list())[pid]
        self.assertEqual(saved["params"], {"C030": "3000", "C031": "0"})

    async def test_update_cancel_and_concurrent_change_do_not_overwrite(self):
        pid = await self.add_profile({"BoilerTempCmd": "6500", "C031": "1"})
        original = deepcopy((await self.flow._profile_store().list())[pid])
        await self.flow.async_step_profile_save_update()
        await self.flow._profile_update_target(pid)
        await self.flow.async_step_profile_write_cancel()
        self.assertEqual((await self.flow._profile_store().list())[pid], original)
        await self.flow.async_step_profile_save_update()
        await self.flow._profile_update_target(pid)
        result = await self.flow.async_step_profile_write_select(self.selection(["BoilerTempCmd"]))
        self.assertEqual(result["step_id"], "profile_update_confirm")
        store = self.flow._profile_store()
        snapshot = (await store.list())[pid]
        renamed = deepcopy(snapshot)
        renamed["_meta"]["name"] = "Zmieniony w innym oknie"
        await store.update(pid, renamed, snapshot)
        result = await self.flow.async_step_profile_write_commit()
        self.assertEqual(result["errors"]["base"], "profile_changed")
        saved = (await self.flow._profile_store().list())[pid]
        self.assertEqual(saved["params"], original["params"])
        self.assertEqual(saved["_meta"]["name"], "Zmieniony w innym oknie")
        self.client.send_command.assert_not_called()

    async def test_duplicate_name_warns_and_explicit_continue_creates_new(self):
        pid = await self.add_profile({"C031": "1"})
        await self.flow.async_step_profile_save_new()
        result = await self.flow.async_step_profile_name({"name": "test"})
        self.assertEqual(result["step_id"], "profile_duplicate")
        self.assertEqual(len(await self.flow._profile_store().list()), 1)
        await self.flow.async_step_profile_duplicate_continue()
        await self.flow.async_step_profile_write_select(self.selection(["C031"]))
        await self.flow.async_step_profile_write_commit()
        records = await self.flow._profile_store().list()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[pid]["params"], {"C031": "1"})
        self.client.send_command.assert_not_called()

    async def test_duplicate_metadata_rename_requires_confirmation(self):
        first = await self.flow._profile_store().add(
            profiles.create_profile(self.client, "Lato", {"C031": "1"}))
        second = await self.flow._profile_store().add(
            profiles.create_profile(self.client, "Zima", {"C031": "0"}))
        await self.pick_profile(second)
        result = await self.flow.async_step_profile_rename({"name": "Lato", "description": "nowy"})
        self.assertEqual(result["step_id"], "profile_duplicate")
        self.assertEqual((await self.flow._profile_store().list())[second]["_meta"]["name"], "Zima")
        await self.flow.async_step_profile_duplicate_continue()
        self.assertEqual((await self.flow._profile_store().list())[second]["_meta"]["name"], "Lato")
        self.assertEqual((await self.flow._profile_store().list())[first]["params"], {"C031": "1"})

    async def test_edit_numbers_modes_and_switches_only_changes_profile(self):
        self.client.data["K006"] = "1"
        pid = await self.add_profile({"BoilerTempCmd": "6000", "K006": "1", "C031": "0", "Unsupported": "keep"})
        await self.pick_profile(pid)
        view = await self.flow.async_step_profile_edit_settings()
        self.assertEqual(view["step_id"], "profile_settings")
        self.assertEqual(view["description_placeholders"]["form_title"], "Edytuj nastawy")
        self.assert_translated_screen(view)
        self.assertEqual(view["description_placeholders"]["value_boiler_temp_setpoint"],
                         "Zapisana wartość: **60 °C** · Zakres: **40–90 °C**")
        group = next(value for key, value in view["data_schema"].schema.items() if key.name == "boiler_burner")
        native = next(key.options["default"] for key in group.schema if key.name == "boiler_temp_setpoint")
        self.assertEqual(native, 60)
        result = await self.flow.async_step_profile_settings({
            "boiler_burner": {"boiler_temp_setpoint": 65, "boiler_mode": "timer"}, "ch1": {"ch1_return_protection": "on"}})
        self.assertEqual(result["step_id"], "profile_update_confirm")
        self.assertIn("Nastawy do aktualizacji (3)", result["description_placeholders"]["changes"])
        self.assertIn("<td>65 °C</td>", result["description_placeholders"]["changes"])
        self.assertNotIn("<td>60 °C</td>", result["description_placeholders"]["changes"])
        self.assertEqual((await self.flow._profile_store().list())[pid]["params"]["K006"], "1")
        back = await self.flow.async_step_profile_write_back()
        self.assertEqual(back["description_placeholders"]["value_boiler_temp_setpoint"],
                         "Zapisana wartość: **60 °C** · Zakres: **40–90 °C**")
        group = next(value for key, value in back["data_schema"].schema.items() if key.name == "boiler_burner")
        self.assertEqual(next(key.options["default"] for key in group.schema if key.name == "boiler_temp_setpoint"), 65)
        await self.flow.async_step_profile_edit_settings({
            "boiler_burner": {"boiler_temp_setpoint": 65, "boiler_mode": "timer"}, "ch1": {"ch1_return_protection": "on"}})
        await self.flow.async_step_profile_write_commit()
        saved = (await self.flow._profile_store().list())[pid]
        self.assertEqual(saved["params"], {"BoilerTempCmd": "6500", "K006": "2", "C031": "1", "Unsupported": "keep"})
        self.client.send_command.assert_not_called()

    async def test_edit_rejects_invalid_range_step_modes_and_pairs(self):
        self.client.data.update(C028="3000", C029="5000", K006="1")
        pid = await self.add_profile({"BoilerTempCmd": "6000", "C028": "3000", "C029": "5000", "K006": "1"})
        original = deepcopy((await self.flow._profile_store().list())[pid])
        for value in (91, 65.5, True, "NaN"):
            await self.pick_profile(pid)
            await self.flow.async_step_profile_edit_settings()
            result = await self.flow.async_step_profile_edit_settings({"boiler_burner": {"boiler_temp_setpoint": value}})
            self.assertIn(result["errors"]["base"], ("out_of_range", "invalid_value"))
        await self.pick_profile(pid)
        await self.flow.async_step_profile_edit_settings()
        result = await self.flow.async_step_profile_edit_settings({"boiler_burner": {"boiler_mode": "invalid"}})
        self.assertEqual(result["errors"]["base"], "invalid_value")
        result = await self.flow.async_step_profile_edit_settings({"ch1": {"ch1_mixer_temp_min": 50, "ch1_mixer_temp_max": 40}})
        self.assertEqual(result["errors"]["base"], "profile_invalid_pair")
        self.assertEqual((await self.flow._profile_store().list())[pid], original)
        self.client.send_command.assert_not_called()

    async def test_confirmation_tables_escape_text_and_only_include_selected(self):
        self.client.data.update(C027="4000", C127="3500")
        pid = await self.add_profile({"BoilerTempCmd": "6500", "C027": "4500", "C127": "3800", "C031": "1"})
        await self.load_profile(pid)
        result = await self.flow.async_step_profile_preview(self.selection(["BoilerTempCmd", "C127"]))
        html = result["description_placeholders"]["changes"]
        self.assert_profile_table(html)
        self.assertEqual(html.count("<details>"), 2)
        self.assertIn("Nastawy do wysłania (2)", html)
        self.assertIn("Obieg CO2 (1)", html)
        self.assertNotIn("Obieg CO1", html)
        self.assertIn('>Nastawa</th>', html)
        self.assertIn('>Wartość</th>', html)
        self.assertNotIn('>Sterownik</th>', html)
        self.assertNotIn('>Profil</th>', html)
        self.assertIn('<td>65 °C</td>', html)
        self.assertNotIn('<td>60 °C</td>', html)
        original_translate = self.client.translate
        self.client.translate = lambda key, **kwargs: '<img src="x">' if key.endswith(".name") else original_translate(key, **kwargs)
        result = await self.flow.async_step_profile_confirm()
        self.assertNotIn("<img", result["description_placeholders"]["changes"])
        self.assertIn("&lt;img", result["description_placeholders"]["changes"])
        for language in ("pl", "en"):
            strings = translation_strings(language)
            template = strings["options"]["step"]["profile_confirm"]["description"]
            self.assertNotIn("<", template)
            self.assertNotIn("zamknię", template.lower())
            template.format(**result["description_placeholders"])
        self.client.send_command.assert_not_called()

    async def test_all_new_forms_have_translations_and_valid_navigation(self):
        await self.flow.async_step_profile_save_new()
        form = await self.flow.async_step_profile_name({"name": "Nowy"})
        await self.flow.async_step_profile_write_select(self.selection(["BoilerTempCmd"]))
        screens = [form, await self.flow.async_step_profile_write_confirm()]
        await self.flow.async_step_profile_write_commit()
        screens += [await self.flow.async_step_profile_save(), await self.flow.async_step_profile_import()]
        await self.flow.async_step_profile_save_update()
        screens += [await self.flow.async_step_profile_update_pick()]
        for screen in screens:
            self.assert_translated_screen(screen)

    async def test_remove_settings_only_after_confirmation(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000", "C030": "3000",
                                      "C031": "0", "Unsupported": "keep"})
        original = deepcopy((await self.flow._profile_store().list())[pid])
        await self.pick_profile(pid)
        view = await self.flow.async_step_profile_remove_settings()
        self.assertEqual([page.name for page in view["data_schema"].schema], ["boiler_burner", "ch1"])
        for page, group in view["data_schema"].schema.items():
            for field, selector in group.schema.items():
                self.assertEqual(field.name, "settings")
                self.assertTrue(selector.config["multiple"])
                self.assertEqual(selector.config["mode"], "list")
                self.assertEqual(field.options["default"], [])
            for language in ("pl", "en"):
                strings = translation_strings(language)
                translations = strings["options"]["step"]["profile_remove_settings"]["sections"][page.name]
                for field in group.schema:
                    translations["data"][field.name].format(**view["description_placeholders"])
        change = {"ch1": {"settings": ["C030"]}}
        confirm = await self.flow.async_step_profile_remove_settings(change)
        self.assertEqual(confirm["step_id"], "profile_remove_confirm")
        self.assertIn("Nastawy do usunięcia z profilu", confirm["description_placeholders"]["changes"])
        html = confirm["description_placeholders"]["changes"]
        self.assertNotIn("Nastawy do aktualizacji", html)
        self.assertIn("Nastawy do usunięcia z profilu (1)", html)
        self.assertNotIn("<h3>", html)
        self.assertNotIn("count", confirm["description_placeholders"])
        self.assertEqual((await self.flow._profile_store().list())[pid], original)
        back = await self.flow.async_step_profile_write_back()
        group = next(group for page, group in back["data_schema"].schema.items() if page.name == "ch1")
        self.assertEqual(next(field.options["default"] for field in group.schema), ["C030"])
        await self.flow.async_step_profile_remove_settings(change)
        await self.flow.async_step_profile_write_commit()
        saved = (await self.flow._profile_store().list())[pid]
        self.assertEqual(saved["params"], {"BoilerTempCmd": "6000", "C031": "0", "Unsupported": "keep"})
        self.assertEqual(saved["_meta"]["date"], original["_meta"]["date"])
        self.assertIn("updated_at", saved["_meta"])
        self.client.send_command.assert_not_called()

    async def test_removal_only_cleans_alias_and_cancel_preserves_profile(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000", "CH1ReturnTempCmd": "3000"})
        original = deepcopy((await self.flow._profile_store().list())[pid])
        await self.pick_profile(pid)
        await self.flow.async_step_profile_remove_settings()
        confirm = await self.flow.async_step_profile_remove_settings({"ch1": {"settings": ["C030"]}})
        self.assertEqual(confirm["step_id"], "profile_remove_confirm")
        self.assertNotIn("Nastawy do aktualizacji", confirm["description_placeholders"]["changes"])
        self.assertIn("Nastawy do usunięcia z profilu (1)", confirm["description_placeholders"]["changes"])
        await self.flow.async_step_profile_write_cancel()
        self.assertEqual((await self.flow._profile_store().list())[pid], original)
        await self.pick_profile(pid)
        await self.flow.async_step_profile_remove_settings()
        await self.flow.async_step_profile_remove_settings({"ch1": {"settings": ["C030"]}})
        await self.flow.async_step_profile_write_commit()
        self.assertEqual((await self.flow._profile_store().list())[pid]["params"], {"BoilerTempCmd": "6000"})
        self.client.send_command.assert_not_called()

    async def test_removals_validate_fields_and_reject_empty_profile(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000"})
        original = deepcopy((await self.flow._profile_store().list())[pid])
        for payload, expected in (
            ({"boiler_burner": {"settings": "BoilerTempCmd"}}, "invalid_value"),
            ({"boiler_burner": {"settings": [True]}}, "invalid_value"),
            ({"boiler_burner": {"settings": ["K006"]}}, "profile_changed"),
            ({"ch1": {"remove_boiler_temp_setpoint": True}}, "profile_changed"),
            ({"boiler_burner": {"remove_boiler_temp_setpoint": True}}, "profile_changed"),
            ({"remove_settings": {"boiler_temp_setpoint": 65}}, "profile_changed"),
            ({"boiler_burner": {"settings": ["BoilerTempCmd", "BoilerTempCmd"]}}, "profile_changed"),
            ({"boiler_burner": {"settings": []}}, "profile_no_removal"),
            ({"boiler_burner": {"settings": ["BoilerTempCmd"]}}, "profile_empty"),
        ):
            await self.pick_profile(pid)
            await self.flow.async_step_profile_remove_settings()
            result = await self.flow.async_step_profile_remove_settings(payload)
            self.assertEqual(result["errors"]["base"], expected)
            self.assertEqual((await self.flow._profile_store().list())[pid], original)
        self.client.send_command.assert_not_called()

    async def test_remove_and_concurrent_update_do_not_overwrite(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000", "C031": "0"})
        await self.pick_profile(pid)
        await self.flow.async_step_profile_remove_settings()
        await self.flow.async_step_profile_remove_settings({"ch1": {"settings": ["C031"]}})
        store = self.flow._profile_store()
        snapshot = (await store.list())[pid]
        renamed = deepcopy(snapshot)
        renamed["_meta"]["name"] = "Inne okno"
        await store.update(pid, renamed, snapshot)
        result = await self.flow.async_step_profile_write_commit()
        self.assertEqual(result["errors"]["base"], "profile_changed")
        self.assertIn("C031", (await self.flow._profile_store().list())[pid]["params"])
        self.client.send_command.assert_not_called()

    async def test_edit_helper_shows_standard_range_without_changing_user_limit(self):
        self.flow.config_entry.options["boiler_temp_max"] = 90
        pid = await self.add_profile({"BoilerTempCmd": "6000"})
        await self.pick_profile(pid)
        view = await self.flow.async_step_profile_edit_settings()
        self.assertEqual(view["description_placeholders"]["value_boiler_temp_setpoint"],
                         "Zapisana wartość: **60 °C** · Zakres: **40–90 °C**")
        result = await self.flow.async_step_profile_edit_settings({"boiler_burner": {"boiler_temp_setpoint": 90}})
        self.assertEqual(result["step_id"], "profile_update_confirm")
        self.assertEqual(self.flow.config_entry.options["boiler_temp_max"], 90)
        self.client.send_command.assert_not_called()

    async def test_missing_or_invalid_current_data_never_means_identical(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000"})
        for value in (None, "bad", "NaN"):
            self.client.data["BoilerTempCmd"] = value
            result = await self.load_profile(pid)
            self.assertEqual(result["errors"]["base"], "profile_comparison_unavailable")
            self.assertNotEqual(result["step_id"], "profile_unchanged")
        self.client.available = False
        result = await self.flow.async_step_profile_load()
        self.assertEqual(result["errors"]["base"], "profile_offline")
        self.client.send_command.assert_not_called()

    async def test_download_names_are_consistent_in_both_languages(self):
        pid = await self.add_profile({"BoilerTempCmd": "6500"})
        await self.pick_profile(pid)
        view = await self.flow.async_step_profile_export()
        self.assertIn('>Pobierz plik</a>', view["description_placeholders"]["download_link"])
        for language, label in (("pl", "Pobierz profil"), ("en", "Download profile")):
            strings = translation_strings(language)
            self.assertEqual(strings["options"]["step"]["profile_menu"]["menu_options"]["profile_export"], label)
        self.client.send_command.assert_not_called()

    async def test_table_values_are_not_bold_and_switches_are_shortened_only_in_tables(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000", "C031": "1"})
        await self.load_profile(pid)
        catalog, params, _ = profiles.prepare_profile(self.client, self.flow._profile)
        table = self.flow._profile_tables(self.client, catalog, params)
        self.assertIn('<td>Kocioł Temp. zadana</td><td>60 °C</td>', table)
        self.assertIn('<td>Wł.</td>', table)
        self.assertNotIn('<strong>', table)
        self.assertEqual(catalog["C031"].display(self.client, params["C031"]), "Włączony")

    async def test_removal_uses_compact_grouped_lists_and_editor_has_no_removal_fields(self):
        self.client.data.update(C013="2100", C113="2200")
        pid = await self.add_profile({"C013": "2100", "C113": "2200"})
        await self.pick_profile(pid)
        view = await self.flow.async_step_profile_edit_settings()
        placeholders = view["description_placeholders"]
        self.assertEqual(placeholders["label_ch1_room_comfort_temp"], placeholders["label_ch2_room_comfort_temp"])
        self.assertNotIn("remove_settings", [page.name for page in view["data_schema"].schema])
        view = await self.flow.async_step_profile_remove_settings()
        for page, group in view["data_schema"].schema.items():
            self.assertTrue(group.options["collapsed"])
            field, selector = next(iter(group.schema.items()))
            expected = "C013" if page.name == "ch1" else "C113"
            self.assertEqual(field.name, "settings")
            self.assertEqual(selector.config["options"],
                             [{"value": expected, "label": "Reg. pokojowy Temp. komfortowa"}])
        self.client.send_command.assert_not_called()

    async def test_confirmation_has_one_collapsed_level_and_two_plain_cells_per_row(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000", "C030": "3000", "C031": "0"})
        await self.pick_profile(pid)
        await self.flow.async_step_profile_remove_settings()
        confirm = await self.flow.async_step_profile_remove_settings({
            "boiler_burner": {"settings": ["BoilerTempCmd"]}, "ch1": {"settings": ["C031"]}})
        html = confirm["description_placeholders"]["changes"]

        structure = self.assert_profile_table(html)
        self.assertNotIn("<p>Nastawy do aktualizacji", html)
        self.assertIn("<p>Nastawy do usunięcia z profilu (2)</p>", html)
        self.assertEqual(structure.rows, [2, 2, 2, 2])
        self.assertIn("<td>Wył.</td>", html)
        for language in ("pl", "en"):
            strings = translation_strings(language)
            template = strings["options"]["step"]["profile_remove_confirm"]["description"]
            self.assertEqual(template, "{changes}")
            template.format(**confirm["description_placeholders"])
        self.client.send_command.assert_not_called()

    async def test_new_profile_confirmation_has_one_count_and_only_target_values(self):
        await self.flow.async_step_profile_save_new()
        await self.flow.async_step_profile_name({"name": "Nowy"})
        confirm = await self.flow.async_step_profile_write_select(self.selection(["BoilerTempCmd", "C031"]))
        html = confirm["description_placeholders"]["changes"]
        self.assert_profile_table(html)
        self.assertIn("Nastawy do zapisania (2)", html)
        self.assertNotIn("Nastawy do usunięcia", html)
        self.assertEqual(html.count(">Wartość</th>"), 2)
        self.assertEqual(len(await self.flow._profile_store().list()), 0)
        self.client.send_command.assert_not_called()


class ProfileUpdateTests(ProfileFlowFixture, unittest.IsolatedAsyncioTestCase):

    async def test_controller_update_without_differences_skips_selection_and_preserves_dates(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000.0", "C030": "3000", "C031": "0", "D200": "0"})
        before = deepcopy((await self.flow._profile_store().list())[pid])
        await self.flow.async_step_profile_save_update()
        result = await self.flow._profile_update_target(pid)
        self.assertEqual(result["step_id"], "profile_write_unchanged")
        self.assertEqual(result["data_schema"].schema, {})
        self.assertEqual(result["description_placeholders"]["message"],
                         "Profil jest zgodny z aktualnymi nastawami sterownika.")
        back = await self.flow.async_step_profile_write_unchanged({})
        self.assertEqual(back["step_id"], "profile_menu")
        self.assertEqual((await self.flow._profile_store().list())[pid], before)
        self.client.send_command.assert_not_called()

    async def test_file_update_without_differences_does_not_compare_with_controller(self):
        pid = await self.add_profile({"BoilerTempCmd": "6500", "C031": "1"})
        before = deepcopy((await self.flow._profile_store().list())[pid])
        self.env["_read_upload"] = lambda *_: profiles.create_profile(
            self.client, "Plik", {"BoilerTempCmd": "6500.0", "C031": "1"})
        await self.flow.async_step_profile_import_update()
        await self.flow.async_step_profile_file({"file": "id"})
        result = await self.flow._profile_update_target(pid)
        self.assertEqual(result["step_id"], "profile_write_unchanged")
        self.assertEqual(result["description_placeholders"]["message"], "Profil jest zgodny z nastawami z pliku.")
        self.assertEqual((await self.flow._profile_store().list())[pid], before)
        self.client.send_command.assert_not_called()

    async def test_file_update_only_offers_changed_and_new_settings_with_bold_values(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000", "C031": "1"})
        self.env["_read_upload"] = lambda *_: profiles.create_profile(
            self.client, "Plik", {"BoilerTempCmd": "6000", "C030": "3500", "C031": "0"})
        await self.flow.async_step_profile_import_update()
        await self.flow.async_step_profile_file({"file": "id"})
        result = await self.flow._profile_update_target(pid)
        self.assertEqual(self.defaults(result), {"C030": True, "C031": True})
        self.assertEqual([page.name for page in result["data_schema"].schema], ["ch1"])
        self.assertEqual(result["description_placeholders"]["value_ch1_return_temp_min"],
                         "Profil: **brak nastawy** · Plik: **35 °C**")
        self.assertEqual(result["description_placeholders"]["value_ch1_return_protection"],
                         "Profil: **Włączony** · Plik: **Wyłączony**")
        result = await self.flow.async_step_profile_write_select(self.selection(["C030"]))
        self.assertEqual(result["step_id"], "profile_update_confirm")
        self.assertEqual((await self.flow._profile_store().list())[pid]["params"],
                         {"BoilerTempCmd": "6000", "C031": "1"})
        await self.flow.async_step_profile_write_commit()
        self.assertEqual((await self.flow._profile_store().list())[pid]["params"],
                         {"BoilerTempCmd": "6000", "C030": "3500", "C031": "1"})
        self.client.send_command.assert_not_called()

    async def test_controller_update_filters_equal_settings_and_preserves_unselected(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000", "C030": "3500", "C031": "1", "D200": "0"})
        await self.flow.async_step_profile_save_update()
        view = await self.flow._profile_update_target(pid)
        self.assertEqual(self.defaults(view), {"C030": True, "C031": True})
        self.assertEqual([page.name for page in view["data_schema"].schema], ["ch1"])
        self.assertEqual(view["description_placeholders"]["value_ch1_return_temp_min"],
                         "Profil: **35 °C** · Sterownik: **30 °C**")
        result = await self.flow.async_step_profile_write_select(self.selection(["C031"]))
        self.assertEqual(result["step_id"], "profile_update_confirm")
        before = (await self.flow._profile_store().list())[pid]
        self.assertEqual(before["params"]["C031"], "1")
        self.assertIn("Nastawy do aktualizacji (1)", result["description_placeholders"]["changes"])
        self.assertIn("<td>Wył.</td>", result["description_placeholders"]["changes"])
        self.assertEqual(result["menu_options"],
                         ["profile_write_commit", "profile_write_back", "profile_write_cancel"])
        back = await self.flow.async_step_profile_write_back()
        self.assertEqual(back["step_id"], "profile_settings")
        self.assertEqual(self.defaults(back), {"C030": False, "C031": True})
        await self.flow.async_step_profile_write_select(self.selection(["C031"]))
        self.assertEqual((await self.flow._profile_store().list())[pid], before)
        await self.flow.async_step_profile_write_commit()
        self.assertEqual((await self.flow._profile_store().list())[pid]["params"]["C031"], "0")
        self.assertEqual((await self.flow._profile_store().list())[pid]["params"]["C030"], "3500")
        self.client.send_command.assert_not_called()

    async def test_hidden_equal_setting_cannot_be_injected_into_selection(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000", "C031": "1"})
        await self.load_profile(pid)
        result = await self.flow.async_step_profile_preview({
            "boiler_burner": {"boiler_temp_setpoint": True}, "ch1": {"ch1_return_protection": True}})
        self.assertEqual(result["errors"]["base"], "profile_changed")
        self.client.send_command.assert_not_called()

    async def test_update_success_opens_profile_with_or_without_skipped_parameters(self):
        pid = await self.add_profile({"C031": "1"})
        for source in ("controller", "file"):
            for skipped in ({}, {"C987": "unsupported"}):
                with self.subTest(source=source, skipped=bool(skipped)):
                    before = (await self.flow._profile_store().list())[pid]
                    target = "0" if before["params"]["C031"] == "1" else "1"
                    if source == "controller":
                        self.env["export_parameters"] = lambda *_: ({"C031": target}, skipped)
                        await self.flow.async_step_profile_save_update()
                    else:
                        self.env["_read_upload"] = lambda *_: profiles.create_profile(
                            self.client, "Plik", {"C031": target, **({"C987": "1"} if skipped else {})})
                        await self.flow.async_step_profile_import_update()
                        await self.flow.async_step_profile_file({"file": "id"})
                    selection = await self.flow._profile_update_target(pid)
                    self.assertEqual(bool(selection["description_placeholders"]["skipped"]), bool(skipped))
                    confirm = await self.flow.async_step_profile_write_select(self.selection(["C031"]))
                    self.assertEqual(confirm["step_id"], "profile_update_confirm")
                    self.assertEqual((await self.flow._profile_store().list())[pid], before)
                    result = await self.flow.async_step_profile_write_commit()
                    self.assertEqual(result["step_id"], "profile_menu")
                    self.assertEqual(self.flow._profile_id, pid)
                    self.assertNotIn("skipped", result["description_placeholders"])
                    self.assertEqual(self.flow._profile["params"], {"C031": target})
        self.client.send_command.assert_not_called()

    async def test_cancel_returns_to_selected_profile_for_edit_remove_and_update(self):
        pid = await self.add_profile({"BoilerTempCmd": "6500", "C031": "1"})
        original = deepcopy((await self.flow._profile_store().list())[pid])
        for mode in ("edit", "remove", "update"):
            await self.pick_profile(pid)
            if mode == "edit":
                await self.flow.async_step_profile_edit_settings()
                await self.flow.async_step_profile_edit_settings({"boiler_burner": {"boiler_temp_setpoint": 70}})
            elif mode == "remove":
                await self.flow.async_step_profile_remove_settings()
                await self.flow.async_step_profile_remove_settings({"ch1": {"settings": ["C031"]}})
            else:
                await self.flow.async_step_profile_save_update()
                await self.flow._profile_update_target(pid)
                result = await self.flow.async_step_profile_write_select(self.selection(["C031"]))
                self.assertEqual(result["step_id"], "profile_update_confirm")
            result = await self.flow.async_step_profile_write_cancel()
            self.assertEqual(result["step_id"], "profiles" if mode == "update" else "profile_menu")
            self.assertEqual(self.flow._profile_id, pid)
            self.assertEqual((await self.flow._profile_store().list())[pid], original)
        await self.flow.async_step_profile_save_new()
        await self.flow.async_step_profile_name({"name": "Nie zapisuj"})
        await self.flow.async_step_profile_write_select(self.selection(["C031"]))
        result = await self.flow.async_step_profile_write_cancel()
        self.assertEqual(result["step_id"], "profiles")
        self.assertEqual(len(await self.flow._profile_store().list()), 1)
        self.client.send_command.assert_not_called()

    async def test_main_menu_descriptions_follow_store_contents_in_both_languages(self):
        empty = await self.flow.async_step_profiles()
        self.assertNotIn("profile_manage", empty["menu_options"])
        self.assertEqual(empty["description_placeholders"], {
            "profile_save_description": "Utwórz profil z nastaw sterownika.",
            "profile_import_description": "Utwórz profil z pliku."})
        await self.add_profile({"C031": "1"})
        stored = await self.flow.async_step_profiles()
        self.assertIn("profile_manage", stored["menu_options"])
        self.assertEqual(stored["description_placeholders"], {
            "profile_save_description": "Utwórz lub aktualizuj profil z nastaw sterownika.",
            "profile_import_description": "Utwórz lub aktualizuj profil z pliku."})
        for language in ("pl", "en"):
            strings = translation_strings(language)
            step = strings["options"]["step"]["profiles"]
            for view in (empty, stored):
                for option in ("profile_save", "profile_import"):
                    step["menu_option_descriptions"][option].format(**view["description_placeholders"])

    async def test_profile_menu_has_no_descriptions_and_safe_header_date(self):
        record = profiles.create_profile(self.client, '<DOM> *30kW*', {"C031": "1"})
        record["_meta"].update(date="2026-10-18T18:22:00+00:00", description="12.88")
        pid = await self.flow._profile_store().add(record)
        menu = await self.pick_profile(pid)
        placeholders = menu["description_placeholders"]
        self.assertEqual(placeholders["description"], "\n\n12\\.88")
        self.assertEqual(placeholders["updated"],
                         "\n\n<small>Ostatnia aktualizacja: 18.10.2026, 18:22</small>")
        for language in ("pl", "en"):
            strings = translation_strings(language)
            step = strings["options"]["step"]["profile_menu"]
            self.assertNotIn("menu_option_descriptions", step)
            step["title"].format()
            self.assertTrue(step["description"].format(**placeholders).startswith("### "))
            self.assertIn("profile_remove_settings", step["menu_options"])
        self.assertNotIn("<DOM>", placeholders["name"])
        await self.flow.async_step_profile_rename({"name": "Bez opisu", "description": ""})
        menu = await self.flow.async_step_profile_menu()
        self.assertEqual(menu["description_placeholders"]["description"], "")

    async def test_editor_rejects_legacy_removal_checkbox_without_mutation(self):
        pid = await self.add_profile({"BoilerTempCmd": "6000", "C031": "1"})
        before = deepcopy((await self.flow._profile_store().list())[pid])
        await self.pick_profile(pid)
        view = await self.flow.async_step_profile_edit_settings()
        self.assertNotIn("remove_settings", [page.name for page in view["data_schema"].schema])
        result = await self.flow.async_step_profile_edit_settings({"remove_settings": {"remove_ch1_return_protection": True}})
        self.assertEqual(result["errors"]["base"], "profile_changed")
        self.assertEqual((await self.flow._profile_store().list())[pid], before)
        self.client.send_command.assert_not_called()
