import asyncio
import logging

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
import homeassistant.helpers.config_validation as cv

from .api.async_api import get_camera_profiles, refresh_cubo_token
from .api.cuboai_functions import (
    load_access_token,
    load_refresh_token,
    save_access_token,
    save_refresh_token,
    set_token_paths,
)
from .downloader import async_ensure_dependencies
from .go2rtc import Go2RTCManager
from .const import DOMAIN
from .utils import set_log_path

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.CAMERA, Platform.LIGHT, Platform.SWITCH, Platform.MEDIA_PLAYER, Platform.NUMBER, Platform.SELECT]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the CuboAI component."""
    # Set portable token storage and log paths based on HA config directory
    set_token_paths(hass.config.path())
    set_log_path(hass.config.path())
    
    # Register frontend card
    try:
        import os
        import shutil
        from homeassistant.components.frontend import add_extra_js_url
        
        # Copy the card JS into the user's /config/www directory so HA serves it natively at /local/
        www_dir = hass.config.path("www")
        os.makedirs(www_dir, exist_ok=True)
        src = hass.config.path("custom_components", "cuboai", "www", "cuboai-card.js")
        dst = os.path.join(www_dir, "cuboai-card.js")
        shutil.copy2(src, dst)
        
        # Add the script to the frontend using the reliable /local/ path
        # Use file mtime as cache-buster so browsers always pick up changes after a restart
        mtime = int(os.path.getmtime(dst))
        add_extra_js_url(hass, f"/local/cuboai-card.js?v={mtime}")
    except Exception as e:
        _LOGGER.error(f"Failed to register CuboAI frontend card: {e}")
        try:
            import traceback
            import datetime
            with open("/config/cuboai_last_alert_debug.log", "a") as f:
                f.write(f"{datetime.datetime.now()} - Frontend register error: {e}\n{traceback.format_exc()}\n")
        except:
            pass

    from .media_library import async_setup_services
    async_setup_services(hass)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CuboAI from a config entry."""
    # Ensure token and log paths are set (in case async_setup wasn't called)
    set_token_paths(hass.config.path())
    set_log_path(hass.config.path())

    # Ensure media library services are set up
    from .media_library import async_setup_services
    async_setup_services(hass)

    # Ensure dependencies
    deps_ok = await async_ensure_dependencies(hass)
    if not deps_ok:
        _LOGGER.warning("Failed to download CuboAI native dependencies. Local features may be disabled.")

    # Dynamically refresh/sync the cameras list from the API on startup
    access_token = entry.data.get("access_token")
    refresh_token = entry.data.get("refresh_token")
    user_agent = entry.data.get("user_agent")

    latest_access = await hass.async_add_executor_job(load_access_token)
    latest_refresh = await hass.async_add_executor_job(load_refresh_token)
    if not latest_access:
        latest_access = access_token
    if not latest_refresh:
        latest_refresh = refresh_token

    try:
        async with aiohttp.ClientSession() as session:
            try:
                device_map = await get_camera_profiles(latest_access, user_agent, session)
            except aiohttp.ClientResponseError as e:
                if e.status == 401:
                    _LOGGER.debug("Access token expired on startup, refreshing...")
                    resp = await refresh_cubo_token(latest_refresh, user_agent, session)
                    latest_access = resp.get("access_token")
                    latest_refresh = resp.get("refresh_token", latest_refresh)
                    await asyncio.gather(
                        hass.async_add_executor_job(save_access_token, latest_access),
                        hass.async_add_executor_job(save_refresh_token, latest_refresh),
                    )
                    device_map = await get_camera_profiles(latest_access, user_agent, session)
                else:
                    raise

            if device_map:
                new_cameras = device_map

                old_cameras = entry.data.get("cameras", [])
                if sorted(new_cameras, key=lambda c: c["device_id"]) != sorted(old_cameras, key=lambda c: c["device_id"]):
                    _LOGGER.info("Dynamic camera list update detected: %s", new_cameras)
                    new_data = dict(entry.data)
                    new_data["cameras"] = new_cameras
                    new_data["access_token"] = latest_access
                    new_data["refresh_token"] = latest_refresh
                    hass.config_entries.async_update_entry(entry, data=new_data)
                elif latest_access != access_token or latest_refresh != refresh_token:
                    # Update token if refreshed even if cameras didn't change
                    new_data = dict(entry.data)
                    new_data["access_token"] = latest_access
                    new_data["refresh_token"] = latest_refresh
                    hass.config_entries.async_update_entry(entry, data=new_data)
    except Exception as e:
        _LOGGER.warning("Failed to dynamically refresh CuboAI camera profiles: %s", e)

    from .coordinator import CuboAICoordinator

    coordinator = CuboAICoordinator(
        hass, entry, latest_access, latest_refresh, user_agent
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
    }

    # Setup go2rtc manager
    go2rtc_manager = Go2RTCManager(hass)
    go2rtc_manager.update_streams(entry.data.get("cameras", []), dict(entry.options))
    await go2rtc_manager.start()
    
    hass.data[DOMAIN][entry.entry_id]["go2rtc"] = go2rtc_manager

    # Register update listener to reload when options change
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a CuboAI config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id, {})
        coordinator = data.get("coordinator")
        if coordinator:
            await coordinator.async_close()
        
        go2rtc = data.get("go2rtc")
        if go2rtc:
            await go2rtc.stop()
    return unload_ok
