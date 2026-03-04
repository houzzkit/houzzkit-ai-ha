import logging
import anyio

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry

from . import Dict, EntryAuthFailedError, get_entry_data
from .ws_transport import WsTransport

_LOGGER = logging.getLogger(__name__)
ATTR_ENDPOINT = "stt_endpoint"
ATTR_TRANSPORT = "stt_transport"


def get_entry_transport(hass: HomeAssistant, entry: ConfigEntry) -> "SttTransport":
    """Set up from a config entry."""
    endpoint: str | None = entry.data.get(ATTR_ENDPOINT)
    if not endpoint:
        raise EntryAuthFailedError(hass, entry)
    
    this_data: dict = get_entry_data(hass, entry)
    transport: SttTransport | None = this_data.get(ATTR_TRANSPORT)
    if transport and transport.endpoint == endpoint and transport.available:
        return transport
    
    _LOGGER.info("Creating new SttTransport for entry: %s %s", entry.entry_id, entry.title)
    transport = SttTransport(hass, entry, endpoint, ATTR_ENDPOINT, _LOGGER)
    this_data[ATTR_TRANSPORT] = transport
    return transport


class SttTransport(WsTransport):
    _transport_type = "stt"

    async def await_message(self, timeout: int = 60):
        """Wait response message"""
        try:
            with anyio.fail_after(timeout):
                async for data in self._recv_reader:
                    if data.type in ["stt", "tts"]:
                        yield data
                        break
        except RuntimeError as exc:
            self.logger.info(str(exc), exc_info=True)
        except TimeoutError:
            yield Dict(error="Response timeout")

    async def async_remove_entry(self):
        entry = self.entry
        this_data: dict = get_entry_data(self.hass, entry)
        transport: SttTransport | None = this_data.pop(ATTR_TRANSPORT, None)
        self.logger.info("Remove entry from STT transport: title=%s id=%s", entry.title, entry.entry_id)
        if transport:
            await transport.stop("Remove entry")
