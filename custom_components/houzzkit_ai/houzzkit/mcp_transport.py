import logging
import anyio
import aiohttp
from mcp import types

from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.helpers import llm
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.components import conversation
from homeassistant.components.mcp_server.server import create_server
from homeassistant.components.mcp_server.session import Session, SessionManager
from homeassistant.exceptions import ConfigEntryAuthFailed

from ..const import DOMAIN
from . import get_entry_data
from .ws_transport import WsTransport

try:
    from mcp.shared.message import SessionMessage  # ha>=2025.10,mcp>=1.14.1
except (ImportError, ModuleNotFoundError):
    SessionMessage = None


_LOGGER = logging.getLogger(__name__)
ATTR_ENDPOINT = "mcp_endpoint"
ATTR_TRANSPORT = "mcp_transport"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up MCP Server from a config entry."""
    transport = get_entry_data(hass, entry, ATTR_TRANSPORT)
    _LOGGER.debug("Setup mcp endpoint entry: %s", [entry.title, transport])
    if not transport:
        transport = get_entry_data(hass, entry, ATTR_TRANSPORT, McpTransport(hass, entry))
    if new_endpoint := entry.data.get(ATTR_ENDPOINT):
        if new_endpoint != transport.endpoint:
            _LOGGER.info("Entry mcp endpoint changed: %s", new_endpoint)
            await transport.set_endpoint(new_endpoint)
        else:
            await transport.ensure_connected()
    transport.entries.setdefault(entry.entry_id, entry)
    return True


class McpTransport(WsTransport):
    """Handles WebSocket transport for MCP server."""
    _transport_type = "mcp"
    _mcp_server = None

    def init(self):
        entry_data = get_entry_data(self.hass, self.entry)
        self.session_manager = entry_data.setdefault("session_manager", SessionManager())
        self.endpoint = self.entry.data.get(ATTR_ENDPOINT)
        self.logger = _LOGGER

    async def _create_server(self, context: llm.LLMContext):
        """Create MCP server instance."""
        llm_api_id = self.entry.options.get(CONF_LLM_HASS_API) or llm.LLM_API_ASSIST
        mcp_api = await llm.async_get_api(self.hass, llm_api_id, context)
        tools = [tool.name for tool in mcp_api.tools]
        self.logger.info("MCP server tools: %s", tools)
        return await create_server(self.hass, llm_api_id, context)

    async def connect_to_client(self) -> bool:
        """Connect to WebSocket endpoint."""
        if not self.endpoint:
            self.logger.error("No endpoint configured in config entry")
            return False

        self.logger.debug("Websocket connect to client")
        try:
            context = llm.LLMContext(
                platform=DOMAIN,
                context=None,
                language="*",
                assistant=conversation.DOMAIN,
                device_id=None,
            )
            self._mcp_server = await self._create_server(context)
            self._mcp_server.version = "2.1.0"
            options = await self.hass.async_add_executor_job(self._mcp_server.create_initialization_options)

            await self._create_streams()

            async with self.session_manager.create(Session(self._recv_writer)) as session_id:
                await self._establish_websocket_connection(options)

        except Exception as err:
            self.logger.exception("Failed to connect to websocket at %s: %s", self.endpoint, err)
            raise

        return self.should_reconnect

    async def _establish_websocket_connection(self, options: dict):
        """Establish WebSocket connection and run server tasks."""
        self.logger.info("Connecting to: %s", self.endpoint)
        assert self.endpoint
        timeout = aiohttp.ClientTimeout(total=None, connect=60)
        async with aiohttp.ClientSession(timeout=timeout) as client_session:
            try:
                assert self.endpoint
                async with client_session.ws_connect(self.endpoint) as ws:
                    self._current_ws = ws
                    self._is_connected = True
                    self.update_activity_time()
                    self.reconnect_times = 0
                    async with anyio.create_task_group() as tg:
                        try:
                            tg.start_soon(self._handle_incoming_messages, tg.cancel_scope)
                            tg.start_soon(self._handle_outgoing_messages)
                            tg.start_soon(self._heartbeat_task)
                            # tg.start_soon(self._idle_monitor_task, tg.cancel_scope)
                            try:
                                await self._mcp_server.run(self._recv_reader, self._send_writer, options)
                            except Exception as err:
                                self.logger.error("Error in server run: %s", err)
                        except Exception as err:
                            self.logger.error("Error in server tasks: %s", err)
                            tg.cancel_scope.cancel()
                            raise
            except aiohttp.WSServerHandshakeError as err:
                self.logger.warning("WebSocket handshake failed: %s", err)
                if err.status == 401:
                    self.should_reconnect = False
                    self.logger.warning("WebSocket unauthorized, disable reconnect")
                    self.entry.async_start_reauth(self.hass)
                    raise ConfigEntryAuthFailed(
                        translation_domain=DOMAIN,
                        translation_key="houzzkit_auth_error",
                        translation_placeholders={"name": self.entry.title},
                    ) from err
            except Exception as err:
                self.logger.exception("WebSocket connection failed: %s", err)
                raise
            finally:
                self._is_connected = False

    async def _handle_outgoing_messages(self):
        """Handle outgoing messages to WebSocket."""
        try:
            async for session_message in self._send_reader:
                if SessionMessage is not None and isinstance(session_message, SessionMessage):
                    message = session_message.message
                else:
                    message = session_message
                self.logger.info("Send message: %s", message)
                await self._current_ws.send_str(message.model_dump_json(by_alias=True, exclude_none=True))
        except Exception as err:
            self.logger.error("Error writing to WebSocket: %s", str(err), exc_info=True)
        finally:
            self.logger.info("Websocket writer stopped")
            try:
                await self._current_ws.close()
            except Exception as err:
                self.logger.error("Error closing WebSocket: %s", err)

    async def _process_text_message(self, msg: aiohttp.WSMessage):
        """Process a text message from WebSocket."""
        try:
            json_data = msg.json()
            message = types.JSONRPCMessage.model_validate(json_data)
            self.logger.debug("Process incoming msg: %s", message)
            if SessionMessage:
                message = SessionMessage(message)
            await self._recv_writer.send(message)
        except Exception as err:
            self.logger.error("Invalid incoming msg: %s", msg, exc_info=True)

    async def async_remove_entry(self):
        transport = get_entry_data(self.hass, self.entry, ATTR_TRANSPORT)
        if transport:
            transport.entries.pop(self.entry.entry_id, None)
        if not transport.entries:
            get_entry_data(self.hass, self.entry, ATTR_TRANSPORT, pop=True)
            await transport.stop()
