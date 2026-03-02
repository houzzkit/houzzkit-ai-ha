import json
import logging
from ..const import DOMAIN
from homeassistant.helpers import entity_registry as er, instance_id

LOGGER = logging.getLogger(__name__)


class Dict(dict):
    def __getattr__(self, item):
        value = self.get(item)
        return Dict(value) if isinstance(value, dict) else value

    def __setattr__(self, key, value):
        self[key] = Dict(value) if isinstance(value, dict) else value

    def to_json(self, **kwargs):
        return json.dumps(self, **kwargs)


async def get_haid(hass):
    return await instance_id.async_get(hass)

def get_entry_data(hass, entry, field=None, set_default=None, pop=False):
    data = hass.data.setdefault(DOMAIN, {})
    if field != "mcp_transport":
        config_type = entry.data.get("config_type")
        if config_type == "assist":
            data = entry.runtime_data
        
    if field and pop:
        return data.pop(field, None)
    if field and set_default is not None:
        return data.setdefault(field, set_default)
    if field:
        return data.get(field)
    return data

def get_houzzkit_ai_data(hass, field=None, set_default=None, pop=False):
    data = hass.data.setdefault(DOMAIN, {})
    if field and pop:
        return data.pop(field, None)
    if field and set_default is not None:
        return data.setdefault(field, set_default)
    if field:
        return data.get(field)
    return data

def get_config_entry(hass, speak_id=None, mac=None):
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = Dict(entry.data)
        if speak_id and speak_id == data.speak_id:
            return entry
        if mac and mac == data.mac:
            return entry
    return None

def get_entities(hass, speak_id=None, mac=None):
    entry = get_config_entry(hass, speak_id, mac)
    if not entry:
        return []
    return er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)

def get_entities_ids(hass, speak_id=None, mac=None):
    return [
        entity.entity_id
        for entity in get_entities(hass, speak_id, mac)
    ]
