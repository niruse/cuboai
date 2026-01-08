from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

from .api.cuboai_functions import set_token_paths
from .const import DOMAIN

PLATFORMS: list[Platform] = [Platform.SENSOR]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the CuboAI component."""
    # Set portable token storage paths based on HA config directory
    set_token_paths(hass.config.path())
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CuboAI from a config entry."""
    # Ensure token paths are set (in case async_setup wasn't called)
    set_token_paths(hass.config.path())
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = entry
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a CuboAI config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id, None)
    return unload_ok
