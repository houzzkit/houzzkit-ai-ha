import logging
from homeassistant.core import HomeAssistant
from homeassistant.components.tts.const import DOMAIN as ENTITY_DOMAIN
from homeassistant.components.tts import (
    TextToSpeechEntity as BaseEntity,
    TtsAudioType,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.config_entries import ConfigEntry
import opuslib_next as opuslib

from .const import DOMAIN
from .houzzkit import tts_transport
from .houzzkit.audio import async_convert_audio

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    """Set up entities."""
    async_add_entities([HouzzkitTtsEntity(hass, config_entry)])

class HouzzkitTtsEntity(BaseEntity):
    domain = ENTITY_DOMAIN
    opus_channels = 1
    opus_sample_rate = 16000
    opus_frame_duration = 60
    opus_frame_samples = int(opus_sample_rate * opus_frame_duration / 1000)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.entity_id = f"{self.domain}.houzzkit_speech"
        self._attr_name = "Houzzkit AI 语音合成"
        self._attr_unique_id = f"{self.entry.entry_id}-{ENTITY_DOMAIN}"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name="Houzzkit AI",
            manufacturer="Houzzkit",
            entry_type=dr.DeviceEntryType.SERVICE,
        )
        self._attr_default_language = "zh-Hans"
        self._attr_supported_languages = ["en", "zh", "zh-Hans"]
        self._attr_supported_options = []
        self._attr_extra_state_attributes = {}
    
    @property
    def transport(self) -> tts_transport.TtsTransport:
        return tts_transport.get_entry_transport(self.hass, self.entry)
    
    async def async_added_to_hass(self):
        await super().async_added_to_hass()
    

    async def async_get_tts_audio(
        self, message: str, language: str, options: dict
    ) -> TtsAudioType:
        if not await self.transport.ensure_connected():
            _LOGGER.error("Failed to establish WebSocket connection for TTS")
            return None, None

        format = options.get("audio_format") or "mp3"
        await self.transport.send_message({
            "type": "tts",
            "state": "detect",
            "text": message,
        })
        async def data_gen():
            decoder = opuslib.Decoder(self.opus_sample_rate, self.opus_channels)
            async for resp in self.transport.await_message():
                if isinstance(resp, bytes):
                    try:
                        resp = decoder.decode(resp, self.opus_frame_samples)
                    except Exception as e:
                        _LOGGER.error("Decode opus failed: %s", e)
                    _LOGGER.info("Received bytes: %s %s", len(resp), resp.hex()[0:64])
                    yield resp
                else:
                    if getattr(resp, "error", None):
                        raise RuntimeError(resp.error)
                    _LOGGER.info("Received response: %s", resp)
        audio = b""
        converting = async_convert_audio(
            self.hass, data_gen(), "s16le",
            to_extension=format,
            input_params=[
                "-ar", str(self.opus_sample_rate),
                "-ac", str(self.opus_channels),
            ],
        )
        async for chunk in converting:
            audio += chunk
        return format, audio
