"""Testy formatu, walidacji i bezpiecznego wysyłania profili."""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock

import support
from helpers import ProfileFixture
from skzp_control import profiles
from skzp_control.pending_change import PendingChangeMixin


class ProfileTests(ProfileFixture, unittest.IsolatedAsyncioTestCase):


    def test_export_whitelist_and_roundtrip(self):
        self.client.data.update(Token="secret", DevPin="secret", BoilerTempAct="5500",
                                DHWHours="0" * 48, CommandToDo="Cmd_BurnerStart")
        params, skipped = profiles.export_parameters(self.client)
        self.assertEqual(params, {"BoilerTempCmd": "6000", "C030": "3000", "C031": "0", "D200": "0"})
        self.assertFalse(skipped)
        payload = json.dumps(self.profile(params))
        self.assertNotIn("secret", payload)
        parsed = profiles.parse_profile(payload)
        catalog, valid, skipped = profiles.prepare_profile(self.client, parsed)
        self.assertEqual(valid, params)
        self.assertEqual(profiles.changed_parameters(catalog, valid, self.client.data), {})

    def test_invalid_json_and_metadata(self):
        for text in ('[]', '{}', '{"a":1,"a":2}', 'NaN', 'x' * 65537,
                     json.dumps({"_meta": {"devType": "SKZP"}, "params": {"x": 1}})):
            with self.subTest(text=text[:50]), self.assertRaises(ValueError):
                profiles.parse_profile(text)
        p = self.profile({"BoilerTempCmd": "6500"})
        p["_meta"]["version"] = 2
        with self.assertRaises(ValueError):
            profiles.parse_profile(json.dumps(p))

    def test_ranges_and_aliases(self):
        _, valid, skipped = profiles.prepare_profile(self.client, self.profile({
            "BoilerTempCmd": "9500", "CH1ReturnTempCmd": "5000", "CH1ReturnProtAct": "On",
            "DHWDays": "1" * 28, "Token": "secret", "DevStatus": "OFF"}))
        self.assertEqual(valid, {"C030": "5000", "C031": "1"})
        self.assertEqual(skipped["BoilerTempCmd"], "out_of_range")
        self.assertEqual(skipped["Token"], "unsupported")
        for invalid in (True, [], {}, "nan", "inf", "-10", "1e999999999"):
            _, valid, skipped = profiles.prepare_profile(self.client, self.profile({"BoilerTempCmd": invalid}))
            self.assertFalse(valid)
            self.assertIn("BoilerTempCmd", skipped)

    def test_duplicate_alias_does_not_overwrite(self):
        _, valid, skipped = profiles.prepare_profile(self.client, self.profile({
            "C030": "4000", "CH1ReturnTempCmd": "5000"}))
        self.assertNotIn("C030", valid)
        self.assertEqual(skipped["C030"], "duplicate")

    def test_differences_normalize_numbers_aliases_and_missing_values(self):
        catalog = profiles.setting_catalog(self.client)
        incoming = {"BoilerTempCmd": "6000", "C030": "3000", "C031": "1"}
        cases = (
            ({"BoilerTempCmd": "6000.0", "CH1ReturnTempCmd": 3000, "C031": "1"}, {}),
            ({"BoilerTempCmd": "6000", "C031": "1"}, {"C030": "3000"}),
            ({"BoilerTempCmd": "6500", "C030": "bad", "C031": "0"}, incoming),
        )
        for baseline, expected in cases:
            with self.subTest(baseline=baseline):
                before = dict(baseline)
                self.assertEqual(profiles.changed_parameters(catalog, incoming, baseline), expected)
                self.assertEqual(baseline, before)
        self.assertEqual(incoming, {"BoilerTempCmd": "6000", "C030": "3000", "C031": "1"})

    def test_model_and_firmware(self):
        p = self.profile({"D200": "1"})
        self.client.data["DevType"] = "SKZP-05S_V5.65"
        _, params, skipped = profiles.prepare_profile(self.client, p)
        self.assertFalse(params)
        self.assertEqual(skipped["D200"], "unsupported")
        self.client.model = "SKZP-02S"
        with self.assertRaisesRegex(ValueError, "profile_model_mismatch"):
            profiles.prepare_profile(self.client, p)

    def test_pair_order_and_partial_selection(self):
        from skzp_control.number import MIN_MAX_PAIRS, NUMBER_DESCRIPTIONS

        for low, high in MIN_MAX_PAIRS:
            with self.subTest(low=low, high=high):
                def wire(key, bound):
                    meta = NUMBER_DESCRIPTIONS[key]
                    return str(int(meta[bound] * meta.get("divider", 1)))

                self.client.data = {"DevType": "SKZP-05S_V5.71",
                                    low: wire(low, "min"), high: wire(high, "min")}
                catalog = profiles.setting_catalog(self.client)
                raised = {low: wire(low, "max"), high: wire(high, "max")}
                self.assertEqual(profiles.order_changes(self.client, catalog, raised), [high, low])
                with self.assertRaisesRegex(ValueError, "profile_invalid_pair"):
                    profiles.order_changes(self.client, catalog, {low: raised[low]})

                self.client.data.update(raised)
                lowered = {high: wire(high, "min"), low: wire(low, "min")}
                self.assertEqual(profiles.order_changes(self.client, catalog, lowered), [low, high])
                with self.assertRaisesRegex(ValueError, "profile_invalid_pair"):
                    profiles.order_changes(self.client, catalog, {high: lowered[high]})

    async def test_success_uses_write_alias_and_confirmation(self):
        sent = []
        async def send(command, **kwargs):
            self.assertTrue(kwargs["profile"])
            sent.append(command)
            self.client.data["C031"] = "1"
            self.client.hass.bus.fire()
        self.client.send_command = send
        record = self.profile({"C031": "1"})
        record["_meta"].update(name="private_profile_name", description="private_profile_description")
        self.client.data.update(Token="private_token", DevPin="private_pin")
        with self.assertLogs(profiles._LOGGER, level="DEBUG") as logs:
            result = await profiles.apply_profile(self.client, {}, record, {"C031": "1"})
        self.assertEqual(sent, [{"CH1ReturnProtAct": "On"}])
        self.assertEqual(result["saved"], ["C031"])
        self.assertEqual(result["remaining"], [])
        self.assertFalse(self.client.profile_import_active)
        self.assertEqual(self.client.hass.bus.listeners, [])
        messages = [item.getMessage() for item in logs.records]
        self.assertTrue(messages[0].endswith("Profile sending started: selected=1."))
        self.assertTrue(messages[-1].endswith("Profile sending finished: confirmed=1, unconfirmed=0, not_sent=0."))
        for secret in ("private_profile_name", "private_profile_description", "private_token", "private_pin"):
            self.assertNotIn(secret, "\n".join(messages))

    async def test_retry_and_stop_no_rollback(self):
        self.client.send_command = AsyncMock()
        params = {"C031": "1", "BoilerTempCmd": "6500"}
        with self.assertLogs(profiles._LOGGER, level="DEBUG") as logs:
            result = await profiles.apply_profile(self.client, {}, self.profile(params), params)
        self.assertEqual(self.client.send_command.await_count, 2)
        self.assertEqual(result["failed"], ["C031"])
        self.assertEqual(result["remaining"], ["BoilerTempCmd"])
        self.assertEqual([call.args[2] for call in self.client.log_command_sent.call_args_list], [0, 1])
        self.assertTrue(logs.records[-1].getMessage().endswith(
            "Profile sending stopped: confirmed=0, unconfirmed=1, not_sent=1."))

    async def test_busy_offline_cancel_and_validation_send_nothing(self):
        self.client.send_command = AsyncMock()
        p = self.profile({"C031": "1"})
        self.client.pending_entities = {1}
        result = await profiles.apply_profile(self.client, {}, p, p["params"])
        self.assertEqual(result["error"], "profile_busy")
        self.client.pending_entities.clear()
        self.client.available = False
        result = await profiles.apply_profile(self.client, {}, p, p["params"])
        self.assertEqual(result["error"], "profile_offline")
        self.client.available = True
        result = await profiles.apply_profile(self.client, {}, p, {"C031": "bad"})
        self.assertEqual(result["error"], "profile_changed")
        self.client.send_command.assert_not_called()
        self.client.send_command.side_effect = asyncio.CancelledError
        with self.assertLogs(profiles._LOGGER, level="DEBUG") as logs:
            with self.assertRaises(asyncio.CancelledError):
                await profiles.apply_profile(self.client, {}, p, p["params"])
        self.assertTrue(logs.records[-1].getMessage().endswith(
            "Profile sending cancelled: confirmed=0, unconfirmed=1, not_sent=0."))
        self.assertFalse(any("Profile sending finished" in item.getMessage() for item in logs.records))
        self.assertFalse(self.client.profile_import_active)
        self.assertEqual(self.client.command_manager.active_count, 0)
        self.assertFalse(self.client.hass.bus.listeners)

    async def test_late_confirmation_avoids_retry(self):
        def confirm():
            self.client.data["C031"] = "1"
            self.client.hass.bus.fire()
        async def send(*args, **kwargs):
            asyncio.get_running_loop().call_later(0.01, confirm)
        self.client.command_retry_delay = 0.03
        self.client.send_command = AsyncMock(side_effect=send)
        p = self.profile({"C031": "1"})
        result = await profiles.apply_profile(self.client, {}, p, p["params"])
        self.assertEqual(result["saved"], ["C031"])
        self.client.send_command.assert_awaited_once()
        self.client.log_command_sent.assert_called_once()
        self.assertEqual(self.client.log_command_sent.call_args.args[2], 0)

    async def test_partial_failure_keeps_confirmed_results(self):
        for cancelled in (False, True):
            with self.subTest(cancelled=cancelled):
                async def send(command, **kwargs):
                    if "CH1ReturnProtAct" in command:
                        self.client.data["C031"] = "1"
                        self.client.hass.bus.fire()
                    elif cancelled:
                        raise asyncio.CancelledError()
                    else:
                        raise support.HAError(translation_key="not_connected")
                self.client.send_command = send
                params = {"C031": "1", "BoilerTempCmd": "6500"}
                with self.assertLogs(profiles._LOGGER, level="DEBUG") as logs:
                    if cancelled:
                        with self.assertRaises(asyncio.CancelledError):
                            await profiles.apply_profile(self.client, {}, self.profile(params), params)
                    else:
                        result = await profiles.apply_profile(self.client, {}, self.profile(params), params)
                        self.assertEqual(result["saved"], ["C031"])
                        self.assertEqual(result["failed"], ["BoilerTempCmd"])
                        self.assertFalse(result["remaining"])
                status = "cancelled" if cancelled else "stopped"
                self.assertTrue(logs.records[-1].getMessage().endswith(
                    f"Profile sending {status}: confirmed=1, unconfirmed=1, not_sent=0."))
                self.assertFalse(self.client.profile_import_active)
                self.assertFalse(self.client.hass.bus.listeners)

    def test_pending_debounce_blocks_import_and_import_blocks_new_changes(self):
        entity = PendingChangeMixin()
        entity._client = self.client
        entity._init_pending_change()
        entity._begin_pending_change(1, 10)
        self.assertIn(id(entity), self.client.pending_entities)
        entity._clear_pending_change()
        self.assertFalse(self.client.pending_entities)
        self.client.profile_import_active = True
        with self.assertRaises(support.HAError):
            entity._begin_pending_change(1, 10)


