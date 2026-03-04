"""Support for esphome devices."""

from __future__ import annotations

from aioesphomeapi import APIClient

from homeassistant.components import zeroconf
from homeassistant.components.bluetooth import async_remove_scanner
from homeassistant.const import (
    Platform,
    CONF_HOST,
    CONF_PASSWORD,
    CONF_PORT,
    __version__ as ha_version,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.issue_registry import async_delete_issue
from homeassistant.helpers.typing import ConfigType

from . import dashboard, ffmpeg_proxy
from .const import CONF_BLUETOOTH_MAC_ADDRESS, CONF_NOISE_PSK, DOMAIN
from .domain_data import DomainData
from .entry_data import ESPHomeConfigEntry, RuntimeEntryData
from .manager import DEVICE_CONFLICT_ISSUE_FORMAT, ESPHomeManager, cleanup_instance

from .houzzkit import LOGGER, Dict, get_entry_data, mcp_transport
from .houzzkit.http import async_setup_https

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

CLIENT_INFO = f"Home Assistant {ha_version}"


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the esphome component."""
    ffmpeg_proxy.async_setup(hass)
    await dashboard.async_setup(hass)

    await async_setup_https(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ESPHomeConfigEntry) -> bool:
    """Set up the esphome component."""
    LOGGER.info("Setup entry: %s", [entry.title, entry.entry_id])
    config_type = entry.data.get("config_type")
    if config_type == "assist":
        PLATFORMS = set()
        entry.runtime_data = Dict(loaded_platforms=PLATFORMS)
        if entry.data.get("llm_endpoint"):
            PLATFORMS.add(Platform.CONVERSATION)
        if entry.data.get("stt_endpoint"):
            PLATFORMS.add(Platform.STT)
        if entry.data.get("tts_endpoint"):
            PLATFORMS.add(Platform.TTS)
        await mcp_transport.async_setup_entry(hass, entry)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
        return True

    host: str = entry.data[CONF_HOST]
    port: int = entry.data[CONF_PORT]
    password: str | None = entry.data[CONF_PASSWORD]
    noise_psk: str | None = entry.data.get(CONF_NOISE_PSK)

    zeroconf_instance = await zeroconf.async_get_instance(hass)

    cli = APIClient(
        host,
        port,
        password,
        client_info=CLIENT_INFO,
        zeroconf_instance=zeroconf_instance,
        noise_psk=noise_psk,
        timezone=hass.config.time_zone,
    )

    domain_data = DomainData.get(hass)
    entry_data = RuntimeEntryData(
        client=cli,
        entry_id=entry.entry_id,
        title=entry.title,
        store=domain_data.get_or_create_store(hass, entry),
        original_options=dict(entry.options),
    )
    entry.runtime_data = entry_data

    manager = ESPHomeManager(
        hass, entry, host, password, cli, zeroconf_instance, domain_data
    )
    await manager.async_start()

    await mcp_transport.async_setup_entry(hass, entry)
    return True


async def async_reload_entry(hass: HomeAssistant, entry: ESPHomeConfigEntry):
    """Handle update."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ESPHomeConfigEntry) -> bool:
    """Unload an esphome config entry."""
    this_data = get_entry_data(hass, entry)
    for k in ["llm_transport", "stt_transport", "tts_transport"]:
        if transport := this_data.get(k):
            await transport.async_remove_entry()

    entry_data = await cleanup_instance(entry)
    return await hass.config_entries.async_unload_platforms(
        entry, entry_data.loaded_platforms
    )


async def async_remove_entry(hass: HomeAssistant, entry: ESPHomeConfigEntry) -> None:
    """Remove an esphome config entry."""
    if bluetooth_mac_address := entry.data.get(CONF_BLUETOOTH_MAC_ADDRESS):
        async_remove_scanner(hass, bluetooth_mac_address.upper())
    async_delete_issue(
        hass, DOMAIN, DEVICE_CONFLICT_ISSUE_FORMAT.format(entry.entry_id)
    )
    await DomainData.get(hass).get_or_create_store(hass, entry).async_remove()

    if transport := get_entry_data(hass, entry, "mcp_transport"):
        await transport.async_remove_entry(entry)
