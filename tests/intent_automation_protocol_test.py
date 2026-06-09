"""Targeted tests for the Houzzkit automation planning protocol."""

from __future__ import annotations

import asyncio
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import types
from typing import Any
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "custom_components/houzzkit_ai/intent_automation.py"
LOCAL_TZ = ZoneInfo("Asia/Shanghai")


def _load_intent_automation() -> types.ModuleType:
    """Load intent_automation without importing integration __init__ side effects."""
    custom_components = types.ModuleType("custom_components")
    custom_components.__path__ = [str(ROOT / "custom_components")]
    sys.modules.setdefault("custom_components", custom_components)

    package = types.ModuleType("custom_components.houzzkit_ai")
    package.__path__ = [str(ROOT / "custom_components/houzzkit_ai")]
    sys.modules["custom_components.houzzkit_ai"] = package

    const = types.ModuleType("custom_components.houzzkit_ai.const")
    const.DOMAIN = "houzzkit_ai"
    sys.modules[const.__name__] = const

    houzzkit = types.ModuleType("custom_components.houzzkit_ai.houzzkit")
    houzzkit.get_entities = lambda *args, **kwargs: []
    sys.modules[houzzkit.__name__] = houzzkit

    intent_helper = types.ModuleType("custom_components.houzzkit_ai.intent_helper")
    intent_helper.EntityInfo = object

    async def match_intent_entities(*args: object, **kwargs: object) -> tuple[None, list]:
        return None, []

    intent_helper.match_intent_entities = match_intent_entities
    sys.modules[intent_helper.__name__] = intent_helper

    live_context = types.ModuleType("custom_components.houzzkit_ai.intent_live_context")
    live_context._get_exposed_entities = lambda *args, **kwargs: {}
    sys.modules[live_context.__name__] = live_context

    spec = importlib.util.spec_from_file_location(
        "custom_components.houzzkit_ai.intent_automation",
        MODULE_PATH,
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("Unable to load intent_automation.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ia = _load_intent_automation()
ac = sys.modules["custom_components.houzzkit_ai.automation_capabilities"]


def _convert_triggers(
    automation: dict,
    *,
    now: datetime | None = None,
) -> list[str]:
    errors: list[str] = []
    frozen_now = now or datetime(2026, 6, 3, 9, 0, 0, tzinfo=LOCAL_TZ)
    with (
        patch.object(ia.dt_util, "now", return_value=frozen_now),
        patch.object(ia.dt_util, "get_default_time_zone", return_value=LOCAL_TZ),
    ):
        ia._convert_plan_time_triggers(automation, errors)
    return errors


class _FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, bool | None]] = []

    def async_services(self) -> dict[str, dict[str, dict]]:
        return {"script": {"turn_on": {}}}

    async def async_call(
        self,
        domain: str,
        service: str,
        data: dict,
        *,
        blocking: bool | None = None,
    ) -> None:
        self.calls.append((domain, service, data, blocking))


class _FakeHass:
    services = _FakeServices()


class _FakeIntent:
    assistant = object()
    hass = _FakeHass()


class _FakeServiceRegistry:
    def __init__(self) -> None:
        self.registered: list[tuple[str, str, object, object]] = []

    def has_service(self, domain: str, service: str) -> bool:
        return False

    def async_register(
        self,
        domain: str,
        service: str,
        service_func: object,
        schema: object,
    ) -> None:
        self.registered.append((domain, service, service_func, schema))


class _FakeSetupHass:
    def __init__(self) -> None:
        self.services = _FakeServiceRegistry()


class _FakeTaskHass:
    def __init__(self) -> None:
        self.created_task: object | None = None

    def async_create_task(self, coro: object) -> object:
        self.created_task = coro
        return object()


def _matched_entity(
    entity_id: str,
    domain: str,
    *,
    name: str,
    area_name: str,
) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        name=name,
        area_name=area_name,
        state=types.SimpleNamespace(entity_id=entity_id, domain=domain),
    )


