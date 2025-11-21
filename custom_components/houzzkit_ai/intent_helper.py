import logging
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
import voluptuous as vol
from homeassistant.helpers import config_validation as cv

from homeassistant.core import HomeAssistant, State
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent

_LOGGER = logging.getLogger(__name__)

def target_paramter_type():
    return vol.All(cv.ensure_list, [vol.Schema({
            vol.Optional("devices"): vol.All(cv.ensure_list, [vol.Schema({
                vol.Required("domains"): vol.All(cv.ensure_list, [cv.string]), 
                vol.Optional("name"): cv.string
            })]),
            vol.Optional("area"): cv.string,
        })])

def get_entity_name(entity_entry: er.RegistryEntry, state: State) -> str:
    if len(entity_entry.aliases) > 0:
        return list(entity_entry.aliases)[0]
    
    if entity_entry.name:
        return entity_entry.name
    
    return state.name

@dataclass
class AreaInfo:
    name: str
    id: str

def get_entity_area(hass: HomeAssistant, entity_entry: er.RegistryEntry) -> AreaInfo | None:
    area_names = []
    area_registry = ar.async_get(hass)
    device_registry = dr.async_get(hass)
    if entity_entry.area_id and (
        area := area_registry.async_get_area(entity_entry.area_id)
    ):
        # Entity is in area
        area_names.extend(area.aliases)
        area_names.append(area.name)
        if len(area_names) == 0:
            return
        return AreaInfo(id=entity_entry.area_id, name=area_names[0])
    elif entity_entry.device_id and (
        device := device_registry.async_get(entity_entry.device_id)
    ):
        # Check device area
        if device.area_id and (
            area := area_registry.async_get_area(device.area_id)
        ):
            area_names.extend(area.aliases)
            area_names.append(area.name)
            if len(area_names) == 0:
                return
            return AreaInfo(id=device.area_id, name=area_names[0])

@dataclass
class EntityInfo:
    name: str
    area: AreaInfo | None
    state: State
    entity: er.RegistryEntry
    on_off: Literal["on", "off"]
    
    @property
    def area_name(self) -> str:
        if self.area:
            return self.area.name
        return ""
    
    @property
    def area_id(self) -> str:
        if self.area:
            return self.area.id
        return ""
    
class HaDeviceItem(TypedDict):
    domains: list[str]
    name: str | None

class HaTargetItem(TypedDict):
    area: str | None
    devices: list[HaDeviceItem]

async def match_intent_entities(intent_obj: intent.Intent, targets: list[HaTargetItem]) -> tuple[dict | None, list[EntityInfo] | None]:
    """Match entities by request parameters."""
    hass = intent_obj.hass
    found_states: list[State] = []
    for target in targets:
        for device in target["devices"]:
            match_constraints = intent.MatchTargetsConstraints(
                name=device.get("name"),
                area_name=target.get("area"),
                domains=device["domains"],
                assistant=intent_obj.assistant,
                single_target=False,
                allow_duplicate_names=True,
            )
            _LOGGER.info(f"Match intent constraints: {match_constraints}")
            match_result = intent.async_match_targets(
                hass, match_constraints
            )
            
            if not match_result.is_match:
                continue
            
            found_states.extend(match_result.states)
    
    if len(found_states) == 0:
        return {
            "success": False,
            "error": "No available devices found"
        }, None
    
    # Filter out candidate targets.
    candidate_entities: list[EntityInfo] = []
    for state in found_states:
        if state.state == "unavailable":
            continue
        
        entity_registry = er.async_get(hass)
        entity_entry = entity_registry.async_get(state.entity_id)
        if not entity_entry:
            continue
        
        entity_name = get_entity_name(entity_entry, state)
        entity_area = get_entity_area(hass, entity_entry)
        on_off = "off" if state.state == "off" else "on"
        entity_info = EntityInfo(name=entity_name, area=entity_area, state=state, entity=entity_entry, on_off=on_off)
        _LOGGER.info(f"Match intent available target: {entity_info}")
        candidate_entities.append(entity_info)
                
    # No any available.
    if len(candidate_entities) == 0:
        return {
            "success": False,
            "error": "No available devices found"
        }, None
        
    return None, candidate_entities
