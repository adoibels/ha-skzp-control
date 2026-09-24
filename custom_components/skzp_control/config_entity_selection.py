"""Budowanie formularzy wyboru encji."""

import logging
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.data_entry_flow import section
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .device import SUPPORTED_DEVICE_MODELS
from .entity_layout import (
    FIELD_BUFFER,
    PAGE_DHW_BUFFER,
    PAGE_ORDER,
    SECTION_ORDER,
    SECTION_SENSORS,
    EntityChoice,
    entity_location,
)
from .entity_support import EntitySupportContext, should_create_sensor
from .parameter_resolver import is_parameter_definition_supported

PAGE_DHW_ONLY = "dhw"

_LOGGER = logging.getLogger(__name__)


def build_entity_choices(
    model: str,
    device_data: Mapping[str, Any] | None = None,
    retained_buffer_sensors: frozenset[str] = frozenset(),
) -> list[EntityChoice]:
    """Buduje listę encji obsługiwanych przez wykryty sterownik."""
    if device_data is None or model not in SUPPORTED_DEVICE_MODELS:
        return []

    # Platformy ładujemy dopiero po potwierdzeniu obsługi modelu.
    from .binary_sensor import get_output_descriptions, is_output_supported
    from .number import get_number_descriptions
    from .select import SELECT_KEYS, get_select_descriptions
    from .sensor import (
        DIAGNOSTIC_KEYS,
        get_sensor_descriptions,
        is_sensor_supported,
    )
    from .switch import SWITCH_KEYS, get_switch_descriptions

    device_identity = device_data.get("DevType") or model
    context = EntitySupportContext.from_device(model, device_data)
    choices: list[EntityChoice] = []

    def add(
        platform: str,
        key: str,
        unique_suffix: str,
        translation_key: str | None = None,
    ) -> None:
        location = entity_location(platform, key)
        if location is None:
            _LOGGER.debug(
                "Supported entity missing from ENTITY_LAYOUT: %s:%s",
                platform,
                key,
            )
            return
        page, section_name, field, order = location
        choices.append(
            EntityChoice(
                platform=platform,
                key=key,
                unique_suffix=unique_suffix,
                page=page,
                section=section_name,
                field=field,
                order=order,
                translation_key=translation_key,
            )
        )

    sensor_descriptions = get_sensor_descriptions(model)
    for key, metadata in sensor_descriptions.items():
        if (
            key not in DIAGNOSTIC_KEYS
            and is_sensor_supported(device_data, key)
            and should_create_sensor(key, context, retained_buffer_sensors)
        ):
            add(
                "sensor",
                key,
                key.lower(),
                metadata.get("translation_key"),
            )

    output_descriptions = get_output_descriptions(model)
    for key, index in output_descriptions.items():
        if is_output_supported(device_data, index):
            add("binary_sensor", key, key.lower())

    number_descriptions = get_number_descriptions(model, device_identity)
    for key, metadata in number_descriptions.items():
        if is_parameter_definition_supported(
            device_data,
            key,
            number_descriptions,
        ):
            add("number", key, metadata["unique_suffix"])

    for description in get_select_descriptions(model):
        if is_parameter_definition_supported(
            device_data,
            description.data_key,
            SELECT_KEYS,
        ):
            add("select", description.data_key, description.unique_suffix)

    for description in get_switch_descriptions(model):
        if is_parameter_definition_supported(
            device_data,
            description.data_key,
            SWITCH_KEYS,
        ):
            add("switch", description.data_key, description.unique_suffix)

    return sorted(choices, key=lambda choice: choice.order)


def page_choices(choices: list[EntityChoice], page: str) -> list[EntityChoice]:
    """Zwraca encje widoczne na wskazanym ekranie."""
    return [choice for choice in choices if choice.page == page]


def selection_count_placeholders(
    choices: list[EntityChoice],
    disabled_entities: set[str],
    page: str | None = None,
) -> dict[str, str]:
    """Zwraca licznik encji dostępnych do wyboru w formularzu."""
    selectable = page_choices(choices, page) if page is not None else choices
    selected = sum(
        choice.selection_id not in disabled_entities for choice in selectable
    )
    return {"selected": str(selected), "total": str(len(selectable))}


def selectable_entity_ids(choices: list[EntityChoice]) -> set[str]:
    """Zwraca identyfikatory encji, które użytkownik może zaznaczać."""
    return {choice.selection_id for choice in choices}


def _ordered_fields(
    choices: list[EntityChoice],
    section_name: str,
) -> list[str]:
    """Zwraca grupy encji zgodnie z kolejnością formularza."""
    fields: list[str] = []
    for choice in choices:
        if choice.section == section_name and choice.field not in fields:
            fields.append(choice.field)
    return fields


def build_entity_page_schema(
    choices: list[EntityChoice],
    page: str,
    disabled_entities: set[str],
) -> vol.Schema:
    """Buduje ekran wyboru encji z pogrupowanymi listami."""
    current_page_choices = page_choices(choices, page)
    schema = {}
    for section_name in SECTION_ORDER:
        section_schema = {}
        for field in _ordered_fields(current_page_choices, section_name):
            field_choices = [
                choice
                for choice in current_page_choices
                if choice.section == section_name and choice.field == field
            ]
            selected = [
                choice.option_id
                for choice in field_choices
                if choice.selection_id not in disabled_entities
            ]
            section_schema[vol.Required(field, default=selected)] = SelectSelector(
                SelectSelectorConfig(
                    options=[choice.option_id for choice in field_choices],
                    translation_key="entity_selection",
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            )
        if section_schema:
            schema[vol.Required(section_name)] = section(
                vol.Schema(section_schema),
                {"collapsed": section_name != SECTION_SENSORS},
            )
    return vol.Schema(schema)


def disabled_entities_from_page(
    user_input: Mapping[str, Any],
    page: str,
    choices: list[EntityChoice],
    previous_disabled: set[str],
) -> set[str]:
    """Aktualizuje wybór encji tylko dla zapisanego ekranu."""
    current_page_choices = page_choices(choices, page)
    available_ids = {choice.selection_id for choice in current_page_choices}
    selected_options: set[str] = set()
    for section_name in SECTION_ORDER:
        section_data = user_input.get(section_name, {})
        for field in _ordered_fields(current_page_choices, section_name):
            selected_options.update(section_data.get(field, []))

    selected_ids = {
        choice.selection_id
        for choice in current_page_choices
        if choice.option_id in selected_options
    }
    disabled = previous_disabled - available_ids
    disabled.update(available_ids - selected_ids)
    return disabled


def available_menu_pages(choices: list[EntityChoice]) -> list[str]:
    """Zwraca dostępne ekrany menu dopasowane do wykrytych funkcji."""
    available_pages = [
        page for page in PAGE_ORDER if page_choices(choices, page)
    ]
    has_buffer = any(
        choice.page == PAGE_DHW_BUFFER and choice.field == FIELD_BUFFER
        for choice in choices
    )
    return [
        PAGE_DHW_ONLY
        if page == PAGE_DHW_BUFFER and not has_buffer
        else page
        for page in available_pages
    ]
