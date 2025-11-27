import logging

from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent

from .intent_adjust_attribute import AdjustDeviceAttributeIntent
from .intent_live_context import HouzzkitGetLiveContextIntent
from .intent_set_mode import SetDeviceModeIntent
from .intent_turn import TurnDeviceOffIntent, TurnDeviceOnIntent

_LOGGER = logging.getLogger(__name__)


async def async_setup_intents(hass: HomeAssistant):
    """Set up the intents."""
    _LOGGER.info("Register houzzkit-ai intents begin")
    intent.async_register(hass, HouzzkitGetLiveContextIntent())
    intent.async_register(hass, TurnDeviceOnIntent())
    intent.async_register(hass, TurnDeviceOffIntent())
    intent.async_register(hass, SetDeviceModeIntent())
    intent.async_register(hass, AdjustDeviceAttributeIntent())
    _LOGGER.info("Register houzzkit-ai intents end")
    
