import logging
from dataclasses import asdict, dataclass, field
from typing import Callable

import voluptuous as vol
from homeassistant.components import climate, humidifier
from homeassistant.const import ATTR_ENTITY_ID, ATTR_MODE, Platform
from homeassistant.core import State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import intent
from homeassistant.util.json import JsonObjectType
from homeassistant.helpers import entity_registry as er

from .intent_helper import (HaTargetItem, match_intent_entities,
                            target_paramter_type)

_LOGGER = logging.getLogger(__name__)

@dataclass
class OperationContext:
    state: State
    entity: er.RegistryEntry
    mode: str

@dataclass
class OperationTarget:
    service: str = ""
    service_data: dict = field(default_factory=dict)
    avail_modes: list[str] = field(default_factory=list)
    
handle_map: dict[str, dict[str, Callable[[OperationContext, OperationTarget], None]]] = {}

def register_handler(domain: str, attrbute: str):
    def decorator(func):
        attrbute_handlers = handle_map.setdefault(domain, {})
        attrbute_handlers[attrbute] = func
        
        def wrapper(ctx: OperationContext, target: OperationTarget):
            func(ctx, target)
        return wrapper
    return decorator


@register_handler("climate", "mode")
def set_climate_mode(ctx: OperationContext, target: OperationTarget):
    avail_modes = []
    if ctx.entity.capabilities:
        # Current device
        avail_modes = ctx.entity.capabilities.get('hvac_modes')
    if not avail_modes:
        # Global
        avail_modes = climate.const.HVAC_MODES
    
    # Remove 'off' from mode list.
    avail_modes = avail_modes[:]
    if "off" in avail_modes:
        avail_modes.remove("off")
    
    if len(avail_modes) == 0:
        raise intent.IntentHandleError("Unsupported set mode")
    
    if ctx.mode not in avail_modes:
        raise intent.IntentHandleError(f"Invalid mode, not in [{','.join(avail_modes)}]")
    
    target.service = climate.const.SERVICE_SET_HVAC_MODE
    target.service_data[climate.const.ATTR_HVAC_MODE] = ctx.mode
    target.avail_modes = avail_modes
    
@register_handler("humidifier", "mode")
def set_humidifier_mode(ctx: OperationContext, target: OperationTarget):
    mode = ctx.mode
    state = ctx.state
    avail_modes = state.attributes.get(humidifier.const.ATTR_AVAILABLE_MODES, [])
    if len(avail_modes) == 0:
        raise intent.IntentHandleError("Unsupported set mode")
    
    if mode not in avail_modes:
        raise intent.IntentHandleError(f"Invalid mode, not in [{','.join(avail_modes)}]")
        
    target.service = humidifier.const.SERVICE_SET_MODE
    target.service_data[ATTR_MODE] = ctx.mode
    target.avail_modes = avail_modes
    
class SetDeviceModeIntent(intent.IntentHandler):
    intent_type = "SetDeviceMode"
    description = "Set the mode of the device."
    platforms = {Platform.CLIMATE, Platform.HUMIDIFIER}
    
    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return {
            vol.Required("mode"): intent.non_empty_string,
            vol.Required("target"): target_paramter_type(),
        }

    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType: # type: ignore
        """Handle the intent."""
        hass = intent_obj.hass
        slots = self.async_validate_slots(intent_obj.slots)
        
        mode: str = slots.get("mode", {}).get("value")
        targets: list[HaTargetItem] = slots.get("target", {}).get("value", [])
        
        error_msg, candidate_entities = await match_intent_entities(intent_obj, targets)
        if error_msg:
            return error_msg
        assert candidate_entities
        
        results = []
        for item in candidate_entities:
            domain = item.state.domain
            state = item.state
            _LOGGER.info(f"SetDeviceMode state: {item.state.as_dict_json}")
            
            error: str | None = None
            target = OperationTarget()
            try:
                handle = handle_map.get(domain, {}).get("mode")
                if not handle:
                    raise intent.IntentHandleError("unspported")
                
                # Find the paramters to adjust.
                handle(OperationContext(state=state, entity=item.entity, mode=mode), target)
                target.service_data[ATTR_ENTITY_ID] = state.entity_id
                
                # Execute.
                _LOGGER.info(f"AdjustDeviceAttribute call target: {asdict(target)}")
                await hass.services.async_call(
                    domain,
                    target.service,
                    service_data=target.service_data,
                    blocking=True,
                    context=intent_obj.context,
                )
            except (intent.IntentHandleError, ServiceValidationError) as e:
                error = str(e)
            
            success = not error
            result = {"success": success, "name": item.name, "area": item.area_name, "supported_modes": target.avail_modes}
            if error:
                result["error"] = error
            
            results.append(result)
            
        return {
            "results": results,
        }
