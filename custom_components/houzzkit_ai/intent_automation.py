"""Home Assistant automation intents for Houzzkit AI MCP."""

from __future__ import annotations

import asyncio
from copy import deepcopy
import logging
import os
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
from homeassistant.components.valve.const import DOMAIN as VALVE_DOMAIN
from homeassistant.config import AUTOMATION_CONFIG_PATH
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_ACTIONS,
    CONF_ALIAS,
    CONF_CONDITIONS,
    CONF_ID,
    CONF_MODE,
    CONF_TRIGGERS,
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
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.util.file import write_utf8_file_atomic
from homeassistant.util.json import JsonObjectType
from homeassistant.util.yaml import dump, load_yaml

from .intent_helper import EntityInfo, match_intent_entities
from .intent_live_context import _get_exposed_entities

_LOGGER = logging.getLogger(__name__)
_AUTOMATION_WRITE_LOCK = asyncio.Lock()
_AUTOMATION_ID_PREFIX = "houzzkit_ai_"
_ACTION_SERVICE_DOMAINS = {"notify", "persistent_notification", "scene", "script"}
_RESOLVED_TARGETS_FIELD = "resolved_targets"


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
        return {vol.Required("automation"): dict}

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
        return {vol.Required("automation"): dict}

    async def async_handle(  # type: ignore[override]
        self,
        intent_obj: intent.Intent,
    ) -> JsonObjectType:
        """Create automation config."""
        slots = self.async_validate_slots(intent_obj.slots)
        automation = slots["automation"]["value"]
        return await _create_automation(intent_obj, automation)


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
    actions: list[dict[str, str]] = []
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

    config_id = automation_id or _AUTOMATION_ID_PREFIX + "validation"
    config, errors = await _automation_config_from_internal_plan(
        intent_obj,
        config_id,
        automation,
    )
    if errors:
        return {"success": True, "valid": False, "errors": errors}

    try:
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
) -> dict[str, Any]:
    if not isinstance(automation, dict):
        return {"success": False, "errors": ["automation must be an object"]}

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

        automation_id = _generate_automation_id(existing_ids)
        config, errors = await _automation_config_from_internal_plan(
            intent_obj,
            automation_id,
            automation,
        )
        if errors:
            return {
                "success": False,
                "errors": errors,
            }
        try:
            await async_validate_config_item(hass, automation_id, config)
        except (vol.Invalid, HomeAssistantError) as exc:
            return {"success": False, "errors": [str(exc)]}

        configs.append(config)
        await _write_automation_configs(hass, configs)

    await hass.services.async_call(
        AUTOMATION_DOMAIN,
        SERVICE_RELOAD,
        {CONF_ID: automation_id},
        blocking=True,
    )

    alias = str(config.get(CONF_ALIAS, ""))
    result: dict[str, Any] = {
        "success": True,
        "id": automation_id,
        "alias": alias,
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
) -> tuple[dict[str, Any], list[str]]:
    errors = _raw_target_errors(automation)
    if errors:
        return {}, errors

    converted = deepcopy(automation)
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

    actions = converted.get(CONF_ACTIONS)
    if isinstance(actions, list):
        converted_actions: list[dict[str, Any]] = []
        for index, action in enumerate(actions):
            converted_actions.extend(
                await _convert_action_with_resolved_targets(
                    intent_obj,
                    action,
                    errors,
                    path=f"automation.actions[{index}]",
                )
            )
        converted[CONF_ACTIONS] = converted_actions

    if errors:
        return {}, errors
    return _automation_config_for_write(automation_id, converted), []


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
    expected_count = _resolved_target_device_count(resolved_targets)
    entity_ids = {item.state.entity_id for item in entity_infos}
    if expected_count and len(entity_ids) != expected_count:
        errors.append(
            f"{path}.{_RESOLVED_TARGETS_FIELD}: target resolution is ambiguous"
        )
        return []
    return entity_infos


def _resolved_target_device_count(resolved_targets: list[Any]) -> int:
    count = 0
    for target in resolved_targets:
        if not isinstance(target, dict):
            continue
        devices = target.get("devices")
        if isinstance(devices, list):
            count += len([device for device in devices if isinstance(device, dict)])
    return count


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
        if key != CONF_ID and key not in config:
            config[key] = value
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
