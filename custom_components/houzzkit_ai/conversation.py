import json
import logging
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.components import conversation
from homeassistant.components.conversation import (
    DOMAIN as ENTITY_DOMAIN,
    ConversationEntity as BaseEntity,
    ConversationInput,
    ConversationResult,
    ChatLog,
)
from homeassistant.const import MATCH_ALL
from homeassistant.helpers import device_registry as dr
from homeassistant.config_entries import ConfigEntry

from .const import DOMAIN
from .houzzkit import get_entry_data, llm_transport

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    """Set up conversation entities."""
    async_add_entities([HouzzkitConversationEntity(hass, config_entry)])

class HouzzkitConversationEntity(BaseEntity):
    domain = ENTITY_DOMAIN

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.entity_id = f"{self.domain}.houzzkit_agent"
        self._attr_name = "Houzzkit AI 对话代理"
        self._attr_unique_id = f"{self.entry.entry_id}-{ENTITY_DOMAIN}"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name="Houzzkit AI",
            manufacturer="Houzzkit",
            entry_type=dr.DeviceEntryType.SERVICE,
        )


    @property
    def supported_languages(self):
        """Return a list of supported languages."""
        return MATCH_ALL

    async def _async_handle_message(
        self,
        user_input: ConversationInput,
        chat_log: ChatLog,
    ) -> ConversationResult:
        """Call the API."""
        transport = llm_transport.get_entry_transport(self.hass, self.entry)
        if not await transport.ensure_connected():
            raise HomeAssistantError("Failed to establish WebSocket connection for LLM")

        try:
            await chat_log.async_provide_llm_data(
                user_input.as_llm_context(DOMAIN),
                user_extra_system_prompt=user_input.extra_system_prompt,
            )
        except conversation.ConverseError as err:
            return err.as_conversation_result()

        await self._async_handle_chat_log(transport, user_input, chat_log)
        return conversation.async_get_result_from_chat_log(user_input, chat_log)

    async def _async_handle_chat_log(
        self,
        transport: llm_transport.LlmTransport,
        user_input: ConversationInput,
        chat_log: conversation.ChatLog,
    ):
        await transport.send_message(json.dumps({
            "type": "listen",
            "state": "detect",
            "text": user_input.text,
        }))
        async for content in chat_log.async_add_delta_content_stream(
            self.entity_id,
            transport.await_message() # type: ignore
        ):
            _LOGGER.info("LLM response: %s", content)
