import asyncio
from datetime import timedelta
import logging

import aiofiles.os
import aiohttp

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api.async_api import (
    download_image,
    get_camera_profiles_raw,
    get_camera_state,
    get_n_alerts_paged,
    get_subscription_info,
    refresh_cubo_token,
)
from .api.cuboai_functions import save_access_token, save_refresh_token
from .const import DOMAIN, DEFAULT_UPDATE_INTERVAL
from .utils import log_to_file

_LOGGER = logging.getLogger(__name__)

def _fetch_local_data(uid, account, password, lib_path, camera_ip=None):
    """Synchronous function to fetch local data via TUTK."""
    log_to_file(f"Starting _fetch_local_data for UID={uid}, IP={camera_ip}, Lib={lib_path}")
    try:
        from .tutk.cuboai_tutk import TUTKSession
        from .tutk.cuboai_messages import CuboAIClient
    except ImportError:
        return {}

    data = {}
    try:
        with TUTKSession(uid, account, password, lib_path, camera_ip=camera_ip) as sess:
            client = CuboAIClient(sess)
            try:
                hw = client.get_hw_control()
                data["temperature"] = hw.temperature
                data["humidity"] = hw.humidity
                data["night_light_on"] = hw.night_light_on
                data["status_light_on"] = hw.status_light_on_off == 1
            except Exception as e:
                import traceback
                log_to_file(f"Failed to get HW control: {e}\n{traceback.format_exc()}")
                
            try:
                ls = client.get_lullaby_status()
                data["lullaby_playing"] = ls.is_playing
                data["lullaby_song"] = ls.current_song_uuid
            except Exception as e:
                import traceback
                log_to_file(f"Failed to get lullaby status: {e}\n{traceback.format_exc()}")
                
    except OSError as e:
        import traceback
        error_msg = str(e)
        if "__register_atfork" in error_msg or "Error relocating" in error_msg:
            _LOGGER.warning("Missing glibc compat symbols in Alpine Linux. Attempting to install gcompat...")
            log_to_file("Missing glibc compat symbols. Running 'apk add --no-cache gcompat'...")
            import os
            res = os.system("apk add --no-cache gcompat")
            log_to_file(f"apk add returned: {res}")
            if res == 0:
                log_to_file("gcompat installed successfully. Retrying TUTK connection...")
                try:
                    import ctypes
                    try:
                        ctypes.CDLL("libgcompat.so.0", mode=ctypes.RTLD_GLOBAL)
                        log_to_file("Loaded libgcompat.so.0 globally.")
                    except Exception as gerr:
                        try:
                            ctypes.CDLL("libgcompat.so", mode=ctypes.RTLD_GLOBAL)
                            log_to_file("Loaded libgcompat.so globally.")
                        except Exception as gerr2:
                            log_to_file(f"Failed to load libgcompat.so: {gerr2}")
                    
                    with TUTKSession(uid, account, password, lib_path, camera_ip=camera_ip) as sess:
                        client = CuboAIClient(sess)
                        try:
                            hw = client.get_hw_control()
                            data["temperature"] = hw.temperature
                            data["humidity"] = hw.humidity
                            data["night_light_on"] = hw.night_light_on
                            data["status_light_on"] = hw.status_light_on_off == 1
                        except Exception as e2:
                            log_to_file(f"Retry HW control failed: {e2}")
                        try:
                            ls = client.get_lullaby_status()
                            data["lullaby_playing"] = ls.is_playing
                            data["lullaby_song"] = ls.current_song_uuid
                        except Exception as e2:
                            log_to_file(f"Retry lullaby status failed: {e2}")
                    return data
                except Exception as retry_e:
                    log_to_file(f"Retry failed: {retry_e}\n{traceback.format_exc()}")
        log_to_file(f"Failed to connect to camera via TUTK for local polling: {e}\n{traceback.format_exc()}")
    except Exception as e:
        import traceback
        log_to_file(f"Failed to connect to camera via TUTK for local polling: {e}\n{traceback.format_exc()}")
    return data


