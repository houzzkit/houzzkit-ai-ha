import asyncio
import logging
from typing import Any, Literal

import voluptuous as vol

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.button import \
    SERVICE_PRESS as SERVICE_PRESS_BUTTON
from homeassistant.components.cover import DOMAIN as COVER_DOMAIN
from homeassistant.components.cover import (SERVICE_CLOSE_COVER,
                                            SERVICE_OPEN_COVER)
from homeassistant.components.input_button import DOMAIN as INPUT_BUTTON_DOMAIN
from homeassistant.components.lock import DOMAIN as LOCK_DOMAIN
from homeassistant.components.lock import SERVICE_LOCK, SERVICE_UNLOCK
from homeassistant.components.valve import DOMAIN as VALVE_DOMAIN
from homeassistant.components.valve import (SERVICE_CLOSE_VALVE,
                                            SERVICE_OPEN_VALVE)
from homeassistant.const import ATTR_ENTITY_ID, SERVICE_TURN_ON
from homeassistant.core import State
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as intent
from homeassistant.helpers import intent
from homeassistant.util.json import JsonObjectType

from .intent_helper import match_intent_entities

_LOGGER = logging.getLogger(__name__)


class TurnDeviceIntentBase(intent.IntentHandler):
    service_timeout = 5

    async def _async_handle(self, intent_obj: intent.Intent, slots: dict[str, Any], service: Literal["turn_on", "turn_off"]) -> JsonObjectType:
        """Get the current state of exposed entities."""
        error_msg, candidate_entities = await match_intent_entities(intent_obj, slots)
        if error_msg:
            return error_msg
        assert candidate_entities
        
        # Execute operation.
        control_targets = []
        entity_key_map = set() # for deduplication
        for item in candidate_entities:
            _LOGGER.info(f"Operate target: area={item.area_name} name={item.name} id={item.entity.id}")
            await self.handle_match_target(intent_obj, item.state, service)
            
            entity_key = f"{item.area_name}-{item.name}"
            if entity_key not in entity_key_map:
                entity_key_map.add(entity_key)
                control_targets.append({"name": item.name, "area": item.area_name})

        return {
            "success": True,
            "control_targets": control_targets,
        }

    async def handle_match_target(self, intent_obj: intent.Intent, state: State, service: str):
        hass = intent_obj.hass
        if state.domain in (BUTTON_DOMAIN, INPUT_BUTTON_DOMAIN):
            if service != SERVICE_TURN_ON:
                raise intent.IntentHandleError(
                    f"Entity {state.entity_id} cannot be turned off"
                )

            await hass.services.async_call(
                    state.domain,
                    SERVICE_PRESS_BUTTON,
                    {ATTR_ENTITY_ID: state.entity_id},
                    context=intent_obj.context,
                    blocking=True,
                )
            return

        if state.domain == COVER_DOMAIN:
            # on = open
            # off = close
            if service == SERVICE_TURN_ON:
                service_name = SERVICE_OPEN_COVER
            else:
                service_name = SERVICE_CLOSE_COVER

            await hass.services.async_call(
                    COVER_DOMAIN,
                    service_name,
                    {ATTR_ENTITY_ID: state.entity_id},
                    context=intent_obj.context,
                    blocking=True,
                )
            return

        if state.domain == LOCK_DOMAIN:
            # on = lock
            # off = unlock
            if service == SERVICE_TURN_ON:
                service_name = SERVICE_LOCK
            else:
                service_name = SERVICE_UNLOCK

            await hass.services.async_call(
                    LOCK_DOMAIN,
                    service_name,
                    {ATTR_ENTITY_ID: state.entity_id},
                    context=intent_obj.context,
                    blocking=True,
                )
            return

        if state.domain == VALVE_DOMAIN:
            # on = opened
            # off = closed
            if service == SERVICE_TURN_ON:
                service_name = SERVICE_OPEN_VALVE
            else:
                service_name = SERVICE_CLOSE_VALVE

            await hass.services.async_call(
                    VALVE_DOMAIN,
                    service_name,
                    {ATTR_ENTITY_ID: state.entity_id},
                    context=intent_obj.context,
                    blocking=True,
                )
            return

        if not hass.services.has_service(state.domain, service):
            raise intent.IntentHandleError(
                f"Service {service} does not support entity {state.entity_id}"
            )
        
        # Fall back to homeassistant.turn_on/off
        service_data: dict[str, Any] = {ATTR_ENTITY_ID: state.entity_id}
        _LOGGER.info(f"Operate target fallback: service={service} name={service_data}")
        await hass.services.async_call(
                state.domain,
                service,
                service_data,
                context=intent_obj.context,
                blocking=True,
            )
            
    async def _run_then_background(self, task: asyncio.Task[Any]) -> None:
        """Run task with timeout to (hopefully) catch validation errors.

        After the timeout the task will continue to run in the background.
        """
        try:
            await asyncio.wait({task}, timeout=self.service_timeout)
        except TimeoutError:
            _LOGGER.error("Service call is timeout: %s", task.get_name())
        except asyncio.CancelledError:
            # Task calling us was cancelled, so cancel service call task, and wait for
            # it to be cancelled, within reason, before leaving.
            _LOGGER.debug("Service call was cancelled: %s", task.get_name())
            task.cancel()
            await asyncio.wait({task}, timeout=5)
            raise
        
supported_domain_list = [
    "light",
    "switch",
    "cover",
    "fan",
    "climate",
    "humidifier",
]
        
class TurnDeviceOnIntent(TurnDeviceIntentBase):
    intent_type = "TurnDeviceOn"
    description = (
        "Turns on/opens/presses a device or entity."
    )
    slot_schema = {
        vol.Required("domain"): vol.Any("button", *supported_domain_list),
        vol.Optional("name"): cv.string,
        vol.Optional("area"): cv.string,
        vol.Optional("except_area"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("floor"): cv.string,
        vol.Optional("preferred_area_id"): cv.string,
        vol.Optional("preferred_floor_id"): cv.string,
    } # type: ignore
    
    service_timeout = 3
    
    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        """Get the current state of exposed entities."""
        slots = self.async_validate_slots(intent_obj.slots)
        return await super()._async_handle(intent_obj, slots, "turn_on")
    
class TurnDeviceOffIntent(TurnDeviceIntentBase):
    intent_type = "TurnDeviceOff"
    description = (
        "Turns off/closes a device or entity."
    )
    slot_schema = {
        vol.Required("domain"): vol.Any(*supported_domain_list),
        vol.Optional("name"): cv.string,
        vol.Optional("area"): cv.string,
        vol.Optional("except_area"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("floor"): cv.string,
        vol.Optional("preferred_area_id"): cv.string,
        vol.Optional("preferred_floor_id"): cv.string,
    } # type: ignore
    
    service_timeout = 3
    
    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType:
        """Get the current state of exposed entities."""
        slots = self.async_validate_slots(intent_obj.slots)
        return await super()._async_handle(intent_obj, slots, "turn_off")
    