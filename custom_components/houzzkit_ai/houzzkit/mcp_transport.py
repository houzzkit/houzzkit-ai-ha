import logging
import anyio
import asyncio
import aiohttp
from mcp import types
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from homeassistant.core import HomeAssistant
from homeassistant.const import CONF_LLM_HASS_API
from homeassistant.helpers import llm
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.components import conversation
from homeassistant.components.mcp_server.server import create_server
from homeassistant.components.mcp_server.session import Session, SessionManager

from ..const import DOMAIN

try:
    from mcp.shared.message import SessionMessage  # ha>=2025.10,mcp>=1.14.1
except (ImportError, ModuleNotFoundError):
    SessionMessage = None


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry):
    """Set up MCP Server from a config entry."""
    entry_data = await async_remove_entry(hass, entry)
    transport = McpTransport(hass, entry)
    hass.async_create_task(transport.run_connection_loop())
    entry_data["transport"] = transport

    for ent in hass.config_entries.async_loaded_entries(DOMAIN):
        endpoint = ent.data.get("mcp_endpoint")
        transport = hass.data.setdefault(DOMAIN, {}).setdefault(ent.entry_id, {}).get("transport")
        if not endpoint or not transport:
            continue
        if endpoint == transport.endpoint:
            continue
        if ent.state not in [ConfigEntryState.LOADED, ConfigEntryState.FAILED_UNLOAD]:
            continue
        _LOGGER.info("Entry mcp endpoint changed: %s", endpoint)
        transport.set_endpoint(endpoint)
        hass.config_entries.async_update_entry(ent, data={
            **ent.data,
            "mcp_endpoint": endpoint,
        })

    return True

async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry):
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    if transport := entry_data.pop("transport", None):
        await transport.stop()
    return entry_data


