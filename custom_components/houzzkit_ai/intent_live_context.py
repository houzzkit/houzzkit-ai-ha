import logging
from decimal import Decimal
from enum import Enum
from operator import attrgetter
from typing import Any

from homeassistant.components import calendar, script
from homeassistant.components.homeassistant.const import DATA_EXPOSED_ENTITIES
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import intent
from homeassistant.util import dt as dt_util
from homeassistant.util import yaml as yaml_util
from homeassistant.util.json import JsonObjectType

from .houzzkit import get_entities

_LOGGER = logging.getLogger(__name__)

def async_should_expose(hass: HomeAssistant, assistant: str, entity_id: str) -> bool:
    """Return True if an entity should be exposed to an assistant."""
    exposed_entities = hass.data[DATA_EXPOSED_ENTITIES]
    return exposed_entities.async_should_expose(assistant, entity_id)

def _get_exposed_entities(
    hass: HomeAssistant,
    assistant: str,
    include_state: bool = True,
) -> dict[str, dict[str, dict[str, Any]]]:
    """Get exposed entities.

    Splits out calendars and scripts.
    """
    area_registry = ar.async_get(hass)
    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
        
    interesting_attributes = {
        "temperature",
        "current_temperature",
        "temperature_unit",
        "brightness",
        "humidity",
        "unit_of_measurement",
        "device_class",
        "current_position",
        "percentage",
        "volume_level",
        "media_title",
        "media_artist",
        "media_album_name",
        
        "color_temp_kelvin",
        "min_color_temp_kelvin",
        "max_color_temp_kelvin",
        "percentage_step",
        "min_temp",
        "max_temp",
        "target_temp_step",
        "min_humidity",
        "max_humidity",
        
    }

    entities = {}
    data: dict[str, dict[str, Any]] = {
        script.const.DOMAIN: {},
        calendar.const.DOMAIN: {},
    }

    for state in sorted(hass.states.async_all(), key=attrgetter("name")):
        if not async_should_expose(hass, assistant, state.entity_id):
            continue

        entity_entry = entity_registry.async_get(state.entity_id)
        names = [state.name]
        area_names = []

        if entity_entry is not None:
            names.extend(entity_entry.aliases)
            if entity_entry.area_id and (
                area := area_registry.async_get_area(entity_entry.area_id)
            ):
                # Entity is in area
                area_names.append(area.name)
                area_names.extend(area.aliases)
            elif entity_entry.device_id and (
                device := device_registry.async_get(entity_entry.device_id)
            ):
                # Check device area
                if device.area_id and (
                    area := area_registry.async_get_area(device.area_id)
                ):
                    area_names.append(area.name)
                    area_names.extend(area.aliases)

        info: dict[str, Any] = {
            "names": ", ".join(names),
            "domain": state.domain,
        }

        if include_state:
            info["state"] = state.state

            # Convert timestamp device_class states from UTC to local time
            if state.attributes.get("device_class") == "timestamp" and state.state:
                if (parsed_utc := dt_util.parse_datetime(state.state)) is not None:
                    info["state"] = dt_util.as_local(parsed_utc).isoformat()

        if area_names:
            info["areas"] = ", ".join(area_names)

        if include_state and (
            attributes := {
                attr_name: (
                    str(attr_value)
                    if isinstance(attr_value, (Enum, Decimal, int))
                    else attr_value
                )
                for attr_name, attr_value in state.attributes.items()
                if attr_name in interesting_attributes
            }
        ):
            info["attributes"] = attributes

        if state.domain in data:
            data[state.domain][state.entity_id] = info
        else:
            entities[state.entity_id] = info

    data["entities"] = entities
    return data

def find_speaker_area(hass: HomeAssistant, speaker_id: str) -> ar.AreaEntry | None:
    speaker_entities = get_entities(hass, speaker_id)
    if not speaker_entities or len(speaker_entities) == 0:
        return
    
    speaker_device_id = speaker_entities[0].device_id
    if not speaker_device_id:
        return
    
    device_registry = dr.async_get(hass)
    speaker_device = device_registry.async_get(speaker_device_id)
    if not speaker_device:
        return
    
    if not speaker_device.area_id:
        return
    
    area_registry = ar.async_get(hass)
    return area_registry.async_get_area(speaker_device.area_id)
    
class HouzzkitGetLiveContextIntent(intent.IntentHandler):
    intent_type = "HouzzkitGetLiveContext"
    description = (
        "Provides real-time information about the CURRENT state, value, or mode of devices, sensors, entities, or areas. "
        "Use this tool for: "
        "1. Answering questions about current conditions (e.g., 'Is the light on?'). "
        "2. As the first step in conditional actions (e.g., 'If there is someone in the bedroom, turn on the bedroom light'), checking if there's anyone present is required."
    )

    @property
    def slot_schema(self) -> dict | None:
        """Return a slot schema."""
        return None
    
    async def async_handle(self, intent_obj: intent.Intent) -> JsonObjectType: # type: ignore
        """Get the current state of exposed entities."""
        slots = self.async_validate_slots(intent_obj.slots)
        _LOGGER.info(f"HouzzkitGetLiveContext: slots={slots}")
        
        speaker_id: str = slots.get("_speaker_id", {}).get("value")
        
        hass = intent_obj.hass
        intent_obj.assistant
        if intent_obj.assistant is None:
            # Note this doesn't happen in practice since this tool won't be
            # exposed if no assistant is configured.
            return {"success": False, "error": "No assistant configured"}
        
        speaker_info: dict | None = None
        if speaker_id:
            # Query the speaker's area by its ID.
            speaker_area = find_speaker_area(hass, speaker_id)
            if speaker_area:
                speaker_info = {
                    "area_id": speaker_area.id,
                    "floor_id": speaker_area.floor_id,
                    "area_name": speaker_area.name,
                }
                _LOGGER.info(f"HouzzkitGetLiveContext: speaker_info={speaker_info}")

        exposed_entities = _get_exposed_entities(hass, intent_obj.assistant)
        if not exposed_entities["entities"]:
            return {"success": False, "error": "No devices available for operation. Please expose them in the Home Assistant voice assistant."}
        
        prompt = [
            "Live Context: An overview of the areas and the devices in this smart home:",
            yaml_util.dump(list(exposed_entities["entities"].values())),
        ]
        return {
            "success": True,
            "speaker": speaker_info,
            "result": "\n".join(prompt),
        }
