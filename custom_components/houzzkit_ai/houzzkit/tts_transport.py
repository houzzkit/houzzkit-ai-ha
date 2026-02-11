import logging
import anyio

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from . import Dict, get_entry_data
from .ws_transport import WsTransport

_LOGGER = logging.getLogger(__name__)
ATTR_ENDPOINT = "tts_endpoint"
ATTR_TRANSPORT = "tts_transport"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up from a config entry."""
    this_data = get_entry_data(hass, entry)
    transport = this_data.get(ATTR_TRANSPORT)
    if not transport:
        transport = this_data.setdefault(ATTR_TRANSPORT, TtsTransport(hass, entry))
    transport.entries.setdefault(entry.entry_id, entry)
    return transport


class TtsTransport(WsTransport):
    _transport_type = "tts"
    _recv_binary = True

    def init(self):
        self.endpoint = self.entry.data.get(ATTR_ENDPOINT)
        self.logger = _LOGGER

    async def await_message(self, timeout: int = 60):
        """Wait response message"""
        try:
            with anyio.fail_after(timeout):
                async for data in self._recv_reader:
                    if isinstance(data, bytes):
                        yield data
                    elif data.state == "stop":
                        break
        except TimeoutError:
            yield Dict(error="Response timeout")

    async def async_remove_entry(self):
        transport = get_entry_data(self.hass, self.entry, ATTR_TRANSPORT)
        if transport:
            transport.entries.pop(self.entry.entry_id, None)
        if not transport.entries:
            get_entry_data(self.hass, self.entry, ATTR_TRANSPORT, pop=True)
            await transport.stop()