class McpTransport:
    """Handles WebSocket transport for MCP server."""
    endpoint = None
    reconnect_times = 0
    should_reconnect = True
    _mcp_server = None
    _current_ws = None
    _recv_writer: MemoryObjectSendStream = None
    _recv_reader: MemoryObjectReceiveStream = None
    _send_writer: MemoryObjectSendStream = None
    _send_reader: MemoryObjectReceiveStream = None

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
        self.session_manager = entry_data.setdefault("session_manager", SessionManager())
        self.endpoint = entry.data.get("mcp_endpoint")

    def set_endpoint(self, endpoint):
        self.should_reconnect = False
        self.endpoint = endpoint
        self.reconnect_times = 0
        self.should_reconnect = True

    async def _create_server(self, context: llm.LLMContext):
        """Create MCP server instance."""
        llm_api_id = self.entry.data.get(CONF_LLM_HASS_API) or llm.LLM_API_ASSIST
        return await create_server(self.hass, llm_api_id, context)

    async def _create_streams(self):
        """Create memory object streams for communication."""
        self._recv_writer, self._recv_reader = anyio.create_memory_object_stream(0)
        self._send_writer, self._send_reader = anyio.create_memory_object_stream(0)

    async def run_connection_loop(self) -> None:
        """Run the connection loop with automatic reconnection."""
        while self.should_reconnect:
            try:
                _LOGGER.debug("mcp websocket loop")
                if not await self.connect_to_client():
                    break
            except Exception as err:
                _LOGGER.warning("mcp websocket disconnected or failed: %s", err)
            if self.should_reconnect:
                seconds = max(min(60, self.reconnect_times * 5), 1)
                _LOGGER.info("mcp websocket retry after %s seconds", seconds)
                self.reconnect_times += 1
                if seconds > 0:
                    await asyncio.sleep(seconds)

    async def connect_to_client(self) -> bool:
        """Connect to external WebSocket endpoint as MCP server."""
        if not self.endpoint:
            _LOGGER.error("No client endpoint configured in config entry")
            return False

        _LOGGER.debug("mcp websocket connect_to_client")
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
            _LOGGER.exception("mcp Failed to connect to client WebSocket at %s: %s", self.endpoint, err)
            raise

        return self.should_reconnect

    async def _establish_websocket_connection(self, options: dict):
        """Establish WebSocket connection and run server tasks."""
        _LOGGER.info("mcp Connecting to MCP client at: %s", self.endpoint)
        timeout = aiohttp.ClientTimeout(total=60)
        assert self.endpoint
        async with aiohttp.ClientSession(timeout=timeout) as client_session:
            try:
                assert self.endpoint
                async with client_session.ws_connect(self.endpoint) as ws:
                    self._current_ws = ws
                    self.reconnect_times = 0
                    async with anyio.create_task_group() as tg:
                        try:
                            tg.start_soon(self._handle_websocket_messages)
                            tg.start_soon(self._handle_outgoing_messages)
                            tg.start_soon(self._heartbeat_task)
                            try:
                                await self._mcp_server.run(self._recv_reader, self._send_writer, options)
                            except Exception as err:
                                _LOGGER.error("mcp Error in server run: %s", err)
                        except Exception as err:
                            _LOGGER.error("mcp Error in server tasks: %s", err)
                            tg.cancel_scope.cancel()
                            raise
            except aiohttp.WSServerHandshakeError as err:
                _LOGGER.warning("mcp WebSocket handshake failed: %s", err)
                if err.status == 401:
                    self.should_reconnect = False
                    _LOGGER.warning("mcp WebSocket unauthorized, disable reconnect")
            except Exception as err:
                _LOGGER.exception("mcp WebSocket connection failed: %s", err)
                raise

    async def _handle_websocket_messages(self):
        """Handle incoming WebSocket messages."""
        try:
            async for msg in self._current_ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._process_text_message(msg)
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    _LOGGER.error("mcp WebSocket closed: %s", msg.extra)
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    _LOGGER.error("mcp WebSocket error: %s", msg.data)
                    break
        except Exception as err:
            _LOGGER.error("mcp Error reading WebSocket messages: %s", err)
            raise
        finally:
            _LOGGER.error("WebSocket reader stopped")
            raise StopAsyncIteration

    async def _handle_outgoing_messages(self):
        """Handle outgoing messages to WebSocket."""
        try:
            async for session_message in self._send_reader:
                if SessionMessage is not None and isinstance(session_message, SessionMessage):
                    message = session_message.message
                else:
                    message = session_message
                _LOGGER.info("mcp writer: %s", message)
                await self._current_ws.send_str(message.model_dump_json(by_alias=True, exclude_none=True))
        except Exception as err:
            _LOGGER.error("mcp Error writing to WebSocket: %s", err)
        finally:
            _LOGGER.info("WebSocket writer stopped")
            try:
                await self._current_ws.close()
            except Exception as err:
                _LOGGER.error("mcp Error closing WebSocket: %s", err)

    async def _process_text_message(self, msg: aiohttp.WSMessage):
        """Process a text message from WebSocket."""
        try:
            json_data = msg.json()
            message = types.JSONRPCMessage.model_validate(json_data)
            _LOGGER.debug("mcp reader: %s", message)
            if SessionMessage:
                message = SessionMessage(message)
            await self._recv_writer.send(message)
        except Exception as err:
            _LOGGER.error("mcp Invalid message from client: %s", err)

    async def _heartbeat_task(self):
        """Send periodic heartbeat pings."""
        try:
            while self.should_reconnect and self._current_ws and not self._current_ws.closed:
                await asyncio.sleep(55)
                _LOGGER.debug("mcp heartbeat")
                await self._current_ws.ping()
        except Exception as err:
            _LOGGER.error("mcp heartbeat ping failed: %s", err)

    async def stop(self):
        self.should_reconnect = False
        self.reconnect_times = 0

        for stream in (self._recv_writer, self._recv_reader, self._send_writer, self._send_reader):
            if stream:
                await stream.aclose()
        if self._current_ws:
            await self._current_ws.close()
