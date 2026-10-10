"""Regression tests for switch intent control without a Home Assistant runtime."""

import asyncio
import importlib.util
import logging
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch


class FakeState:
    def __init__(self, entity_id, state):
        self.entity_id = entity_id
        self.state = state
        self.domain = entity_id.split(".", 1)[0]


class FakeIntentHandler:
    def async_validate_slots(self, slots):
        return slots


class FakeIntentHandleError(Exception):
    pass


def load_intent_module():
    """Load only intent_turn, isolating integration setup and HA dependencies."""
    modules = {}

    def add_module(name, **attributes):
        module = ModuleType(name)
        module.__dict__.update(attributes)
        modules[name] = module
        return module

    add_module("voluptuous", Required=lambda value: value)
    for name in (
        "homeassistant",
        "homeassistant.components",
        "homeassistant.helpers",
        "homeassistant.util",
    ):
        add_module(name).__path__ = []
    for domain in ("button", "cover", "lock", "switch", "valve"):
        add_module(f"homeassistant.components.{domain}").__path__ = []
        attributes = {"DOMAIN": domain}
        if domain == "button":
            attributes["SERVICE_PRESS"] = "press"
        add_module(f"homeassistant.components.{domain}.const", **attributes)
    add_module("homeassistant.components.input_button", DOMAIN="input_button")
    constants = {
        "ATTR_ENTITY_ID": "entity_id",
        "STATE_OFF": "off",
        "STATE_ON": "on",
    }
    for service in (
        "close_cover", "close_valve", "lock", "open_cover", "open_valve",
        "turn_off", "turn_on", "unlock",
    ):
        constants[f"SERVICE_{service.upper()}"] = service
    add_module("homeassistant.const", **constants)
    add_module("homeassistant.core", State=FakeState)
    helpers = modules["homeassistant.helpers"]
    helpers.config_validation = add_module("homeassistant.helpers.config_validation")
    helpers.entity_registry = add_module("homeassistant.helpers.entity_registry")
    helpers.intent = add_module(
        "homeassistant.helpers.intent",
        IntentHandler=FakeIntentHandler,
        Intent=SimpleNamespace,
        IntentHandleError=FakeIntentHandleError,
    )
    add_module("homeassistant.util.json", JsonObjectType=dict)

    package = "_houzzkit_intent_turn_tests"
    add_module(package).__path__ = []
    add_module(
        f"{package}.intent_helper",
        HaTargetItem=dict,
        match_intent_entities=AsyncMock(),
        target_paramter_type=lambda: list,
    )
    path = (
        Path(__file__).resolve().parents[1]
        / "custom_components/houzzkit_ai/intent_turn.py"
    )
    spec = importlib.util.spec_from_file_location(f"{package}.intent_turn", path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


intent_turn = load_intent_module()


class IntentTurnTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.states = {}
        self.services = SimpleNamespace(
            has_service=Mock(return_value=True),
            async_call=AsyncMock(),
        )
        self.hass = SimpleNamespace(
            states=SimpleNamespace(get=Mock(side_effect=self.states.get)),
            services=self.services,
            async_create_task=asyncio.create_task,
            async_create_task_internal=lambda coro, name: asyncio.create_task(
                coro, name=name
            ),
        )
        self.intent = SimpleNamespace(hass=self.hass, context=object(), slots={})
        self.handler = intent_turn.TurnDeviceIntentBase()

    def candidate(self, entity_id, value, name="开关", area="厨房"):
        return SimpleNamespace(
            state=FakeState(entity_id, value),
            entity=SimpleNamespace(id=entity_id),
            name=name,
            area_name=area,
        )

    async def handle_candidates(self, candidates, service, handler=None):
        with patch.object(
            intent_turn, "match_intent_entities",
            new=AsyncMock(return_value=(None, candidates)),
        ):
            if handler is not None:
                return await handler.async_handle(self.intent)
            return await self.handler._async_handle(self.intent, {}, service)

    async def test_already_at_target_returns_success_without_service_call(self):
        for value, service in (("off", "turn_off"), ("on", "turn_on")):
            with self.subTest(value=value, service=service):
                self.services.async_call.reset_mock()
                self.states["switch.water"] = FakeState("switch.water", value)
                with self.assertLogs(intent_turn._LOGGER, logging.WARNING) as logs:
                    result = await self.handle_candidates(
                        [self.candidate("switch.water", value)], service
                    )
                self.services.async_call.assert_not_called()
                self.assertEqual(result, {
                    "success": True,
                    "control_targets": [{"name": "开关", "area": "厨房"}],
                })
                self.assertEqual(logs.records[0].getMessage(), (
                    f"Switch control: entity_id=switch.water service={service} "
                    f"current_state={value} action=skip"
                ))

    async def test_warning_precedes_normal_service_call(self):
        for value, service in (("on", "turn_off"), ("off", "turn_on")):
            with self.subTest(value=value, service=service):
                self.services.async_call.reset_mock()
                self.states["switch.water"] = FakeState("switch.water", value)
                observed_at_call = []
                with self.assertLogs(intent_turn._LOGGER, logging.WARNING) as logs:
                    async def check_log_before_call(*args, **kwargs):
                        observed_at_call.append([
                            (record.levelno, record.getMessage())
                            for record in logs.records
                        ])

                    self.services.async_call.side_effect = check_log_before_call
                    await self.handler.handle_match_target(
                        self.intent, FakeState("switch.water", value), service
                    )
                self.assertEqual(observed_at_call, [[(
                    logging.WARNING,
                    f"Switch control: entity_id=switch.water service={service} "
                    f"current_state={value} action=execute",
                )]])
                self.services.async_call.assert_awaited_once_with(
                    "switch", service, {"entity_id": "switch.water"},
                    context=self.intent.context, blocking=True,
                )

    async def test_latest_state_overrides_matched_snapshot(self):
        for service, target, opposite in (
            ("turn_off", "off", "on"), ("turn_on", "on", "off")
        ):
            for latest, snapshot, should_call in (
                (target, opposite, False), (opposite, target, True)
            ):
                with self.subTest(service=service, latest=latest):
                    self.services.async_call.reset_mock()
                    self.states["switch.water"] = FakeState("switch.water", latest)
                    with self.assertLogs(intent_turn._LOGGER, logging.WARNING):
                        await self.handler.handle_match_target(
                            self.intent, FakeState("switch.water", snapshot), service
                        )
                    self.assertEqual(self.services.async_call.await_count, int(should_call))

    async def test_unknown_unavailable_and_missing_do_not_skip(self):
        for latest in ("unknown", "unavailable", None):
            for service, snapshot in (("turn_off", "off"), ("turn_on", "on")):
                with self.subTest(latest=latest, service=service):
                    self.services.async_call.reset_mock()
                    self.states.clear()
                    if latest is not None:
                        self.states["switch.water"] = FakeState("switch.water", latest)
                    with self.assertLogs(intent_turn._LOGGER, logging.WARNING) as logs:
                        await self.handler.handle_match_target(
                            self.intent, FakeState("switch.water", snapshot), service
                        )
                    self.services.async_call.assert_awaited_once()
                    self.assertIn(f"current_state={latest or 'missing'}", logs.output[0])
                    self.assertIn("action=execute", logs.output[0])

    async def test_duplicate_switches_and_mixed_targets(self):
        self.states.update({
            "switch.water": FakeState("switch.water", "on"),
            "switch.idle": FakeState("switch.idle", "off"),
        })
        candidates = [
            self.candidate("switch.water", "on", "净饮机"),
            self.candidate("switch.water", "on", "净饮机"),
            self.candidate("switch.water", "on", "净饮机别名"),
            self.candidate("switch.idle", "off", "空闲开关"),
            self.candidate("switch.idle", "off", "空闲开关"),
            self.candidate("light.kitchen", "off", "厨房灯"),
        ]
        with self.assertLogs(intent_turn._LOGGER, logging.WARNING) as logs:
            result = await self.handle_candidates(candidates, "turn_off")
        self.assertEqual(self.services.async_call.await_count, 2)
        self.assertEqual(
            [call.args[:3] for call in self.services.async_call.await_args_list],
            [
                ("switch", "turn_off", {"entity_id": "switch.water"}),
                ("light", "turn_off", {"entity_id": "light.kitchen"}),
            ],
        )
        self.assertEqual(len(logs.records), 2)
        self.assertEqual(result, {
            "success": True,
            "control_targets": [
                {"name": name, "area": "厨房"}
                for name in ("净饮机", "净饮机别名", "空闲开关", "厨房灯")
            ],
        })

    async def test_deduplication_is_limited_to_one_request(self):
        candidates = [self.candidate("switch.water", "on")]
        self.states["switch.water"] = FakeState("switch.water", "on")
        with self.assertLogs(intent_turn._LOGGER, logging.WARNING):
            await self.handle_candidates(candidates, "turn_off")
            self.states["switch.water"] = FakeState("switch.water", "off")
            await self.handle_candidates(candidates, "turn_on")
        self.assertEqual(
            [call.args[1] for call in self.services.async_call.await_args_list],
            ["turn_off", "turn_on"],
        )

    async def test_non_switch_services_keep_existing_behavior(self):
        for domain, value, service, expected in (
            ("light", "off", "turn_off", "turn_off"),
            ("fan", "on", "turn_on", "turn_on"),
            ("cover", "closed", "turn_off", "close_cover"),
            ("lock", "locked", "turn_on", "lock"),
            ("valve", "closed", "turn_off", "close_valve"),
            ("button", "unknown", "turn_on", "press"),
            ("input_button", "unknown", "turn_on", "press"),
        ):
            with self.subTest(domain=domain):
                self.services.async_call.reset_mock()
                await self.handler.handle_match_target(
                    self.intent, FakeState(f"{domain}.test", value), service
                )
                self.services.async_call.assert_awaited_once_with(
                    domain, expected, {"entity_id": f"{domain}.test"},
                    context=self.intent.context, blocking=True,
                )
        self.hass.states.get.assert_not_called()

    async def test_unsupported_service_is_not_reported_as_skipped_success(self):
        self.services.has_service.return_value = False
        self.states["switch.water"] = FakeState("switch.water", "off")
        with self.assertRaises(FakeIntentHandleError):
            await self.handler.handle_match_target(
                self.intent, FakeState("switch.water", "off"), "turn_off"
            )
        self.services.async_call.assert_not_called()

    async def test_public_intents_dispatch_and_log_correct_direction(self):
        for handler, service, value, intent_name in (
            (intent_turn.TurnDeviceOnIntent(), "turn_on", "off", "TurnDeviceOn"),
            (intent_turn.TurnDeviceOffIntent(), "turn_off", "on", "TurnDeviceOff"),
        ):
            with self.subTest(service=service):
                self.services.async_call.reset_mock()
                self.states["switch.water"] = FakeState("switch.water", value)
                with self.assertLogs(intent_turn._LOGGER, logging.INFO) as logs:
                    result = await self.handle_candidates(
                        [self.candidate("switch.water", value)], service, handler
                    )
                self.assertTrue(result["success"])
                self.assertIn(f"{intent_name} slots=", logs.output[0])
                self.services.async_call.assert_awaited_once_with(
                    "switch", service, {"entity_id": "switch.water"},
                    context=self.intent.context, blocking=True,
                )


if __name__ == "__main__":
    unittest.main()
