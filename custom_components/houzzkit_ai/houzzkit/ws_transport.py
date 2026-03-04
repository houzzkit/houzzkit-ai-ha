import logging
import json
import time
import anyio
import asyncio
import aiohttp
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from homeassistant.core import HomeAssistant
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import ConfigEntryAuthFailed

from . import Dict

_LOGGER = logging.getLogger(__name__)

class WsTransport:
    """Handles WebSocket transport."""
    _transport_type = ""
    
    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, endpoint: str, attr_endpoint: str, logger=None):
        self.stop_event = asyncio.Event()
        self.endpoint = endpoint
        self.attr_endpoint = attr_endpoint
        self.reconnect_times = 0
        self.should_reconnect = True
        self._current_ws = None
        self._idle_timeout = 180
        self._last_activity_time = 0
        self._is_connected = False
        
        self.hass = hass
        self.entry = entry
        self.logger = logger or _LOGGER
        self._connection_lock = asyncio.Lock()
        
        self._recv_writer: MemoryObjectSendStream = None # type: ignore
        self._recv_reader: MemoryObjectReceiveStream = None # type: ignore
        self._send_writer: MemoryObjectSendStream = None # type: ignore
        self._send_reader: MemoryObjectReceiveStream = None # type: ignore
        
    @property
    def available(self):
        return not self.stop_event.is_set() and self.should_reconnect
        

    def init(self):
        pass

    def update_activity_time(self):
        self._last_activity_time = time.monotonic()

    def ws_log(self, msg, *args, **kwargs):
        lvl = logging.ERROR if self.reconnect_times >= 3 else logging.INFO
        self.logger.log(lvl, msg, *args, **kwargs)

    @property
    def is_connected(self):
        return self._is_connected and self._current_ws and not self._current_ws.closed
    
    def clear_endpoint_from_data(self):
        if self.entry.data.get(self.attr_endpoint, "") == self.endpoint:
            self.logger.info("Clearing endpoint from config entry data: %s %s", self.attr_endpoint, self.endpoint)
            self.hass.config_entries.async_update_entry(self.entry, data={
                **self.entry.data,
                self.attr_endpoint: "",
            })

    async def ensure_connected(self):
        """Ensure WebSocket is connected. Connect if not already connected."""
        if not self.should_reconnect:
            self.logger.info("Interrupted before ensure connected")
            return False
        
        if self.is_connected:
            self.update_activity_time()
            return True

        async with self._connection_lock:
            if self.is_connected:
                self.update_activity_time()
                return True

            if not self.endpoint:
                self.logger.error("No endpoint configured in config entry")
                return False

            self.logger.info("On-demand connecting to WebSocket: %s", self.endpoint)
            self.update_activity_time()
            
            task = self.entry.async_create_background_task(
                self.hass,
                self.run_connection_loop(),
                f"transport_loop:{self._transport_type}"
            )

            # Wait for connection to be established
            for _ in range(150):
                if not self.should_reconnect:
                    self.logger.info("Interrupted wait connected")
                    return False
                if self.is_connected:
                    return True
                await asyncio.sleep(0.1)

            self.logger.error("Timed out waiting for WebSocket connection")
            return False
        
    async def _create_streams(self):
        """Create memory object streams for communication."""
        self._recv_writer, self._recv_reader = anyio.create_memory_object_stream(0)
        self._send_writer, self._send_reader = anyio.create_memory_object_stream(0)

    async def run_connection_loop(self) -> None:
        """Run the connection loop with automatic reconnection."""
        while self.should_reconnect:
            try:
                if not await self.connect_to_client():
                    break
            except ConfigEntryAuthFailed:
                raise
            except Exception as err:
                self.logger.warning("Websocket disconnected or failed: %s", err)
            finally:
                self._is_connected = False
            if self.should_reconnect:
                seconds = max(min(60, self.reconnect_times * 5), 3)
                self.logger.info("Websocket retry after %s seconds, times: %s", seconds, self.reconnect_times)
                self.reconnect_times += 1
                if seconds > 0:
                    await asyncio.sleep(seconds)

    async def connect_to_client(self) -> bool:
        """Connect to WebSocket endpoint."""
        if not self.endpoint:
            self.logger.error("No endpoint configured in config entry")
            return False
        
        if not self.should_reconnect:
            # 提前终止
            self.logger.info("Interrupted before connect")
            return False

        try:
            await self._create_streams()
            await self._establish_websocket_connection()
        except Exception as err:
            self.logger.exception("Failed to connect to websocket at %s: %s", self.endpoint, err)
            raise

        return self.should_reconnect

    async def _establish_websocket_connection(self):
        """Establish WebSocket connection and run server tasks."""
        self.logger.info("Connecting to: %s", self.endpoint)
        assert self.endpoint
        timeout = aiohttp.ClientTimeout(total=None, connect=60)
        async with aiohttp.ClientSession(timeout=timeout) as client_session:
            try:
                if not self.should_reconnect:
                    # 提前终止
                    self.logger.info("Interrupted after session created")
                    return

                assert self.endpoint
                async with client_session.ws_connect(self.endpoint) as ws:
                    if not self.should_reconnect:
                        # 提前终止
                        self.logger.info("Interrupted after websocket connected")
                        return
                    self._current_ws = ws
                    self._is_connected = True
                    self.update_activity_time()
                    self.reconnect_times = 0
                    async with anyio.create_task_group() as tg:
                        try:
                            tg.start_soon(self._handle_incoming_messages, tg.cancel_scope)
                            tg.start_soon(self._handle_outgoing_messages)
                            tg.start_soon(self._heartbeat_task)
                            tg.start_soon(self._idle_monitor_task, tg.cancel_scope)
                        except Exception as err:
                            self.logger.error("Error in server tasks: %s", err)
                            tg.cancel_scope.cancel()
                            raise
                    self.logger.info("WebSocket connection tasks completed.")
            except aiohttp.WSServerHandshakeError as err:
                self.logger.warning("WebSocket handshake failed: %s", err)
                if err.status == 401:
                    self.should_reconnect = False
                    self.clear_endpoint_from_data()
                    self.logger.warning("WebSocket unauthorized, disable reconnect")
            except Exception as err:
                self.logger.exception("WebSocket connection failed: %s", err)
                raise
            finally:
                self.logger.info("WebSocket connection stop over.")
                self._is_connected = False

    async def _idle_monitor_task(self, cancel_scope: anyio.CancelScope):
        """Monitor idle time and close connection if idle too long."""
        try:
            while self.should_reconnect and self._current_ws and not self._current_ws.closed:
                await asyncio.sleep(30)  # Check every 30 seconds
                idle_seconds = time.monotonic() - self._last_activity_time
                if idle_seconds >= self._idle_timeout:
                    self.logger.info(
                        "WebSocket idle for %.0f seconds (>%ds), closing to save resources",
                        idle_seconds, self._idle_timeout
                    )
                    self.should_reconnect = False
                    cancel_scope.cancel()
                    return
        except Exception as err:
            self.logger.error("Idle monitor error: %s", err)

    async def _handle_incoming_messages(self, cancel_scope: anyio.CancelScope):
        """Handle incoming WebSocket messages."""
        assert self._current_ws, "WebSocket connection not established"
        try:
            async for msg in self._current_ws:
                self.update_activity_time()
                if msg.type == aiohttp.WSMsgType.TEXT:
                    await self._process_text_message(msg)
                elif msg.type == aiohttp.WSMsgType.BINARY:
                    await self._recv_writer.send(msg.data)
                elif msg.type == aiohttp.WSMsgType.CLOSE:
                    self.logger.error("WebSocket closed: %s", msg.extra)
                    break
                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self.logger.error("WebSocket error: %s", msg.data)
                    break
        except Exception as err:
            self.logger.error("Error reading WebSocket messages: %s", err)
            raise
        finally:
            self.ws_log("WebSocket connection stopped. Final close code: %s", self._current_ws.close_code)
            if self._current_ws.close_code == 1008:
                # 被顶号后，禁止重连
                self.should_reconnect = False
            cancel_scope.cancel()

    async def send_message(self, message):
        """Send a message to the WebSocket server."""
        self.update_activity_time()
        if not self._send_writer:
            self.logger.warning("Cannot send message, send writer is not available")
            return
        await self._send_writer.send(message)

    async def send_hello(self):
        await self.send_message({
            "type": "hello",
            "version": 1,
            "transport": "websocket",
            "audio_params": {
                "format": "opus",
                "sample_rate": 16000,
                "channels": 1,
                "frame_duration": 60,
            }
        })

    async def _handle_outgoing_messages(self):
        """Handle outgoing messages to WebSocket."""
        assert self._current_ws, "WebSocket connection not established"
        try:
            async for message in self._send_reader:
                if isinstance(message, dict):
                    message = json.dumps(message, ensure_ascii=False)
                if isinstance(message, str):
                    self.logger.info("Send message: %s", message)
                    await self._current_ws.send_str(message)
                else:
                    await self._current_ws.send_bytes(message)
        except Exception as err:
            self.logger.error("Error writing to WebSocket: %s", err)
        finally:
            self.logger.info("Websocket writer stopped")
            try:
                if self._current_ws and not self._current_ws.closed:
                    await self._current_ws.close()
            except Exception as err:
                self.logger.error("Error closing WebSocket: %s", err)

    async def _process_text_message(self, msg: aiohttp.WSMessage):
        """Process a text message from WebSocket."""
        try:
            if msg.data[0:2] == '"{':
                json_data = Dict(json.loads(msg.json()))
            else:
                json_data = Dict(msg.json())
            self.logger.debug("Process incoming msg: %s", json_data)
            await self._recv_writer.send(json_data)
        except Exception as err:
            self.logger.error("Invalid incoming msg: %s", msg)

    async def await_message(self, timeout: int = 120):
        """Wait response message"""
        try:
            with anyio.fail_after(timeout):
                async for data in self._recv_reader:
                    yield data
        except TimeoutError:
            yield Dict(error="Response timeout")

    async def _heartbeat_task(self):
        """Send periodic heartbeat pings."""
        try:
            while self.should_reconnect and self._current_ws and not self._current_ws.closed:
                await asyncio.sleep(55)
                self.logger.debug("heartbeat ping for %s", self.endpoint)
                await self._current_ws.ping()
        except Exception as err:
            self.ws_log("heartbeat ping failed: %s", err)

    async def stop(self, reason: str = ""):
        if self.stop_event.is_set():
            return
        self.stop_event.set()
        
        self.logger.info("Stop begin, reason: '%s'", reason)
        self.should_reconnect = False
        self._is_connected = False
        self.reconnect_times = 0

        if self._current_ws and not self._current_ws.closed:
            self.logger.info("Closing websocket")
            await self._current_ws.close()
        for stream in (self._recv_writer, self._recv_reader, self._send_writer, self._send_reader):
            if stream:
                await stream.aclose()
        self.logger.info("Stop end")