"""Testy menu, formularzy i potwierdzeń wysyłania profili."""

import asyncio
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import support
from helpers import ProfileFlowFixture, translation_strings
from skzp_control import profiles


class ProfileFlowTests(ProfileFlowFixture, unittest.IsolatedAsyncioTestCase):


    async def test_save_load_preview_and_explicit_confirmation(self):
        saved = await self.save_profile({"name": "Zima"})
        self.assertEqual(saved["step_id"], "profile_menu")
        data = await self.flow._profile_store().list()
        profile_id = next(iter(data))
        self.client.data["BoilerTempCmd"] = "6500"
        preview = await self.load_profile(profile_id)
        self.assertEqual(preview["step_id"], "profile_settings")
        self.assertEqual(preview["description_placeholders"]["form_title"], "Wybierz nastawy")
        self.assert_translated_screen(preview)
        self.assertEqual(self.defaults(preview)["BoilerTempCmd"], True)
        confirm = await self.flow.async_step_profile_settings(self.selection(["BoilerTempCmd"]))
        self.assertEqual(confirm["step_id"], "profile_confirm")
        self.client.send_command.assert_not_called()
        async def send(command, **kwargs):
            self.client.data.update(command)
            self.client.hass.bus.fire()
        self.client.send_command.side_effect = send
        progress = await self.flow.async_step_profile_send()
        self.assertEqual(progress["type"], "progress")
        await self.flow._profile_task
        done = await self.flow.async_step_profile_apply()
        self.assertEqual(done["next_step_id"], "profile_result")
        result = await self.flow.async_step_profile_result()
        summary = result["description_placeholders"]["summary"]
        self.assertIn("<p>Zapisano nastaw: 1 z 1.</p>", summary)
        self.assertIn("<details><summary>Zapisane (1)</summary>", summary)
        self.assertIn("<li>Kocioł Temp. zadana</li>", summary)
        self.assertNotIn("Niepotwierdzone", summary)

    async def test_change_after_preview_requires_new_review(self):
        profile_id = await self.add_profile({"BoilerTempCmd": "6500"})
        await self.load_profile(profile_id)
        self.client.data["BoilerTempCmd"] = "7000"
        result = await self.flow.async_step_profile_preview(self.selection(["BoilerTempCmd"]))
        self.assertEqual(result["errors"]["base"], "profile_changed")
        self.client.send_command.assert_not_called()

    async def test_identical_profile_has_no_write_action(self):
        for raw in ("6000", "6000.0"):
            with self.subTest(raw=raw):
                profile_id = await self.flow._profile_store().add(
                    profiles.create_profile(self.client, "Test", {"BoilerTempCmd": raw}))
                result = await self.load_profile(profile_id)
                self.assertEqual(result["step_id"], "profile_unchanged")
                self.assertEqual(result["data_schema"].schema, {})
                self.assertIsNone(self.flow._profile_task)
                self.assertFalse(hasattr(self.flow, "_profile_offered"))
        self.client.send_command.assert_not_called()

    async def test_import_saves_only_valid_settings_without_sending(self):
        p = profiles.create_profile(self.client, "Source", {"BoilerTempCmd": "6500", "Token": "secret", "DHWDays": "1" * 28})
        self.env["_read_upload"] = lambda *_: p
        result = await self.import_file()
        self.assertEqual(result["step_id"], "profile_import_review")
        self.assertFalse(await self.flow._profile_store().list())
        self.assertIn("6500", str(self.flow._profile_source))
        result = await self.finish_import({"name": "Import"})
        self.assertEqual(result["step_id"], "profile_menu")
        stored = next(iter((await self.flow._profile_store().list()).values()))
        self.assertEqual(stored["params"], {"BoilerTempCmd": "6500"})
        self.client.send_command.assert_not_called()

    async def test_delete_requires_confirmation_export_signed(self):
        pid = await self.add_profile({"C031": "1"})
        await self.pick_profile(pid)
        download = await self.flow.async_step_profile_export()
        remove_download = self.flow._profile_download_remove
        self.assertIn('href="/signed/link"', download["description_placeholders"]["download_link"])
        self.env["async_create_frame_download"].assert_called_once()
        prompt = await self.flow.async_step_profile_delete()
        self.assertEqual(prompt["step_id"], "profile_delete")
        self.assertEqual(prompt["type"], "menu")
        self.assertTrue(await self.flow._profile_store().list())
        await self.flow.async_step_profile_menu()
        self.assertTrue(await self.flow._profile_store().list())
        await self.flow.async_step_profile_delete_confirm()
        self.assertFalse(await self.flow._profile_store().list())
        self.flow.async_remove()
        remove_download.assert_called_once()

    async def test_import_close_before_review_does_not_save(self):
        self.env["_read_upload"] = lambda *_: profiles.create_profile(
            self.client, "Source", {"BoilerTempCmd": "6500"})
        await self.import_file()
        self.flow.async_remove()
        self.assertFalse(await self.flow._profile_store().list())
        self.client.send_command.assert_not_called()

    async def test_download_order_is_stable_and_keeps_profile_unchanged_offline(self):
        params = {
            "ZFuture": "keep", "C224": "20", "DHWMode": "Auto", "C031": "1",
            "K006": "1", "BuOptClrTime": "10", "C124": "20",
            "BoilerTempCmd": "6000", "C987": "123", "D203": "6000",
            "D200": "1", "C027": "4500", "C012": "1", "C024": "5",
            "C030": "3000", "BuMode": "Auto", "C112": "1",
        }
        expected = [
            "K006", "BoilerTempCmd", "BuMode", "BuOptClrTime",
            "D200", "D203", "DHWMode", "C012", "C024", "C027", "C031", "C030",
            "C112", "C124", "C224", "C987", "ZFuture",
        ]
        aliases = {
            "C012": "CH1Mode", "C024": "CH1MixGain", "C027": "CH1MixTempBase",
            "C031": "CH1ReturnProtAct", "C030": "CH1ReturnTempCmd",
        }
        self.client.available = False
        for names in ({}, aliases):
            for reverse in (False, True):
                with self.subTest(aliases=bool(names), reverse=reverse):
                    source = list(params.items())
                    if reverse:
                        source.reverse()
                    record = self.profile({names.get(key, key): value for key, value in source})
                    pid = await self.flow._profile_store().add(record)
                    await self.pick_profile(pid)
                    before = deepcopy(self.flow._profile)
                    self.env["async_create_frame_download"].reset_mock()
                    await self.flow.async_step_profile_export()
                    call = self.env["async_create_frame_download"].call_args
                    downloaded = call.args[1]
                    self.assertEqual(call.kwargs, {"profile": True})
                    self.assertEqual(list(downloaded["params"]), [names.get(key, key) for key in expected])
                    self.assertEqual(downloaded, before)
                    self.assertEqual(list(self.flow._profile["params"]), list(before["params"]))
                    stored = (await self.flow._profile_store().list())[pid]
                    self.assertEqual(stored, before)
                    self.assertEqual(list(stored["params"]), list(before["params"]))
        self.client.send_command.assert_not_called()

    async def test_changed_value_after_confirmation_does_not_send(self):
        pid = await self.add_profile({"BoilerTempCmd": "6500"})
        await self.load_profile(pid)
        await self.flow.async_step_profile_preview(self.selection(["BoilerTempCmd"]))
        self.client.data["BoilerTempCmd"] = "7000"
        result = await self.flow.async_step_profile_send()
        self.assertEqual(result["step_id"], "profile_settings")
        self.client.send_command.assert_not_called()

    async def test_manage_missing_profile_and_delete_then_import(self):
        pid = await self.add_profile({"C031": "1"})
        await self.pick_profile(pid)
        await self.flow.async_step_profile_delete_confirm()
        result = await self.flow.async_step_profile_menu()
        self.assertEqual(result["step_id"], "profiles")
        self.assertNotIn("profile_manage", result["menu_options"])
        result = await self.flow.async_step_profile_import()
        self.assertEqual(result["step_id"], "profile_file")
        self.client.send_command.assert_not_called()

    async def test_menu_options_have_translations_and_handlers(self):
        pid = await self.add_profile({"C031": "1"})
        await self.load_profile(pid)
        await self.flow.async_step_profile_preview(self.selection(["C031"]))
        for step in ("profiles", "profile_menu", "profile_confirm", "profile_delete"):
            result = await getattr(self.flow, "async_step_" + step)()
            self.assert_translated_screen(result)


    async def test_only_differences_are_shown_and_selected(self):
        pid = await self.add_profile({"C031": "1", "BoilerTempCmd": "6000"})
        result = await self.load_profile(pid)
        self.assertEqual(self.defaults(result), {"C031": True})
        self.assertNotIn("value_boiler_temp_setpoint", result["description_placeholders"])
        self.assertEqual([page.name for page in result["data_schema"].schema], ["ch1"])
        self.assertIn("Profil:", result["description_placeholders"]["value_ch1_return_protection"])
        self.assertNotIn("action", [key.name for key in result["data_schema"].schema])
        self.client.send_command.assert_not_called()
        await self.flow.async_step_profile_preview(self.selection(["C031", "BoilerTempCmd"]))
        self.assertEqual(self.flow._profile_selected, {"C031": "1"})

    async def test_group_order_helpers_and_all_unchecked_never_send(self):
        self.client.data.update(C027="4000", C028="3000", DHWTempCmd="5000")
        pid = await self.add_profile({
            "C031": "1", "C027": "4500", "C028": "3000",
            "DHWTempCmd": "5500", "BoilerTempCmd": "6500"})
        view = await self.load_profile(pid)
        self.assertEqual([field.name for field in view["data_schema"].schema],
                         ["boiler_burner", "dhw_buffer", "ch1"])
        for group in view["data_schema"].schema.values():
            self.assertTrue(group.options["collapsed"])
            self.assertTrue(all(value is bool for value in group.schema.values()))
        self.assertEqual(list(self.flow._profile_fields_by_page["ch1"].values()),
                         ["C027", "C031"])
        self.assertEqual(view["description_placeholders"]["label_ch1_mixer_temp_setpoint"],
                         "Mieszacz Temp. zadana")
        self.assertEqual(view["description_placeholders"]["value_ch1_mixer_temp_setpoint"],
                         "Sterownik: **40 °C** · Profil: **45 °C**")
        self.assertNotIn("value_ch1_mixer_temp_min", view["description_placeholders"])
        for language in ("pl", "en"):
            strings = translation_strings(language)
            template = strings["options"]["step"]["profile_settings"]
            for page, group in view["data_schema"].schema.items():
                translations = template["sections"][page.name]
                translations["name"].format(**view["description_placeholders"])
                for field in group.schema:
                    translations["data"][field.name].format(**view["description_placeholders"])
                    translations["data_description"][field.name].format(**view["description_placeholders"])
        result = await self.flow.async_step_profile_preview(self.selection([]))
        self.assertEqual(result["errors"]["base"], "profile_no_selection")
        self.assertFalse(any(self.defaults(result).values()))
        self.client.send_command.assert_not_called()

    async def test_common_fields_preserve_parameter_keys_for_both_protocol_styles(self):
        cases = (
            ("SKZP-05S", "SKZP-05S_V5.71",
             {"C027": "4000", "C012": "1", "C020": "1"},
             {"C027": "4500", "C012": "2", "C020": "0"}),
            ("SKZP-02S", "SKZP-02S_V2.50",
             {"CH1MixTempBase": "4000", "CH1Mode": "Active", "CH1MixActive": "On"},
             {"CH1MixTempBase": "4500", "CH1Mode": "Timer", "CH1MixActive": "Off"}),
        )
        for model, dev_type, current, target in cases:
            with self.subTest(model=model):
                self.client.model = model
                self.client.data = {"DevType": dev_type, **current}
                profile_id = await self.flow._profile_store().add(
                    profiles.create_profile(self.client, "Test", target))
                view = await self.load_profile(profile_id)
                self.assertEqual(set(self.flow._profile_fields_by_page["ch1"]),
                                 {"ch1_mixer_temp_setpoint", "ch1_mode", "ch1_mixer"})
                self.assertEqual(set(self.flow._profile_fields_by_page["ch1"].values()), set(target))
                self.assert_translated_screen(view)
                for language in ("pl", "en"):
                    group = translation_strings(language)["options"]["step"]["profile_settings"]["sections"]["ch1"]
                    for field in self.flow._profile_fields_by_page["ch1"]:
                        group["data"][field].format(**view["description_placeholders"])
                        group["data_description"][field].format(**view["description_placeholders"])
                confirm = await self.flow.async_step_profile_settings(self.selection(target))
                self.assertEqual(confirm["step_id"], "profile_confirm")
                self.assertEqual(self.flow._profile_selected, target)
                self.client.send_command.assert_not_called()

    async def test_unknown_checkbox_is_rejected(self):
        pid = await self.add_profile({"C031": "1"})
        await self.load_profile(pid)
        result = await self.flow.async_step_profile_preview({"ch1": {"ch1_return_protection": True, "ch1_return_temp_min": True}})
        self.assertEqual(result["errors"]["base"], "profile_changed")
        self.client.send_command.assert_not_called()

    async def test_profile_list_short_description_keeps_full_text(self):
        description = "Opis " * 12
        await self.save_profile({"name": "Zima", "description": description})
        pid = self.flow._profile_id
        menu = await self.flow.async_step_profile_manage()
        self.assertEqual(menu["description_placeholders"]["profile_description_0"],
                         description[:30] + "…")
        for language in ("pl", "en"):
            strings = translation_strings(language)
            template = strings["options"]["step"]["profile_manage"]["menu_option_descriptions"]["profile_pick_0"]
            self.assertEqual(template.format(**menu["description_placeholders"]), description[:30] + "…")
        await self.pick_profile(pid)
        self.assertEqual(self.flow._profile["_meta"]["description"], description.strip())
        await self.flow.async_step_profile_rename({"name": "Krótki", "description": "Krótki opis"})
        menu = await self.flow.async_step_profile_manage()
        self.assertEqual(menu["description_placeholders"]["profile_description_0"], "Krótki opis")
        await self.flow.async_step_profile_rename({"name": "Bez opisu"})
        menu = await self.flow.async_step_profile_manage()
        self.assertEqual(menu["description_placeholders"]["profile_description_0"], "")

    async def test_skipped_save_notes_stay_in_selection_without_extra_success_screen(self):
        self.env["export_parameters"] = lambda *_: ({"C031": "0"}, {"Unknown": "unsupported"})
        await self.flow.async_step_profile_save_new()
        result = await self.flow.async_step_profile_name({"name": "Test"})
        self.assertEqual(result["step_id"], "profile_settings")
        self.assertIn("Pominięte parametry:", result["description_placeholders"]["skipped"])
        confirm = await self.flow.async_step_profile_write_select(self.selection(["C031"]))
        self.assertEqual(confirm["step_id"], "profile_create_confirm")
        self.assertFalse(await self.flow._profile_store().list())
        result = await self.flow.async_step_profile_write_commit()
        self.assertEqual(result["step_id"], "profile_menu")
        self.assertNotIn("skipped", result["description_placeholders"])
        self.assertEqual(self.flow._profile["params"], {"C031": "0"})
        self.client.send_command.assert_not_called()

    async def test_menu_navigation_and_forms_without_action(self):
        pid = await self.add_profile({"C031": "1"})
        result = await self.flow.async_step_profiles()
        self.assertEqual(result["menu_options"],
                         ["profile_save", "profile_import", "profile_manage", "init"])
        result = await self.pick_profile(pid)
        self.assertEqual(result["menu_options"], [
            "profile_load", "profile_rename", "profile_edit_settings", "profile_remove_settings",
            "profile_export", "profile_delete", "profile_manage"])
        await self.flow.async_step_profile_save_new()
        for step in ("profile_name", "profile_file", "profile_rename"):
            result = await getattr(self.flow, "async_step_" + step)()
            self.assertNotIn("action", [key.name for key in result["data_schema"].schema])
        await self.load_profile(pid)
        await self.flow.async_step_profile_preview(self.selection(["C031"]))
        result = await self.flow.async_step_profile_confirm()
        self.assertEqual(result["menu_options"], ["profile_send", "profile_preview", "profile_menu"])
        await self.flow.async_step_profile_menu()
        self.client.send_command.assert_not_called()

    async def test_rename_keeps_id_and_settings(self):
        pid = await self.add_profile({"C031": "1"})
        await self.pick_profile(pid)
        before = (await self.flow._profile_store().list())[pid]
        result = await self.flow.async_step_profile_rename({"name": "Nowa nazwa"})
        self.assertEqual(result["step_id"], "profile_menu")
        stored = await self.flow._profile_store().list()
        self.assertEqual(list(stored), [pid])
        self.assertEqual(stored[pid]["_meta"]["name"], "Nowa nazwa")
        self.assertEqual(stored[pid]["params"], before["params"])
        self.assertEqual(stored[pid]["_meta"]["date"], before["_meta"]["date"])
        self.client.send_command.assert_not_called()

    async def test_clickable_profiles_back_and_correct_selection(self):
        winter = profiles.create_profile(self.client, "Zima", {"C031": "1"})
        summer = profiles.create_profile(self.client, "lato", {"C031": "0"})
        winter["_meta"]["description"] = "Opis zimowy"
        summer["_meta"]["description"] = "Opis letni"
        first = await self.flow._profile_store().add(winter)
        second = await self.flow._profile_store().add(summer)
        before = await self.flow._profile_store().list()
        menu = await self.flow.async_step_profile_manage()
        self.assertEqual(menu["menu_options"], {
            "profile_pick_0": "lato", "profile_pick_1": "Zima",
            "profiles": "← Wróć"})
        self.assertEqual(list(menu["menu_options"]), ["profile_pick_0", "profile_pick_1", "profiles"])
        self.assertEqual(menu["description_placeholders"], {
            "profile_description_0": "Opis letni", "profile_description_1": "Opis zimowy"})
        for index, pid in enumerate((second, first)):
            result = await getattr(self.flow, f"async_step_profile_pick_{index}")()
            self.assertEqual(self.flow._profile_id, pid)
            self.assertEqual(result["step_id"], "profile_menu")
        self.assertEqual(await self.flow._profile_store().list(), before)
        await self.flow.async_step_profile_rename({"name": "Alfa", "description": "Opis zimowy"})
        menu = await self.flow.async_step_profile_manage()
        self.assertEqual(list(menu["menu_options"].values()), ["Alfa", "lato", "← Wróć"])
        self.assertEqual(menu["description_placeholders"]["profile_description_0"], "Opis zimowy")
        for source in ("controller", "file"):
            with self.subTest(source=source):
                if source == "controller":
                    menu = await self.flow.async_step_profile_save_update()
                else:
                    self.env["_read_upload"] = lambda *_: profiles.create_profile(self.client, "Plik", {"C031": "0"})
                    await self.flow.async_step_profile_import_update()
                    menu = await self.flow.async_step_profile_file({"file": "id"})
                self.assertEqual(list(menu["menu_options"].values())[:2], ["Alfa", "lato"])
                self.assertEqual(list(menu["menu_options"].values())[-1], "← Wróć")
                await self.flow.async_step_profile_pick_0()
                self.assertEqual(self.flow._profile_target_id, first)
        result = await self.flow.async_step_profiles()
        self.assertEqual(result["step_id"], "profiles")
        await self.flow._profile_store().delete(first)
        await self.flow.async_step_profile_manage()
        self.assertFalse(hasattr(self.flow, "async_step_profile_pick_1"))
        await self.flow.async_step_profile_pick_0()
        self.assertEqual(self.flow._profile_id, second)
        self.client.send_command.assert_not_called()

    async def test_description_save_edit_export_import_and_header(self):
        await self.save_profile({
            "name": "Zima", "description": "Pierwszy wiersz\nDrugi wiersz"})
        menu = await self.flow.async_step_profile_menu()
        self.assertEqual(menu["description_placeholders"]["name"], "Zima")
        self.assertIn("Drugi wiersz", menu["description_placeholders"]["description"])
        for language, expected in (("pl", "Zarządzaj profilem"),
                                   ("en", "Manage profile")):
            strings = translation_strings(language)
            step = strings["options"]["step"]["profile_menu"]
            # HA renderuje nagłówek menu bez description_placeholders.
            self.assertEqual(step["title"].format(), expected)
            description = step["description"].format(**menu["description_placeholders"])
            self.assertTrue(description.startswith("### Zima\n\n"))
            self.assertIn("Drugi wiersz", description)
        await self.flow.async_step_profile_export()
        exported = self.env["async_create_frame_download"].call_args.args[1]
        self.assertEqual(exported["_meta"]["description"], "Pierwszy wiersz\nDrugi wiersz")
        self.env["_read_upload"] = lambda *_: exported
        await self.import_file()
        review = await self.flow.async_step_profile_import_review()
        self.assertEqual(self.field(review, "description")[0].options["default"],
                         exported["_meta"]["description"])
        await self.finish_import({
            "name": "Kopia", "description": exported["_meta"]["description"]})
        await self.flow.async_step_profile_rename({"name": "Nowa", "description": ""})
        self.assertEqual(self.flow._profile["_meta"]["description"], "")
        self.client.send_command.assert_not_called()

    async def test_old_profile_without_description_and_validation(self):
        pid = await self.add_profile({"C031": "1"})
        menu = await self.pick_profile(pid)
        self.assertEqual(menu["description_placeholders"]["description"], "")
        form = await self.flow.async_step_profile_rename()
        self.assertEqual(self.field(form, "description")[0].options["default"], "")
        result = await self.flow.async_step_profile_rename({
            "name": "Test", "description": "x" * 1001})
        self.assertEqual(result["errors"]["base"], "profile_invalid_description")
        self.assertNotIn("description", (await self.flow._profile_store().list())[pid]["_meta"])

    async def test_back_from_confirmation_keeps_manual_selection(self):
        pid = await self.add_profile({"C031": "1", "BoilerTempCmd": "6500"})
        await self.load_profile(pid)
        await self.flow.async_step_profile_preview(self.selection(["C031"]))
        result = await self.flow.async_step_profile_preview()
        self.assertEqual(result["step_id"], "profile_settings")
        self.assertTrue(self.defaults(result)["C031"])
        self.assertFalse(self.defaults(result)["BoilerTempCmd"])
        self.client.send_command.assert_not_called()

    async def test_missing_required_fields_report_errors(self):
        await self.flow.async_step_profile_save_new()
        result = await self.flow.async_step_profile_name({})
        self.assertEqual(result["errors"]["base"], "profile_invalid_name")
        await self.flow.async_step_profile_import_new()
        result = await self.flow.async_step_profile_file({})
        self.assertEqual(result["errors"]["base"], "profile_file_required")
        await self.add_profile({"C031": "1"})
        result = await self.flow.async_step_profile_manage()
        self.assertEqual(result["type"], "menu")

    async def test_empty_notes_hidden_and_download_html_escaped(self):
        result = await self.save_profile({"name": "Test"})
        self.assertEqual(result["step_id"], "profile_menu")
        self.assertIn("Pominięte parametry:", self.flow._profile_notes({"Unknown": "unsupported"}))
        self.flow._profile_url = '/api/test?authSig=abc&test="x"'
        result = await self.flow.async_step_profile_download()
        self.assertIn('href="/api/test?authSig=abc&amp;test=&quot;x&quot;"',
                      result["description_placeholders"]["download_link"])
        from html.parser import HTMLParser
        class Links(HTMLParser):
            def handle_starttag(self, tag, attrs):
                if tag == "a":
                    self.attrs = dict(attrs)
        for language in ("pl", "en"):
            strings = translation_strings(language)
            template = strings["options"]["step"]["profile_download"]["description"]
            self.assertNotIn("<", template)
            self.assertIn("{download_link}", template)
            rendered = template.format(**result["description_placeholders"])
            parser = Links()
            parser.feed(rendered)
            self.assertEqual(parser.attrs["target"], "_blank")
            self.assertEqual(parser.attrs["href"], self.flow._profile_url)

    async def test_offline_form_has_all_translation_placeholders(self):
        pid = await self.add_profile({"C031": "1"})
        self.client.available = False
        result = await self.load_profile(pid)
        self.assertEqual(result["errors"]["base"], "profile_offline")
        self.assertEqual(set(result["description_placeholders"]),
                         {"name", "skipped", "form_title", "form_description"})
        self.assert_translated_screen(result)

    async def test_cancel_flow_stops_task_and_clears_lock(self):
        pid = await self.add_profile({"C031": "1"})
        self.client.command_confirmation_timeout = 30
        await self.load_profile(pid)
        await self.flow.async_step_profile_preview(self.selection(["C031"]))
        await self.flow.async_step_profile_send()
        await asyncio.sleep(0)
        self.flow.async_remove()
        with self.assertRaises(asyncio.CancelledError):
            await self.flow._profile_task
        self.assertFalse(self.client.profile_import_active)

    async def test_failed_task_shows_result_when_client_is_offline_or_missing(self):
        self.flow.config_entry.data = {"host": "fallback", "port": 1180}
        self.flow._profile_selected = {"C031": "1"}
        self.client.available = False
        for missing in (False, True):
            with self.subTest(missing=missing):
                if missing:
                    self.flow.hass.data["skzp_control"].pop("entry")
                task = asyncio.get_running_loop().create_future()
                task.set_exception(RuntimeError("test failure"))
                self.flow._profile_task = task
                with self.assertLogs(self.env["_LOGGER"], level="ERROR") as logs:
                    done = await self.flow.async_step_profile_apply()
                self.assertEqual(done, {"type": "progress_done", "next_step_id": "profile_result"})
                self.assertEqual(self.flow._profile_result, {
                    "saved": [], "failed": [], "remaining": ["C031"], "error": "profile_send_failed"})
                self.assertIn("fallback:1180" if missing else "test:1", logs.output[0])
                result = await self.flow.async_step_profile_result()
                self.assertEqual(result["step_id"], "profile_result")
                self.assertIn("<details>", result["description_placeholders"]["summary"])
        self.client.send_command.assert_not_called()

    def test_upload_cleanup_on_invalid_file(self):
        cleaned = []
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profile.json"
            path.write_text("invalid", encoding="utf-8")
            @contextmanager
            def uploaded(*args):
                try:
                    yield path
                finally:
                    cleaned.append(True)
            self.env["process_uploaded_file"] = uploaded
            with self.assertRaises(ValueError):
                self.env["_read_upload"](None, "id")
        self.assertEqual(cleaned, [True])

    def test_store_is_created_once_per_entry(self):
        constructor = Mock(wraps=self.env["ProfileStore"])
        with patch.dict(self.env, {"ProfileStore": constructor}):
            store = self.flow._profile_store()
            self.assertIs(self.flow._profile_store(), store)
            constructor.assert_called_once_with(self.flow.hass, "entry")
            self.flow.config_entry = SimpleNamespace(entry_id="other", options={})
            other = self.flow._profile_store()
            self.assertIsNot(other, store)
            self.assertIs(self.flow._profile_store(), other)
            self.assertEqual(constructor.call_count, 2)

    async def test_store_is_separate_for_each_entry_and_limited(self):
        store = self.flow._profile_store()
        record = profiles.create_profile(self.client, "private_name", {"C031": "1"})
        record["_meta"].update(description="private_description", Token="private_token", DevPin="private_pin")
        with self.assertLogs(self.env["_LOGGER"], level="DEBUG") as logs:
            pid = await store.add(record)
            updated = {**record, "params": {"C031": "0"}}
            await store.update(pid, updated, (await store.list())[pid])
            snapshot = (await store.list())[pid]
            renamed = deepcopy(snapshot)
            renamed["_meta"].update(name="private_new_name", description="private_new_description")
            await store.update(pid, renamed, snapshot)
            await store.delete(pid)
            await store.delete(pid)
        messages = [item.getMessage() for item in logs.records]
        self.assertEqual(len(messages), 4)
        for message, action in zip(messages, ("created in", "updated in", "updated in", "deleted from")):
            self.assertIn(f"Profile {pid} {action} Home Assistant", message)
        for secret in ("private_name", "private_description", "private_token", "private_pin",
                       "private_new_name", "private_new_description"):
            self.assertNotIn(secret, "\n".join(messages))
        pid = await store.add(record)
        for action in ("add", "update", "delete"):
            with self.subTest(action=action):
                before = await store.list()
                with patch.object(store.store, "async_save", AsyncMock(side_effect=OSError("private_error"))):
                    with self.assertNoLogs(self.env["_LOGGER"], level="DEBUG"):
                        with self.assertRaises(OSError):
                            if action == "add":
                                await store.add(record)
                            elif action == "update":
                                await store.update(pid, updated, before[pid])
                            else:
                                await store.delete(pid)
                self.assertEqual(await store.list(), before)
        await store.delete(pid)
        for index in range(20):
            await self.flow._profile_store().add(profiles.create_profile(self.client, str(index), {"C031": "1"}))
        with self.assertRaisesRegex(ValueError, "^profile_count_limit$"):
            await self.flow._profile_store().add(profiles.create_profile(self.client, "extra", {"C031": "1"}))
        for language in ("pl", "en"):
            errors = translation_strings(language)["options"]["error"]
            self.assertIn("profile_count_limit", errors)
            self.assertNotIn("profile_limit", errors)
        self.flow.config_entry = SimpleNamespace(entry_id="other", options={})
        self.assertEqual(await self.flow._profile_store().list(), {})


