import asyncio
import logging
import logging.handlers

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

from .api.async_api import get_camera_profiles, refresh_cubo_token
from .api.cuboai_functions import (
    load_access_token,
    load_refresh_token,
    save_access_token,
    save_refresh_token,
    set_token_paths,
)
from .const import DOMAIN
from .downloader import async_ensure_dependencies
from .go2rtc import Go2RTCManager
from .utils import set_debug_logs_enabled, set_log_path

_LOGGER = logging.getLogger(__name__)

_FILE_HANDLER = None
_FILE_LISTENER = None


def _setup_component_logger(hass: HomeAssistant, enable: bool):
    global _FILE_HANDLER, _FILE_LISTENER
    component_logger = logging.getLogger("custom_components.cuboai")

    if enable:
        if _FILE_HANDLER is None:
            import queue

            log_path = hass.config.path("cuboai_debug.log")
            # 2 MB max size, keep 1 backup (4MB total max)
            file_handler = logging.handlers.RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=1)
            file_handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter("%(asctime)s - %(name)s - %(levelname)s - %(message)s")
            file_handler.setFormatter(formatter)
            # Log records are enqueued (safe from the event loop); a
            # QueueListener thread performs the actual file writes/rotation.
            log_queue = queue.SimpleQueue()
            _FILE_HANDLER = logging.handlers.QueueHandler(log_queue)
            _FILE_HANDLER.setLevel(logging.DEBUG)
            _FILE_LISTENER = logging.handlers.QueueListener(log_queue, file_handler)
            _FILE_LISTENER.start()
            component_logger.addHandler(_FILE_HANDLER)
            component_logger.setLevel(logging.DEBUG)
            _LOGGER.info("CuboAI debug file logging enabled at %s", log_path)
    else:
        if _FILE_HANDLER is not None:
            _LOGGER.info("CuboAI debug file logging disabled")
            component_logger.removeHandler(_FILE_HANDLER)
            if _FILE_LISTENER is not None:
                _FILE_LISTENER.stop()
                _FILE_LISTENER = None
            _FILE_HANDLER.close()
            _FILE_HANDLER = None


PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.CAMERA,
    Platform.LIGHT,
    Platform.SWITCH,
    Platform.MEDIA_PLAYER,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.BUTTON,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the CuboAI component."""
    # Set portable token storage and log paths based on HA config directory
    set_token_paths(hass.config.path())
    set_log_path(hass.config.path())

    # Register frontend card
    try:
        from homeassistant.components.frontend import add_extra_js_url

        def _copy_frontend_card():
            import os
            import shutil

            # Copy the card JS into the user's /config/www directory so HA serves it natively at /local/
            www_dir = hass.config.path("www")
            os.makedirs(www_dir, exist_ok=True)
            src = hass.config.path("custom_components", "cuboai", "www", "cuboai-card.js")
            dst = os.path.join(www_dir, "cuboai-card.js")
            shutil.copy2(src, dst)

            # Use file mtime as cache-buster so browsers always pick up changes after a restart
            return int(os.path.getmtime(dst))

        mtime = await hass.async_add_executor_job(_copy_frontend_card)
        add_extra_js_url(hass, f"/local/cuboai-card.js?v={mtime}")
    except Exception as e:
        _LOGGER.error(f"Failed to register CuboAI frontend card: {e}")
        try:
            import traceback

            from .utils import log_to_file

            log_to_file(f"Frontend register error: {e}\n{traceback.format_exc()}")
        except Exception:
            pass

    from .media_library import async_setup_services

    await async_setup_services(hass)

    async def handle_clear_youtube_cache(call):
        import shutil

        cache_dir = hass.config.path("www", "cuboai_cache")
        await hass.async_add_executor_job(lambda: shutil.rmtree(cache_dir, True))

    hass.services.async_register(DOMAIN, "clear_youtube_cache", handle_clear_youtube_cache)

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up CuboAI from a config entry."""
    _LOGGER.debug("Starting CuboAI async_setup_entry...")

    # Ensure token and log paths are set (in case async_setup wasn't called)
    set_token_paths(hass.config.path())
    set_log_path(hass.config.path())

    debug_enabled = entry.options.get("enable_debug_logs", False)
    # Both attach file handlers (open a file) — keep that off the event loop.
    await hass.async_add_executor_job(set_debug_logs_enabled, debug_enabled)
    await hass.async_add_executor_job(_setup_component_logger, hass, debug_enabled)

    # Ensure media library services are set up
    from .media_library import async_setup_services

    await async_setup_services(hass)

    _LOGGER.debug("Ensuring native dependencies...")
    # Ensure dependencies
    deps_ok = await async_ensure_dependencies(hass)
    if not deps_ok:
        _LOGGER.warning("Failed to download CuboAI native dependencies. Local features may be disabled.")

    _LOGGER.debug("Dependencies ok. Fetching cameras...")
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
                all_cameras = list(device_map)
                old_cameras = entry.data.get("cameras", [])

                # Cameras are NEVER added automatically: only the ones the user
                # selected (config/options flow) are managed. New cameras on the
                # account are stored in all_cameras so the Options flow can offer
                # them, but they are not set up until the user checks them.
                selected_ids = entry.data.get("selected_camera_ids")
                if selected_ids is None:
                    # Entries created before camera selection existed: treat the
                    # currently configured cameras as the selection.
                    selected_ids = [c["device_id"] for c in old_cameras] or [c["device_id"] for c in all_cameras]

                new_cameras = [c for c in all_cameras if c["device_id"] in selected_ids]
                unselected = [c["device_id"] for c in all_cameras if c["device_id"] not in selected_ids]
                if unselected:
                    _LOGGER.info(
                        "Cameras on the account NOT added (select them in the integration Options to add): %s",
                        unselected,
                    )

                # Never shrink the camera list from a single startup fetch: a camera
                # that is transiently offline (or whose state query failed) would be
                # dropped from the entry, deleting its entities and credentials.
                new_ids = {c["device_id"] for c in new_cameras}
                for old in old_cameras:
                    if old["device_id"] not in new_ids:
                        _LOGGER.warning(
                            "Camera %s missing from API response — keeping existing config",
                            old["device_id"],
                        )
                        new_cameras.append(old)

                account_ids_changed = sorted(c["device_id"] for c in all_cameras) != sorted(
                    c["device_id"] for c in entry.data.get("all_cameras", [])
                )
                if account_ids_changed or sorted(new_cameras, key=lambda c: c["device_id"]) != sorted(
                    old_cameras, key=lambda c: c["device_id"]
                ):
                    # Log device ids only — the full dicts contain admin passwords.
                    _LOGGER.info(
                        "Dynamic camera list update detected: %s",
                        [c.get("device_id") for c in new_cameras],
                    )
                    new_data = dict(entry.data)
                    new_data["cameras"] = new_cameras
                    new_data["all_cameras"] = all_cameras
                    new_data["selected_camera_ids"] = selected_ids
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

    # Remove devices (and their entities) for cameras the user deselected —
    # otherwise they linger in the registry as "unavailable" and still show up.
    try:
        from homeassistant.helpers import device_registry as dr

        configured_ids = {c["device_id"] for c in entry.data.get("cameras", [])}
        known_account_ids = {c["device_id"] for c in entry.data.get("all_cameras", [])}
        dev_reg = dr.async_get(hass)
        for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
            cam_ids = {ident[1] for ident in device.identifiers if ident[0] == DOMAIN}
            # Only touch devices that are account cameras and no longer selected
            if cam_ids and cam_ids <= known_account_ids and not (cam_ids & configured_ids):
                _LOGGER.info("Removing deselected camera device: %s (%s)", device.name, cam_ids)
                dev_reg.async_update_device(device.id, remove_config_entry_id=entry.entry_id)
    except Exception as e:
        _LOGGER.warning("Failed to clean up deselected camera devices: %s", e)

    # Heal entries that already persisted duplicate camera profiles (issue #84):
    # a duplicated device_id makes every platform register colliding unique_ids
    # ("Platform cuboai does not generate unique IDs" across sensor/camera/...).
    stored_cameras = entry.data.get("cameras", [])
    unique_cameras = list({c.get("device_id", id(c)): c for c in stored_cameras}.values())
    if len(unique_cameras) != len(stored_cameras):
        _LOGGER.warning(
            "Removed %d duplicate camera profile(s) from the config entry",
            len(stored_cameras) - len(unique_cameras),
        )
        hass.config_entries.async_update_entry(entry, data={**entry.data, "cameras": unique_cameras})

    _LOGGER.debug("Cameras fetched. Setting up coordinator...")
    from .coordinator import CuboAICoordinator

    coordinator = CuboAICoordinator(hass, entry, latest_access, latest_refresh, user_agent)

    _LOGGER.debug("Triggering coordinator first refresh...")
    await coordinator.async_config_entry_first_refresh()
    _LOGGER.debug("Coordinator first refresh complete.")

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator,
        # Snapshot of the options this entry was set up with, used by
        # async_update_options to skip a full reload when the only change is a
        # coordinator-written camera_ip_* discovery (reloading mid-refresh tears
        # down the coordinator while it is still iterating cameras).
        "options_snapshot": dict(entry.options),
    }

    _LOGGER.debug("Starting go2rtc manager...")
    # Setup go2rtc manager
    go2rtc_manager = Go2RTCManager(hass)
    go2rtc_manager.update_streams(entry.data.get("cameras", []), dict(entry.options))
    await go2rtc_manager.start()
    _LOGGER.debug("go2rtc manager started.")

    hass.data[DOMAIN][entry.entry_id]["go2rtc"] = go2rtc_manager

    # Register update listener to reload when options change
    entry.async_on_unload(entry.add_update_listener(async_update_options))

    _LOGGER.debug("Forwarding entry setups...")
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    _LOGGER.debug("CuboAI async_setup_entry finished successfully.")
    return True


async def async_update_options(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Update options."""
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if data is not None:
        snapshot = data.get("options_snapshot", {})
        new_options = dict(entry.options)
        changed_keys = {k for k in set(snapshot) | set(new_options) if snapshot.get(k) != new_options.get(k)}
        data["options_snapshot"] = new_options
        if changed_keys and all(k.startswith("camera_ip_") and not snapshot.get(k) for k in changed_keys):
            # Auto-discovered camera IP written by the coordinator (it only ever
            # fills in previously-empty IPs): picked up on the next poll, no
            # reason to tear the whole integration down mid-refresh. A user
            # *changing* an existing IP still reloads normally.
            _LOGGER.debug("Skipping reload for camera IP discovery: %s", changed_keys)
            return
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

        # Allow the global sensor/switch singletons to be recreated on re-setup,
        # but only once the LAST entry unloads — with multiple entries the
        # other entry's global entities still exist and re-adding would
        # collide on their fixed unique_ids.
        from homeassistant.config_entries import ConfigEntryState

        others_loaded = any(
            e.entry_id != entry.entry_id and e.state is ConfigEntryState.LOADED
            for e in hass.config_entries.async_entries(DOMAIN)
        )
        if not others_loaded:
            hass.data.pop("cuboai_media_library_added", None)
            hass.data.get(DOMAIN, {}).pop("_youtube_cache_switch_added", None)
            hass.data.get(DOMAIN, {}).pop("_clear_cache_button_added", None)
    return unload_ok
