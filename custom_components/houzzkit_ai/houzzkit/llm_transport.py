import logging
import anyio

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from . import Dict, get_entry_data
from .ws_transport import WsTransport

_LOGGER = logging.getLogger(__name__)
ATTR_ENDPOINT = "llm_endpoint"
ATTR_TRANSPORT = "llm_transport"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up from a config entry."""
    this_data = get_entry_data(hass, entry)
    transport = this_data.get(ATTR_TRANSPORT)
    if not transport:
        transport = this_data.setdefault(ATTR_TRANSPORT, LlmTransport(hass, entry))
    transport.entries.setdefault(entry.entry_id, entry)
    return transport


class LlmTransport(WsTransport):
    _transport_type = "llm"

    def init(self):
        self.endpoint = self.entry.data.get(ATTR_ENDPOINT)
        self.logger = _LOGGER

    async def await_message(self, timeout: int = 180):
        """Wait response message"""
        content = ""
        try:
            with anyio.fail_after(timeout):
                async for data in self._recv_reader:
                    if data.state == "end":
                        break
                    if data.type != "text":
                        continue
                    if data.state == "start":
                        content = ""
                    if data.state == "sentence_end" and isinstance(data.data, str):
                        content += data.data
                yield Dict(role="assistant", content=content)
        except TimeoutError:
            _LOGGER.error("response timeout")
            yield Dict(error="Response timeout")

    async def async_remove_entry(self):
        this_data = get_entry_data(self.hass, self.entry)
        transport = this_data.get(ATTR_TRANSPORT)
        if transport:
            transport.entries.pop(self.entry.entry_id, None)
        if not transport.entries:
            this_data.pop(ATTR_TRANSPORT, None)
            await transport.stop()