class ProfileImportSafetyTests(ProfileFixture, unittest.IsolatedAsyncioTestCase):

    def test_all_room_modes_import_as_numeric_for_each_circuit(self):
        self.client.data.update(C016="0", C116="0", C216="0")
        for key in ("C016", "C116", "C216"):
            for text, number in (("Off", "0"), ("Economy", "1"), ("Comfort", "2"), ("Timer", "3")):
                for raw in (text, text.lower(), number):
                    with self.subTest(key=key, raw=raw):
                        _, valid, skipped = profiles.prepare_profile(self.client, self.profile({key: raw}))
                        self.assertEqual(valid, {key: number})
                        self.assertFalse(skipped)

    def test_numeric_room_modes_import_as_text_when_controller_uses_text(self):
        self.client.data["C016"] = "Comfort"
        for raw, expected in (("0", "Off"), ("1", "Economy"), ("2", "Comfort"), ("3", "Timer")):
            _, valid, skipped = profiles.prepare_profile(self.client, self.profile({"C016": raw}))
            self.assertEqual(valid, {"C016": expected})
            self.assertFalse(skipped)
        for invalid in ("Auto", "4", True, [], {}):
            _, valid, skipped = profiles.prepare_profile(self.client, self.profile({"C016": invalid}))
            self.assertFalse(valid)
            self.assertEqual(skipped["C016"], "invalid_value")

    def test_storage_and_export_ignore_user_limit_but_keep_standard_range(self):
        options = {"boiler_temp_max": 60}
        self.client.data["BoilerTempCmd"] = "8500"
        params, skipped = profiles.export_parameters(self.client)
        self.assertEqual(params["BoilerTempCmd"], "8500")
        self.assertNotIn("BoilerTempCmd", skipped)
        for raw in ("8500", "9000"):
            catalog, valid, skipped = profiles.prepare_profile(self.client, self.profile({"BoilerTempCmd": raw}))
            self.assertEqual(valid, {"BoilerTempCmd": raw})
            self.assertFalse(skipped)
            with self.assertRaisesRegex(ValueError, "^profile_boiler_limit$"):
                profiles.validate_profile_limit(catalog, options, valid)
        _, valid, skipped = profiles.prepare_profile(self.client, self.profile({"BoilerTempCmd": "9500"}))
        self.assertFalse(valid)
        self.assertEqual(skipped["BoilerTempCmd"], "out_of_range")
        self.assertEqual(options, {"boiler_temp_max": 60})

    def test_numeric_modes_import_for_readable_skzp02_key(self):
        self.client.model = "SKZP-02S"
        self.client.data = {"DevType": "SKZP-02S_V2.41", "CH1RoomMode": "Off"}
        for number, text in (("0", "Off"), ("1", "Economy"), ("2", "Comfort"), ("3", "Timer")):
            _, valid, skipped = profiles.prepare_profile(self.client, self.profile({"CH1RoomMode": number}))
            self.assertEqual(valid, {"CH1RoomMode": text})
            self.assertFalse(skipped)

    def test_skip_reasons_distinguish_schedules_readings_and_unknown_keys(self):
        _, valid, skipped = profiles.prepare_profile(self.client, self.profile({
            "C042": "0000", "DHWHours": "0000", "BuFuelCaloric": "69", "C987": "123"}))
        self.assertFalse(valid)
        self.assertEqual(skipped, {"C042": "schedule", "DHWHours": "schedule",
                                   "BuFuelCaloric": "read_only", "C987": "unsupported"})

    async def test_limit_blocks_all_commands_before_first_send(self):
        self.client.send_command = AsyncMock()
        selected = {"C031": "1", "BoilerTempCmd": "8500"}
        record = self.profile(selected)
        record["_meta"]["boiler_limit_override"] = ["8500", 70]
        result = await profiles.apply_profile(self.client, {"boiler_temp_max": 70}, record, selected)
        self.assertEqual(result["error"], "profile_boiler_limit")
        self.assertEqual(result["remaining"], list(selected))
        self.client.send_command.assert_not_called()
        self.assertFalse(self.client.profile_import_active)

    async def test_only_exact_approval_allows_the_value_without_changing_options(self):
        options = {"boiler_temp_max": 70}
        selected = {"BoilerTempCmd": "8500"}
        self.client.send_command = AsyncMock()
        for wrong in (None, ("8600", 70), ("8500", 75), ("8500.0", 70)):
            result = await profiles.apply_profile(self.client, options, self.profile(selected), selected,
                                                  boiler_limit_override=wrong)
            self.assertEqual(result["error"], "profile_boiler_limit")
        self.client.send_command.assert_not_called()
        async def send(command, **kwargs):
            self.client.data.update(command)
            self.client.hass.bus.fire()
        self.client.send_command.side_effect = send
        result = await profiles.apply_profile(self.client, options, self.profile(selected), selected,
                                              boiler_limit_override=("8500", 70))
        self.assertEqual(result["saved"], ["BoilerTempCmd"])
        self.client.send_command.assert_awaited_once_with({"BoilerTempCmd": "8500"}, profile=True)
        self.assertEqual(options, {"boiler_temp_max": 70})
        self.client.send_command.reset_mock()
        result = await profiles.apply_profile(self.client, options, self.profile(selected), selected)
        self.assertEqual(result["error"], "profile_boiler_limit")
        self.client.send_command.assert_not_called()

    async def test_approval_cannot_bypass_standard_range(self):
        self.client.send_command = AsyncMock()
        selected = {"BoilerTempCmd": "9500"}
        result = await profiles.apply_profile(self.client, {"boiler_temp_max": 70}, self.profile(selected), selected,
                                              boiler_limit_override=("9500", 70))
        self.assertEqual(result["error"], "profile_changed")
        self.client.send_command.assert_not_called()
