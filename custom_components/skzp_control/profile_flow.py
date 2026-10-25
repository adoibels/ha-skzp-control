"""Formularze zapisu, importu i wczytywania profili nastaw."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, DecimalException
from html import escape

import voluptuous as vol
from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import callback
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    FileSelector, FileSelectorConfig, TextSelector, TextSelectorConfig,
    NumberSelector, NumberSelectorConfig, NumberSelectorMode,
    SelectSelector, SelectSelectorConfig, SelectSelectorMode,
)
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .entity_layout import PAGE_ORDER, entity_location
from .frame_download import async_create_frame_download
from .profiles import (
    MAX_PROFILE_BYTES, MAX_PROFILES, apply_profile, changed_parameters, create_profile,
    export_parameters, order_changes, order_profile_parameters, parse_profile, prepare_profile,
    setting_catalog, profile_boiler_limit,
)
from .parameter_resolver import get_parameter_value, parameter_candidates
from .localization import profile_page_title, translate

_LOGGER = logging.getLogger(__name__)


def _safe_text(text) -> str:
    """Neutralizuje formatowanie w nazwach pochodzących z pliku."""
    return re.sub(r"([\\`*_{}\[\]()#+.!|<>~-])", r"\\\1", str(text).replace("\n", " "))


def _read_upload(hass, file_id):
    """Czyta i usuwa plik tymczasowy w wątku roboczym HA."""
    with process_uploaded_file(hass, file_id) as path:
        with path.open("rb") as stream:
            payload = stream.read(MAX_PROFILE_BYTES + 1)
        if len(payload) > MAX_PROFILE_BYTES:
            raise ValueError("invalid_profile")
        return parse_profile(payload.decode("utf-8-sig"))


class ProfileStore:
    """Przechowuje profile poza opcjami, bez przeładowywania integracji."""

    def __init__(self, hass, entry_id):
        self.store = Store(hass, 1, f"{DOMAIN}.profiles.{entry_id}")
        self.lock = asyncio.Lock()

    async def list(self):
        return deepcopy(await self.store.async_load() or {})

    async def add(self, profile):
        async with self.lock:
            profiles = await self.list()
            if len(profiles) >= MAX_PROFILES:
                raise ValueError("profile_count_limit")
            profile_id = uuid.uuid4().hex
            profiles[profile_id] = deepcopy(profile)
            await self.store.async_save(profiles)
            _LOGGER.debug("[SKZP Control] Profile %s created in Home Assistant (settings=%s).",
                          profile_id, len(profile["params"]))
            return profile_id

    async def update(self, profile_id, profile, expected):
        """Chroni profil przed nadpisaniem zmian z innego formularza."""
        async with self.lock:
            profiles = await self.list()
            if profile_id not in profiles or profiles[profile_id] != expected:
                raise ValueError("profile_changed")
            replacement = deepcopy(profile)
            replacement["_meta"]["updated_at"] = datetime.now(timezone.utc).isoformat()
            profiles[profile_id] = replacement
            await self.store.async_save(profiles)
            _LOGGER.debug("[SKZP Control] Profile %s updated in Home Assistant (settings=%s).",
                          profile_id, len(replacement["params"]))

    async def delete(self, profile_id):
        async with self.lock:
            profiles = await self.list()
            removed = profiles.pop(profile_id, None)
            await self.store.async_save(profiles)
            if removed is not None:
                _LOGGER.debug("[SKZP Control] Profile %s deleted from Home Assistant.", profile_id)


class ProfileFlowMixin:
    """Dodaje profile do formularza opcji istniejącego sterownika."""

    _profile_task = None
    _profile_download_remove = None

    def _profile_client(self):
        client = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if client is None or not client.available:
            raise ValueError("profile_offline")
        return client

    def _profile_store(self):
        stores = self.hass.data.setdefault(f"{DOMAIN}_profiles", {})
        entry_id = self.config_entry.entry_id
        if entry_id not in stores:
            stores[entry_id] = ProfileStore(self.hass, entry_id)
        return stores[entry_id]

    def _profile_notes(self, skipped):
        if not skipped:
            return ""
        from .sensors_map import SENSOR_MAP
        client = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        catalog = setting_catalog(client) if client else {}
        groups, details = {}, []
        for key, reason in skipped.items():
            if reason not in {"schedule", "read_only", "unsupported", "invalid_value", "out_of_range", "duplicate"}:
                reason = "unsupported"
            groups[reason] = groups.get(reason, 0) + 1
            setting = next((catalog[item] for item in parameter_candidates(key) if item in catalog), None)
            name = setting.name(client) if setting else key
            if setting is None and key in SENSOR_MAP:
                suffix = SENSOR_MAP[key].get("translation_key", key.lower())
                translated = translate(self.hass, f"entity.sensor.{suffix}.name")
                if translated != f"entity.sensor.{suffix}.name":
                    name = translated
            label = name if name == key else f"{name} ({key})"
            details.append("<li>" + escape(label) + " — " + escape(translate(
                self.hass, "common.profile_skip_" + reason)) + "</li>")
        summary = "".join("<li>" + escape(translate(self.hass,
            "common.profile_skip_group_" + reason, count=count)) + "</li>"
            for reason, count in groups.items())
        return ("\n\n<p>" + escape(translate(self.hass, "common.profile_skipped")) + "</p><ul>" +
                summary + "</ul><details><summary>" + escape(translate(
                self.hass, "common.profile_skip_details")) + "</summary><ul>" +
                "".join(details) + "</ul></details>")

    async def async_step_profiles(self, user_input=None):
        self._profile_edit_active = False
        self._profile_remove_active = False
        profiles = await self._profile_store().list()
        choices = ["profile_save", "profile_import"]
        if profiles:
            choices += ["profile_manage"]
        variant = "existing" if profiles else "new"
        return self.async_show_menu(step_id="profiles", menu_options=[*choices, "init"],
            description_placeholders={
                "profile_save_description": translate(self.hass, "common.profile_save_" + variant),
                "profile_import_description": translate(self.hass, "common.profile_import_" + variant)})

    async def async_step_profile_save(self, user_input=None):
        if not await self._profile_store().list():
            return await self.async_step_profile_save_new()
        return self.async_show_menu(step_id="profile_save",
            menu_options=["profile_save_new", "profile_save_update", "profiles"])

    async def _profile_capture(self, mode):
        client = self._profile_client()
        params, skipped = export_parameters(client)
        if not params:
            raise ValueError("profile_empty")
        self._profile_write_mode = mode
        self._profile_source_kind = "controller"
        self._profile_source = create_profile(client, "", params)
        self._profile_message = self._profile_notes(skipped)
        self._profile_write_checked = None
        self._profile_duplicate_ack = None
        self._profile_name_values = {}

    async def async_step_profile_save_new(self, user_input=None):
        try:
            await self._profile_capture("new")
            return await self.async_step_profile_name()
        except ValueError as err:
            return self._profile_error("profile_save_error", str(err))

    async def async_step_profile_save_update(self, user_input=None):
        try:
            await self._profile_capture("update")
            return await self.async_step_profile_update_pick()
        except ValueError as err:
            return self._profile_error("profile_save_error", str(err))

    def _profile_error(self, step, reason):
        return self.async_show_form(step_id=step, data_schema=vol.Schema({}),
                                    errors={"base": reason})

    async def async_step_profile_save_error(self, user_input=None):
        return await self.async_step_profiles()

    def _profile_fields(self, values):
        return {
            vol.Required("name", default=values.get("name", "")): str,
            vol.Optional("description", default=values.get("description", "")):
                TextSelector(TextSelectorConfig(multiline=True)),
        }

    @staticmethod
    def _profile_description(value):
        if not isinstance(value, str) or len(value) > 1000:
            raise ValueError("profile_invalid_description")
        if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in value):
            raise ValueError("profile_invalid_description")
        return value.strip()

    @staticmethod
    def _profile_name(value):
        name = str(value).strip()
        if not name or len(name) > 80 or any(ord(ch) < 32 for ch in name):
            raise ValueError("profile_invalid_name")
        return name

    async def _profile_name_exists(self, name, exclude=None):
        return any(
            key != exclude and profile["_meta"]["name"].strip().casefold() == name.casefold()
            for key, profile in (await self._profile_store().list()).items()
        )

    async def async_step_profile_name(self, user_input=None):
        errors = {}
        if user_input is not None:
            self._profile_name_values = user_input
            try:
                name = self._profile_name(user_input.get("name", ""))
                description = self._profile_description(user_input.get("description", ""))
                self._profile_source["_meta"].update(name=name, description=description)
                if await self._profile_name_exists(name):
                    self._profile_duplicate_origin = "new"
                    return await self.async_step_profile_duplicate()
                return await self.async_step_profile_write_select()
            except ValueError as err:
                errors["base"] = str(err)
        step = "profile_name"
        placeholders = {"skipped": self._profile_message}
        if self._profile_source_kind == "file":
            try:
                client = self._profile_client()
                catalog, params, _ = prepare_profile(
                    client, self._profile_source)
                placeholders["settings"] = self._profile_tables(
                    client, catalog, params)
                step = "profile_import_review"
            except ValueError as err:
                return self._profile_error("profile_write_error", str(err))
        return self.async_show_form(step_id=step, errors=errors,
            data_schema=vol.Schema(self._profile_fields(self._profile_name_values)),
            description_placeholders=placeholders)

    async def async_step_profile_import(self, user_input=None):
        if not await self._profile_store().list():
            return await self.async_step_profile_import_new()
        return self.async_show_menu(step_id="profile_import",
            menu_options=["profile_import_new", "profile_import_update", "profiles"])

    async def _profile_start_import(self, mode):
        self._profile_write_mode = mode
        self._profile_source_kind = "file"
        self._profile_write_checked = None
        self._profile_duplicate_ack = None
        return await self.async_step_profile_file()

    async def async_step_profile_import_new(self, user_input=None):
        return await self._profile_start_import("new")

    async def async_step_profile_import_update(self, user_input=None):
        return await self._profile_start_import("update")

    async def async_step_profile_file(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                client = self._profile_client()
                if not user_input.get("file"):
                    raise ValueError("profile_file_required")
                profile = await self.hass.async_add_executor_job(
                    _read_upload, self.hass, user_input["file"])
                _, params, skipped = prepare_profile(client, profile)
                if not params:
                    raise ValueError("profile_empty")
                self._profile_source = create_profile(client, "", params)
                self._profile_source["_meta"]["devType"] = profile["_meta"]["devType"]
                self._profile_name_values = {
                    "name": str(profile["_meta"].get("name", ""))[:80],
                    "description": self._profile_description(profile["_meta"].get("description", "")),
                }
                self._profile_message = self._profile_notes(skipped)
                if self._profile_write_mode == "update":
                    return await self.async_step_profile_update_pick()
                return await self.async_step_profile_import_review()
            except (ValueError, UnicodeError) as err:
                reason = str(err)
                errors["base"] = reason if reason in {
                    "profile_offline", "profile_empty", "profile_model_mismatch",
                    "profile_file_required", "profile_invalid_description",
                } else "invalid_profile"
            except OSError:
                errors["base"] = "profile_storage_error"
        return self.async_show_form(step_id="profile_file", errors=errors,
            data_schema=vol.Schema({
                vol.Required("file"): FileSelector(FileSelectorConfig(accept=".json"))}))

    async def async_step_profile_import_review(self, user_input=None):
        return await self.async_step_profile_name(user_input)

    async def async_step_profile_duplicate(self, user_input=None):
        values = self._profile_name_values
        return self.async_show_menu(step_id="profile_duplicate",
            menu_options=["profile_duplicate_continue", "profile_duplicate_edit", "profiles"],
            description_placeholders={"name": _safe_text(values["name"])})

    async def async_step_profile_duplicate_continue(self, user_input=None):
        self._profile_duplicate_ack = self._profile_name_values["name"].strip().casefold()
        if self._profile_duplicate_origin == "rename":
            return await self._profile_commit_metadata()
        if self._profile_duplicate_origin == "commit":
            return await self.async_step_profile_write_confirm()
        return await self.async_step_profile_write_select()

    async def async_step_profile_duplicate_edit(self, user_input=None):
        if self._profile_duplicate_origin == "rename":
            return await self.async_step_profile_rename()
        return await self.async_step_profile_name()

    async def async_step_profile_manage(self, user_input=None):
        return await self._profile_picker("profile_manage", self._profile_open, "profiles")

    async def _profile_open(self, profile_id):
        self._profile_id = profile_id
        return await self.async_step_profile_menu()

    async def _profile_picker(self, step_id, on_pick, back):
        profiles = await self._profile_store().list()
        if not profiles:
            return await self.async_step_profiles()
        for step in getattr(self, "_profile_choice_steps", ()):
            delattr(self, "async_step_" + step)
        self._profile_choice_steps = []
        choices = {}
        placeholders = {}
        ordered = sorted(profiles.items(), key=lambda item: item[1]["_meta"]["name"].casefold())
        for index, (profile_id, profile) in enumerate(ordered):
            step = f"profile_pick_{index}"

            async def pick(user_input=None, selected=profile_id):
                return await on_pick(selected)

            setattr(self, "async_step_" + step, pick)
            self._profile_choice_steps.append(step)
            choices[step] = profile["_meta"]["name"]
            description = " ".join(profile["_meta"].get("description", "").split())
            placeholders[f"profile_description_{index}"] = (
                description[:30] + "…" if len(description) > 30 else description)
        choices[back] = translate(self.hass, "common.profile_back")
        return self.async_show_menu(step_id=step_id, menu_options=choices,
                                    description_placeholders=placeholders)

    async def async_step_profile_menu(self, user_input=None):
        profiles = await self._profile_store().list()
        if getattr(self, "_profile_id", None) not in profiles:
            return await self.async_step_profile_manage()
        self._profile = profiles[self._profile_id]
        self._profile_edit_active = False
        self._profile_remove_active = False
        self._profile_rename_values = None
        self._profile_duplicate_ack = None
        return self.async_show_menu(step_id="profile_menu",
            description_placeholders={
                "name": _safe_text(self._profile["_meta"]["name"]),
                "description": ("\n\n" + "\n\n".join(_safe_text(line) for line in
                    self._profile["_meta"].get("description", "").splitlines())
                    if self._profile["_meta"].get("description", "").strip() else ""),
                "updated": self._profile_updated_text(self._profile)},
            menu_options=["profile_load", "profile_rename", "profile_edit_settings",
                          "profile_remove_settings", "profile_export", "profile_delete", "profile_manage"])

    def _profile_updated_text(self, profile):
        value = profile["_meta"].get("updated_at", profile["_meta"].get("date"))
        try:
            date = dt_util.parse_datetime(value) if isinstance(value, str) else None
            if date:
                formatted = dt_util.as_local(date).strftime("%d.%m.%Y, %H:%M")
                return "\n\n<small>" + escape(translate(
                    self.hass, "common.profile_updated", date=formatted)) + "</small>"
        except (ValueError, TypeError, OverflowError):
            pass
        return ""

    async def async_step_profile_rename(self, user_input=None):
        errors = {}
        if self._profile_rename_values is None:
            self._profile_metadata_snapshot = deepcopy(self._profile)
        if user_input is not None:
            self._profile_rename_values = user_input
            try:
                name = self._profile_name(user_input.get("name", ""))
                description = self._profile_description(user_input.get("description", ""))
                self._profile_name_values = {"name": name, "description": description}
                if await self._profile_name_exists(name, self._profile_id):
                    self._profile_duplicate_origin = "rename"
                    return await self.async_step_profile_duplicate()
                return await self._profile_commit_metadata()
            except ValueError as err:
                errors["base"] = str(err)
            except OSError:
                errors["base"] = "profile_storage_error"
        return self.async_show_form(step_id="profile_rename", errors=errors,
            data_schema=vol.Schema(self._profile_fields(
                self._profile_rename_values or self._profile["_meta"])))

    async def _profile_commit_metadata(self):
        try:
            updated = deepcopy(self._profile_metadata_snapshot)
            updated["_meta"].update(self._profile_name_values)
            await self._profile_store().update(self._profile_id, updated,
                                                self._profile_metadata_snapshot)
            return await self.async_step_profile_menu()
        except ValueError as err:
            return self._profile_error("profile_write_error", str(err))
        except OSError:
            return self._profile_error("profile_write_error", "profile_storage_error")

    async def async_step_profile_update_pick(self, user_input=None):
        if not await self._profile_store().list():
            return await self.async_step_profiles()
        back = "profile_import" if self._profile_source_kind == "file" else "profile_save"
        return await self._profile_picker("profile_update_pick", self._profile_update_target, back)

    async def _profile_update_target(self, profile_id):
        profiles = await self._profile_store().list()
        if profile_id not in profiles:
            return await self.async_step_profile_update_pick()
        self._profile_target_id = profile_id
        self._profile_target = profiles[profile_id]
        self._profile_write_checked = None
        return await self.async_step_profile_write_select()

    @staticmethod
    def _profile_merge(original, selected, removed=()):
        """Zmienia wybrane klucze, zachowując pozostałe i usuwając ich stare aliasy."""
        merged = deepcopy(original)
        for key in removed:
            for previous in tuple(merged):
                if previous in parameter_candidates(key) or key in parameter_candidates(previous):
                    merged.pop(previous)
        for key, value in selected.items():
            for previous in tuple(merged):
                if previous in parameter_candidates(key) or key in parameter_candidates(previous):
                    merged.pop(previous)
            merged[key] = value
        return merged

    @staticmethod
    def _profile_validate_pairs(catalog, params):
        from .number import MIN_MAX_PAIRS
        for low, high in MIN_MAX_PAIRS:
            if low in catalog and high in catalog and low in params and high in params:
                if catalog[low].decode(params[low]) > catalog[high].decode(params[high]):
                    raise ValueError("profile_invalid_pair")

    async def async_step_profile_write_select(self, user_input=None):
        errors = {}
        try:
            client = self._profile_client()
            catalog, params, skipped = prepare_profile(client, self._profile_source)
            if self._profile_write_mode == "new":
                baseline = None
                changes = params
            else:
                _, baseline, _ = prepare_profile(client, self._profile_target)
                changes = changed_parameters(catalog, params, baseline)
                if not changes:
                    self._profile_message += self._profile_notes(skipped)
                    return await self.async_step_profile_write_unchanged()
                params = changes
            checked = self._profile_write_checked
            if checked is None:
                checked = list(changes)
            if user_input is not None:
                selected = self._profile_selected_fields(user_input)
                if any(key not in params for key in selected):
                    raise ValueError("profile_changed")
                checked = selected
                self._profile_write_checked = checked
                if not checked:
                    raise ValueError("profile_no_selection")
                self._profile_write_selected = {key: params[key] for key in checked}
                self._profile_write_catalog = catalog
                self._profile_write_baseline = baseline
                candidate = self._profile_write_selected if baseline is None else self._profile_merge(
                    self._profile_target["params"], self._profile_write_selected)
                _, valid, _ = prepare_profile(client,
                    {**self._profile_source, "params": candidate})
                self._profile_validate_pairs(catalog, valid)
                return await self.async_step_profile_write_confirm()
            self._profile_write_checked = checked
            schema, placeholders = self._profile_preview_schema(
                client, catalog, params, checked, baseline=baseline,
                comparison="new" if baseline is None else self._profile_source_kind)
            return self._show_profile_settings("write", schema, {
                    **placeholders, "skipped": self._profile_message + self._profile_notes(skipped),
                    "form_title": translate(self.hass, "common.profile_select_" +
                        ("new" if self._profile_write_mode == "new" else "update"))}, errors)
        except ValueError as err:
            if user_input is not None:
                result = await self.async_step_profile_write_select()
                result["errors"] = {"base": str(err)}
                return result
            return self._profile_error("profile_write_error", str(err))

    async def async_step_profile_write_confirm(self, user_input=None):
        try:
            client = self._profile_client()
        except ValueError as err:
            return self._profile_error("profile_write_error", str(err))
        selected = self._profile_write_selected
        removed = self._profile_removed if self._profile_write_mode == "remove" else {}
        title = "profile_settings_to_save" if self._profile_write_mode == "new" else "profile_settings_to_update"
        tables = self._profile_confirmation_section(client, self._profile_write_catalog, selected, title)
        tables += self._profile_confirmation_section(
            client, self._profile_write_catalog, removed, "profile_removed_settings")
        return self.async_show_menu(
            step_id={"new": "profile_create_confirm", "remove": "profile_remove_confirm"}.get(
                self._profile_write_mode, "profile_update_confirm"),
            menu_options=["profile_write_commit", "profile_write_back", "profile_write_cancel"],
            description_placeholders={"changes": tables})

    async def async_step_profile_create_confirm(self, user_input=None):
        return await self.async_step_profile_write_confirm()

    async def async_step_profile_update_confirm(self, user_input=None):
        return await self.async_step_profile_write_confirm()

    async def async_step_profile_remove_confirm(self, user_input=None):
        return await self.async_step_profile_write_confirm()

    async def async_step_profile_write_back(self, user_input=None):
        if self._profile_write_mode == "edit":
            return await self.async_step_profile_edit_settings()
        if self._profile_write_mode == "remove":
            return await self.async_step_profile_remove_settings()
        return await self.async_step_profile_write_select()

    async def async_step_profile_write_cancel(self, user_input=None):
        self._profile_write_selected = {}
        self._profile_removed = {}
        self._profile_edit_active = False
        self._profile_remove_active = False
        if self._profile_write_mode in {"edit", "remove"}:
            self._profile_id = self._profile_target_id
            return await self.async_step_profile_menu()
        return await self.async_step_profiles()

    async def async_step_profile_write_unchanged(self, user_input=None):
        if user_input is not None:
            if self._profile_write_mode != "new":
                self._profile_id = self._profile_target_id
                return await self.async_step_profile_menu()
            return await self.async_step_profiles()
        kind = self._profile_source_kind
        message = "profile_matches_" + kind if kind in ("controller", "file") else "profile_no_changes"
        if self._profile_message:
            message = "profile_no_changes"
        return self.async_show_form(step_id="profile_write_unchanged", data_schema=vol.Schema({}),
            description_placeholders={"message": translate(self.hass, "common." + message),
                                      "skipped": self._profile_message})

    async def async_step_profile_write_error(self, user_input=None):
        return await self.async_step_profiles()

    async def async_step_profile_write_commit(self, user_input=None):
        try:
            client = self._profile_client()
            selected = self._profile_write_selected
            removed = self._profile_removed if self._profile_write_mode == "remove" else {}
            if not selected and not removed:
                raise ValueError("profile_no_selection")
            catalog, valid, skipped = prepare_profile(client,
                {**self._profile_source, "params": selected})
            if skipped or set(valid) != set(selected):
                raise ValueError("profile_changed")
            if self._profile_write_mode == "new":
                name = self._profile_source["_meta"]["name"]
                if (await self._profile_name_exists(name)
                        and self._profile_duplicate_ack != name.casefold()):
                    self._profile_duplicate_origin = "commit"
                    return await self.async_step_profile_duplicate()
                record = deepcopy(self._profile_source)
                record["params"] = valid
                record["_meta"]["date"] = datetime.now(timezone.utc).isoformat()
                self._profile_validate_pairs(catalog, valid)
                self._profile_id = await self._profile_store().add(record)
            else:
                record = deepcopy(self._profile_target)
                if any(key not in self._profile_write_baseline for key in removed):
                    raise ValueError("profile_changed")
                record["params"] = self._profile_merge(record["params"], valid, removed)
                if not record["params"]:
                    raise ValueError("profile_empty")
                _, final_valid, _ = prepare_profile(client, record)
                self._profile_validate_pairs(catalog, final_valid)
                await self._profile_store().update(
                    self._profile_target_id, record, self._profile_target)
                self._profile_id = self._profile_target_id
            self._profile = (await self._profile_store().list())[self._profile_id]
            if self._profile_download_remove is not None:
                self._profile_download_remove()
                self._profile_download_remove = None
            return await self.async_step_profile_menu()
        except ValueError as err:
            return self._profile_error("profile_write_error", str(err))
        except OSError:
            return self._profile_error("profile_write_error", "profile_storage_error")

    async def async_step_profile_load(self, user_input=None):
        self._profile_checked = None
        self._profile_limit_request = None
        return await self.async_step_profile_preview()

    def _profile_groups(self, catalog, params):
        """Grupuje i sortuje nastawy tak samo w wyborze, edycji i podsumowaniu."""
        groups = {}
        for key in params:
            location = entity_location(catalog[key].platform, key)
            if location is not None:
                page, _, _, order = location
                groups.setdefault(page, []).append((order, key))
        for page in PAGE_ORDER:
            entries = sorted(groups.get(page, []))
            if not entries:
                continue
            title_page = page
            if page == "dhw_buffer" and not any(key in params for key in ("D200", "D203", "D204")):
                title_page = "dhw"
            yield page, profile_page_title(self.hass, title_page), [key for _, key in entries]

    @staticmethod
    def _profile_setting_name(client, setting, page):
        name = setting.name(client)
        return re.sub(r"^(?:CO|CH)[123]\s+", "", name) if page.startswith("ch") else name

    def _profile_preview_schema(self, client, catalog, params, checked,
                                *, baseline=None, comparison="send", editable=False,
                                values=None):
        """Buduje pojedyncze pola z opisami w kolejności wyboru encji."""
        schema, placeholders, fields = {}, {}, {}
        for page, title, keys in self._profile_groups(catalog, params):
            fields[page] = {}
            page_schema = {}
            placeholders[f"group_{page}"] = title
            for key in keys:
                setting = catalog[key]
                field = setting.suffix
                fields[page][field] = key
                placeholders[f"label_{field}"] = self._profile_setting_name(client, setting, page)
                previous = get_parameter_value(
                    client.data if comparison == "send" else (baseline or {}), key)
                if editable:
                    value = translate(self.hass, "common.profile_saved_value",
                                      value="**" + _safe_text(setting.display(client, params[key])) + "**")
                    if setting.platform == "number":
                        low, high = (format(Decimal(str(setting.meta[bound])).normalize(), "f")
                                     for bound in ("min", "max"))
                        bounds = f"{low}–{high} {setting.meta.get('unit') or ''}".strip()
                        value += " · " + translate(self.hass, "common.profile_value_range",
                                                  value="**" + _safe_text(bounds) + "**")
                elif comparison == "new":
                    value = setting.display(client, params[key])
                else:
                    old = _safe_text(setting.display(client, previous) if previous is not None else
                                     translate(self.hass, "common.profile_value_new"))
                    incoming = _safe_text(setting.display(client, params[key]))
                    old, incoming = "**" + old + "**", "**" + incoming + "**"
                    if comparison == "send":
                        value = translate(self.hass, "common.profile_value_difference",
                                          current=old, profile=incoming)
                    else:
                        value = translate(self.hass, f"common.profile_compare_{comparison}",
                                          saved=old, incoming=incoming)
                placeholders[f"value_{field}"] = value
                if editable:
                    native = setting.decode(params[key])
                    default = float(native) if setting.platform == "number" else native
                    if values is not None:
                        default = values.get(page, {}).get(field, default)
                    if setting.platform == "number":
                        config = NumberSelectorConfig(
                            min=setting.meta["min"], max=setting.meta["max"],
                            step=setting.meta["step"], mode=NumberSelectorMode.BOX)
                        if setting.meta.get("unit"):
                            config["unit_of_measurement"] = setting.meta["unit"]
                        selector = NumberSelector(config)
                    else:
                        selector = SelectSelector(SelectSelectorConfig(
                            options=[{"value": option, "label": setting.display(client, raw)}
                                     for option, raw in setting.meta["modes"].items()],
                            mode=SelectSelectorMode.DROPDOWN))
                    page_schema[vol.Optional(field, default=default)] = selector
                else:
                    page_schema[vol.Optional(field, default=key in checked)] = bool
            schema[vol.Required(page)] = section(vol.Schema(page_schema), {"collapsed": True})
        self._profile_fields_by_page = fields
        return vol.Schema(schema), placeholders

    def _show_profile_settings(self, operation, schema, placeholders, errors=None):
        """Wyświetla wspólne pola wyboru lub edycji nastaw."""
        self._profile_settings_operation = operation
        placeholders = {**placeholders, "form_description": translate(
            self.hass, f"common.profile_{operation}_description", **placeholders)}
        return self.async_show_form(step_id="profile_settings", data_schema=schema,
            errors=errors or {}, description_placeholders=placeholders)

    async def async_step_profile_settings(self, user_input=None):
        """Kieruje zapis wspólnego formularza do właściwej operacji."""
        handler = {
            "send": self.async_step_profile_preview,
            "write": self.async_step_profile_write_select,
            "edit": self.async_step_profile_edit_settings,
        }.get(getattr(self, "_profile_settings_operation", None))
        if handler is None:
            return await self.async_step_profiles()
        return await handler(user_input)

    async def async_step_profile_edit_settings(self, user_input=None):
        errors = {}
        try:
            client = self._profile_client()
            if not getattr(self, "_profile_edit_active", False):
                stored = await self._profile_store().list()
                if self._profile_id not in stored:
                    raise ValueError("profile_required")
                self._profile_target_id = self._profile_id
                self._profile_target = stored[self._profile_id]
                self._profile_write_mode = "edit"
                self._profile_source_kind = "edit"
                self._profile_source = deepcopy(self._profile_target)
                self._profile_edit_input = None
                self._profile_message = ""
                self._profile_edit_active = True
            catalog, params, skipped = prepare_profile(
                client, self._profile_target)
            if not params:
                raise ValueError("profile_empty")
            if user_input is not None:
                self._profile_edit_input = user_input
                candidate = dict(params)
                for page, fields in user_input.items():
                    offered = getattr(self, "_profile_fields_by_page", {}).get(page, {})
                    if not offered or not isinstance(fields, dict):
                        raise ValueError("profile_changed")
                    for field, value in fields.items():
                        if field not in offered or offered[field] not in params:
                            raise ValueError("profile_changed")
                        key = offered[field]
                        setting = catalog[key]
                        if setting.platform == "number":
                            if isinstance(value, bool):
                                raise ValueError("invalid_value")
                            native = Decimal(str(value))
                            if not native.is_finite():
                                raise ValueError("invalid_value")
                        else:
                            if not isinstance(value, str) or value not in setting.meta["modes"]:
                                raise ValueError("invalid_value")
                            native = value
                        raw = setting.encode(native)
                        setting.validate(raw)
                        if setting.platform == "number" and native != setting.decode(params[key]):
                            steps = (native - Decimal(str(setting.meta["min"]))) / Decimal(
                                str(setting.meta["step"]))
                            if steps != steps.to_integral_value():
                                raise ValueError("invalid_value")
                        candidate[key] = raw
                self._profile_validate_pairs(catalog, candidate)
                changes = changed_parameters(catalog, candidate, params)
                if not changes:
                    return await self.async_step_profile_write_unchanged()
                self._profile_source["params"] = candidate
                self._profile_write_catalog = catalog
                self._profile_write_baseline = params
                self._profile_write_selected = changes
                self._profile_message = self._profile_notes(skipped)
                return await self.async_step_profile_write_confirm()
            schema, placeholders = self._profile_preview_schema(
                client, catalog, params, [], comparison="new", editable=True,
                values=self._profile_edit_input)
            return self._show_profile_settings("edit", schema, {**placeholders,
                    "form_title": translate(self.hass, "common.profile_edit_title"),
                    "name": _safe_text(self._profile_target["_meta"]["name"]),
                    "skipped": self._profile_notes(skipped)}, errors)
        except (ValueError, DecimalException, TypeError, KeyError) as err:
            reason = str(err)
            if reason not in {"profile_offline", "profile_required", "profile_empty",
                              "profile_changed", "profile_invalid_pair", "out_of_range"}:
                reason = "invalid_value"
            if user_input is not None:
                result = await self.async_step_profile_edit_settings()
                result["errors"] = {"base": reason}
                return result
            return self._profile_error("profile_write_error", reason)

    async def async_step_profile_remove_settings(self, user_input=None):
        """Wybiera nastawy do usunięcia, nie zmieniając wartości sterownika."""
        errors = {}
        try:
            client = self._profile_client()
            if not getattr(self, "_profile_remove_active", False):
                stored = await self._profile_store().list()
                if self._profile_id not in stored:
                    raise ValueError("profile_required")
                self._profile_target_id = self._profile_id
                self._profile_target = stored[self._profile_id]
                self._profile_source = deepcopy(self._profile_target)
                self._profile_write_mode = "remove"
                self._profile_source_kind = "remove"
                self._profile_remove_checked = {}
                self._profile_removed = {}
                self._profile_write_selected = {}
                self._profile_message = ""
                self._profile_remove_active = True
            catalog, params, skipped = prepare_profile(
                client, self._profile_target)
            if not params:
                raise ValueError("profile_empty")
            if user_input is not None:
                offered = self._profile_remove_offered
                removed = {}
                checked = {}
                for page, fields in user_input.items():
                    if page not in offered or not isinstance(fields, dict) or set(fields) - {"settings"}:
                        raise ValueError("profile_changed")
                    selected = fields.get("settings", [])
                    if not isinstance(selected, list) or any(not isinstance(key, str) for key in selected):
                        raise ValueError("invalid_value")
                    if len(selected) != len(set(selected)) or any(key not in offered[page] or key not in params
                                                               for key in selected):
                        raise ValueError("profile_changed")
                    checked[page] = list(selected)
                    removed.update({key: params[key] for key in selected})
                self._profile_remove_checked = checked
                if not removed:
                    raise ValueError("profile_no_removal")
                if not self._profile_merge(self._profile_target["params"], {}, removed):
                    raise ValueError("profile_empty")
                self._profile_removed = removed
                self._profile_write_catalog = catalog
                self._profile_write_baseline = params
                self._profile_message = self._profile_notes(skipped)
                return await self.async_step_profile_write_confirm()

            schema, placeholders, offered = {}, {}, {}
            for page, title, keys in self._profile_groups(catalog, params):
                placeholders["group_" + page] = title
                offered[page] = set(keys)
                options = [{"value": key, "label": self._profile_setting_name(client, catalog[key], page)}
                           for key in keys]
                selected = [key for key in self._profile_remove_checked.get(page, []) if key in offered[page]]
                schema[vol.Required(page)] = section(vol.Schema({
                    vol.Optional("settings", default=selected): SelectSelector(SelectSelectorConfig(
                        options=options, multiple=True, mode=SelectSelectorMode.LIST))}), {"collapsed": True})
            self._profile_remove_offered = offered
            return self.async_show_form(step_id="profile_remove_settings", data_schema=vol.Schema(schema),
                errors=errors, description_placeholders={**placeholders,
                    "name": _safe_text(self._profile_target["_meta"]["name"]),
                    "skipped": self._profile_notes(skipped)})
        except (ValueError, TypeError, KeyError) as err:
            reason = str(err)
            if reason not in {"profile_offline", "profile_required", "profile_empty",
                              "profile_changed", "profile_no_removal", "invalid_value"}:
                reason = "invalid_value"
            if user_input is not None:
                result = await self.async_step_profile_remove_settings()
                result["errors"] = {"base": reason}
                return result
            return self._profile_error("profile_write_error", reason)

    def _profile_confirmation_section(self, client, catalog, params, title_key):
        """Grupuje nastawy pod wspólnym nagłówkiem z licznikiem."""
        if not params:
            return ""
        title = escape(translate(self.hass, "common." + title_key))
        return ("<p>" + title + f" ({len(params)})</p>" +
                self._profile_tables(client, catalog, params))

    def _profile_tables(self, client, catalog, params):
        """Pokazuje pełne nazwy i wartości docelowe w grupach formularza."""
        tables = []
        for page, title, keys in self._profile_groups(catalog, params):
            header = [translate(self.hass, "common." + key)
                      for key in ("profile_table_setting", "profile_table_value")]
            rows = []
            for key in keys:
                setting = catalog[key]
                name = self._profile_setting_name(client, setting, page)
                value = (translate(self.hass, "common.profile_table_" + setting.decode(params[key]))
                         if setting.platform == "switch" else setting.display(client, params[key]))
                content = [escape(name), escape(value)]
                rows.append("<tr>" + "".join("<td>" + cell + "</td>" for cell in content) + "</tr>")
            widths = ("65%", "35%")
            headings = "".join('<th width="' + width + '" align="left">' + escape(label) + "</th>"
                               for width, label in zip(widths, header))
            columns = "".join('<col width="' + width + '">' for width in widths)
            tables.append(
                "<details><summary>" + escape(title) + f" ({len(keys)})</summary>"
                '<div><table width="100%"><colgroup>' + columns + '</colgroup><thead><tr>' +
                headings + "</tr></thead><tbody>" +
                "".join(rows) + "</tbody></table></div></details>")
        return "\n".join(tables)

    def _profile_selected_fields(self, user_input):
        """Odczytuje wyłącznie pola pokazane w poprzednim podglądzie."""
        selected = []
        for page, values in user_input.items():
            fields = getattr(self, "_profile_fields_by_page", {}).get(page)
            if fields is None or not isinstance(values, dict):
                raise ValueError("profile_changed")
            for field, value in values.items():
                if field not in fields or not isinstance(value, bool):
                    raise ValueError("profile_changed")
                if value:
                    selected.append(fields[field])
        return selected

    async def async_step_profile_preview(self, user_input=None):
        """Pokazuje różnice i wysyła wyłącznie zaznaczone nastawy."""
        self._profile_limit_request = None
        errors = {}
        try:
            client = self._profile_client()
            catalog, params, skipped = prepare_profile(client, self._profile)
            if not params:
                raise ValueError("profile_comparison_unavailable")
            try:
                for key in params:
                    catalog[key].decode(get_parameter_value(client.data, key))
            except ValueError:
                raise ValueError("profile_comparison_unavailable") from None
            changes = changed_parameters(catalog, params, client.data)
            if not changes:
                self._profile_message = self._profile_notes(skipped)
                return await self.async_step_profile_unchanged()
        except ValueError as err:
            return self._show_profile_settings("send", vol.Schema({}), {
                "form_title": translate(self.hass, "common.profile_select_send"),
                "name": _safe_text(self._profile["_meta"]["name"]), "skipped": ""},
                {"base": str(err)})

        checked = getattr(self, "_profile_checked", None)
        if checked is None:
            checked = list(changes)
        if user_input is not None:
            try:
                selected = self._profile_selected_fields(user_input)
                checked = [key for key in selected if key in params]
                if not selected:
                    raise ValueError("profile_no_selection")
                offered = getattr(self, "_profile_offered", {})
                if any(key not in offered or key not in params for key in selected):
                    raise ValueError("profile_changed")
                if (client.data.get("DevType") != self._profile_view_identity
                        or any(get_parameter_value(client.data, key) != self._profile_view_values[key]
                               for key in selected)):
                    raise ValueError("profile_changed")
                self._profile_checked = checked
                self._profile_selected = {key: changes[key] for key in checked if key in changes}
                self._profile_limit_request = None
                if not self._profile_selected:
                    self._profile_message = self._profile_notes(skipped)
                    return await self.async_step_profile_unchanged()
                order_changes(client, catalog, self._profile_selected)
                self._profile_baseline = {
                    key: get_parameter_value(client.data, key) for key in self._profile_selected
                }
                self._profile_identity = client.data.get("DevType")
                return await self.async_step_profile_confirm()
            except ValueError as err:
                errors["base"] = str(err)

        self._profile_checked = [key for key in checked if key in changes]
        self._profile_offered = changes
        self._profile_view_identity = client.data.get("DevType")
        self._profile_view_values = {key: get_parameter_value(client.data, key) for key in params}
        schema, placeholders = self._profile_preview_schema(
            client, catalog, changes, self._profile_checked)
        return self._show_profile_settings("send", schema, {
                                      **placeholders, "skipped": self._profile_notes(skipped),
                                      "form_title": translate(self.hass, "common.profile_select_send"),
                                      "name": _safe_text(self._profile["_meta"]["name"])},
                                      errors)

    async def async_step_profile_confirm(self, user_input=None):
        try:
            client = self._profile_client()
        except ValueError:
            return await self.async_step_profile_preview()
        catalog = setting_catalog(client)
        return self.async_show_menu(step_id="profile_confirm",
            menu_options=["profile_send", "profile_preview", "profile_menu"],
            description_placeholders={"changes": self._profile_confirmation_section(
                client, catalog, self._profile_selected, "profile_settings_to_send")})

    async def async_step_profile_send(self, user_input=None):
        return await self._profile_start_send()

    async def _profile_start_send(self, approved_request=None):
        try:
            client = self._profile_client()
            if (client.data.get("DevType") != self._profile_identity
                    or any(get_parameter_value(client.data, k) != v
                           for k, v in self._profile_baseline.items())):
                return await self.async_step_profile_preview()
            options = dict(self.config_entry.options)
            catalog = setting_catalog(client)
            exceeded = profile_boiler_limit(catalog, options, self._profile_selected)
            request = {"selected": dict(self._profile_selected), "baseline": dict(self._profile_baseline),
                       "identity": self._profile_identity, "limit": exceeded}
            if exceeded is not None and request != approved_request:
                self._profile_limit_request = request
                return await self.async_step_profile_temperature_limit()
        except ValueError:
            return await self.async_step_profile_preview()
        self._profile_limit_request = None
        self._profile_task = self.hass.async_create_task(apply_profile(
            client, options, self._profile, dict(self._profile_selected),
            boiler_limit_override=exceeded if approved_request is not None else None))
        return await self.async_step_profile_apply()

    async def async_step_profile_temperature_limit(self, user_input=None):
        request = getattr(self, "_profile_limit_request", None)
        if request is None:
            return await self.async_step_profile_preview()
        client = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        if client is None:
            return await self.async_step_profile_preview()
        setting = setting_catalog(client)["BoilerTempCmd"]
        raw, limit = request["limit"]
        return self.async_show_menu(step_id="profile_temperature_limit",
            menu_options=["profile_preview", "profile_send_override"],
            description_placeholders={"value": setting.display(client, raw),
                "limit": setting.display(client, setting.encode(Decimal(limit)))})

    async def async_step_profile_send_override(self, user_input=None):
        request = getattr(self, "_profile_limit_request", None)
        self._profile_limit_request = None
        if request is None:
            return await self.async_step_profile_preview()
        return await self._profile_start_send(request)

    async def async_step_profile_unchanged(self, user_input=None):
        if user_input is not None:
            return await self.async_step_profile_menu()
        return self.async_show_form(step_id="profile_unchanged", data_schema=vol.Schema({}),
            description_placeholders={"skipped": self._profile_message})

    async def async_step_profile_apply(self, user_input=None):
        if not self._profile_task.done():
            return self.async_show_progress(step_id="profile_apply", progress_action="profile_apply",
                                             progress_task=self._profile_task)
        try:
            self._profile_result = self._profile_task.result()
        except asyncio.CancelledError:
            self._profile_result = {"saved": [], "failed": [],
                                    "remaining": list(self._profile_selected), "error": "profile_cancelled"}
        except Exception:
            client = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
            connection = self.config_entry.data
            _LOGGER.exception("[SKZP Control] %s:%s — Unexpected error while sending profile settings.",
                              client.host if client else connection.get("host", "unknown"),
                              client.port if client else connection.get("port", "unknown"))
            self._profile_result = {"saved": [], "failed": [],
                                    "remaining": list(self._profile_selected), "error": "profile_send_failed"}
        return self.async_show_progress_done(next_step_id="profile_result")

    async def async_step_profile_result(self, user_input=None):
        if user_input is not None:
            self._profile_task = None
            return await self.async_step_profile_menu()
        result = self._profile_result
        client = self.hass.data.get(DOMAIN, {}).get(self.config_entry.entry_id)
        catalog = setting_catalog(client) if client else {}
        total = sum(len(result[key]) for key in ("saved", "failed", "remaining"))
        lines = ["<p>" + escape(translate(self.hass, "common.profile_result_count",
                           count=len(result["saved"]), total=total)) + "</p>"]
        if result["error"]:
            lines.append("<p>" + escape(translate(self.hass, "options.error." + result["error"])) + "</p>")
        for key in ("saved", "failed", "remaining"):
            if not result[key]:
                continue
            names = [
                escape(catalog[item].name(client) if item in catalog else item)
                for item in result[key]
            ]
            title = escape(translate(self.hass, "common.profile_result_" + key))
            lines.append("<details><summary>" + title + f" ({len(names)})</summary><ul>" +
                         "".join("<li>" + name + "</li>" for name in names) + "</ul></details>")
        return self.async_show_form(step_id="profile_result", data_schema=vol.Schema({}),
            description_placeholders={"summary": "\n\n".join(lines)})

    async def async_step_profile_export(self, user_input=None):
        if self._profile_download_remove is not None:
            self._profile_download_remove()
        profile = {**self._profile,
                   "params": order_profile_parameters(self._profile["params"])}
        self._profile_url, self._profile_download_remove = async_create_frame_download(
            self.hass, profile, profile=True)
        return await self.async_step_profile_download()

    async def async_step_profile_download(self, user_input=None):
        if user_input is not None:
            return await self.async_step_profile_menu()
        # HTML przekazujemy jako wartość, nie jako składnię tłumaczenia ICU.
        label = escape(translate(self.hass, "common.profile_download_label"))
        link = f'<a href="{escape(self._profile_url, quote=True)}" target="_blank">{label}</a>'
        return self.async_show_form(step_id="profile_download", data_schema=vol.Schema({}),
                                    description_placeholders={"download_link": link})

    async def async_step_profile_delete(self, user_input=None):
        return self.async_show_menu(step_id="profile_delete",
            menu_options=["profile_delete_confirm", "profile_menu"],
            description_placeholders={"name": _safe_text(self._profile["_meta"]["name"])})

    async def async_step_profile_delete_confirm(self, user_input=None):
        try:
            await self._profile_store().delete(self._profile_id)
        except OSError:
            return self.async_show_form(step_id="profile_delete_error",
                data_schema=vol.Schema({}), errors={"base": "profile_storage_error"})
        if self._profile_download_remove is not None:
            self._profile_download_remove()
            self._profile_download_remove = None
        self._profile_id = None
        self._profile = None
        return await self.async_step_profile_manage()

    async def async_step_profile_delete_error(self, user_input=None):
        return await self.async_step_profile_menu()

    @callback
    def async_remove(self):
        """Kończy zapis przy zamknięciu formularza i unieważnia pobieranie."""
        if self._profile_task is not None and not self._profile_task.done():
            self._profile_task.cancel()
        if self._profile_download_remove is not None:
            self._profile_download_remove()
        super().async_remove()
