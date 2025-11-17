import time
import yaml
import logging
import aiofiles
import voluptuous as vol

from pathlib import Path
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, intent
from homeassistant.const import (
    Platform, ATTR_ENTITY_ID,
    CONF_ID, SERVICE_RELOAD,
    CONF_ALIAS, CONF_MODE, CONF_TRIGGER, CONF_ACTION,
)
from homeassistant.components.automation import (
    DOMAIN as AUTOMATION_DOMAIN,
    CONF_TRIGGERS, CONF_ACTIONS,
)
from homeassistant.components.climate.const import (
    HVAC_MODES,
    SERVICE_SET_HVAC_MODE,
    SERVICE_SET_FAN_MODE,
    ATTR_HVAC_MODE,
    ATTR_FAN_MODES,
    ATTR_FAN_MODE,
)
from homeassistant.util.percentage import percentage_to_ordered_list_item

from .houzzkit import get_entities
from .intent_adjust_attribute import AdjustDeviceAttributeIntent
from .intent_live_context import HouzzkitGetLiveContextIntent
from .intent_turn import TurnDeviceOnIntent, TurnDeviceOffIntent
from .intent_alarm import CreateAlarmClockIntent, CreateCountdownAlarmClockIntent
from .intent_helper import match_intent_entities

_LOGGER = logging.getLogger(__name__)


async def async_setup_intents(hass: HomeAssistant):
    """Set up the intents ."""
    intent.async_register(hass, ClimateSetHvacModeIntent())
    intent.async_register(hass, ClimateSetFanModeIntent())
    intent.async_register(hass, CreateAlarmClockIntent())
    # intent.async_register(hass, CreateCountdownAlarmClockIntent())
    intent.async_register(hass, AdjustDeviceAttributeIntent())
    intent.async_register(hass, HouzzkitGetLiveContextIntent())
    intent.async_register(hass, TurnDeviceOnIntent())
    intent.async_register(hass, TurnDeviceOffIntent())

class ClimateSetHvacModeIntent(intent.IntentHandler):
    intent_type = "ClimateSetHvacMode"
    description = "Sets the target hvac mode of a climate device or entity"
    slot_schema = {
        vol.Required(ATTR_HVAC_MODE): vol.Any(*HVAC_MODES),
        vol.Required("domain"): vol.Any("climate"),
        vol.Optional("name"): intent.non_empty_string,
        vol.Optional("area"): intent.non_empty_string,
        vol.Optional("except_area"): vol.All(cv.ensure_list, [cv.string]),
        vol.Optional("floor"): intent.non_empty_string,
        vol.Optional("preferred_area_id"): cv.string,
        vol.Optional("preferred_floor_id"): cv.string,
    } # type: ignore
    platforms = {Platform.CLIMATE}

    async def async_handle(self, intent_obj: intent.Intent):
        """Handle the intent."""
        hass = intent_obj.hass
        slots = self.async_validate_slots(intent_obj.slots)
        mode = slots[ATTR_HVAC_MODE]["value"]
        
        error_msg, candidate_entities = await match_intent_entities(intent_obj, slots)
        if error_msg:
            return error_msg
        assert candidate_entities

        # Execute operation.
        control_targets = []
        entity_key_map = set() # for deduplication
        for item in candidate_entities:
            _LOGGER.info(f"ClimateSetHvacMode target: area={item.area_name} name={item.name} id={item.entity.id}")
            await hass.services.async_call(
                Platform.CLIMATE,
                SERVICE_SET_HVAC_MODE,
                target={ATTR_ENTITY_ID: item.state.entity_id},
                service_data={ATTR_HVAC_MODE: mode},
                blocking=True,
            )
            
            entity_key = f"{item.area_name}-{item.name}"
            if entity_key not in entity_key_map:
                entity_key_map.add(entity_key)
                control_targets.append({"name": item.name, "area": item.area_name})

        return {
            "success": True,
            "control_targets": control_targets,
        }


class ClimateSetFanModeIntent(intent.IntentHandler):
    intent_type = "ClimateSetFanMode"
    description = (
        "Sets the target fan level of a climate device or entity. "
        "Users may describe fan level using terms like high, medium, low, 1st gear, level 7, etc., "
        "and these need to be converted to percentages and returned. "
        "Use 0 to represent automatic wind speed. "
    )
    slot_schema = {
        vol.Required(ATTR_FAN_MODE): vol.All(vol.Coerce(int), vol.Range(0, 100)),
        vol.Required("domain"): vol.Any("climate"),
        vol.Optional("area"): intent.non_empty_string,
        vol.Optional("name"): intent.non_empty_string,
        vol.Optional("floor"): intent.non_empty_string,
        vol.Optional("preferred_area_id"): cv.string,
        vol.Optional("preferred_floor_id"): cv.string,
    } # type: ignore
    platforms = {Platform.CLIMATE}

    async def async_handle(self, intent_obj: intent.Intent):
        """Handle the intent."""
        hass = intent_obj.hass
        slots = self.async_validate_slots(intent_obj.slots)

        mode = slots[ATTR_FAN_MODE]["value"]
        name = slots.get("name", {}).get("value")
        area_name = slots.get("area", {}).get("value")
        floor_name = slots.get("floor", {}).get("value")

        match_constraints = intent.MatchTargetsConstraints(
            name=name,
            area_name=area_name,
            floor_name=floor_name,
            domains=[Platform.CLIMATE],
            assistant=intent_obj.assistant,
            single_target=True,
        )
        match_preferences = intent.MatchTargetsPreferences(
            area_id=slots.get("preferred_area_id", {}).get("value"),
            floor_id=slots.get("preferred_floor_id", {}).get("value"),
        )
        match_result = intent.async_match_targets(
            hass, match_constraints, match_preferences
        )
        if not match_result.is_match:
            raise intent.MatchFailedError(
                result=match_result, constraints=match_constraints
            )

        assert match_result.states
        state = match_result.states[0]
        fan_modes = state.attributes.get(ATTR_FAN_MODES, [])
        if not fan_modes:
            raise intent.IntentHandleError("This climate device does not have fan modes")
        fan_mode = None
        if mode == 0:
            for m in fan_modes:
                if str(m).lower() in ["auto", "smart", "自动", "智能"]:
                    fan_mode = m
                    break
        if fan_mode is None:
            fan_mode = percentage_to_ordered_list_item(fan_modes, mode)

        await hass.services.async_call(
            Platform.CLIMATE,
            SERVICE_SET_FAN_MODE,
            target={ATTR_ENTITY_ID: state.entity_id},
            service_data={ATTR_FAN_MODE: fan_mode},
            blocking=True,
        )

        response = intent_obj.create_response()
        response.response_type = intent.IntentResponseType.ACTION_DONE
        response.async_set_states(matched_states=[state])
        response.async_set_results(
            success_results=[
                intent.IntentResponseTarget(
                    type=intent.IntentResponseTargetType.ENTITY,
                    name=state.name,
                    id=state.entity_id,
                ),
            ],
        )
        return response