class AutomationProtocolTest(unittest.TestCase):
    def test_list_context_returns_plan_features_and_current_date(self) -> None:
        async def fake_summaries(hass: object) -> list[dict[str, str]]:
            return []

        now = datetime(2026, 6, 3, 18, 20, 0, tzinfo=LOCAL_TZ)
        with (
            patch.object(ia, "_get_exposed_entities", return_value={}),
            patch.object(ia, "_read_automation_summaries", fake_summaries),
            patch.object(ia.dt_util, "now", return_value=now),
        ):
            result = asyncio.run(
                ia.HouzzkitListAutomationContextIntent().async_handle(_FakeIntent())
            )

        self.assertTrue(result["success"])
        self.assertEqual(
            result["supported_plan_features"],
            ["time_trigger_date", "time_trigger_delay"],
        )
        self.assertEqual(result["current_date"], "2026-06-03")

    def test_automation_initialize_metadata_uses_valid_local_timezone(self) -> None:
        metadata = ac.automation_initialize_metadata("Asia/Shanghai")

        self.assertEqual(
            metadata,
            {
                "local_timezone": "Asia/Shanghai",
                "supported_plan_features": [
                    "time_trigger_date",
                    "time_trigger_delay",
                ],
            },
        )
        self.assertIsNone(ac.automation_initialize_metadata("UTC+8"))
        self.assertIsNone(ac.automation_initialize_metadata(""))

    def test_managed_kind_schema_uses_json_schema_array_enum(self) -> None:
        # MCP 会把 voluptuous schema 转为 JSON Schema；enum 必须来自 list，
        # 不能用 set/tuple，否则远端工具 schema 校验会失败。
        self.assertIsInstance(ia._MANAGED_KINDS, list)
        self.assertNotIn("query", ia._LIST_MANAGED_AUTOMATIONS_SLOT_SCHEMA)
        self.assertNotIn("limit", ia._LIST_MANAGED_AUTOMATIONS_SLOT_SCHEMA)
        self.assertNotIn("cursor", ia._LIST_MANAGED_AUTOMATIONS_SLOT_SCHEMA)

    def test_list_managed_automations_returns_only_semantic_houzzkit_items(self) -> None:
        async def fake_configs(hass: object) -> list[dict[str, Any]]:
            return [
                {
                    "id": "houzzkit_ai_morning",
                    "alias": "早上提醒",
                    "description": "每天早上 8 点提醒你",
                    "variables": {
                        "houzzkit_ai_managed_kind": "automation",
                        "houzzkit_ai_semantic_text": "主题: 早上提醒\n动作: 提醒用户\n对象: 用户\n意图: 早上提醒用户",
                    },
                },
                {
                    "id": "manual_automation",
                    "alias": "手工自动化",
                    "description": "不应返回",
                },
                {
                    "id": "houzzkit_ai_water",
                    "alias": "厨房漏水提醒",
                    "summary": "检测到漏水时发出警告播报",
                    "variables": {"houzzkit_ai_managed_kind": "automation"},
                },
            ]

        with patch.object(ia, "_read_automation_configs", fake_configs):
            result = asyncio.run(
                ia._list_managed_automations(
                    _FakeHass(),
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(
            result["automations"],
            [
                {
                    "id": "houzzkit_ai_morning",
                    "alias": "早上提醒",
                    "managed_kind": "automation",
                    "semantic_text": "主题: 早上提醒\n动作: 提醒用户\n对象: 用户\n意图: 早上提醒用户",
                }
            ],
        )

    def test_list_managed_automations_filters_by_managed_kind(self) -> None:
        async def fake_configs(hass: object) -> list[dict[str, Any]]:
            return [
                {
                    "id": "houzzkit_ai_legacy",
                    "alias": "旧自动化",
                    "summary": "缺少内部分类的历史自动化",
                },
                {
                    "id": "houzzkit_ai_reminder",
                    "alias": "喝水提醒",
                    "summary": "每天 9 点提醒喝水",
                    "variables": {
                        "houzzkit_ai_managed_kind": "reminder",
                        "houzzkit_ai_semantic_text": "主题: 喝水提醒\n动作: 提醒用户喝水\n对象: 水\n意图: 到时间提醒用户喝水",
                        "houzzkit_ai_reminder": {
                            "schedule": {
                                "type": "time",
                                "at": "09:00:00",
                            },
                            "message": "喝水",
                        },
                    },
                },
                {
                    "id": "houzzkit_ai_light",
                    "alias": "开灯自动化",
                    "summary": "晚上 7 点开灯",
                    "triggers": [{"platform": "time", "at": "19:00:00"}],
                    "actions": [{"action": "light.turn_on"}],
                    "variables": {
                        "houzzkit_ai_managed_kind": "automation",
                        "houzzkit_ai_semantic_text": "主题: 开灯自动化\n触发: 晚上\n动作: 打开灯\n对象: 灯\n意图: 到条件时开灯",
                    },
                },
            ]

        with patch.object(ia, "_read_automation_configs", fake_configs):
            reminder_result = asyncio.run(
                ia._list_managed_automations(
                    _FakeHass(),
                    kind="reminder",
                )
            )
            automation_result = asyncio.run(
                ia._list_managed_automations(
                    _FakeHass(),
                    kind="automation",
                )
            )

        self.assertEqual(reminder_result["total_count"], 1)
        self.assertEqual(
            reminder_result["automations"],
            [
                {
                    "id": "houzzkit_ai_reminder",
                    "alias": "喝水提醒",
                    "managed_kind": "reminder",
                    "schedule": {
                        "type": "time",
                        "at": "09:00:00",
                    },
                    "message": "喝水",
                    "semantic_text": "主题: 喝水提醒\n动作: 提醒用户喝水\n对象: 水\n意图: 到时间提醒用户喝水",
                }
            ],
        )
        self.assertEqual(automation_result["total_count"], 1)
        self.assertEqual(
            automation_result["automations"],
            [
                {
                    "id": "houzzkit_ai_light",
                    "alias": "开灯自动化",
                    "managed_kind": "automation",
                    "semantic_text": "主题: 开灯自动化\n触发: 晚上\n动作: 打开灯\n对象: 灯\n意图: 到条件时开灯",
                }
            ],
        )

    def test_list_reminder_requires_metadata(self) -> None:
        async def fake_configs(hass: object) -> list[dict[str, Any]]:
            return [
                {
                    "id": "houzzkit_ai_reminder",
                    "alias": "喝水提醒",
                    "variables": {
                        "houzzkit_ai_managed_kind": "reminder",
                        "houzzkit_ai_semantic_text": "主题: 喝水提醒\n动作: 提醒用户喝水\n对象: 水\n意图: 到时间提醒用户喝水",
                    },
                }
            ]

        with patch.object(ia, "_read_automation_configs", fake_configs):
            result = asyncio.run(
                ia._list_managed_automations(
                    _FakeHass(),
                    kind="reminder",
                )
            )

        self.assertFalse(result["success"])
        self.assertIn("metadata", result["error"])

    def test_list_reminder_returns_structured_schedules(self) -> None:
        async def fake_configs(hass: object) -> list[dict[str, Any]]:
            return [
                {
                    "id": "houzzkit_ai_date",
                    "alias": "上班提醒",
                    "variables": {
                        "houzzkit_ai_managed_kind": "reminder",
                        "houzzkit_ai_semantic_text": "主题: 上班提醒\n动作: 提醒用户上班\n对象: 上班\n意图: 到时间提醒用户上班",
                        "houzzkit_ai_reminder": {
                            "schedule": {
                                "type": "time",
                                "date": "2026-06-09",
                                "at": "08:00:00",
                            },
                            "message": "该上班了",
                        },
                    },
                },
                {
                    "id": "houzzkit_ai_weekday",
                    "alias": "打球提醒",
                    "variables": {
                        "houzzkit_ai_managed_kind": "reminder",
                        "houzzkit_ai_semantic_text": "主题: 打球提醒\n动作: 提醒用户打球\n对象: 打球\n意图: 到时间提醒用户打球",
                        "houzzkit_ai_reminder": {
                            "schedule": {
                                "type": "time",
                                "at": "17:00:00",
                                "weekday": ["mon", "wed"],
                            },
                            "message": "该去打球了",
                        },
                    },
                },
                {
                    "id": "houzzkit_ai_delay",
                    "alias": "出门提醒",
                    "variables": {
                        "houzzkit_ai_managed_kind": "reminder",
                        "houzzkit_ai_semantic_text": "主题: 出门提醒\n动作: 提醒用户出门\n对象: 出门\n意图: 延迟后提醒用户出门",
                        "houzzkit_ai_reminder": {
                            "schedule": {
                                "type": "delay",
                                "duration": {"minutes": 3},
                            },
                            "message": "该出门了",
                        },
                    },
                },
            ]

        with patch.object(ia, "_read_automation_configs", fake_configs):
            result = asyncio.run(
                ia._list_managed_automations(
                    _FakeHass(),
                    kind="reminder",
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(
            result["automations"],
            [
                {
                    "id": "houzzkit_ai_date",
                    "alias": "上班提醒",
                    "managed_kind": "reminder",
                    "schedule": {
                        "type": "time",
                        "at": "08:00:00",
                        "date": "2026-06-09",
                    },
                    "message": "该上班了",
                    "semantic_text": "主题: 上班提醒\n动作: 提醒用户上班\n对象: 上班\n意图: 到时间提醒用户上班",
                },
                {
                    "id": "houzzkit_ai_weekday",
                    "alias": "打球提醒",
                    "managed_kind": "reminder",
                    "schedule": {
                        "type": "time",
                        "at": "17:00:00",
                        "weekday": ["mon", "wed"],
                    },
                    "message": "该去打球了",
                    "semantic_text": "主题: 打球提醒\n动作: 提醒用户打球\n对象: 打球\n意图: 到时间提醒用户打球",
                },
                {
                    "id": "houzzkit_ai_delay",
                    "alias": "出门提醒",
                    "managed_kind": "reminder",
                    "schedule": {
                        "type": "delay",
                        "duration": {
                            "days": 0,
                            "hours": 0,
                            "minutes": 3,
                            "seconds": 0,
                        },
                    },
                    "message": "该出门了",
                    "semantic_text": "主题: 出门提醒\n动作: 提醒用户出门\n对象: 出门\n意图: 延迟后提醒用户出门",
                },
            ],
        )

    def test_list_managed_automations_returns_all_without_explicit_limit(self) -> None:
        async def fake_configs(hass: object) -> list[dict[str, Any]]:
            return [
                {
                    "id": f"houzzkit_ai_{index:02d}",
                    "alias": f"自动化 {index}",
                    "description": f"第 {index} 条自动化",
                    "variables": {
                        "houzzkit_ai_managed_kind": "automation",
                        "houzzkit_ai_semantic_text": f"主题: 自动化 {index}\n动作: 执行动作 {index}\n对象: 对象 {index}\n意图: 管理自动化 {index}",
                    },
                }
                for index in range(1, 26)
            ]

        with patch.object(ia, "_read_automation_configs", fake_configs):
            result = asyncio.run(
                ia._list_managed_automations(
                    _FakeHass(),
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["total_count"], 25)
        self.assertEqual(len(result["automations"]), 25)
        self.assertNotIn("next_cursor", result)

    def test_list_managed_automations_filters_by_schedule(self) -> None:
        async def fake_configs(hass: object) -> list[dict[str, Any]]:
            return [
                {
                    "id": "houzzkit_ai_monday",
                    "alias": "周一提醒",
                    "variables": {
                        "houzzkit_ai_managed_kind": "reminder",
                        "houzzkit_ai_semantic_text": "主题: 周一提醒\n动作: 提醒用户\n对象: 用户\n意图: 定期提醒用户",
                        "houzzkit_ai_reminder": {
                            "schedule": {
                                "type": "time",
                                "at": "09:00:00",
                                "weekday": ["mon"],
                            },
                            "message": "周一提醒",
                        },
                    },
                },
                {
                    "id": "houzzkit_ai_delay",
                    "alias": "延迟提醒",
                    "variables": {
                        "houzzkit_ai_managed_kind": "reminder",
                        "houzzkit_ai_semantic_text": "主题: 延迟提醒\n动作: 提醒用户\n对象: 用户\n意图: 延迟后提醒用户",
                        "houzzkit_ai_reminder": {
                            "schedule": {
                                "type": "delay",
                                "duration": {"minutes": 3},
                            },
                            "message": "延迟提醒",
                        },
                    },
                },
                {
                    "id": "houzzkit_ai_motion_then_time",
                    "alias": "多人触发自动化",
                    "triggers": [
                        {
                            "trigger": "state",
                            "entity_id": "binary_sensor.motion",
                            "to": "on",
                        },
                        {
                            "trigger": "time",
                            "at": "09:00:00",
                            "weekday": ["mon"],
                        },
                    ],
                    "variables": {
                        "houzzkit_ai_managed_kind": "automation",
                        "houzzkit_ai_semantic_text": "主题: 多人触发自动化\n触发: 传感器或定时触发\n动作: 执行动作\n对象: 自动化\n意图: 任一触发条件满足时执行",
                    },
                },
            ]

        with patch.object(ia, "_read_automation_configs", fake_configs):
            weekday_result = asyncio.run(
                ia._list_managed_automations(
                    _FakeHass(),
                    kind="reminder",
                    schedule_filter={"type": "time", "weekday": ["mon"]},
                )
            )
            delay_result = asyncio.run(
                ia._list_managed_automations(
                    _FakeHass(),
                    kind="reminder",
                    schedule_filter={"type": "delay"},
                )
            )
            automation_weekday_result = asyncio.run(
                ia._list_managed_automations(
                    _FakeHass(),
                    kind="automation",
                    schedule_filter={"type": "time", "weekday": ["mon"]},
                )
            )

        self.assertTrue(weekday_result["success"])
        self.assertEqual(
            [item["id"] for item in weekday_result["automations"]],
            ["houzzkit_ai_monday"],
        )
        self.assertTrue(delay_result["success"])
        self.assertEqual(
            [item["id"] for item in delay_result["automations"]],
            ["houzzkit_ai_delay"],
        )
        self.assertTrue(automation_weekday_result["success"])
        self.assertEqual(
            [item["id"] for item in automation_weekday_result["automations"]],
            ["houzzkit_ai_motion_then_time"],
        )

    def test_delete_managed_automation_rejects_non_houzzkit_id(self) -> None:
        async def fake_write(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("write must not be called")

        with patch.object(ia, "_write_automation_configs", fake_write):
            result = asyncio.run(
                ia._delete_managed_automation(_FakeHass(), "manual_automation")
            )

        self.assertFalse(result["success"])
        self.assertIn("Houzzkit", result["error"])

    def test_delete_managed_automation_writes_remaining_and_reloads(self) -> None:
        written: list[list[dict[str, str]]] = []

        async def fake_configs(hass: object) -> list[dict[str, str]]:
            return [
                {
                    "id": "houzzkit_ai_delete",
                    "alias": "早上提醒",
                    "description": "每天早上 8 点提醒你",
                },
                {
                    "id": "houzzkit_ai_keep",
                    "alias": "保留提醒",
                },
            ]

        async def fake_write(hass: object, configs: list[dict[str, str]]) -> None:
            written.append(configs)

        hass = types.SimpleNamespace(services=_FakeServices())
        with (
            patch.object(ia, "_read_automation_configs", fake_configs),
            patch.object(ia, "_write_automation_configs", fake_write),
        ):
            result = asyncio.run(
                ia._delete_managed_automation(hass, "houzzkit_ai_delete")
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["deleted_automation"]["alias"], "早上提醒")
        self.assertEqual(written, [[{"id": "houzzkit_ai_keep", "alias": "保留提醒"}]])
        self.assertEqual(
            hass.services.calls,
            [("automation", "reload", {}, True)],
        )

    def test_get_managed_automation_returns_editable_snapshot(self) -> None:
        snapshot = {
            "alias": "晚上开灯",
            "semantic_text": "主题: 开灯\n触发: 晚上\n动作: 开灯\n对象: 灯",
            "triggers": [{"trigger": "time", "at": "19:00:00"}],
            "actions": [{"action": "script.turn_on"}],
            "mode": "single",
        }

        async def fake_configs(hass: object) -> list[dict[str, Any]]:
            return [
                {
                    "id": "houzzkit_ai_light",
                    "alias": "晚上开灯",
                    "variables": {
                        "houzzkit_ai_managed_kind": "automation",
                        "houzzkit_ai_semantic_text": snapshot["semantic_text"],
                        "houzzkit_ai_editable_snapshot": snapshot,
                    },
                }
            ]

        with patch.object(ia, "_read_automation_configs", fake_configs):
            result = asyncio.run(
                ia._get_managed_automation(_FakeHass(), "houzzkit_ai_light")
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["automation"], snapshot)
        self.assertNotIn("id", result["automation"])

    def test_get_managed_automation_without_snapshot_returns_not_editable(self) -> None:
        async def fake_configs(hass: object) -> list[dict[str, Any]]:
            return [
                {
                    "id": "houzzkit_ai_old",
                    "alias": "旧自动化",
                    "variables": {
                        "houzzkit_ai_managed_kind": "automation",
                        "houzzkit_ai_semantic_text": "主题: 旧自动化\n触发: 晚上\n动作: 开灯\n对象: 灯",
                    },
                }
            ]

        with patch.object(ia, "_read_automation_configs", fake_configs):
            result = asyncio.run(
                ia._get_managed_automation(_FakeHass(), "houzzkit_ai_old")
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["failure_type"], "not_editable")

    def test_replace_managed_automation_replaces_same_id_and_saves_snapshot(self) -> None:
        written: list[list[dict[str, Any]]] = []
        old_snapshot = {
            "alias": "晚上开灯",
            "semantic_text": "主题: 开灯\n触发: 晚上7点\n动作: 开灯\n对象: 灯",
            "triggers": [{"trigger": "time", "at": "19:00:00"}],
            "actions": [{"action": "script.turn_on"}],
            "mode": "single",
        }
        new_snapshot = {
            "alias": "晚上8点开灯",
            "semantic_text": "主题: 开灯\n触发: 晚上8点\n动作: 开灯\n对象: 灯",
            "triggers": [{"trigger": "time", "at": "20:00:00"}],
            "actions": [{"action": "script.turn_on"}],
            "mode": "single",
        }

        async def fake_configs(hass: object) -> list[dict[str, Any]]:
            return [
                {
                    "id": "houzzkit_ai_light",
                    "alias": "晚上开灯",
                    "triggers": [{"platform": "time", "at": "19:00:00"}],
                    "actions": [{"action": "script.turn_on"}],
                    "variables": {
                        "houzzkit_ai_managed_kind": "automation",
                        "houzzkit_ai_semantic_text": old_snapshot["semantic_text"],
                        "houzzkit_ai_editable_snapshot": old_snapshot,
                    },
                },
                {"id": "manual_keep", "alias": "保留"},
            ]

        async def fake_write(hass: object, configs: list[dict[str, Any]]) -> None:
            written.append(configs)

        async def fake_validate(hass: object, config_id: str, config: dict[str, Any]) -> None:
            return None

        hass = types.SimpleNamespace(services=_FakeServices())
        intent_obj = types.SimpleNamespace(hass=hass)
        with (
            patch.object(ia, "_read_automation_configs", fake_configs),
            patch.object(ia, "_write_automation_configs", fake_write),
            patch.object(ia, "async_validate_config_item", fake_validate),
        ):
            result = asyncio.run(
                ia._replace_managed_automation(
                    intent_obj,
                    "houzzkit_ai_light",
                    new_snapshot,
                    editable_snapshot=new_snapshot,
                )
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["id"], "houzzkit_ai_light")
        self.assertEqual(len(written), 1)
        replaced = written[0][0]
        self.assertEqual(replaced["id"], "houzzkit_ai_light")
        self.assertEqual(replaced["alias"], "晚上8点开灯")
        self.assertEqual(replaced["triggers"], [{"trigger": "time", "at": "20:00:00"}])
        self.assertEqual(
            replaced["variables"]["houzzkit_ai_editable_snapshot"],
            new_snapshot,
        )
        self.assertEqual(written[0][1], {"id": "manual_keep", "alias": "保留"})
        self.assertEqual(hass.services.calls, [("automation", "reload", {}, True)])

    def test_replace_managed_automation_validation_failure_does_not_write(self) -> None:
        written: list[list[dict[str, Any]]] = []
        old_snapshot = {
            "alias": "晚上开灯",
            "semantic_text": "主题: 开灯\n触发: 晚上\n动作: 开灯\n对象: 灯",
            "triggers": [{"trigger": "time", "at": "19:00:00"}],
            "actions": [{"action": "script.turn_on"}],
        }
        invalid_snapshot = {
            "alias": "坏计划",
            "semantic_text": "主题: 坏计划\n触发: 晚上\n动作: 开灯\n对象: 灯",
            "triggers": [{"trigger": "time", "at": "20:00:00"}],
            "actions": [{"action": "script.turn_on"}],
        }

        async def fake_configs(hass: object) -> list[dict[str, Any]]:
            return [
                {
                    "id": "houzzkit_ai_light",
                    "alias": "晚上开灯",
                    "variables": {
                        "houzzkit_ai_managed_kind": "automation",
                        "houzzkit_ai_semantic_text": old_snapshot["semantic_text"],
                        "houzzkit_ai_editable_snapshot": old_snapshot,
                    },
                }
            ]

        async def fake_write(hass: object, configs: list[dict[str, Any]]) -> None:
            written.append(configs)

        async def fake_validate(
            hass: object,
            config_id: str,
            config: dict[str, Any],
        ) -> None:
            raise ia.HomeAssistantError("invalid automation")

        hass = types.SimpleNamespace(services=_FakeServices())
        intent_obj = types.SimpleNamespace(hass=hass)
        with (
            patch.object(ia, "_read_automation_configs", fake_configs),
            patch.object(ia, "_write_automation_configs", fake_write),
            patch.object(ia, "async_validate_config_item", fake_validate),
        ):
            result = asyncio.run(
                ia._replace_managed_automation(
                    intent_obj,
                    "houzzkit_ai_light",
                    invalid_snapshot,
                    editable_snapshot=invalid_snapshot,
                )
            )

        self.assertFalse(result["success"])
        self.assertEqual(result["failure_type"], "plan_invalid")
        self.assertEqual(written, [])
        self.assertEqual(hass.services.calls, [])

    def test_time_date_trigger_becomes_time_trigger_with_date_guard(self) -> None:
        automation = {
            "triggers": [
                {"trigger": "time", "date": "2026-06-04", "at": "08:00:00"}
            ]
        }

        errors = _convert_triggers(automation)

        self.assertEqual(errors, [])
        trigger = automation["triggers"][0]
        self.assertEqual(trigger["platform"], "time")
        self.assertEqual(trigger["at"], "08:00:00")
        self.assertNotIn("trigger", trigger)
        self.assertNotIn("date", trigger)
        self.assertRegex(trigger["id"], r"^houzzkit_ai_once_\d+$")
        self.assertEqual(automation["conditions"]["condition"], "template")
        self.assertIn("2026-06-04", automation["conditions"]["value_template"])

    def test_delay_trigger_uses_local_now_to_build_one_shot_time(self) -> None:
        automation = {"triggers": [{"trigger": "delay", "duration": {"minutes": 3}}]}

        errors = _convert_triggers(automation)

        self.assertEqual(errors, [])
        trigger = automation["triggers"][0]
        self.assertEqual(trigger["platform"], "time")
        self.assertEqual(trigger["at"], "09:03:00")
        self.assertNotIn("duration", trigger)
        self.assertIn("2026-06-03", automation["conditions"]["value_template"])

    def test_delay_duration_rejects_invalid_shapes(self) -> None:
        cases = [
            ({"duration": "1d3h2m3s"}, "must be an object"),
            ({"duration": {}}, "at least one positive"),
            ({"duration": {"minutes": 0}}, "at least one positive"),
            ({"duration": {"minutes": -1}}, "non-negative integer"),
            ({"duration": {"minutes": 1.5}}, "non-negative integer"),
            ({"duration": {"weeks": 1}}, "unsupported keys"),
            ({"duration": {"minutes": 3}, "timezone": "UTC"}, "unsupported keys"),
            ({"duration": {"minutes": 3}, "at": "00:03:00"}, "cannot include"),
        ]
        for payload, expected_error in cases:
            with self.subTest(payload=payload):
                automation = {"triggers": [{"trigger": "delay", **payload}]}

                errors = _convert_triggers(automation)

                self.assertTrue(errors)
                self.assertIn(expected_error, " ".join(errors))

    def test_delay_condition_is_rejected(self) -> None:
        cases = [
            ("conditions", {"condition": "delay"}),
            ("condition", {"condition": "delay"}),
            ("condition", {"trigger": "delay"}),
        ]
        for key, condition in cases:
            with self.subTest(key=key, condition=condition):
                errors: list[str] = []
                ia._reject_delay_conditions(
                    condition,
                    errors,
                    path=f"automation.{key}",
                )

                self.assertIn("only supported as a trigger", " ".join(errors))

    def test_date_trigger_validation_rejects_bad_protocol_fields(self) -> None:
        cases = [
            (
                {"trigger": "state", "date": "2026-06-04", "at": "08:00:00"},
                "only supported on time triggers",
            ),
            (
                {
                    "trigger": "time",
                    "date": "2026-06-04",
                    "at": "08:00:00",
                    "weekday": ["mon"],
                },
                "cannot be used with weekday",
            ),
            (
                {
                    "trigger": "time",
                    "date": "2026-06-04",
                    "at": "2026-06-04T08:00:00",
                },
                "must use HH:MM:SS",
            ),
            (
                {
                    "trigger": "time",
                    "date": "2026-06-02",
                    "at": "08:00:00",
                },
                "in the past",
            ),
        ]
        for trigger, expected_error in cases:
            with self.subTest(trigger=trigger):
                automation = {"triggers": [trigger]}

                errors = _convert_triggers(automation)

                self.assertTrue(errors)
                self.assertIn(expected_error, " ".join(errors))

    def test_multi_trigger_guard_does_not_block_regular_triggers(self) -> None:
        automation = {
            "triggers": [
                {"trigger": "delay", "duration": {"minutes": 3}},
                {"trigger": "state", "entity_id": "binary_sensor.door", "to": "on"},
            ]
        }

        errors = _convert_triggers(automation)

        self.assertEqual(errors, [])
        guard = automation["conditions"]
        self.assertEqual(guard["condition"], "or")
        guard_conditions = guard["conditions"]
        self.assertEqual(guard_conditions[0]["condition"], "not")
        self.assertEqual(
            guard_conditions[0]["conditions"][0]["condition"],
            "trigger",
        )
        self.assertEqual(guard_conditions[1]["condition"], "and")
        self.assertIn(
            "2026-06-03",
            guard_conditions[1]["conditions"][1]["value_template"],
        )

    def test_split_mixed_one_shot_and_regular_triggers(self) -> None:
        automation = {
            "alias": "Mixed automation",
            "triggers": [
                {"trigger": "delay", "duration": {"minutes": 3}},
                {"trigger": "state", "entity_id": "binary_sensor.door", "to": "on"},
            ],
            "actions": [{"action": "script.turn_on"}],
        }

        specs, errors = ia._split_automation_specs(automation)

        self.assertEqual(errors, [])
        self.assertEqual([spec["type"] for spec in specs], ["one_shot", "regular"])
        self.assertEqual(len(specs[0]["automation"]["triggers"]), 1)
        self.assertEqual(specs[0]["automation"]["triggers"][0]["trigger"], "delay")
        self.assertEqual(len(specs[1]["automation"]["triggers"]), 1)
        self.assertEqual(specs[1]["automation"]["triggers"][0]["trigger"], "state")
        self.assertEqual(specs[0]["automation"]["actions"], automation["actions"])
        self.assertEqual(specs[1]["automation"]["actions"], automation["actions"])

    def test_split_multiple_one_shot_triggers(self) -> None:
        automation = {
            "triggers": [
                {"trigger": "delay", "duration": {"minutes": 3}},
                {"trigger": "time", "date": "2026-06-04", "at": "08:00:00"},
            ],
            "actions": [{"action": "script.turn_on"}],
        }

        specs, errors = ia._split_automation_specs(automation)

        self.assertEqual(errors, [])
        self.assertEqual([spec["type"] for spec in specs], ["one_shot", "one_shot"])
        self.assertEqual(len(specs[0]["automation"]["triggers"]), 1)
        self.assertEqual(len(specs[1]["automation"]["triggers"]), 1)

    def test_split_rejects_trigger_conditions_when_multiple_specs_needed(self) -> None:
        automation = {
            "triggers": [
                {"trigger": "delay", "duration": {"minutes": 3}},
                {"trigger": "state", "entity_id": "binary_sensor.door", "to": "on"},
            ],
            "conditions": {"condition": "trigger", "id": "door"},
            "actions": [{"action": "script.turn_on"}],
        }

        specs, errors = ia._split_automation_specs(automation)

        self.assertEqual(specs, [])
        self.assertIn("trigger conditions", " ".join(errors))

    def test_split_rejects_invalid_trigger_shapes(self) -> None:
        cases = [
            (
                {
                    "triggers": [
                        {"trigger": "delay", "duration": {"minutes": 3}},
                        "bad",
                    ]
                },
                "items must be objects",
            ),
            ({"trigger": "bad"}, "must be a list or object"),
        ]
        for automation, expected_error in cases:
            with self.subTest(automation=automation):
                specs, errors = ia._split_automation_specs(automation)

                self.assertEqual(specs, [])
                self.assertIn(expected_error, " ".join(errors))

    def test_internal_cleanup_action_is_appended_only_by_internal_flow(self) -> None:
        automation = {
            "semantic_text": "主题: 一次性自动化\n触发: 指定日期时间\n动作: 执行脚本\n对象: 脚本\n意图: 到指定日期执行脚本",
            "triggers": [
                {"trigger": "time", "date": "2026-06-04", "at": "08:00:00"}
            ],
            "actions": [{"action": "script.turn_on"}],
        }
        frozen_now = datetime(2026, 6, 3, 9, 0, 0, tzinfo=LOCAL_TZ)

        with (
            patch.object(ia.dt_util, "now", return_value=frozen_now),
            patch.object(ia.dt_util, "get_default_time_zone", return_value=LOCAL_TZ),
        ):
            config, errors = asyncio.run(
                ia._automation_config_from_internal_plan(
                    _FakeIntent(),
                    "houzzkit_ai_test",
                    automation,
                    append_one_shot_cleanup=True,
                )
            )

        self.assertEqual(errors, [])
        cleanup = config["actions"][-1]
        self.assertEqual(cleanup["action"], "houzzkit_ai.delete_one_shot_automation")
        self.assertEqual(cleanup["data"]["id"], "houzzkit_ai_test")
        self.assertEqual(cleanup["data"]["marker"], "houzzkit_ai_one_shot")

    def test_reminder_config_writes_managed_kind_variable(self) -> None:
        automation = {
            "alias": "喝水提醒",
            "semantic_text": "主题: 喝水提醒\n动作: 提醒用户喝水\n对象: 水\n意图: 到时间提醒用户喝水",
            "triggers": [{"trigger": "time", "at": "09:00:00"}],
            "actions": [{"action": "houzzkit_ai.notify", "data": {"message": "喝水"}}],
        }

        async def fake_convert_action(
            intent_obj: object,
            action: dict[str, Any],
            errors: list[str],
            *,
            path: str,
        ) -> list[dict[str, Any]]:
            return [action]

        with patch.object(ia, "_convert_action_with_resolved_targets", fake_convert_action):
            config, errors = asyncio.run(
                ia._automation_config_from_internal_plan(
                    _FakeIntent(),
                    "houzzkit_ai_reminder",
                    automation,
                    managed_kind="reminder",
                )
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            config["variables"]["houzzkit_ai_managed_kind"],
            "reminder",
        )
        self.assertEqual(
            config["variables"]["houzzkit_ai_reminder"],
            {
                "schedule": {"type": "time", "at": "09:00:00"},
                "message": "喝水",
            },
        )
        self.assertEqual(
            config["variables"]["houzzkit_ai_semantic_text"],
            "主题: 喝水提醒\n动作: 提醒用户喝水\n对象: 水\n意图: 到时间提醒用户喝水",
        )
        self.assertNotIn("semantic_text", config)

    def test_managed_kind_variable_preserves_existing_variables(self) -> None:
        automation = {
            "alias": "开灯自动化",
            "semantic_text": "主题: 开灯自动化\n触发: 晚上\n动作: 打开灯\n对象: 灯\n意图: 到条件时开灯",
            "triggers": [{"trigger": "time", "at": "19:00:00"}],
            "actions": [{"action": "script.turn_on"}],
            "variables": {
                "user_value": "keep",
                "houzzkit_ai_managed_kind": "reminder",
                "houzzkit_ai_semantic_text": "旧文本",
            },
        }

        config, errors = asyncio.run(
            ia._automation_config_from_internal_plan(
                _FakeIntent(),
                "houzzkit_ai_light",
                automation,
                managed_kind="automation",
            )
        )

        self.assertEqual(errors, [])
        self.assertEqual(config["variables"]["user_value"], "keep")
        self.assertEqual(
            config["variables"]["houzzkit_ai_managed_kind"],
            "automation",
        )
        self.assertEqual(
            config["variables"]["houzzkit_ai_semantic_text"],
            "主题: 开灯自动化\n触发: 晚上\n动作: 打开灯\n对象: 灯\n意图: 到条件时开灯",
        )
        self.assertNotIn("semantic_text", config)

    def test_automation_config_requires_semantic_text(self) -> None:
        automation = {
            "alias": "开灯自动化",
            "triggers": [{"trigger": "time", "at": "19:00:00"}],
            "actions": [{"action": "script.turn_on"}],
        }

        config, errors = asyncio.run(
            ia._automation_config_from_internal_plan(
                _FakeIntent(),
                "houzzkit_ai_light",
                automation,
                managed_kind="automation",
            )
        )

        self.assertEqual(config, {})
        self.assertIn("automation.semantic_text is required.", errors)

    def test_user_cleanup_action_is_rejected(self) -> None:
        errors: list[str] = []

        result = asyncio.run(
            ia._convert_action_with_resolved_targets(
                _FakeIntent(),
                {"action": "houzzkit_ai.delete_one_shot_automation"},
                errors,
                path="automation.actions[0]",
            )
        )

        self.assertEqual(result, [])
        self.assertIn("internal", " ".join(errors))

    def test_resolved_target_accepts_duplicate_fan_entities(self) -> None:
        async def fake_match_intent_entities(
            intent_obj: object,
            targets: list[dict[str, Any]],
        ) -> tuple[None, list[types.SimpleNamespace]]:
            self.assertEqual(
                targets,
                [
                    {
                        "area": "办公区",
                        "devices": [{"name": "空气净化器", "domains": ["fan"]}],
                    }
                ],
            )
            return None, [
                _matched_entity(
                    "fan.air_purifier_left",
                    "fan",
                    name="空气净化器",
                    area_name="办公区",
                ),
                _matched_entity(
                    "fan.air_purifier_right",
                    "fan",
                    name="空气净化器",
                    area_name="办公区",
                ),
            ]

        errors: list[str] = []
        action = {
            "operation": "turn_on",
            "resolved_targets": [
                {
                    "area": "办公区",
                    "devices": [{"name": "空气净化器", "domains": ["fan"]}],
                }
            ],
        }

        with patch.object(ia, "match_intent_entities", fake_match_intent_entities):
            result = asyncio.run(
                ia._convert_action_with_resolved_targets(
                    _FakeIntent(),
                    action,
                    errors,
                    path="automation.actions[0]",
                )
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            result,
            [
                {
                    "action": "fan.turn_on",
                    "target": {
                        "entity_id": [
                            "fan.air_purifier_left",
                            "fan.air_purifier_right",
                        ]
                    },
                }
            ],
        )

    def test_resolved_target_accepts_duplicate_cover_entities(self) -> None:
        async def fake_match_intent_entities(
            intent_obj: object,
            targets: list[dict[str, Any]],
        ) -> tuple[None, list[types.SimpleNamespace]]:
            self.assertEqual(
                targets,
                [
                    {
                        "area": "直播间",
                        "devices": [{"name": "窗帘 电机", "domains": ["cover"]}],
                    }
                ],
            )
            return None, [
                _matched_entity(
                    "cover.curtain_left",
                    "cover",
                    name="窗帘 电机",
                    area_name="直播间",
                ),
                _matched_entity(
                    "cover.curtain_right",
                    "cover",
                    name="窗帘 电机",
                    area_name="直播间",
                ),
            ]

        errors: list[str] = []
        action = {
            "operation": "turn_on",
            "resolved_targets": [
                {
                    "area": "直播间",
                    "devices": [{"name": "窗帘 电机", "domains": ["cover"]}],
                }
            ],
        }

        with patch.object(ia, "match_intent_entities", fake_match_intent_entities):
            result = asyncio.run(
                ia._convert_action_with_resolved_targets(
                    _FakeIntent(),
                    action,
                    errors,
                    path="automation.actions[0]",
                )
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            result,
            [
                {
                    "action": "cover.open_cover",
                    "target": {
                        "entity_id": [
                            "cover.curtain_left",
                            "cover.curtain_right",
                        ]
                    },
                }
            ],
        )

    def test_cleanup_marker_must_match_generated_action(self) -> None:
        automation = {
            "id": "houzzkit_ai_test",
            "actions": [
                {
                    "action": "houzzkit_ai.delete_one_shot_automation",
                    "data": {
                        "id": "houzzkit_ai_test",
                        "marker": "houzzkit_ai_one_shot",
                    },
                }
            ],
        }

        self.assertTrue(
            ia._has_internal_one_shot_cleanup_action(automation, "houzzkit_ai_test")
        )
        self.assertFalse(
            ia._has_internal_one_shot_cleanup_action(automation, "houzzkit_ai_other")
        )

    def test_delete_one_shot_service_registers_callback_handler(self) -> None:
        hass = _FakeSetupHass()

        ia.async_setup_automation_services(hass)

        self.assertEqual(len(hass.services.registered), 1)
        domain, service, service_func, _schema = hass.services.registered[0]
        self.assertEqual(domain, "houzzkit_ai")
        self.assertEqual(service, "delete_one_shot_automation")
        self.assertIs(service_func.func, ia._handle_delete_one_shot_service)
        self.assertIs(service_func.args[0], hass)
        self.assertTrue(getattr(service_func.func, "_hass_callback", False))

    def test_delete_one_shot_handler_schedules_cleanup_task(self) -> None:
        calls: list[tuple[object, str, str]] = []

        async def fake_delete(
            hass: object,
            automation_id: str,
            marker: str,
        ) -> None:
            calls.append((hass, automation_id, marker))

        hass = _FakeTaskHass()
        call = types.SimpleNamespace(
            data={
                "id": "houzzkit_ai_test",
                "marker": "houzzkit_ai_one_shot",
            }
        )

        with patch.object(ia, "_async_delete_one_shot_automation", fake_delete):
            ia._handle_delete_one_shot_service(hass, call)

        self.assertIsNotNone(hass.created_task)
        asyncio.run(hass.created_task)
        self.assertEqual(
            calls,
            [(hass, "houzzkit_ai_test", "houzzkit_ai_one_shot")],
        )


if __name__ == "__main__":
    unittest.main()
