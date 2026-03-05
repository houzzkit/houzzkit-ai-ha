import logging
import anyio

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from . import Dict, EntryAuthFailedError, get_entry_data
from .ws_transport import WsTransport

_LOGGER = logging.getLogger(__name__)
ATTR_ENDPOINT = "llm_endpoint"
ATTR_TRANSPORT = "llm_transport"


def get_entry_transport(hass: HomeAssistant, entry: ConfigEntry) -> "LlmTransport":
    """Set up from a config entry."""
    endpoint: str | None = entry.data.get(ATTR_ENDPOINT)
    if not endpoint:
        raise EntryAuthFailedError(hass, entry)
    
    this_data: dict = get_entry_data(hass, entry)
    transport: LlmTransport | None = this_data.get(ATTR_TRANSPORT)
    if transport and transport.endpoint == endpoint and transport.available:
        return transport
    
    _LOGGER.info("Creating new LlmTransport for entry: %s %s", entry.entry_id, entry.title)
    transport = LlmTransport(hass, entry, endpoint, ATTR_ENDPOINT, _LOGGER)
    this_data[ATTR_TRANSPORT] = transport
    return transport

class LlmTransport(WsTransport):
    _transport_type = "llm"

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
        entry = self.entry
        this_data: dict = get_entry_data(self.hass, entry)
        transport: LlmTransport | None = this_data.pop(ATTR_TRANSPORT, None)
        self.logger.info("Remove entry from LLM transport: title=%s id=%s", entry.title, entry.entry_id)
        if transport:
            await transport.stop("Remove entry")
