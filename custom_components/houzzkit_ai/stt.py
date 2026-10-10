import logging
from homeassistant.core import HomeAssistant
from homeassistant.components.stt import (
    DOMAIN as ENTITY_DOMAIN,
    SpeechToTextEntity as BaseEntity,
    AudioCodecs,
    AudioFormats,
    AudioChannels,
    AudioBitRates,
    AudioSampleRates,
    SpeechMetadata,
    SpeechResult,
    SpeechResultState,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.config_entries import ConfigEntry
from collections.abc import AsyncIterable
import opuslib_next as opuslib

from .const import DOMAIN
from .houzzkit import stt_transport
from .houzzkit.audio import wav_to_opus

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry, async_add_entities):
    """Set up entities."""
    async_add_entities([HouzzkitSttEntity(hass, config_entry)])

class HouzzkitSttEntity(BaseEntity):
    domain = ENTITY_DOMAIN
    opus_channels = 1
    opus_sample_rate = 16000
    opus_frame_duration = 60
    opus_frame_samples = int(opus_sample_rate * opus_frame_duration / 1000)

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry):
        self.hass = hass
        self.entry = entry
        self.entity_id = f"{self.domain}.houzzkit_asr"
        self._attr_name = "Houzzkit AI 语音识别"
        self._attr_unique_id = f"{self.entry.entry_id}-{ENTITY_DOMAIN}"
        self._attr_device_info = dr.DeviceInfo(
            identifiers={(DOMAIN, self.entry.entry_id)},
            name="Houzzkit AI",
            manufacturer="Houzzkit",
            entry_type=dr.DeviceEntryType.SERVICE,
        )
        self._attr_supported_languages = ["en", "zh", "zh-Hans"]
        self._attr_supported_codecs = [AudioCodecs.PCM, AudioCodecs.OPUS]
        self._attr_supported_formats = [AudioFormats.WAV, AudioFormats.OGG]
        self._attr_supported_channels = [x for x in AudioChannels]
        self._attr_supported_bit_rates = [x for x in AudioBitRates]
        self._attr_supported_sample_rates = [x for x in AudioSampleRates]
        self.opus_encoder = opuslib.Encoder(self.opus_sample_rate, self.opus_channels, opuslib.APPLICATION_VOIP)

    @property
    def supported_languages(self):
        return self._attr_supported_languages

    @property
    def supported_codecs(self):
        return self._attr_supported_codecs

    @property
    def supported_formats(self):
        return self._attr_supported_formats

    @property
    def supported_channels(self):
        return self._attr_supported_channels

    @property
    def supported_bit_rates(self):
        return self._attr_supported_bit_rates

    @property
    def supported_sample_rates(self):
        return self._attr_supported_sample_rates

    async def async_added_to_hass(self):
        await super().async_added_to_hass()

    async def async_process_audio_stream(
        self, metadata: SpeechMetadata, stream: AsyncIterable[bytes]
    ) -> SpeechResult:
        _LOGGER.info(
            "Processing audio stream: language=%s, format=%s, codec=%s, bit_rate=%s, sample_rate=%s",
            metadata.language,
            metadata.format,
            metadata.codec,
            metadata.bit_rate,
            metadata.sample_rate,
        )
        transport = stt_transport.get_entry_transport(self.hass, self.entry)
        if not await transport.ensure_connected():
            _LOGGER.error("Failed to establish WebSocket connection for STT")
            return SpeechResult(None, SpeechResultState.ERROR)
        await transport.send_hello()

        await transport.send_message({"type": "listen", "state": "start"})
        async for chunk in wav_to_opus(stream):
            await transport.send_message(chunk)
            _LOGGER.info("Sent audio data, size: %s", len(chunk))
        await transport.send_message({"type": "listen", "state": "stop"})

        text = None
        async for resp in transport.await_message(60):
            _LOGGER.info("Received response: %s", resp)
            if resp.type in ["stt", "tts"]:
                text = resp.text
        return SpeechResult(text, SpeechResultState.SUCCESS) # type: ignore
