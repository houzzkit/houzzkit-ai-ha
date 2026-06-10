"""Home Assistant automation intents for Houzzkit AI MCP."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, time, timedelta
from functools import partial
import json
import logging
import os
import re
from typing import Any
from uuid import uuid4

import voluptuous as vol

from homeassistant.components.automation import DOMAIN as AUTOMATION_DOMAIN
from homeassistant.components.automation.config import async_validate_config_item
from homeassistant.components.button.const import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button.const import SERVICE_PRESS as SERVICE_PRESS_BUTTON
from homeassistant.components.cover.const import DOMAIN as COVER_DOMAIN
from homeassistant.components.input_button import DOMAIN as INPUT_BUTTON_DOMAIN
from homeassistant.components.lock.const import DOMAIN as LOCK_DOMAIN
from homeassistant.components.text import (
    ATTR_VALUE as TEXT_ATTR_VALUE,
    DOMAIN as TEXT_DOMAIN,
    SERVICE_SET_VALUE as TEXT_SERVICE_SET_VALUE,
)
from homeassistant.components.valve.const import DOMAIN as VALVE_DOMAIN
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ACTIONS,
    CONF_ALIAS,
    CONF_AT,
    CONF_CONDITION,
    CONF_CONDITIONS,
    CONF_ID,
    CONF_MODE,
    CONF_PLATFORM,
    CONF_TRIGGERS,
    CONF_VARIABLES,
    CONF_VALUE_TEMPLATE,
    CONF_WEEKDAY,
    SERVICE_CLOSE_COVER,
    SERVICE_CLOSE_VALVE,
    SERVICE_LOCK,
    SERVICE_OPEN_COVER,
    SERVICE_OPEN_VALVE,
    SERVICE_RELOAD,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    SERVICE_UNLOCK,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.util import dt as dt_util
from homeassistant.util.file import write_utf8_file_atomic
from homeassistant.util.json import JsonObjectType
from homeassistant.util.yaml import dump, load_yaml

from .automation_capabilities import SUPPORTED_PLAN_FEATURES
from .const import DOMAIN
from .houzzkit import get_entities
from .intent_helper import EntityInfo, match_intent_entities
from .intent_live_context import _get_exposed_entities

_LOGGER = logging.getLogger(__name__)
_AUTOMATION_WRITE_LOCK = asyncio.Lock()
_AUTOMATION_ID_PREFIX = "houzzkit_ai_"
_ACTION_SERVICE_DOMAINS = {"scene", "script"}
_REJECTED_NOTIFICATION_DOMAINS = {"notify", "persistent_notification"}
_REJECTED_NOTIFICATION_ACTIONS = {f"{DOMAIN}.speaker_notify", "notify.speaker"}
_HOUZZKIT_NOTIFY_ACTION = f"{DOMAIN}.notify"
_HOUZZKIT_WARN_ACTION = f"{DOMAIN}.warn"
_HOUZZKIT_NOTIFICATION_ACTIONS = {_HOUZZKIT_NOTIFY_ACTION, _HOUZZKIT_WARN_ACTION}
_DELETE_ONE_SHOT_SERVICE = "delete_one_shot_automation"
_DELETE_ONE_SHOT_ACTION = f"{DOMAIN}.{_DELETE_ONE_SHOT_SERVICE}"
_ONE_SHOT_CLEANUP_MARKER = "houzzkit_ai_one_shot"
_VOICE_TEXT_HINTS = ("播放语音", "bo_fang_yu_yin")
_RESOLVED_TARGETS_FIELD = "resolved_targets"
_SUPPORTED_PLAN_FEATURES = SUPPORTED_PLAN_FEATURES
_CURRENT_DATE_FIELD = "current_date"
_DELAY_DURATION_FIELDS = {"days", "hours", "minutes", "seconds"}
_DELAY_TRIGGER_CONFLICT_FIELDS = {CONF_AT, "date", CONF_WEEKDAY}
_INTERVAL_MINUTES_MIN = 1
_INTERVAL_MINUTES_MAX = 12 * 60
_INTERVAL_TRIGGER_CONFLICT_FIELDS = {CONF_AT, "date", CONF_WEEKDAY, "duration"}
_LOCAL_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_LOCAL_TIME_RE = re.compile(r"^\d{2}:\d{2}:\d{2}$")
_ONE_SHOT_TRIGGER_ID_PREFIX = "houzzkit_ai_once_"
_MANAGED_KIND_AUTOMATION = "automation"
_MANAGED_KIND_REMINDER = "reminder"
_MANAGED_KINDS = [_MANAGED_KIND_AUTOMATION, _MANAGED_KIND_REMINDER]
_MANAGED_KIND_VARIABLE = "houzzkit_ai_managed_kind"
_REMINDER_METADATA_VARIABLE = "houzzkit_ai_reminder"
_SEMANTIC_TEXT_FIELD = "semantic_text"
_SEMANTIC_TEXT_VARIABLE = "houzzkit_ai_semantic_text"
_EDITABLE_SNAPSHOT_VARIABLE = "houzzkit_ai_editable_snapshot"
_AUTOMATION_SLOT_SCHEMA = {
    vol.Required("automation"): dict,
    vol.Optional("managed_kind"): vol.In(_MANAGED_KINDS),
    vol.Optional("editable_snapshot"): dict,
    vol.Optional("_speaker_id"): str,
}
_LIST_MANAGED_AUTOMATIONS_SLOT_SCHEMA = {
    vol.Optional("kind"): vol.In(_MANAGED_KINDS),
    vol.Optional("schedule_filter"): dict,
    vol.Optional("_speaker_id"): str,
}
_DELETE_AUTOMATION_SLOT_SCHEMA = {
    vol.Required(CONF_ID): str,
    vol.Optional("_speaker_id"): str,
}
_GET_MANAGED_AUTOMATION_SLOT_SCHEMA = {
    vol.Required(CONF_ID): str,
    vol.Optional("_speaker_id"): str,
}
_REPLACE_AUTOMATION_SLOT_SCHEMA = {
    vol.Required(CONF_ID): str,
    vol.Required("automation"): dict,
    vol.Optional("editable_snapshot"): dict,
    vol.Optional("_speaker_id"): str,
}
_ONE_SHOT_AUTOMATION_TYPE = "one_shot"
_REGULAR_AUTOMATION_TYPE = "regular"


class HouzzkitListAutomationContextIntent(intent.IntentHandler):
    """Return exposed entities, available action services and automation summaries."""

    intent_type = "HouzzkitListAutomationContext"
    description = (
        "List Home Assistant entities exposed to this assistant, action services, "
        "and existing automation summaries for creating a new automation."
    )

    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return None

    async def async_handle(  # type: ignore[override]
        self,
        intent_obj: intent.Intent,
    ) -> JsonObjectType:
        """Get automation creation context."""
        if intent_obj.assistant is None:
            return {"success": False, "error": "No assistant configured"}

        hass = intent_obj.hass
        exposed = _get_exposed_entities(hass, intent_obj.assistant)
        entities = _flatten_exposed_entities(exposed)
        action_services = _list_action_services(hass)
        existing_automations = await _read_automation_summaries(hass)

        return {
            "success": True,
            "entities": entities,
            "services": action_services,
            "supported_actions": action_services,
            "supported_plan_features": _SUPPORTED_PLAN_FEATURES,
            _CURRENT_DATE_FIELD: dt_util.now().date().isoformat(),
            "existing_automations": existing_automations,
            "automation_create_supported": True,
        }


class HouzzkitValidateAutomationIntent(intent.IntentHandler):
    """Validate a Home Assistant automation config without writing it."""

    intent_type = "HouzzkitValidateAutomation"
    description = "Validate a Home Assistant automation config before creation."

    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return _AUTOMATION_SLOT_SCHEMA

    async def async_handle(  # type: ignore[override]
        self,
        intent_obj: intent.Intent,
    ) -> JsonObjectType:
        """Validate automation config."""
        slots = self.async_validate_slots(intent_obj.slots)
        automation = slots["automation"]["value"]
        return await _validate_automation(intent_obj, automation)


class HouzzkitCreateAutomationIntent(intent.IntentHandler):
    """Create a new Home Assistant automation config."""

    intent_type = "HouzzkitCreateAutomation"
    description = "Create a new Home Assistant automation config and reload automations."

    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return _AUTOMATION_SLOT_SCHEMA

    async def async_handle(  # type: ignore[override]
        self,
        intent_obj: intent.Intent,
    ) -> JsonObjectType:
        """Create automation config."""
        slots = self.async_validate_slots(intent_obj.slots)
        automation = slots["automation"]["value"]
        editable_snapshot = _slot_value(slots, "editable_snapshot", None)
        editable_snapshot_value = (
            editable_snapshot if isinstance(editable_snapshot, dict) else None
        )
        managed_kind = str(
            _slot_value(slots, "managed_kind", _MANAGED_KIND_AUTOMATION)
        )
        return await _create_automation(
            intent_obj,
            automation,
            managed_kind=managed_kind,
            editable_snapshot=editable_snapshot_value,
        )


class HouzzkitListManagedAutomationsIntent(intent.IntentHandler):
    """List Houzzkit-created automation summaries for management flows."""

    intent_type = "HouzzkitListManagedAutomations"
    description = "List Houzzkit AI managed automation summaries."

    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return _LIST_MANAGED_AUTOMATIONS_SLOT_SCHEMA

    async def async_handle(  # type: ignore[override]
        self,
        intent_obj: intent.Intent,
    ) -> JsonObjectType:
        """List managed automation candidates."""
        slots = self.async_validate_slots(intent_obj.slots)
        kind = _slot_value(slots, "kind", None)
        kind_value = kind if isinstance(kind, str) else None
        schedule_filter = _slot_value(slots, "schedule_filter", None)
        schedule_filter_value = (
            schedule_filter if isinstance(schedule_filter, dict) else None
        )
        return await _list_managed_automations(
            intent_obj.hass,
            kind=kind_value,
            schedule_filter=schedule_filter_value,
        )


class HouzzkitDeleteAutomationIntent(intent.IntentHandler):
    """Delete one Houzzkit-created automation and reload automations."""

    intent_type = "HouzzkitDeleteAutomation"
    description = "Delete one Houzzkit AI managed automation by id."

    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return _DELETE_AUTOMATION_SLOT_SCHEMA

    async def async_handle(  # type: ignore[override]
        self,
        intent_obj: intent.Intent,
    ) -> JsonObjectType:
        """Delete a managed automation config."""
        slots = self.async_validate_slots(intent_obj.slots)
        automation_id = str(slots[CONF_ID]["value"])
        return await _delete_managed_automation(intent_obj.hass, automation_id)


class HouzzkitGetManagedAutomationIntent(intent.IntentHandler):
    """Return one editable Houzzkit-created automation snapshot."""

    intent_type = "HouzzkitGetManagedAutomation"
    description = "Get one Houzzkit AI managed automation editable snapshot by id."

    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return _GET_MANAGED_AUTOMATION_SLOT_SCHEMA

    async def async_handle(  # type: ignore[override]
        self,
        intent_obj: intent.Intent,
    ) -> JsonObjectType:
        """Get a managed automation editable snapshot."""
        slots = self.async_validate_slots(intent_obj.slots)
        automation_id = str(slots[CONF_ID]["value"])
        return await _get_managed_automation(intent_obj.hass, automation_id)


class HouzzkitReplaceAutomationIntent(intent.IntentHandler):
    """Replace one Houzzkit-created automation and reload automations."""

    intent_type = "HouzzkitReplaceAutomation"
    description = "Replace one Houzzkit AI managed automation by id."

    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return _REPLACE_AUTOMATION_SLOT_SCHEMA

    async def async_handle(  # type: ignore[override]
        self,
        intent_obj: intent.Intent,
    ) -> JsonObjectType:
        """Replace a managed automation config."""
        slots = self.async_validate_slots(intent_obj.slots)
        automation_id = str(slots[CONF_ID]["value"])
        automation = slots["automation"]["value"]
        editable_snapshot = _slot_value(slots, "editable_snapshot", None)
        editable_snapshot_value = (
            editable_snapshot if isinstance(editable_snapshot, dict) else None
        )
        return await _replace_managed_automation(
            intent_obj,
            automation_id,
            automation,
            editable_snapshot=editable_snapshot_value,
        )


def async_setup_automation_services(hass: HomeAssistant) -> None:
    """Register internal automation maintenance services."""
    if hass.services.has_service(DOMAIN, _DELETE_ONE_SHOT_SERVICE):
        return
    hass.services.async_register(
        DOMAIN,
        _DELETE_ONE_SHOT_SERVICE,
        partial(_handle_delete_one_shot_service, hass),
        vol.Schema(
            {
                vol.Required(CONF_ID): str,
                vol.Required("marker"): str,
            }
        ),
    )


@callback
def _handle_delete_one_shot_service(
    hass: HomeAssistant,
    call: ServiceCall,
) -> None:
    """Schedule one-shot automation deletion after the calling automation returns."""
    hass.async_create_task(
        _async_delete_one_shot_automation(
            hass,
            call.data[CONF_ID],
            call.data["marker"],
        )
    )


async def _async_delete_one_shot_automation(
    hass: HomeAssistant,
    automation_id: str,
    marker: str,
) -> None:
    """Delete a Houzzkit one-shot automation if its internal marker matches."""
    if marker != _ONE_SHOT_CLEANUP_MARKER:
        _LOGGER.debug("Skip one-shot cleanup with invalid marker for %s", automation_id)
        return
    if not _is_houzzkit_automation_id(automation_id):
        _LOGGER.debug("Skip one-shot cleanup for non-Houzzkit id: %s", automation_id)
        return

    removed = False
    async with _AUTOMATION_WRITE_LOCK:
        configs = await _read_automation_configs(hass)
        remaining: list[dict[str, Any]] = []
        for config in configs:
            if config.get(CONF_ID) != automation_id:
                remaining.append(config)
                continue
            if not _has_internal_one_shot_cleanup_action(config, automation_id):
                _LOGGER.debug(
                    "Skip one-shot cleanup for automation without internal marker: %s",
                    automation_id,
                )
                remaining.append(config)
                continue
            removed = True

        if not removed:
            return
        await _write_automation_configs(hass, remaining)

    await hass.services.async_call(
        AUTOMATION_DOMAIN,
        SERVICE_RELOAD,
        {},
        blocking=True,
    )


def _is_houzzkit_automation_id(automation_id: str) -> bool:
    return automation_id.startswith(_AUTOMATION_ID_PREFIX)


def _has_internal_one_shot_cleanup_action(
    automation: dict[str, Any],
    automation_id: str,
) -> bool:
    for action in _automation_action_nodes(automation):
        if action.get("action") != _DELETE_ONE_SHOT_ACTION:
            continue
        data = action.get("data")
        if not isinstance(data, dict):
            continue
        if (
            data.get(CONF_ID) == automation_id
            and data.get("marker") == _ONE_SHOT_CLEANUP_MARKER
        ):
            return True
    return False


def _automation_action_nodes(automation: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for key in (CONF_ACTIONS, "action"):
        actions = automation.get(key)
        if isinstance(actions, list):
            nodes.extend(action for action in actions if isinstance(action, dict))
        elif isinstance(actions, dict):
            nodes.append(actions)
    return nodes


def _flatten_exposed_entities(
    exposed: dict[str, dict[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    """把 HA 暴露实体整理成 ai-server 自动化计划可读的扁平列表。"""
    entities: list[dict[str, Any]] = []
    for group in ("entities", "script", "calendar"):
        for entity_id, info in exposed.get(group, {}).items():
            names = str(info.get("names", "")).strip()
            if not names:
                _LOGGER.debug("Skip exposed entity without public name: %s", entity_id)
                continue
            display_name = names.split(", ", 1)[0]
            item: dict[str, Any] = {
                "display_name": display_name,
                "aliases": names,
            }
            if areas := info.get("areas"):
                item["area"] = areas
            if "state" in info:
                item["state"] = info["state"]
            if attributes := info.get("attributes"):
                item["attributes"] = attributes
            entities.append(item)
    return entities


def _list_action_services(
    hass: HomeAssistant,
) -> list[dict[str, str]]:
    """列出非设备动作服务；设备动作必须通过公开 target/area 解析。"""
    allowed_domains = _ACTION_SERVICE_DOMAINS
    services = hass.services.async_services()
    actions: list[dict[str, str]] = [
        {
            "name": _HOUZZKIT_NOTIFY_ACTION,
            "service": _HOUZZKIT_NOTIFY_ACTION,
            "display_name": "当前音箱普通播报",
            "description": "Use data.message to play text on the current speaker.",
        },
        {
            "name": _HOUZZKIT_WARN_ACTION,
            "service": _HOUZZKIT_WARN_ACTION,
            "display_name": "当前音箱警告播报",
            "description": "Use data.message to play warning text on the current speaker.",
        }
    ]
    for domain in sorted(allowed_domains):
        for service in sorted(services.get(domain, {})):
            name = f"{domain}.{service}"
            actions.append({"name": name, "service": name, "display_name": name})
    return actions


async def _read_automation_summaries(hass: HomeAssistant) -> list[dict[str, Any]]:
    configs = await _read_automation_configs(hass)
    summaries: list[dict[str, Any]] = []
    for item in configs:
        if not isinstance(item, dict):
            continue
        summary: dict[str, Any] = {}
        if automation_id := item.get(CONF_ID):
            summary["id"] = automation_id
        if alias := item.get(CONF_ALIAS):
            summary["alias"] = alias
        if description := item.get("description"):
            summary["description"] = description
        summaries.append(summary)
    return summaries


async def _list_managed_automations(
    hass: HomeAssistant,
    *,
    kind: str | None = None,
    schedule_filter: dict[str, Any] | None = None,
) -> dict[str, Any]:
    configs = await _read_automation_configs(hass)
    target_kind = _normalize_managed_kind(kind)
    managed: list[dict[str, Any]] = []
    for item in configs:
        automation_id = item.get(CONF_ID)
        if not isinstance(automation_id, str) or not _is_houzzkit_automation_id(
            automation_id
        ):
            continue
        managed_kind = _managed_kind_for_automation(item)
        if target_kind is not None and managed_kind != target_kind:
            continue
        semantic_text = _semantic_text_for_automation(item)
        if semantic_text is None:
            continue
        alias = _automation_alias_for_user(item)
        if managed_kind == _MANAGED_KIND_REMINDER:
            reminder_item, reminder_errors = _reminder_public_item(
                item,
                automation_id,
                alias,
                semantic_text,
            )
            if reminder_errors:
                return {
                    "success": False,
                    "error": "Reminder metadata is missing or invalid",
                    "errors": reminder_errors,
                }
            if not _schedule_matches_filter(
                reminder_item.get("schedule"),
                schedule_filter,
            ):
                continue
            managed.append(reminder_item)
            continue

        if not _automation_matches_schedule_filter(item, schedule_filter):
            continue
        managed.append(
            {
                CONF_ID: automation_id,
                CONF_ALIAS: alias,
                "managed_kind": managed_kind,
                _SEMANTIC_TEXT_FIELD: semantic_text,
            }
        )

    return {
        "success": True,
        "automations": managed,
        "total_count": len(managed),
    }


async def _delete_managed_automation(
    hass: HomeAssistant,
    automation_id: str,
) -> dict[str, Any]:
    if not isinstance(automation_id, str) or not _is_houzzkit_automation_id(automation_id):
        return {
            "success": False,
            "error": "Only Houzzkit AI managed automations can be deleted",
        }

    async with _AUTOMATION_WRITE_LOCK:
        configs = await _read_automation_configs(hass)
        removed: dict[str, Any] | None = None
        remaining: list[dict[str, Any]] = []
        for item in configs:
            if item.get(CONF_ID) == automation_id:
                removed = item
                continue
            remaining.append(item)

        if removed is None:
            return {"success": False, "error": "Automation not found"}

        await _write_automation_configs(hass, remaining)

    await hass.services.async_call(
        AUTOMATION_DOMAIN,
        SERVICE_RELOAD,
        {},
        blocking=True,
    )
    alias = _automation_alias_for_user(removed)
    return {
        "success": True,
        "deleted_automation": {
            CONF_ALIAS: alias,
        },
    }


async def _get_managed_automation(
    hass: HomeAssistant,
    automation_id: str,
) -> dict[str, Any]:
    if not isinstance(automation_id, str) or not _is_houzzkit_automation_id(automation_id):
        return {
            "success": False,
            "failure_type": "unsupported",
            "error": "Only Houzzkit AI managed automations can be read",
        }

    configs = await _read_automation_configs(hass)
    for item in configs:
        if item.get(CONF_ID) != automation_id:
            continue
        managed_kind = _managed_kind_for_automation(item)
        if managed_kind != _MANAGED_KIND_AUTOMATION:
            return {
                "success": False,
                "failure_type": "unsupported",
                "error": "Only automation managed items can be modified",
            }
        snapshot = _editable_snapshot_for_automation(item)
        if snapshot is None:
            return {
                "success": False,
                "failure_type": "not_editable",
                "error": "This automation was created before editable snapshots were available",
            }
        return {
            "success": True,
            CONF_ID: automation_id,
            "managed_kind": managed_kind,
            "automation": snapshot,
        }

    return {"success": False, "failure_type": "not_found", "error": "Automation not found"}


async def _replace_managed_automation(
    intent_obj: intent.Intent,
    automation_id: str,
    automation: dict[str, Any],
    *,
    editable_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(automation_id, str) or not _is_houzzkit_automation_id(automation_id):
        return {
            "success": False,
            "failure_type": "unsupported",
            "errors": ["Only Houzzkit AI managed automations can be replaced"],
        }
    if not isinstance(automation, dict):
        return {"success": False, "failure_type": "plan_invalid", "errors": ["automation must be an object"]}

    async with _AUTOMATION_WRITE_LOCK:
        hass = intent_obj.hass
        configs = await _read_automation_configs(hass)
        replace_index: int | None = None
        existing: dict[str, Any] | None = None
        for index, item in enumerate(configs):
            if item.get(CONF_ID) == automation_id:
                replace_index = index
                existing = item
                break

        if replace_index is None or existing is None:
            return {
                "success": False,
                "failure_type": "not_found",
                "errors": ["Automation not found"],
            }
        if _managed_kind_for_automation(existing) != _MANAGED_KIND_AUTOMATION:
            return {
                "success": False,
                "failure_type": "unsupported",
                "errors": ["Only automation managed items can be modified"],
            }
        if _editable_snapshot_for_automation(existing) is None:
            return {
                "success": False,
                "failure_type": "not_editable",
                "errors": ["This automation was created before editable snapshots were available"],
            }

        specs, errors = _split_automation_specs(automation)
        if errors:
            return {"success": False, "failure_type": "plan_invalid", "errors": errors}
        if len(specs) != 1:
            return {
                "success": False,
                "failure_type": "plan_invalid",
                "errors": ["replacement automation must compile to exactly one Home Assistant automation"],
            }

        spec = specs[0]
        config, config_errors = await _automation_config_from_internal_plan(
            intent_obj,
            automation_id,
            spec["automation"],
            append_one_shot_cleanup=spec["type"] == _ONE_SHOT_AUTOMATION_TYPE,
            managed_kind=_MANAGED_KIND_AUTOMATION,
            editable_snapshot=editable_snapshot,
        )
        if config_errors:
            return {
                "success": False,
                "failure_type": "plan_invalid",
                "errors": config_errors,
            }

        try:
            await async_validate_config_item(hass, automation_id, config)
        except (vol.Invalid, HomeAssistantError) as exc:
            return {"success": False, "failure_type": "plan_invalid", "errors": [str(exc)]}

        configs[replace_index] = config
        await _write_automation_configs(hass, configs)

    await intent_obj.hass.services.async_call(
        AUTOMATION_DOMAIN,
        SERVICE_RELOAD,
        {},
        blocking=True,
    )
    alias = _automation_alias_for_user(config)
    return {
        "success": True,
        CONF_ID: automation_id,
        CONF_ALIAS: alias,
        "managed_kind": _MANAGED_KIND_AUTOMATION,
    }


async def _validate_automation(
    intent_obj: intent.Intent,
    automation: dict[str, Any],
    *,
    automation_id: str | None = None,
) -> dict[str, Any]:
    if not isinstance(automation, dict):
        return {
            "success": True,
            "valid": False,
            "errors": ["automation must be an object"],
        }

    specs, errors = _split_automation_specs(automation)
    if errors:
        return {"success": True, "valid": False, "errors": errors}

    base_config_id = automation_id or _AUTOMATION_ID_PREFIX + "validation"
    config_ids = [
        base_config_id if len(specs) == 1 else f"{base_config_id}_{index + 1}"
        for index in range(len(specs))
    ]
    configs: list[tuple[str, dict[str, Any]]] = []
    for spec, config_id in zip(specs, config_ids, strict=True):
        config, config_errors = await _automation_config_from_internal_plan(
            intent_obj,
            config_id,
            spec["automation"],
            append_one_shot_cleanup=spec["type"] == _ONE_SHOT_AUTOMATION_TYPE,
        )
        errors.extend(config_errors)
        if config:
            configs.append((config_id, config))
    if errors:
        return {"success": True, "valid": False, "errors": errors}

    try:
        for config_id, config in configs:
            await async_validate_config_item(intent_obj.hass, config_id, config)
    except (vol.Invalid, HomeAssistantError) as exc:
        return {"success": True, "valid": False, "errors": [str(exc)]}
    except Exception as exc:  # noqa: BLE001
        _LOGGER.exception("Unexpected automation validation failure")
        return {"success": False, "valid": False, "errors": [str(exc)]}

    return {"success": True, "valid": True, "errors": []}


async def _create_automation(
    intent_obj: intent.Intent,
    automation: dict[str, Any],
    *,
    managed_kind: str = _MANAGED_KIND_AUTOMATION,
    editable_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(automation, dict):
        return {"success": False, "errors": ["automation must be an object"]}
    normalized_managed_kind = _normalize_managed_kind(managed_kind)
    if normalized_managed_kind is None:
        return {
            "success": False,
            "errors": ["managed_kind must be automation or reminder"],
        }

    async with _AUTOMATION_WRITE_LOCK:
        hass = intent_obj.hass
        configs = await _read_automation_configs(hass)
        existing_ids = {
            item.get(CONF_ID)
            for item in configs
            if isinstance(item, dict) and isinstance(item.get(CONF_ID), str)
        }
        requested_id = automation.get(CONF_ID)
        if isinstance(requested_id, str) and requested_id in existing_ids:
            return {
                "success": False,
                "errors": [f"automation id already exists: {requested_id}"],
            }

        specs, errors = _split_automation_specs(automation)
        if errors:
            return {
                "success": False,
                "errors": errors,
            }

        used_ids = set(existing_ids)
        automation_ids: list[str] = []
        for _spec in specs:
            automation_id = _generate_automation_id(used_ids)
            automation_ids.append(automation_id)
            used_ids.add(automation_id)

        prepared: list[dict[str, Any]] = []
        for spec, automation_id in zip(specs, automation_ids, strict=True):
            config, config_errors = await _automation_config_from_internal_plan(
                intent_obj,
                automation_id,
                spec["automation"],
                append_one_shot_cleanup=spec["type"] == _ONE_SHOT_AUTOMATION_TYPE,
                managed_kind=normalized_managed_kind,
                editable_snapshot=editable_snapshot,
            )
            errors.extend(config_errors)
            if config:
                prepared.append(
                    {
                        CONF_ID: automation_id,
                        CONF_ALIAS: str(config.get(CONF_ALIAS, "")),
                        "type": spec["type"],
                        "config": config,
                    }
                )
        if errors:
            return {
                "success": False,
                "errors": errors,
            }

        try:
            for item in prepared:
                await async_validate_config_item(hass, item[CONF_ID], item["config"])
        except (vol.Invalid, HomeAssistantError) as exc:
            return {"success": False, "errors": [str(exc)]}

        configs.extend(item["config"] for item in prepared)
        await _write_automation_configs(hass, configs)

    await hass.services.async_call(
        AUTOMATION_DOMAIN,
        SERVICE_RELOAD,
        {},
        blocking=True,
    )

    if len(prepared) > 1:
        return {
            "success": True,
            "automations": [
                {
                    CONF_ID: item[CONF_ID],
                    CONF_ALIAS: item[CONF_ALIAS],
                    "type": item["type"],
                    "managed_kind": normalized_managed_kind,
                }
                for item in prepared
            ],
        }

    item = prepared[0]
    automation_id = item[CONF_ID]
    alias = item[CONF_ALIAS]
    result: dict[str, Any] = {
        "success": True,
        "id": automation_id,
        "alias": alias,
        "managed_kind": normalized_managed_kind,
    }
    if entity_id := _find_automation_entity_id(hass, automation_id):
        result["entity_id"] = entity_id
    if _has_alias_duplicate(configs, alias):
        result["warnings"] = ["automation alias already exists"]
    return result


async def _automation_config_from_internal_plan(
    intent_obj: intent.Intent,
    automation_id: str,
    automation: dict[str, Any],
    *,
    append_one_shot_cleanup: bool = False,
    managed_kind: str = _MANAGED_KIND_AUTOMATION,
    editable_snapshot: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    errors = _raw_target_errors(automation)
    if errors:
        return {}, errors

    semantic_text = _semantic_text_from_internal_plan(automation, errors)
    if errors or semantic_text is None:
        return {}, errors

    reminder_metadata: dict[str, Any] | None = None
    if managed_kind == _MANAGED_KIND_REMINDER:
        reminder_metadata = _reminder_metadata_from_internal_plan(automation, errors)
        if errors:
            return {}, errors

    converted = deepcopy(automation)
    converted.pop(_SEMANTIC_TEXT_FIELD, None)
    _convert_plan_time_triggers(converted, errors, managed_kind=managed_kind)
    _reject_delay_conditions(
        converted.get(CONF_CONDITIONS),
        errors,
        path="automation.conditions",
    )
    _reject_delay_conditions(
        converted.get("condition"),
        errors,
        path="automation.condition",
    )

    await _convert_rules_with_resolved_targets(
        intent_obj,
        converted.get(CONF_TRIGGERS),
        errors,
        path="automation.triggers",
    )
    if converted.get(CONF_CONDITIONS) is not None:
        await _convert_rule_with_resolved_targets(
            intent_obj,
            converted[CONF_CONDITIONS],
            errors,
            path="automation.conditions",
        )

    for actions_key in (CONF_ACTIONS, "action"):
        actions = converted.get(actions_key)
        if isinstance(actions, list):
            converted_actions: list[dict[str, Any]] = []
            for index, action in enumerate(actions):
                converted_actions.extend(
                    await _convert_action_with_resolved_targets(
                        intent_obj,
                        action,
                        errors,
                        path=f"automation.{actions_key}[{index}]",
                    )
                )
            converted[actions_key] = converted_actions

    if errors:
        return {}, errors

    if append_one_shot_cleanup:
        _append_one_shot_cleanup_action(converted, automation_id)

    return _automation_config_for_write(
        automation_id,
        converted,
        managed_kind=managed_kind,
        reminder_metadata=reminder_metadata,
        semantic_text=semantic_text,
        editable_snapshot=editable_snapshot or automation,
    ), []


def _split_automation_specs(
    automation: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    actions = _normalized_action_items(automation, errors)
    triggers = _normalized_trigger_items(automation, errors)
    if errors:
        return [], errors

    one_shot_triggers: list[dict[str, Any]] = []
    regular_triggers: list[dict[str, Any]] = []

    for trigger in triggers:
        if _is_one_shot_plan_trigger(trigger):
            one_shot_triggers.append(trigger)
        else:
            regular_triggers.append(trigger)

    spec_count = len(one_shot_triggers) + (1 if regular_triggers else 0)
    if spec_count > 1 and _contains_trigger_condition(automation):
        errors.append(
            "automation conditions cannot include trigger conditions when "
            "one-shot triggers are split into separate automations"
        )
        return [], errors

    specs: list[dict[str, Any]] = []
    for trigger in one_shot_triggers:
        specs.append(
            {
                "type": _ONE_SHOT_AUTOMATION_TYPE,
                "automation": _automation_spec_from_parts(
                    automation,
                    [trigger],
                    actions,
                ),
            }
        )

    if regular_triggers or not one_shot_triggers:
        specs.append(
            {
                "type": _REGULAR_AUTOMATION_TYPE,
                "automation": _automation_spec_from_parts(
                    automation,
                    regular_triggers,
                    actions,
                ),
            }
        )

    return specs, errors


def _normalized_trigger_items(
    automation: dict[str, Any],
    errors: list[str],
) -> list[dict[str, Any]]:
    triggers: list[dict[str, Any]] = []
    for key in (CONF_TRIGGERS, "trigger"):
        value = automation.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            invalid_count = len(
                [item for item in value if not isinstance(item, dict)]
            )
            if invalid_count:
                errors.append(f"automation.{key} items must be objects")
            triggers.extend(deepcopy(item) for item in value if isinstance(item, dict))
            continue
        if isinstance(value, dict):
            triggers.append(deepcopy(value))
            continue
        errors.append(f"automation.{key} must be a list or object")
    return triggers


def _normalized_action_items(
    automation: dict[str, Any],
    errors: list[str],
) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    for key in (CONF_ACTIONS, "action"):
        value = automation.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            invalid_count = len(
                [item for item in value if not isinstance(item, dict)]
            )
            if invalid_count:
                errors.append(f"automation.{key} items must be objects")
            actions.extend(deepcopy(item) for item in value if isinstance(item, dict))
            continue
        if isinstance(value, dict):
            actions.append(deepcopy(value))
            continue
        errors.append(f"automation.{key} must be a list or object")
    return actions


def _automation_spec_from_parts(
    automation: dict[str, Any],
    triggers: list[dict[str, Any]],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    spec = {
        key: deepcopy(value)
        for key, value in automation.items()
        if key not in {CONF_ID, CONF_TRIGGERS, "trigger", CONF_ACTIONS, "action"}
    }
    if triggers:
        spec[CONF_TRIGGERS] = deepcopy(triggers)
    if actions:
        spec[CONF_ACTIONS] = deepcopy(actions)
    return spec


def _is_one_shot_plan_trigger(trigger: dict[str, Any]) -> bool:
    trigger_type = trigger.get("trigger") or trigger.get(CONF_PLATFORM)
    return trigger_type == "delay" or (trigger_type == "time" and "date" in trigger)


def _contains_trigger_condition(automation: dict[str, Any]) -> bool:
    return _node_contains_trigger_condition(automation.get(CONF_CONDITIONS)) or (
        _node_contains_trigger_condition(automation.get("condition"))
    )


def _node_contains_trigger_condition(node: Any) -> bool:
    if isinstance(node, list):
        return any(_node_contains_trigger_condition(item) for item in node)
    if not isinstance(node, dict):
        return False
    if node.get(CONF_CONDITION) == "trigger":
        return True
    children = node.get(CONF_CONDITIONS)
    return _node_contains_trigger_condition(children)


def _append_one_shot_cleanup_action(
    automation: dict[str, Any],
    automation_id: str,
) -> None:
    cleanup_action = {
        "action": _DELETE_ONE_SHOT_ACTION,
        "data": {
            CONF_ID: automation_id,
            "marker": _ONE_SHOT_CLEANUP_MARKER,
        },
    }
    actions = automation.pop("action", None)
    if isinstance(actions, dict):
        actions = [actions]
    elif not isinstance(actions, list):
        actions = []
    if existing_actions := automation.get(CONF_ACTIONS):
        if isinstance(existing_actions, list):
            actions = [*existing_actions, *actions]
        elif isinstance(existing_actions, dict):
            actions = [existing_actions, *actions]
    actions.append(cleanup_action)
    automation[CONF_ACTIONS] = actions


def _convert_plan_time_triggers(
    automation: dict[str, Any],
    errors: list[str],
    *,
    managed_kind: str = _MANAGED_KIND_AUTOMATION,
) -> None:
    """把 ai-server 的日期/延迟/间隔协议转换为 HA 可加载的 trigger。"""
    trigger_nodes = _automation_trigger_nodes(automation)
    if not trigger_nodes:
        return

    id_counts = _trigger_id_counts(trigger_nodes)
    used_ids = set(id_counts)
    generated_id_index = 1
    one_shot_triggers: list[tuple[str, str]] = []

    for path, trigger in trigger_nodes:
        trigger_type = trigger.get("trigger") or trigger.get(CONF_PLATFORM)
        target_dt: datetime | None = None

        if "timezone" in trigger:
            errors.append(f"{path}.timezone is not supported")

        if "date" in trigger and trigger_type != "time":
            errors.append(f"{path}.date is only supported on time triggers")
            continue

        if trigger_type == "interval":
            if managed_kind != _MANAGED_KIND_REMINDER:
                errors.append(f"{path}.interval trigger is only supported for reminder")
                continue
            every_minutes = _rewrite_interval_trigger(trigger, errors, path=path)
            if every_minutes is not None:
                _append_automation_condition(
                    automation,
                    _interval_template_condition(every_minutes),
                )
            continue

        if trigger_type == "delay":
            target_dt = _target_datetime_from_delay_trigger(trigger, errors, path=path)
        elif trigger_type == "time" and "date" in trigger:
            target_dt = _target_datetime_from_date_trigger(trigger, errors, path=path)

        if target_dt is None:
            continue

        trigger_id = trigger.get(CONF_ID)
        if (
            not isinstance(trigger_id, str)
            or not trigger_id.strip()
            or id_counts.get(trigger_id, 0) > 1
        ):
            while True:
                trigger_id = f"{_ONE_SHOT_TRIGGER_ID_PREFIX}{generated_id_index}"
                generated_id_index += 1
                if trigger_id not in used_ids:
                    break
            trigger[CONF_ID] = trigger_id
            used_ids.add(trigger_id)

        _rewrite_one_shot_trigger(trigger, target_dt)
        one_shot_triggers.append((trigger_id, target_dt.date().isoformat()))

    if one_shot_triggers:
        guard = _one_shot_date_guard_condition(
            one_shot_triggers,
            total_trigger_count=len(trigger_nodes),
        )
        _append_automation_condition(automation, guard)


def _automation_trigger_nodes(
    automation: dict[str, Any],
) -> list[tuple[str, dict[str, Any]]]:
    nodes: list[tuple[str, dict[str, Any]]] = []
    for key in (CONF_TRIGGERS, "trigger"):
        triggers = automation.get(key)
        if isinstance(triggers, list):
            for index, trigger in enumerate(triggers):
                if isinstance(trigger, dict):
                    nodes.append((f"automation.{key}[{index}]", trigger))
        elif isinstance(triggers, dict):
            nodes.append((f"automation.{key}", triggers))
    return nodes


def _trigger_id_counts(
    trigger_nodes: list[tuple[str, dict[str, Any]]],
) -> dict[str, int]:
    counts: dict[str, int] = {}
    for _path, trigger in trigger_nodes:
        trigger_id = trigger.get(CONF_ID)
        if isinstance(trigger_id, str) and trigger_id.strip():
            counts[trigger_id] = counts.get(trigger_id, 0) + 1
    return counts


def _target_datetime_from_date_trigger(
    trigger: dict[str, Any],
    errors: list[str],
    *,
    path: str,
) -> datetime | None:
    unsupported_keys = sorted(
        key
        for key in trigger
        if key
        not in {"trigger", CONF_PLATFORM, CONF_ID, "date", CONF_AT, CONF_WEEKDAY}
    )
    if unsupported_keys:
        errors.append(
            f"{path} time date trigger contains unsupported keys: {unsupported_keys}"
        )

    if CONF_WEEKDAY in trigger:
        errors.append(f"{path}.date cannot be used with weekday")

    date_value = _parse_local_date(trigger.get("date"), errors, path=f"{path}.date")
    at_value = _parse_local_time(trigger.get(CONF_AT), errors, path=f"{path}.at")
    if date_value is None or at_value is None:
        return None

    target_dt = datetime.combine(
        date_value,
        at_value,
        tzinfo=dt_util.get_default_time_zone(),
    )
    if target_dt <= dt_util.now():
        errors.append(f"{path} target datetime is in the past")
        return None
    return target_dt


def _target_datetime_from_delay_trigger(
    trigger: dict[str, Any],
    errors: list[str],
    *,
    path: str,
) -> datetime | None:
    unsupported_keys = sorted(
        key
        for key in trigger
        if key
        not in {"trigger", CONF_PLATFORM, CONF_ID, "duration"}
        | _DELAY_TRIGGER_CONFLICT_FIELDS
    )
    if unsupported_keys:
        errors.append(
            f"{path} delay trigger contains unsupported keys: {unsupported_keys}"
        )

    conflict_keys = sorted(
        key for key in _DELAY_TRIGGER_CONFLICT_FIELDS if key in trigger
    )
    if conflict_keys:
        errors.append(f"{path} delay trigger cannot include {conflict_keys}")

    duration = _parse_delay_duration(
        trigger.get("duration"),
        errors,
        path=f"{path}.duration",
    )
    if duration is None:
        return None

    target_dt = dt_util.now() + duration
    if target_dt.microsecond:
        target_dt = target_dt.replace(microsecond=0) + timedelta(seconds=1)
    return target_dt


def _parse_local_date(value: Any, errors: list[str], *, path: str) -> date | None:
    if not isinstance(value, str) or _LOCAL_DATE_RE.fullmatch(value) is None:
        errors.append(f"{path} must use YYYY-MM-DD")
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        errors.append(f"{path} must be a valid local date")
        return None


def _parse_local_time(value: Any, errors: list[str], *, path: str) -> time | None:
    if not isinstance(value, str) or _LOCAL_TIME_RE.fullmatch(value) is None:
        errors.append(f"{path} must use HH:MM:SS")
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        errors.append(f"{path} must be a valid local time")
        return None


def _parse_delay_duration(
    value: Any,
    errors: list[str],
    *,
    path: str,
) -> timedelta | None:
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object")
        return None

    unsupported_keys = sorted(key for key in value if key not in _DELAY_DURATION_FIELDS)
    if unsupported_keys:
        errors.append(f"{path} contains unsupported keys: {unsupported_keys}")

    parts: dict[str, int] = {}
    has_positive_value = False
    for key in sorted(_DELAY_DURATION_FIELDS):
        raw_part = value.get(key, 0)
        if not isinstance(raw_part, int) or isinstance(raw_part, bool) or raw_part < 0:
            errors.append(f"{path}.{key} must be a non-negative integer")
            continue
        parts[key] = raw_part
        if raw_part > 0:
            has_positive_value = True

    if not has_positive_value:
        errors.append(f"{path} must include at least one positive value")
        return None
    if unsupported_keys:
        return None
    if len(parts) != len(_DELAY_DURATION_FIELDS):
        return None

    return timedelta(
        days=parts["days"],
        hours=parts["hours"],
        minutes=parts["minutes"],
        seconds=parts["seconds"],
    )


def _rewrite_interval_trigger(
    trigger: dict[str, Any],
    errors: list[str],
    *,
    path: str,
) -> int | None:
    unsupported_keys = sorted(
        key
        for key in trigger
        if key
        not in {"trigger", CONF_PLATFORM, CONF_ID, "every_minutes"}
        | _INTERVAL_TRIGGER_CONFLICT_FIELDS
    )
    if unsupported_keys:
        errors.append(
            f"{path} interval trigger contains unsupported keys: {unsupported_keys}"
        )

    conflict_keys = sorted(
        key for key in _INTERVAL_TRIGGER_CONFLICT_FIELDS if key in trigger
    )
    if conflict_keys:
        errors.append(f"{path} interval trigger cannot include {conflict_keys}")

    every_minutes = _parse_interval_minutes(
        trigger.get("every_minutes"),
        errors,
        path=f"{path}.every_minutes",
    )
    if every_minutes is None or unsupported_keys or conflict_keys:
        return None

    trigger.pop("trigger", None)
    trigger.pop("every_minutes", None)
    trigger.pop("date", None)
    trigger.pop("duration", None)
    trigger.pop(CONF_WEEKDAY, None)
    trigger[CONF_PLATFORM] = "time_pattern"
    trigger["minutes"] = "*"
    return every_minutes


def _parse_interval_minutes(
    value: Any,
    errors: list[str],
    *,
    path: str,
) -> int | None:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < _INTERVAL_MINUTES_MIN
        or value > _INTERVAL_MINUTES_MAX
    ):
        errors.append(
            f"{path} must be an integer between "
            f"{_INTERVAL_MINUTES_MIN} and {_INTERVAL_MINUTES_MAX}"
        )
        return None
    return value


def _interval_template_condition(every_minutes: int) -> dict[str, Any]:
    # interval 提醒按本地午夜后的分钟数取模；配合每分钟 time_pattern 触发。
    return {
        CONF_CONDITION: "template",
        CONF_VALUE_TEMPLATE: (
            f"{{{{ ((now().hour * 60) + now().minute) % {every_minutes} == 0 }}}}"
        ),
    }


def _rewrite_one_shot_trigger(trigger: dict[str, Any], target_dt: datetime) -> None:
    trigger.pop("trigger", None)
    trigger.pop("date", None)
    trigger.pop("duration", None)
    trigger.pop(CONF_WEEKDAY, None)
    trigger[CONF_PLATFORM] = "time"
    trigger[CONF_AT] = target_dt.strftime("%H:%M:%S")


def _one_shot_date_guard_condition(
    one_shot_triggers: list[tuple[str, str]],
    *,
    total_trigger_count: int,
) -> dict[str, Any]:
    if total_trigger_count == 1:
        return _date_template_condition(one_shot_triggers[0][1])

    one_shot_ids = [trigger_id for trigger_id, _date_value in one_shot_triggers]
    conditions: list[dict[str, Any]] = []
    if total_trigger_count > len(one_shot_triggers):
        conditions.append(
            {
                CONF_CONDITION: "not",
                CONF_CONDITIONS: [
                    {CONF_CONDITION: "trigger", CONF_ID: one_shot_ids}
                ],
            }
        )

    for trigger_id, date_value in one_shot_triggers:
        conditions.append(
            {
                CONF_CONDITION: "and",
                CONF_CONDITIONS: [
                    {CONF_CONDITION: "trigger", CONF_ID: trigger_id},
                    _date_template_condition(date_value),
                ],
            }
        )

    return {CONF_CONDITION: "or", CONF_CONDITIONS: conditions}


def _date_template_condition(date_value: str) -> dict[str, str]:
    return {
        CONF_CONDITION: "template",
        CONF_VALUE_TEMPLATE: (
            "{{ now().strftime('%Y-%m-%d') == '" + date_value + "' }}"
        ),
    }


def _append_automation_condition(
    automation: dict[str, Any],
    guard: dict[str, Any],
) -> None:
    condition_key = (
        CONF_CONDITIONS
        if CONF_CONDITIONS in automation or "condition" not in automation
        else "condition"
    )
    existing = automation.get(condition_key)
    if existing is None:
        automation[condition_key] = guard
        return

    if isinstance(existing, list):
        conditions = [*existing, guard]
    else:
        conditions = [existing, guard]
    automation[condition_key] = {CONF_CONDITION: "and", CONF_CONDITIONS: conditions}


def _reject_delay_conditions(
    condition: Any,
    errors: list[str],
    *,
    path: str,
) -> None:
    if condition is None:
        return
    if isinstance(condition, list):
        for index, item in enumerate(condition):
            _reject_delay_conditions(item, errors, path=f"{path}[{index}]")
        return
    if not isinstance(condition, dict):
        return

    condition_type = condition.get(CONF_CONDITION)
    trigger_type = condition.get("trigger") or condition.get(CONF_PLATFORM)
    if condition_type == "delay" or trigger_type == "delay":
        errors.append(f"{path}.condition delay is only supported as a trigger")

    children = condition.get(CONF_CONDITIONS)
    if isinstance(children, list):
        for index, item in enumerate(children):
            _reject_delay_conditions(
                item,
                errors,
                path=f"{path}.conditions[{index}]",
            )


def _raw_target_errors(value: Any) -> list[str]:
    errors: list[str] = []

    def visit(node: Any, path: str, *, in_resolved_targets: bool = False) -> None:
        if isinstance(node, dict):
            for key, nested in node.items():
                child_path = f"{path}.{key}" if path else key
                child_in_resolved = in_resolved_targets or key == _RESOLVED_TARGETS_FIELD
                if key == ATTR_ENTITY_ID:
                    errors.append(f"{child_path} is not allowed")
                    continue
                if key == "target":
                    errors.append(f"{child_path} must be resolved before HA creation")
                    continue
                if key in {"domain", "domains"} and not in_resolved_targets:
                    errors.append(f"{child_path} is not allowed")
                    continue
                visit(nested, child_path, in_resolved_targets=child_in_resolved)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                visit(item, f"{path}[{index}]", in_resolved_targets=in_resolved_targets)

    visit(value, "automation")
    return errors


async def _convert_rules_with_resolved_targets(
    intent_obj: intent.Intent,
    rules: Any,
    errors: list[str],
    *,
    path: str,
) -> None:
    if not isinstance(rules, list):
        return
    for index, rule in enumerate(rules):
        await _convert_rule_with_resolved_targets(
            intent_obj,
            rule,
            errors,
            path=f"{path}[{index}]",
        )


async def _convert_rule_with_resolved_targets(
    intent_obj: intent.Intent,
    rule: Any,
    errors: list[str],
    *,
    path: str,
) -> None:
    if isinstance(rule, list):
        await _convert_rules_with_resolved_targets(intent_obj, rule, errors, path=path)
        return
    if not isinstance(rule, dict):
        return

    if resolved_targets := rule.pop(_RESOLVED_TARGETS_FIELD, None):
        entity_infos = await _match_resolved_targets(
            intent_obj,
            resolved_targets,
            errors,
            path=path,
        )
        if entity_infos:
            entity_ids = [item.state.entity_id for item in entity_infos]
            rule[ATTR_ENTITY_ID] = entity_ids[0] if len(entity_ids) == 1 else entity_ids

    children = rule.get(CONF_CONDITIONS)
    if isinstance(children, list):
        await _convert_rules_with_resolved_targets(
            intent_obj,
            children,
            errors,
            path=f"{path}.{CONF_CONDITIONS}",
        )


async def _convert_action_with_resolved_targets(
    intent_obj: intent.Intent,
    action: Any,
    errors: list[str],
    *,
    path: str,
) -> list[dict[str, Any]]:
    if not isinstance(action, dict):
        errors.append(f"{path} must be an object")
        return []

    action_name = _action_name(action)
    if action_name == _DELETE_ONE_SHOT_ACTION:
        errors.append(f"{path}.action is internal and cannot be provided")
        return []

    if action.get("service") in _HOUZZKIT_NOTIFICATION_ACTIONS:
        errors.append(f"{path}.service is not allowed; use action instead")
        return []

    if action.get("action") in _HOUZZKIT_NOTIFICATION_ACTIONS:
        return _convert_houzzkit_notify_action(
            intent_obj,
            action,
            errors,
            path=path,
        )

    if _is_rejected_notification_action(action_name):
        errors.append(
            f"{path}.action uses unsupported notification service: {action_name}; "
            f"use {_HOUZZKIT_NOTIFY_ACTION} or {_HOUZZKIT_WARN_ACTION}"
        )
        return []

    resolved_targets = action.pop(_RESOLVED_TARGETS_FIELD, None)
    if resolved_targets is None:
        if "operation" in action:
            errors.append(f"{path}.operation requires resolved targets")
            return []
        return [action]

    operation = action.pop("operation", None)
    if operation not in {SERVICE_TURN_ON, SERVICE_TURN_OFF}:
        errors.append(f"{path}.operation must be turn_on or turn_off")
        return []

    entity_infos = await _match_resolved_targets(
        intent_obj,
        resolved_targets,
        errors,
        path=path,
    )
    if not entity_infos:
        return []

    grouped: dict[tuple[str, str], list[str]] = {}
    for item in entity_infos:
        service = _service_for_operation(item, operation, errors, path=path)
        if service is None:
            continue
        grouped.setdefault(service, []).append(item.state.entity_id)

    converted_actions: list[dict[str, Any]] = []
    passthrough = {k: v for k, v in action.items() if k not in {"action", "target"}}
    for (domain, service), entity_ids in grouped.items():
        converted_actions.append(
            {
                **passthrough,
                "action": f"{domain}.{service}",
                "target": {
                    ATTR_ENTITY_ID: entity_ids[0]
                    if len(entity_ids) == 1
                    else entity_ids
                },
            }
        )
    return converted_actions


def _action_name(action: dict[str, Any]) -> str | None:
    for key in ("action", "service"):
        value = action.get(key)
        if isinstance(value, str):
            return value
    return None


def _is_rejected_notification_action(action_name: str | None) -> bool:
    if action_name is None or "." not in action_name:
        return False
    if action_name in _HOUZZKIT_NOTIFICATION_ACTIONS:
        return False
    if action_name in _REJECTED_NOTIFICATION_ACTIONS:
        return True
    domain, _service = action_name.split(".", 1)
    return domain in _REJECTED_NOTIFICATION_DOMAINS


def _convert_houzzkit_notify_action(
    intent_obj: intent.Intent,
    action: dict[str, Any],
    errors: list[str],
    *,
    path: str,
) -> list[dict[str, Any]]:
    action_name = action.get("action")
    if "service" in action:
        errors.append(f"{path}.service is not allowed for {action_name}")
        return []
    if _RESOLVED_TARGETS_FIELD in action:
        errors.append(
            f"{path}.{_RESOLVED_TARGETS_FIELD} is not allowed for "
            f"{action_name}"
        )
        return []
    if "operation" in action:
        errors.append(f"{path}.operation is not allowed for {action_name}")
        return []

    data = action.get("data")
    if not isinstance(data, dict):
        errors.append(f"{path}.data must be an object")
        return []

    data_keys = set(data)
    extra_data_keys = data_keys - {"message"}
    if extra_data_keys:
        errors.append(
            f"{path}.data contains unsupported keys: {sorted(extra_data_keys)}"
        )
        return []

    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        errors.append(f"{path}.data.message must be a non-empty string")
        return []

    text_entity_id = _current_speaker_voice_text_entity_id(
        intent_obj,
        errors,
        path=path,
    )
    if text_entity_id is None:
        return []

    passthrough = {
        key: value
        for key, value in action.items()
        if key not in {"action", "data", "service", "target", _RESOLVED_TARGETS_FIELD}
    }
    return [
        {
            **passthrough,
            "action": f"{TEXT_DOMAIN}.{TEXT_SERVICE_SET_VALUE}",
            "target": {ATTR_ENTITY_ID: text_entity_id},
            "data": {TEXT_ATTR_VALUE: message.strip()},
        }
    ]


def _current_speaker_voice_text_entity_id(
    intent_obj: intent.Intent,
    errors: list[str],
    *,
    path: str,
) -> str | None:
    speaker_id = _speaker_id_from_intent(intent_obj)
    if speaker_id is None:
        errors.append(
            f"{path}: _speaker_id is required for "
            f"{_HOUZZKIT_NOTIFY_ACTION}/{_HOUZZKIT_WARN_ACTION}"
        )
        return None

    entity_entries = get_entities(intent_obj.hass, speak_id=speaker_id)
    if not entity_entries:
        errors.append(f"{path}: speaker not found: {speaker_id}")
        return None

    text_entries = [
        entry
        for entry in entity_entries
        if entry.domain == TEXT_DOMAIN and not entry.disabled
    ]
    if not text_entries:
        errors.append(f"{path}: no voice text entity found for speaker: {speaker_id}")
        return None

    hinted_entries = [entry for entry in text_entries if _is_voice_text_entry(entry)]
    if len(hinted_entries) == 1:
        return hinted_entries[0].entity_id
    if len(hinted_entries) > 1:
        entity_ids = ", ".join(entry.entity_id for entry in hinted_entries)
        errors.append(f"{path}: ambiguous voice text entities: {entity_ids}")
        return None

    if len(text_entries) == 1:
        return text_entries[0].entity_id

    entity_ids = ", ".join(entry.entity_id for entry in text_entries)
    errors.append(f"{path}: ambiguous text entities for speaker: {entity_ids}")
    return None


def _speaker_id_from_intent(intent_obj: intent.Intent) -> str | None:
    speaker_slot = intent_obj.slots.get("_speaker_id")
    if not isinstance(speaker_slot, dict):
        return None

    speaker_id = speaker_slot.get("value")
    if not isinstance(speaker_id, str):
        return None

    speaker_id = speaker_id.strip()
    return speaker_id or None


def _is_voice_text_entry(entry: Any) -> bool:
    values = (
        entry.entity_id,
        entry.name,
        entry.original_name,
        entry.suggested_object_id,
        entry.unique_id,
    )
    searchable = " ".join(
        str(value).casefold() for value in values if isinstance(value, str)
    )
    return any(hint.casefold() in searchable for hint in _VOICE_TEXT_HINTS)


async def _match_resolved_targets(
    intent_obj: intent.Intent,
    resolved_targets: Any,
    errors: list[str],
    *,
    path: str,
) -> list[EntityInfo]:
    if not isinstance(resolved_targets, list):
        errors.append(f"{path}.{_RESOLVED_TARGETS_FIELD} must be a list")
        return []

    targets = _expand_resolved_target_domains(resolved_targets)
    error_msg, entity_infos = await match_intent_entities(intent_obj, targets)
    if error_msg:
        errors.append(f"{path}.{_RESOLVED_TARGETS_FIELD}: {error_msg.get('error')}")
        return []
    entity_infos = entity_infos or []
    # 自动化目标解析与普通 turn_on/turn_off 保持一致：
    # 完全同名、同区域、同 domain 的多个实体对用户来说是同一个公开设备，
    # 因此这里接受全部匹配结果，并在后续转换中写入 entity_id 列表。
    return entity_infos


def _expand_resolved_target_domains(resolved_targets: list[Any]) -> list[dict[str, Any]]:
    expanded = deepcopy(resolved_targets)
    for target in expanded:
        if not isinstance(target, dict):
            continue
        devices = target.get("devices")
        if not isinstance(devices, list):
            continue
        for device in devices:
            if not isinstance(device, dict):
                continue
            domains = device.get("domains")
            if not isinstance(domains, list):
                continue
            normalized_domains: list[str] = []
            for domain in domains:
                if domain == "sensor":
                    normalized_domains.extend(["sensor", "binary_sensor"])
                else:
                    normalized_domains.append(domain)
            device["domains"] = list(dict.fromkeys(normalized_domains))
    return expanded


def _service_for_operation(
    entity_info: EntityInfo,
    operation: str,
    errors: list[str],
    *,
    path: str,
) -> tuple[str, str] | None:
    domain = entity_info.state.domain
    if domain in (BUTTON_DOMAIN, INPUT_BUTTON_DOMAIN):
        if operation != SERVICE_TURN_ON:
            errors.append(f"{path}: {entity_info.name} cannot be turned off")
            return None
        return domain, SERVICE_PRESS_BUTTON
    if domain == COVER_DOMAIN:
        return (
            domain,
            SERVICE_OPEN_COVER if operation == SERVICE_TURN_ON else SERVICE_CLOSE_COVER,
        )
    if domain == LOCK_DOMAIN:
        return domain, SERVICE_LOCK if operation == SERVICE_TURN_ON else SERVICE_UNLOCK
    if domain == VALVE_DOMAIN:
        return (
            domain,
            SERVICE_OPEN_VALVE if operation == SERVICE_TURN_ON else SERVICE_CLOSE_VALVE,
        )
    return domain, operation


def _automation_config_for_write(
    automation_id: str,
    automation: dict[str, Any],
    *,
    managed_kind: str = _MANAGED_KIND_AUTOMATION,
    reminder_metadata: dict[str, Any] | None = None,
    semantic_text: str,
    editable_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """使用稳定字段顺序写入 HA automation，id 总是由工具侧生成。"""
    config: dict[str, Any] = {CONF_ID: automation_id}
    for key in (
        CONF_ALIAS,
        "description",
        CONF_TRIGGERS,
        "trigger",
        CONF_CONDITIONS,
        "condition",
        CONF_ACTIONS,
        "action",
        CONF_MODE,
    ):
        if key in automation:
            config[key] = automation[key]
    for key, value in automation.items():
        if key not in {CONF_ID, _SEMANTIC_TEXT_FIELD} and key not in config:
            config[key] = value
    config[CONF_VARIABLES] = _variables_with_managed_kind(
        config.get(CONF_VARIABLES),
        managed_kind,
        reminder_metadata=reminder_metadata,
        semantic_text=semantic_text,
        editable_snapshot=editable_snapshot,
    )
    config.setdefault(CONF_MODE, "single")
    return config


def _generate_automation_id(existing_ids: set[Any]) -> str:
    for _ in range(10):
        automation_id = _AUTOMATION_ID_PREFIX + uuid4().hex[:12]
        if automation_id not in existing_ids:
            return automation_id
    raise HomeAssistantError("Unable to generate unique automation id")


async def _read_automation_configs(hass: HomeAssistant) -> list[dict[str, Any]]:
    path = hass.config.path(AUTOMATION_CONFIG_PATH)
    return await hass.async_add_executor_job(_read_automation_file, path)


def _read_automation_file(path: str) -> list[dict[str, Any]]:
    if not os.path.isfile(path):
        return []
    data = load_yaml(path)
    if data is None:
        return []
    if not isinstance(data, list):
        raise HomeAssistantError("automations.yaml must contain a list")
    return [item for item in data if isinstance(item, dict)]


async def _write_automation_configs(
    hass: HomeAssistant,
    configs: list[dict[str, Any]],
) -> None:
    path = hass.config.path(AUTOMATION_CONFIG_PATH)
    await hass.async_add_executor_job(_write_automation_file, path, configs)


def _write_automation_file(path: str, configs: list[dict[str, Any]]) -> None:
    write_utf8_file_atomic(path, dump(configs))


def _find_automation_entity_id(hass: HomeAssistant, automation_id: str) -> str | None:
    entity_registry = er.async_get(hass)
    return entity_registry.async_get_entity_id(
        AUTOMATION_DOMAIN,
        AUTOMATION_DOMAIN,
        automation_id,
    )


def _has_alias_duplicate(configs: list[dict[str, Any]], alias: str) -> bool:
    if not alias:
        return False
    normalized = alias.strip().casefold()
    count = sum(
        1
        for item in configs
        if str(item.get(CONF_ALIAS, "")).strip().casefold() == normalized
    )
    return count > 1


def _slot_value(slots: dict[str, Any], key: str, default: Any) -> Any:
    slot = slots.get(key)
    if isinstance(slot, dict) and "value" in slot:
        return slot["value"]
    return default


def _normalize_managed_kind(value: Any) -> str | None:
    if value in _MANAGED_KINDS:
        return str(value)
    return None


def _managed_kind_for_automation(automation: dict[str, Any]) -> str:
    variables = automation.get(CONF_VARIABLES)
    if isinstance(variables, dict):
        kind = _normalize_managed_kind(variables.get(_MANAGED_KIND_VARIABLE))
        if kind is not None:
            return kind
    # 历史 Houzzkit 自建项没有内部分类变量，按旧设备自动化管理。
    return _MANAGED_KIND_AUTOMATION


def _variables_with_managed_kind(
    value: Any,
    managed_kind: str,
    *,
    reminder_metadata: dict[str, Any] | None = None,
    semantic_text: str,
    editable_snapshot: dict[str, Any],
) -> dict[str, Any]:
    variables = deepcopy(value) if isinstance(value, dict) else {}
    variables[_MANAGED_KIND_VARIABLE] = managed_kind
    variables[_SEMANTIC_TEXT_VARIABLE] = semantic_text
    if managed_kind == _MANAGED_KIND_REMINDER:
        if reminder_metadata is not None:
            variables[_REMINDER_METADATA_VARIABLE] = deepcopy(reminder_metadata)
        else:
            variables.pop(_REMINDER_METADATA_VARIABLE, None)
        variables.pop(_EDITABLE_SNAPSHOT_VARIABLE, None)
    else:
        variables.pop(_REMINDER_METADATA_VARIABLE, None)
        variables[_EDITABLE_SNAPSHOT_VARIABLE] = _editable_snapshot_for_write(
            editable_snapshot
        )
    return variables


def _editable_snapshot_for_write(snapshot: dict[str, Any]) -> dict[str, Any]:
    # 该快照会在修改时回传给 LLM，必须只保存 public plan 字段，不能泄露 HA 内部目标。
    allowed_keys = {
        CONF_ALIAS,
        "description",
        _SEMANTIC_TEXT_FIELD,
        CONF_TRIGGERS,
        "trigger",
        CONF_CONDITIONS,
        "condition",
        CONF_ACTIONS,
        "action",
        CONF_MODE,
    }
    public_snapshot: dict[str, Any] = {}
    for key in allowed_keys:
        if key in snapshot:
            public_snapshot[key] = deepcopy(snapshot[key])
    return public_snapshot


def _editable_snapshot_for_automation(
    automation: dict[str, Any],
) -> dict[str, Any] | None:
    variables = automation.get(CONF_VARIABLES)
    if not isinstance(variables, dict):
        return None
    snapshot = variables.get(_EDITABLE_SNAPSHOT_VARIABLE)
    if not isinstance(snapshot, dict):
        return None
    semantic_text = snapshot.get(_SEMANTIC_TEXT_FIELD)
    if not isinstance(semantic_text, str) or not semantic_text.strip():
        return None
    if not isinstance(snapshot.get(CONF_ALIAS), str) or not snapshot[CONF_ALIAS].strip():
        return None
    triggers = snapshot.get(CONF_TRIGGERS)
    trigger = snapshot.get("trigger")
    actions = snapshot.get(CONF_ACTIONS)
    action = snapshot.get("action")
    if not isinstance(triggers, list) and not isinstance(trigger, dict):
        return None
    if not isinstance(actions, list) and not isinstance(action, dict):
        return None
    return deepcopy(snapshot)


def _semantic_text_from_internal_plan(
    automation: dict[str, Any],
    errors: list[str],
) -> str | None:
    value = automation.get(_SEMANTIC_TEXT_FIELD)
    if not isinstance(value, str) or not value.strip():
        errors.append("automation.semantic_text is required.")
        return None
    return value.strip()


def _reminder_metadata_from_internal_plan(
    automation: dict[str, Any],
    errors: list[str],
) -> dict[str, Any] | None:
    triggers = _normalized_trigger_items(automation, errors)
    actions = _normalized_action_items(automation, errors)
    if len(triggers) != 1:
        errors.append("reminder automation must contain exactly one trigger")
        return None
    if len(actions) != 1:
        errors.append("reminder automation must contain exactly one action")
        return None

    schedule = _reminder_schedule_from_plan_trigger(
        triggers[0],
        errors,
        path="automation.triggers[0]",
    )
    message = _reminder_message_from_plan_action(
        actions[0],
        errors,
        path="automation.actions[0]",
    )
    if schedule is None or message is None:
        return None
    return {"schedule": schedule, "message": message}


def _reminder_schedule_from_plan_trigger(
    trigger: dict[str, Any],
    errors: list[str],
    *,
    path: str,
) -> dict[str, Any] | None:
    trigger_type = trigger.get("trigger") or trigger.get(CONF_PLATFORM)
    if trigger_type == "time":
        schedule: dict[str, Any] = {"type": "time", CONF_AT: trigger.get(CONF_AT)}
        if "date" in trigger:
            schedule["date"] = trigger.get("date")
        if CONF_WEEKDAY in trigger:
            schedule[CONF_WEEKDAY] = deepcopy(trigger.get(CONF_WEEKDAY))
        return _normalize_reminder_schedule(schedule, errors, path=path)
    if trigger_type == "delay":
        schedule = {
            "type": "delay",
            "duration": deepcopy(trigger.get("duration")),
        }
        return _normalize_reminder_schedule(schedule, errors, path=path)
    if trigger_type == "interval":
        schedule = {
            "type": "interval",
            "every_minutes": trigger.get("every_minutes"),
        }
        return _normalize_reminder_schedule(schedule, errors, path=path)
    errors.append(f"{path}.trigger must be time, delay, or interval for reminder")
    return None


def _reminder_message_from_plan_action(
    action: dict[str, Any],
    errors: list[str],
    *,
    path: str,
) -> str | None:
    action_name = action.get("action") or action.get("service")
    if action_name != _HOUZZKIT_NOTIFY_ACTION:
        errors.append(f"{path}.action must be {_HOUZZKIT_NOTIFY_ACTION} for reminder")
        return None
    data = action.get("data")
    if not isinstance(data, dict):
        errors.append(f"{path}.data must be an object")
        return None
    message = data.get("message")
    if not isinstance(message, str) or not message.strip():
        errors.append(f"{path}.data.message must be a non-empty string")
        return None
    return message.strip()


def _reminder_public_item(
    automation: dict[str, Any],
    automation_id: str,
    alias: str,
    semantic_text: str,
) -> tuple[dict[str, Any], list[str]]:
    variables = automation.get(CONF_VARIABLES)
    metadata = (
        variables.get(_REMINDER_METADATA_VARIABLE)
        if isinstance(variables, dict)
        else None
    )
    errors: list[str] = []
    if not isinstance(metadata, dict):
        return {}, [f"{automation_id}: reminder metadata is required"]

    schedule = _normalize_reminder_schedule(
        metadata.get("schedule"),
        errors,
        path=f"{automation_id}.schedule",
    )
    message = metadata.get("message")
    if not isinstance(message, str) or not message.strip():
        errors.append(f"{automation_id}.message must be a non-empty string")
    if schedule is None or errors:
        return {}, errors

    return {
        CONF_ID: automation_id,
        CONF_ALIAS: alias,
        "managed_kind": _MANAGED_KIND_REMINDER,
        "schedule": schedule,
        "message": message.strip(),
        _SEMANTIC_TEXT_FIELD: semantic_text,
    }, []


def _normalize_reminder_schedule(
    value: Any,
    errors: list[str],
    *,
    path: str,
) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        errors.append(f"{path} must be an object")
        return None
    schedule_type = value.get("type")
    if schedule_type == "time":
        return _normalize_time_reminder_schedule(value, errors, path=path)
    if schedule_type == "delay":
        return _normalize_delay_reminder_schedule(value, errors, path=path)
    if schedule_type == "interval":
        return _normalize_interval_reminder_schedule(value, errors, path=path)
    errors.append(f"{path}.type must be time, delay, or interval")
    return None


def _normalize_time_reminder_schedule(
    value: dict[str, Any],
    errors: list[str],
    *,
    path: str,
) -> dict[str, Any] | None:
    extra_keys = set(value) - {"type", CONF_AT, "date", CONF_WEEKDAY}
    if extra_keys:
        errors.append(f"{path} contains unsupported keys: {sorted(extra_keys)}")
    at = value.get(CONF_AT)
    if not isinstance(at, str) or not _LOCAL_TIME_RE.fullmatch(at):
        errors.append(f"{path}.at must use HH:MM:SS")
        return None
    schedule: dict[str, Any] = {"type": "time", CONF_AT: at}

    reminder_date = value.get("date")
    if reminder_date is not None:
        if not isinstance(reminder_date, str) or not _LOCAL_DATE_RE.fullmatch(
            reminder_date
        ):
            errors.append(f"{path}.date must use YYYY-MM-DD")
        else:
            schedule["date"] = reminder_date

    weekday = value.get(CONF_WEEKDAY)
    if weekday is not None:
        if not isinstance(weekday, list) or not weekday:
            errors.append(f"{path}.weekday must be a non-empty list")
        elif not all(isinstance(item, str) and item for item in weekday):
            errors.append(f"{path}.weekday values must be non-empty strings")
        else:
            schedule[CONF_WEEKDAY] = list(weekday)

    if "date" in schedule and CONF_WEEKDAY in schedule:
        errors.append(f"{path}.date cannot be used with weekday")
    return schedule if not errors else None


def _normalize_delay_reminder_schedule(
    value: dict[str, Any],
    errors: list[str],
    *,
    path: str,
) -> dict[str, Any] | None:
    extra_keys = set(value) - {"type", "duration"}
    if extra_keys:
        errors.append(f"{path} contains unsupported keys: {sorted(extra_keys)}")
    duration = value.get("duration")
    if not isinstance(duration, dict):
        errors.append(f"{path}.duration must be an object")
        return None
    normalized_duration: dict[str, int] = {}
    has_positive = False
    for key in ("days", "hours", "minutes", "seconds"):
        raw_value = duration.get(key, 0)
        if not isinstance(raw_value, int) or raw_value < 0:
            errors.append(f"{path}.duration.{key} must be a non-negative integer")
            continue
        normalized_duration[key] = raw_value
        if raw_value > 0:
            has_positive = True
    extra_duration_keys = set(duration) - _DELAY_DURATION_FIELDS
    if extra_duration_keys:
        errors.append(
            f"{path}.duration contains unsupported keys: {sorted(extra_duration_keys)}"
        )
    if not has_positive:
        errors.append(f"{path}.duration must include at least one positive value")
    if errors:
        return None
    return {"type": "delay", "duration": normalized_duration}


def _normalize_interval_reminder_schedule(
    value: dict[str, Any],
    errors: list[str],
    *,
    path: str,
) -> dict[str, Any] | None:
    extra_keys = set(value) - {"type", "every_minutes"}
    if extra_keys:
        errors.append(f"{path} contains unsupported keys: {sorted(extra_keys)}")
    every_minutes = _parse_interval_minutes(
        value.get("every_minutes"),
        errors,
        path=f"{path}.every_minutes",
    )
    if every_minutes is None or extra_keys:
        return None
    return {"type": "interval", "every_minutes": every_minutes}


def _semantic_text_for_automation(automation: dict[str, Any]) -> str | None:
    variables = automation.get(CONF_VARIABLES)
    if not isinstance(variables, dict):
        return None
    value = variables.get(_SEMANTIC_TEXT_VARIABLE)
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()


def _automation_matches_schedule_filter(
    automation: dict[str, Any],
    schedule_filter: dict[str, Any] | None,
) -> bool:
    if schedule_filter is None:
        return True
    return any(
        _schedule_matches_filter(schedule, schedule_filter)
        for schedule in _schedules_from_automation(automation)
    )


def _schedules_from_automation(automation: dict[str, Any]) -> list[dict[str, Any]]:
    schedules: list[dict[str, Any]] = []
    for trigger in _trigger_nodes(automation):
        schedule = _schedule_from_trigger_node(trigger)
        if schedule is not None:
            schedules.append(schedule)
    return schedules


def _schedule_from_trigger_node(trigger: dict[str, Any]) -> dict[str, Any] | None:
    trigger_type = trigger.get("trigger") or trigger.get(CONF_PLATFORM)
    if trigger_type == "time":
        schedule: dict[str, Any] = {"type": "time", CONF_AT: trigger.get(CONF_AT)}
        if "date" in trigger:
            schedule["date"] = trigger.get("date")
        if CONF_WEEKDAY in trigger:
            schedule[CONF_WEEKDAY] = deepcopy(trigger.get(CONF_WEEKDAY))
        errors: list[str] = []
        normalized = _normalize_reminder_schedule(schedule, errors, path="schedule")
        return normalized if not errors else None
    if trigger_type == "delay":
        schedule = {
            "type": "delay",
            "duration": deepcopy(trigger.get("duration")),
        }
        errors = []
        normalized = _normalize_reminder_schedule(schedule, errors, path="schedule")
        return normalized if not errors else None
    if trigger_type == "interval":
        schedule = {
            "type": "interval",
            "every_minutes": trigger.get("every_minutes"),
        }
        errors = []
        normalized = _normalize_reminder_schedule(schedule, errors, path="schedule")
        return normalized if not errors else None
    return None


def _trigger_nodes(automation: dict[str, Any]) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    for key in (CONF_TRIGGERS, "trigger"):
        value = automation.get(key)
        if isinstance(value, list):
            nodes.extend(item for item in value if isinstance(item, dict))
        if isinstance(value, dict):
            nodes.append(value)
    return nodes


def _schedule_matches_filter(
    schedule: Any,
    schedule_filter: dict[str, Any] | None,
) -> bool:
    if schedule_filter is None:
        return True
    if not isinstance(schedule_filter, dict):
        return False
    filter_type = schedule_filter.get("type")
    if filter_type == "delay":
        return isinstance(schedule, dict) and schedule.get("type") == "delay"
    if filter_type == "interval":
        return _interval_schedule_matches_interval_filter(schedule, schedule_filter)
    if filter_type != "time":
        return False
    if not isinstance(schedule, dict):
        return False
    schedule_type = schedule.get("type")
    if schedule_type not in {"time", "interval"}:
        return False

    filter_date = _safe_local_date(schedule_filter.get("date"))
    filter_weekdays = _schedule_weekdays(schedule_filter)
    schedule_date = _safe_local_date(schedule.get("date"))
    schedule_weekdays = _schedule_weekdays(schedule)
    if filter_date is not None:
        weekday = _weekday_for_date(filter_date)
        if schedule_date is not None:
            if schedule_date != filter_date:
                return False
        elif schedule_weekdays and weekday not in schedule_weekdays:
            return False
    elif filter_weekdays:
        if schedule_date is not None:
            if _weekday_for_date(schedule_date) not in filter_weekdays:
                return False
        elif schedule_weekdays and not schedule_weekdays.intersection(filter_weekdays):
            return False

    time_range = schedule_filter.get("time_range")
    if isinstance(time_range, dict):
        if schedule_type == "interval":
            return _interval_schedule_matches_time_range(schedule, time_range)
        at = _safe_time_of_day(schedule.get(CONF_AT))
        range_from = _safe_time_of_day(time_range.get("from"))
        range_to = _safe_time_of_day(time_range.get("to"))
        if at is None or range_from is None or range_to is None:
            return False
        if at < range_from or at > range_to:
            return False
    return True


def _interval_schedule_matches_interval_filter(
    schedule: Any,
    schedule_filter: dict[str, Any],
) -> bool:
    if not isinstance(schedule, dict) or schedule.get("type") != "interval":
        return False
    every_minutes = schedule.get("every_minutes")
    if not _is_valid_interval_minutes(every_minutes):
        return False
    filter_every_minutes = schedule_filter.get("every_minutes")
    if filter_every_minutes is None:
        return True
    return every_minutes == filter_every_minutes


def _interval_schedule_matches_time_range(
    schedule: dict[str, Any],
    time_range: dict[str, Any],
) -> bool:
    every_minutes = schedule.get("every_minutes")
    range_from = _safe_time_of_day(time_range.get("from"))
    range_to = _safe_time_of_day(time_range.get("to"))
    if (
        not _is_valid_interval_minutes(every_minutes)
        or range_from is None
        or range_to is None
    ):
        return False
    from_seconds = _seconds_since_midnight(range_from)
    to_seconds = _seconds_since_midnight(range_to)
    interval_seconds = every_minutes * 60
    first_trigger = (
        (from_seconds + interval_seconds - 1) // interval_seconds
    ) * interval_seconds
    return first_trigger <= to_seconds


def _seconds_since_midnight(value: time) -> int:
    return value.hour * 3600 + value.minute * 60 + value.second


def _is_valid_interval_minutes(value: Any) -> bool:
    return (
        isinstance(value, int)
        and not isinstance(value, bool)
        and _INTERVAL_MINUTES_MIN <= value <= _INTERVAL_MINUTES_MAX
    )


def _safe_local_date(value: Any) -> date | None:
    if not isinstance(value, str) or _LOCAL_DATE_RE.fullmatch(value) is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _safe_time_of_day(value: Any) -> time | None:
    if not isinstance(value, str) or _LOCAL_TIME_RE.fullmatch(value) is None:
        return None
    try:
        return time.fromisoformat(value)
    except ValueError:
        return None


def _schedule_weekdays(schedule: dict[str, Any]) -> set[str]:
    weekdays = schedule.get(CONF_WEEKDAY)
    if not isinstance(weekdays, list):
        return set()
    return {item for item in weekdays if isinstance(item, str)}


def _weekday_for_date(value: date) -> str:
    return ["mon", "tue", "wed", "thu", "fri", "sat", "sun"][value.weekday()]


def _automation_alias_for_user(automation: dict[str, Any]) -> str:
    alias = automation.get(CONF_ALIAS)
    if isinstance(alias, str) and alias.strip():
        return _normalize_user_summary(alias)
    return "未命名自动化"


def _automation_summary_for_user(
    automation: dict[str, Any],
    alias: str,
) -> str:
    # 管理类 intent 的用户摘要只使用安全展示字段，避免泄露实体 id、服务名或内部配置。
    for key in ("summary", "description"):
        value = automation.get(key)
        if isinstance(value, str) and value.strip():
            return _normalize_user_summary(value)
    return alias


def _normalize_user_summary(value: str) -> str:
    summary = re.sub(r"\s+", " ", value.strip())
    summary = summary.rstrip("。.!！?？；;，,")
    return summary or "未命名自动化"
