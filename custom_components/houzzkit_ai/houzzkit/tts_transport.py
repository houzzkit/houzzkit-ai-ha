import logging
import anyio

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed

from . import Dict, EntryAuthFailedError, get_entry_data
from .ws_transport import WsTransport

_LOGGER = logging.getLogger(__name__)
ATTR_ENDPOINT = "tts_endpoint"
ATTR_TRANSPORT = "tts_transport"


def get_entry_transport(hass: HomeAssistant, entry: ConfigEntry) -> "TtsTransport":
    """Set up from a config entry."""
    endpoint: str | None = entry.data.get(ATTR_ENDPOINT)
    if not endpoint:
        raise EntryAuthFailedError(hass, entry)
    
    this_data: dict = get_entry_data(hass, entry)
    transport: TtsTransport | None = this_data.get(ATTR_TRANSPORT)
    if transport and transport.endpoint == endpoint and transport.available:
        return transport
    
    _LOGGER.info("Creating new TtsTransport for entry: %s %s", entry.entry_id, entry.title)
    transport = TtsTransport(hass, entry, endpoint, ATTR_ENDPOINT, _LOGGER)
    this_data[ATTR_TRANSPORT] = transport
    return transport


class TtsTransport(WsTransport):
    _transport_type = "tts"

    async def await_message(self, timeout: int = 60):
        """Wait response message"""
        try:
            with anyio.fail_after(timeout):
                async for data in self._recv_reader:
                    if isinstance(data, bytes):
                        yield data
                    elif data.state == "stop":
                        break
                    else:
                        self.logger.info("Received unknown message: %s", data)
        except TimeoutError:
            yield Dict(error="Response timeout")

    async def async_remove_entry(self):
        this_data = get_entry_data(self.hass, self.entry)
        transport = this_data.pop(ATTR_TRANSPORT, None)
        if transport:
            await transport.stop("Entry removed")