class CuboAICoordinator(DataUpdateCoordinator):
    """Class to manage fetching data from CuboAI API centrally."""

    def __init__(self, hass, entry, access_token, refresh_token, user_agent):
        """Initialize."""
        interval = int(entry.options.get("update_interval", entry.data.get("update_interval", DEFAULT_UPDATE_INTERVAL)))
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=interval),
        )
        self._entry = entry
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._user_agent = user_agent
        self._session: aiohttp.ClientSession | None = None

        # Portable image storage path
        self._images_dir = self._get_images_dir()
        self._web_base = "/local/cuboai_images"

    def _get_images_dir(self) -> str:
        """Get the images directory path with legacy fallback for backwards compatibility."""
        import os
        portable_path = self.hass.config.path("www", "cuboai_images")
        legacy_path = "/config/www/cuboai_images"

        if os.path.exists(portable_path):
            return portable_path
        if os.path.exists(legacy_path):
            log_to_file(f"Using legacy images path: {legacy_path}")
            return legacy_path
        return portable_path

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create an aiohttp session."""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def async_close(self):
        """Close the session."""
        if self._session and not self._session.closed:
            await self._session.close()

    @property
    def download_images(self) -> bool:
        return self._entry.options.get("download_images", self._entry.data.get("download_images", True))

    @property
    def hours_back(self) -> int:
        return int(self._entry.options.get("hours_back", self._entry.data.get("hours_back", 12)))

    @property
    def max_alerts(self) -> int:
        return int(self._entry.options.get("alerts_count", self._entry.data.get("alerts_count", 5)))

    def _cleanup_old_images(self, device_id, limit):
        """Cleanup old images, keeping only the latest N per device."""
        from pathlib import Path

        try:
            files = sorted(
                Path(self._images_dir).glob(f"{device_id}_*.jpg"),
                key=lambda f: f.stat().st_mtime,
                reverse=True,
            )
            for old_file in files[limit:]:
                try:
                    old_file.unlink(missing_ok=True)
                except Exception:
                    pass
        except Exception as e:
            log_to_file(f"[CuboAICoordinator] Error cleaning images for {device_id}: {e}")

    async def _refresh_tokens(self, session):
        """Single centralized token refresh."""
        log_to_file("[CuboAICoordinator] Refreshing tokens centrally")
        resp = await refresh_cubo_token(self._refresh_token, self._user_agent, session)
        if "access_token" in resp:
            self._access_token = resp["access_token"]
            self._refresh_token = resp.get("refresh_token", self._refresh_token)
            await asyncio.gather(
                self.hass.async_add_executor_job(save_access_token, self._access_token),
                self.hass.async_add_executor_job(save_refresh_token, self._refresh_token),
            )
            log_to_file("[CuboAICoordinator] Tokens successfully refreshed and saved")
        else:
            raise UpdateFailed("Failed to refresh token: no access token in response")

    async def _async_update_data(self) -> dict:
        """Fetch all data for all cameras in a single pass."""
        session = await self._get_session()
        try:
            return await self._fetch_all(session)
        except aiohttp.ClientResponseError as e:
            if e.status == 401:
                log_to_file(f"[CuboAICoordinator] Access token expired during update: {e}")
                await self._refresh_tokens(session)
                return await self._fetch_all(session)
            log_to_file(f"[CuboAICoordinator] ClientResponseError: {e}")
            raise UpdateFailed(f"API error: {e}")
        except Exception as e:
            import traceback
            log_to_file(f"[CuboAICoordinator] Unexpected error: {e}\n{traceback.format_exc()}")
            raise UpdateFailed(f"Unexpected error: {e}")

    async def _fetch_all(self, session) -> dict:
        """Single coordinated fetch of all data."""
        import json as _json

        from homeassistant.util.dt import utcnow
        
        cameras = self._entry.data.get("cameras", [])
        if not cameras and "device_id" in self._entry.data:
            cameras = [{"device_id": self._entry.data["device_id"], "baby_name": self._entry.data["baby_name"]}]

        result = {
            "cameras": {}, 
            "subscription": None,
            "last_updated": utcnow().isoformat()
        }

        # Concurrently fetch raw profiles for all cameras and subscription info
        try:
            profiles_raw, sub_info = await asyncio.gather(
                get_camera_profiles_raw(self._access_token, self._user_agent, session),
                get_subscription_info(self._access_token, self._user_agent, session),
                return_exceptions=True
            )
            
            if isinstance(sub_info, Exception):
                log_to_file(f"[CuboAICoordinator] Failed to fetch subscription: {sub_info}")
                result["subscription"] = None
            else:
                result["subscription"] = sub_info
                
            if isinstance(profiles_raw, Exception):
                log_to_file(f"[CuboAICoordinator] Failed to fetch profiles: {profiles_raw}")
                profiles_raw = []
        except Exception as e:
            log_to_file(f"[CuboAICoordinator] Error fetching common data: {e}")
            profiles_raw = []

        # Process per-camera data
        for camera in cameras:
            device_id = camera["device_id"]
            uid = camera.get("uid")
            account = camera.get("account")
            password = camera.get("password")
            camera_ip = self._entry.options.get(f"camera_ip_{device_id}")
            if not camera_ip:
                camera_ip = camera.get("camera_ip")
            cam_data = {"profile": {}, "alerts": [], "latest_alert": None, "camera_state": {}, "local": {}}

            # 1. Profile Data
            for item in profiles_raw:
                if isinstance(item, dict) and item.get("device_id") == device_id:
                    profile_str = item.get("profile", "{}")
                    try:
                        profile = _json.loads(profile_str)
                    except Exception:
                        profile = {}
                    
                    gender = profile.get("gender")
                    gender_text = "male" if gender == 0 else "female" if gender == 1 else "unknown"
                    cam_data["profile"] = {
                        "baby": profile.get("baby"),
                        "birth": profile.get("birth"),
                        "gender": gender_text,
                        "device_id": device_id,
                    }
                    break

            # Concurrently fetch alerts and state for this camera
            try:
                import platform, os
                arch = "x86_64" if platform.machine().lower() in ["x86_64", "amd64"] else "aarch64"
                lib_path = os.path.join(os.path.dirname(__file__), "libs", arch, "libIOTCAPIs_ALL.so")
                
                alerts_data, state_data, local_data = await asyncio.gather(
                    get_n_alerts_paged(
                        device_id, self._access_token, self._user_agent, self.max_alerts, self.hours_back, session
                    ),
                    get_camera_state(device_id, self._access_token, self._user_agent, session),
                    self.hass.async_add_executor_job(_fetch_local_data, uid, account, password, lib_path, camera_ip) if uid else asyncio.sleep(0, result={}),
                    return_exceptions=True
                )
            except Exception as e:
                log_to_file(f"[CuboAICoordinator] Error gathering alerts/state/local for {device_id}: {e}")
                alerts_data, state_data, local_data = [], None, {}

            # 2. Camera State
            if isinstance(state_data, Exception):
                log_to_file(f"[CuboAICoordinator] State fetch failed for {device_id}: {state_data}")
            elif state_data:
                cam_data["camera_state"] = state_data
                
            if isinstance(local_data, Exception):
                log_to_file(f"[CuboAICoordinator] Local data fetch failed for {device_id}: {local_data}")
            elif local_data:
                cam_data["local"] = local_data

            # 3. Alerts Processing
            if isinstance(alerts_data, Exception):
                log_to_file(f"[CuboAICoordinator] Alert fetch failed for {device_id}: {alerts_data}")
                alerts_data = []

            alert_dicts = []
            if alerts_data:
                exists = await aiofiles.os.path.exists(self._images_dir)
                if self.download_images and not exists:
                    try:
                        await aiofiles.os.makedirs(self._images_dir, exist_ok=True)
                    except Exception as e:
                        log_to_file(f"[CuboAICoordinator] Failed to create images dir: {e}")

                for alert in alerts_data:
                    params = alert.get("params")
                    if isinstance(params, str):
                        try:
                            params = _json.loads(params)
                        except Exception:
                            pass

                    local_image_path = None
                    if self.download_images and alert.get("image"):
                        filename = f"{device_id}_{alert.get('id')}.jpg"
                        try:
                            await download_image(
                                alert.get("image"),
                                self._access_token,
                                self._user_agent,
                                self._images_dir,
                                filename,
                                session,
                            )
                            local_image_path = f"{self._web_base}/{filename}"
                        except Exception as e:
                            log_to_file(f"[CuboAICoordinator] Image download failed for {device_id}: {e}")

                    alert_dicts.append(
                        {
                            "type": alert.get("type"),
                            "created": alert.get("created"),
                            "params": params,
                            "image": local_image_path,
                            "id": alert.get("id"),
                            "ts": alert.get("ts"),
                            "device_id": alert.get("device_id"),
                        }
                    )

                if self.download_images:
                    await self.hass.async_add_executor_job(self._cleanup_old_images, device_id, self.max_alerts)

                if alert_dicts:
                    latest = max(alert_dicts, key=lambda a: a.get("ts", 0) or 0)
                    cam_data["latest_alert"] = latest.get("type", "Unknown")
                
                cam_data["alerts"] = alert_dicts

            result["cameras"][device_id] = cam_data

        return result