class ProfileImportFlowSafetyTests(ProfileFlowFixture, unittest.IsolatedAsyncioTestCase):

    async def prepare_warning(self, raw="8500"):
        self.flow.config_entry.options["boiler_temp_max"] = 70
        pid = await self.add_profile({"BoilerTempCmd": raw})
        await self.load_profile(pid)
        selected = await self.flow.async_step_profile_preview(self.selection(["BoilerTempCmd"]))
        self.assertEqual(selected["step_id"], "profile_confirm")
        warning = await self.flow.async_step_profile_send()
        self.assertEqual(warning["step_id"], "profile_temperature_limit")
        self.client.send_command.assert_not_called()
        return warning

    async def test_import_and_edit_keep_temperature_above_user_limit(self):
        self.flow.config_entry.options["boiler_temp_max"] = 70
        self.env["_read_upload"] = lambda *_: profiles.create_profile(self.client, "Import", {"BoilerTempCmd": "8500"})
        await self.flow.async_step_profile_import_new()
        view = await self.flow.async_step_profile_file({"file": "id"})
        self.assertEqual(view["step_id"], "profile_import_review")
        self.assertEqual(view["description_placeholders"]["skipped"], "")
        self.assertIn("85 °C", view["description_placeholders"]["settings"])
        await self.flow.async_step_profile_import_review({"name": "Import"})
        await self.flow.async_step_profile_write_select(self.selection(["BoilerTempCmd"]))
        await self.flow.async_step_profile_write_commit()
        pid = self.flow._profile_id
        self.assertEqual((await self.flow._profile_store().list())[pid]["params"], {"BoilerTempCmd": "8500"})
        view = await self.flow.async_step_profile_edit_settings()
        self.assertIn("Zakres: **40–90 °C**", view["description_placeholders"]["value_boiler_temp_setpoint"])
        view = await self.flow.async_step_profile_edit_settings({"boiler_burner": {"boiler_temp_setpoint": 90}})
        self.assertEqual(view["step_id"], "profile_update_confirm")
        await self.flow.async_step_profile_write_commit()
        self.assertEqual((await self.flow._profile_store().list())[pid]["params"]["BoilerTempCmd"], "9000")
        self.assertEqual(self.flow.config_entry.options["boiler_temp_max"], 70)
        self.client.send_command.assert_not_called()

    async def test_warning_requires_explicit_approval_and_is_not_reused(self):
        warning = await self.prepare_warning()
        self.assertEqual(warning["menu_options"], ["profile_preview", "profile_send_override"])
        self.assertEqual(warning["description_placeholders"], {"value": "85 °C", "limit": "70 °C"})
        for language in ("pl", "en"):
            strings = translation_strings(language)
            strings["options"]["step"]["profile_temperature_limit"]["description"].format(**warning["description_placeholders"])
            self.assertNotIn("profile_limit", strings["options"]["step"])
        async def send(command, **kwargs):
            self.client.data.update(command)
            self.client.hass.bus.fire()
        self.client.send_command.side_effect = send
        progress = await self.flow.async_step_profile_send_override()
        self.assertEqual(progress["step_id"], "profile_apply")
        await self.flow._profile_task
        self.assertIsNone(self.flow._profile_limit_request)
        self.assertEqual(self.flow.config_entry.options["boiler_temp_max"], 70)
        self.client.send_command.assert_awaited_once()
        self.client.send_command.reset_mock()
        self.client.data["BoilerTempCmd"] = "6000"
        await self.flow.async_step_profile_load()
        await self.flow.async_step_profile_preview(self.selection(["BoilerTempCmd"]))
        warning = await self.flow.async_step_profile_send()
        self.assertEqual(warning["step_id"], "profile_temperature_limit")
        self.client.send_command.assert_not_called()

    async def test_changed_value_or_limit_requires_a_new_warning(self):
        await self.prepare_warning()
        self.flow._profile_selected["BoilerTempCmd"] = "8600"
        warning = await self.flow.async_step_profile_send_override()
        self.assertEqual(warning["step_id"], "profile_temperature_limit")
        self.assertEqual(warning["description_placeholders"]["value"], "86 °C")
        self.flow.config_entry.options["boiler_temp_max"] = 75
        warning = await self.flow.async_step_profile_send_override()
        self.assertEqual(warning["description_placeholders"]["limit"], "75 °C")
        self.client.send_command.assert_not_called()

    async def test_return_to_selection_and_stale_controller_data_do_not_send(self):
        await self.prepare_warning()
        view = await self.flow.async_step_profile_preview()
        self.assertEqual(view["step_id"], "profile_settings")
        self.assertIsNone(self.flow._profile_limit_request)
        self.client.send_command.assert_not_called()
        self.client.data["BoilerTempCmd"] = "6500"
        view = await self.flow.async_step_profile_send_override()
        self.assertEqual(view["step_id"], "profile_settings")
        self.client.send_command.assert_not_called()

    async def test_scope_of_skipped_notes_in_both_translations(self):
        notes = self.flow._profile_notes({"C016": "invalid_value", "C030": "duplicate", "CH1ReturnTempCmd": "duplicate"})
        self.assertIn("Nieprawidłowe wartości lub tryby (1)", notes)
        self.assertIn("Powtórzone nastawy (2)", notes)
        self.assertIn("CO1 Powrót Temp. min.", notes)
        for language in ("pl", "en"):
            data = translation_strings(language)
            for reason in ("schedule", "read_only", "unsupported", "invalid_value", "out_of_range", "duplicate"):
                data["common"]["profile_skip_group_" + reason].format(count=2)
            self.assertNotIn("Wartości bez zmian", data["options"]["step"]["profile_settings"]["description"])
            self.assertEqual(data["options"]["step"]["profile_result"]["title"],
                             "Wynik wysyłania nastaw" if language == "pl" else "Settings sending result")
    async def test_skipped_parameters_have_group_counts_and_escaped_details(self):
        notes = self.flow._profile_notes({"C042": "schedule", "DHWHours": "schedule", "BuFuelCaloric": "read_only",
            "C987": "unsupported", '<img src="x">': "unsupported", "BoilerTempCmd": "out_of_range"})
        self.assertIn("Harmonogramy (2)", notes)
        self.assertIn("Parametry tylko do odczytu (1)", notes)
        self.assertIn("Nieobsługiwane parametry (2)", notes)
        self.assertIn("Wartości poza zakresem (1)", notes)
        summary, details = notes.split("<details>", 1)
        self.assertNotIn("C987", summary)
        self.assertIn("C987", details)
        self.assertIn("Szczegóły techniczne", details)
        self.assertIn("Paliwo Kaloryczność (BuFuelCaloric)", details)
        self.assertIn("&lt;img", details)
        self.assertNotIn("<img", notes)
        self.assertNotIn("<details open", notes)
        self.assertEqual(self.flow._profile_notes({}), "")

    async def test_partial_result_is_collapsed_and_error_stays_above_sections(self):
        self.flow._profile_result = {"saved": ["BoilerTempCmd"], "failed": ["C031"],
                                     "remaining": ["D200"], "error": "profile_not_confirmed"}
        view = await self.flow.async_step_profile_result()
        summary = view["description_placeholders"]["summary"]
        self.assertIn("Zapisano nastaw: 1 z 3.", summary)
        for heading in ("Zapisane (1)", "Niepotwierdzone (1)", "Niewysłane (1)"):
            self.assertIn("<details><summary>" + heading + "</summary>", summary)
        before, _ = summary.split("<details>", 1)
        self.assertIn("Sterownik nie potwierdził", before)
        self.assertNotIn("<details open", summary)
        self.flow._profile_result = {"saved": [], "failed": [], "remaining": ["C031"], "error": "profile_busy"}
        view = await self.flow.async_step_profile_result()
        summary = view["description_placeholders"]["summary"]
        self.assertNotIn("<summary>Zapisane", summary)
        self.assertNotIn("<summary>Niepotwierdzone", summary)

    async def test_file_update_saves_selection_only_after_confirmation_without_commands(self):
        self.flow.config_entry.options["boiler_temp_max"] = 70
        pid = await self.add_profile({"BoilerTempCmd": "6500", "C031": "1"})
        before = deepcopy((await self.flow._profile_store().list())[pid])
        self.env["_read_upload"] = lambda *_: profiles.create_profile(self.client, "Plik", {"BoilerTempCmd": "8500", "C031": "0"})
        await self.flow.async_step_profile_import_update()
        await self.flow.async_step_profile_file({"file": "id"})
        await self.flow._profile_update_target(pid)
        result = await self.flow.async_step_profile_write_select(self.selection(["BoilerTempCmd"]))
        self.assertEqual(result["step_id"], "profile_update_confirm")
        self.assertEqual((await self.flow._profile_store().list())[pid], before)
        self.assertIn("85 °C", result["description_placeholders"]["changes"])
        await self.flow.async_step_profile_write_commit()
        saved = (await self.flow._profile_store().list())[pid]
        self.assertEqual(saved["params"], {"BoilerTempCmd": "8500", "C031": "1"})
        self.assertIn("updated_at", saved["_meta"])
        self.client.send_command.assert_not_called()

    async def test_file_update_cancel_returns_to_main_profiles_without_saving(self):
        pid = await self.add_profile({"BoilerTempCmd": "6500"})
        before = deepcopy((await self.flow._profile_store().list())[pid])
        self.env["_read_upload"] = lambda *_: profiles.create_profile(self.client, "Plik", {"BoilerTempCmd": "7000"})
        await self.flow.async_step_profile_import_update()
        await self.flow.async_step_profile_file({"file": "id"})
        await self.flow._profile_update_target(pid)
        result = await self.flow.async_step_profile_write_select(self.selection(["BoilerTempCmd"]))
        self.assertEqual(result["step_id"], "profile_update_confirm")
        view = await self.flow.async_step_profile_write_cancel()
        self.assertEqual(view["step_id"], "profiles")
        self.assertEqual((await self.flow._profile_store().list())[pid], before)
        self.client.send_command.assert_not_called()
