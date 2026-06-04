"""Targeted tests for the Houzzkit automation planning protocol."""

from __future__ import annotations

import asyncio
from datetime import datetime
import importlib.util
from pathlib import Path
import sys
import types
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
    def async_services(self) -> dict[str, dict[str, dict]]:
        return {"script": {"turn_on": {}}}


class _FakeHass:
    services = _FakeServices()


class _FakeIntent:
    assistant = object()
    hass = _FakeHass()


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


if __name__ == "__main__":
    unittest.main()
