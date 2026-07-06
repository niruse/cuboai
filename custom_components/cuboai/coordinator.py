import asyncio
import logging
from datetime import timedelta

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
from .const import DEFAULT_UPDATE_INTERVAL, DOMAIN
from .utils import log_to_file

_LOGGER = logging.getLogger(__name__)


def _fetch_local_data(uid, account, password, camera_ip=None, fetch_extras=True, is_retry=False):
    """Synchronous function to fetch local data via TUTK."""
    from .utils import log_to_file

    log_to_file(f"Starting _fetch_local_data for UID={uid}, IP={camera_ip}")
    try:
        from .tutk.cuboai_messages import CuboAIClient
        from .tutk.cuboai_session import get_session
    except ImportError as e:
        log_to_file(f"ImportError in _fetch_local_data: {e}")
        return {}

    data = {}
    try:
        import time

        # Keep total worst-case time under the coordinator's asyncio timeout so a
        # cancelled poll doesn't leave this thread running for minutes (leaking one
        # executor thread per poll cycle when the camera is unreachable).
        for attempt in range(2):
            try:
                with get_session(
                    uid,
                    account,
                    password,
                    camera_ip=camera_ip if camera_ip else None,
                    defer_stream_start=False,
                    defer_video_start_late=False,
                    auto_discover_lib=True,
                ) as sess:
                    client = CuboAIClient(sess)

                    # Cleaned up experimental blocks

                    try:
                        hw = client.get_hw_control()
                        data["night_light_on"] = hw.night_light_on_off == 1
                        data["status_light_on"] = hw.status_light_on_off == 1
                        data["wifi_quality"] = hw.wifi_quality
                        data["night_vision"] = hw.night_vision_control
                        data["video_v_flip"] = hw.video_v_flip_control == 1
                        data["ssid"] = hw.ssid
                        data["stand_type"] = hw.stand_type
                        data["camera_angle"] = hw.camera_angle
                        try:
                            lw = client.get_lightweight_status()
                            data["firmware_version"] = lw.get("firmware", hw.fw_version if hw.fw_version else "Unknown")
                        except Exception as e:
                            log_to_file(f"Failed to get lightweight status for firmware: {e}")
                            data["firmware_version"] = hw.fw_version if hw.fw_version else "Unknown"
                            lw = {}

                        try:
                            th = client.get_temp_humidity()

                            temp = lw.get("temp_c") if lw.get("temp_c") is not None else th.temperature
                            humid = lw.get("humidity_pct") if lw.get("humidity_pct") is not None else th.humidity

                            if temp is not None and -20 <= temp <= 60:
                                data["temperature"] = temp
                            if humid is not None and 0 <= humid <= 100:
                                data["humidity"] = humid
                        except Exception as e:
                            log_to_file(f"Failed to get temp_humidity: {e}")

                        try:
                            data["sleep_mode_on"] = client.get_sleep_mode().get("enabled")
                        except Exception as e:
                            log_to_file(f"Failed to get sleep_mode: {e}")

                        try:
                            cry_res = client.get_cry_detect_status()
                            data["cry_detect"] = cry_res.get("enabled")
                            data["cry_detect_sensitivity"] = cry_res.get("sensitivity")
                        except Exception as e:
                            log_to_file(f"Failed to get cry_detect: {e}")

                        try:
                            ss_status = client.get_sleep_safety_status()
                            data["sleep_safety"] = ss_status.get("enabled")
                            data["baby_presence"] = ss_status.get("baby_presence_alert")
                        except Exception as e:
                            log_to_file(f"Failed to get sleep_safety: {e}")

                        try:
                            data["cough_detect"] = client.get_cough_status().get("enabled")
                        except Exception as e:
                            log_to_file(f"Failed to get cough_detect: {e}")

                        try:
                            mat = client.get_mat_info()
                            data["mat_state"] = mat.get("state")
                            data["mat_battery"] = mat.get("battery")
                            data["mat_bpm"] = mat.get("bpm")
                        except Exception as e:
                            log_to_file(f"Failed to get mat info: {e}")

                        try:
                            temp_info = client.get_smart_temp_info()
                            data["smart_temp"] = temp_info.get("temp_c")
                            data["smart_temp_battery"] = temp_info.get("battery")
                        except Exception as e:
                            log_to_file(f"Failed to get smart temp info: {e}")

                        try:
                            stats = client.get_session_stats()
                            data["connection_mode"] = stats.get("mode")
                        except Exception as e:
                            log_to_file(f"Failed to get session stats: {e}")

                        try:
                            if fetch_extras:
                                wifi = client.get_wifi()
                                data["wifi_ip"] = wifi.get("ip")
                                data["wifi_mac"] = wifi.get("mac")
                                data["wifi_rssi"] = wifi.get("strength")
                                data["wifi_noise"] = wifi.get("noise")
                                data["wifi_channel"] = wifi.get("channel")
                        except Exception as e:
                            log_to_file(f"Failed to get wifi info: {e}")

                        try:
                            if fetch_extras:
                                policy = client.get_hw_policy()
                                data["temp_alert_high"] = policy.get("temp_high_c")
                                data["temp_alert_low"] = policy.get("temp_low_c")
                                data["humi_alert_high"] = policy.get("humi_high_pct")
                                data["humi_alert_low"] = policy.get("humi_low_pct")
                        except Exception as e:
                            log_to_file(f"Failed to get hw policy: {e}")

                        try:
                            if fetch_extras:
                                st_cfg = client.get_smart_temp_config()
                                data["fever_alert_high"] = st_cfg.get("high_temp_c")
                                data["fever_alert_low"] = st_cfg.get("low_temp_c")
                        except Exception as e:
                            log_to_file(f"Failed to get smart temp config: {e}")

                        try:
                            users = client.get_connected_users()
                            data["connected_users"] = users.get("count", 0)
                        except Exception as e:
                            log_to_file(f"Failed to get connected users: {e}")

                    except Exception as e:
                        import traceback

                        log_to_file(f"Failed to get hw control: {e}\n{traceback.format_exc()}")

                    try:
                        ls = client.get_lullaby_status()
                        data["lullaby_playing"] = ls.is_playing
                        data["lullaby_song"] = ls.current_song_uuid

                        try:
                            sched = client.get_lullaby_schedule()
                            data["lullaby_volume"] = sched.volume
                        except Exception as e:
                            log_to_file(f"Failed to get lullaby schedule: {e}")
                            data["lullaby_volume"] = 50

                    except Exception as e:
                        import traceback

                        log_to_file(f"Failed to get lullaby status: {e}\n{traceback.format_exc()}")

                    return data
            except Exception as conn_e:
                log_to_file(f"Connection attempt {attempt + 1} failed: {conn_e}")
                if attempt < 1:
                    time.sleep(2)
                else:
                    raise conn_e
    except OSError as e:
        import traceback

        error_msg = str(e)
        if ("__register_atfork" in error_msg or "Error relocating" in error_msg) and not is_retry:
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
                    except Exception:
                        try:
                            ctypes.CDLL("libgcompat.so", mode=ctypes.RTLD_GLOBAL)
                            log_to_file("Loaded libgcompat.so globally.")
                        except Exception as gerr2:
                            log_to_file(f"Failed to load libgcompat.so: {gerr2}")

                    return _fetch_local_data(uid, account, password, camera_ip, fetch_extras, is_retry=True)
                except Exception as retry_e:
                    log_to_file(f"Alpine retry failed: {retry_e}\n{traceback.format_exc()}")
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

    @property
    def max_saved_photos(self) -> int:
        return int(self._entry.options.get("max_saved_photos", self._entry.data.get("max_saved_photos", 10)))

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

    @staticmethod
    def _raise_if_unauthorized(*results):
        """Re-raise a 401 from gathered results so the token-refresh path runs.

        All API calls below use return_exceptions=True, which would otherwise
        swallow the 401 and leave the integration polling with a dead token
        until the next HA restart.
        """
        for res in results:
            if isinstance(res, aiohttp.ClientResponseError) and res.status == 401:
                raise res

    async def _fetch_all(self, session) -> dict:
        """Single coordinated fetch of all data."""
        import json as _json

        from homeassistant.util.dt import utcnow

        cameras = self._entry.data.get("cameras", [])
        if not cameras and "device_id" in self._entry.data:
            cameras = [{"device_id": self._entry.data["device_id"], "baby_name": self._entry.data["baby_name"]}]

        result = {"cameras": {}, "subscription": None, "last_updated": utcnow().isoformat()}

        # Concurrently fetch raw profiles for all cameras and subscription info
        try:
            profiles_raw, sub_info = await asyncio.gather(
                asyncio.wait_for(get_camera_profiles_raw(self._access_token, self._user_agent, session), timeout=10.0),
                asyncio.wait_for(get_subscription_info(self._access_token, self._user_agent, session), timeout=10.0),
                return_exceptions=True,
            )

            self._raise_if_unauthorized(profiles_raw, sub_info)

            if isinstance(sub_info, Exception):
                log_to_file(f"[CuboAICoordinator] Failed to fetch subscription: {sub_info}")
                result["subscription"] = None
            else:
                result["subscription"] = sub_info

            if isinstance(profiles_raw, Exception):
                log_to_file(f"[CuboAICoordinator] Failed to fetch profiles: {profiles_raw}")
                profiles_raw = []
        except aiohttp.ClientResponseError:
            raise
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
            old_local = {}
            try:
                if hasattr(self, "data") and self.data and "cameras" in self.data and device_id in self.data["cameras"]:
                    old_local = self.data["cameras"][device_id].get("local", {})
                fetch_extras = not bool(old_local.get("wifi_ip"))

                async def _dummy_async():
                    return {}

                alerts_data, state_data, local_data = await asyncio.gather(
                    asyncio.wait_for(
                        get_n_alerts_paged(
                            device_id, self._access_token, self._user_agent, self.max_alerts, self.hours_back, session
                        ),
                        timeout=15.0,
                    ),
                    asyncio.wait_for(
                        get_camera_state(device_id, self._access_token, self._user_agent, session), timeout=10.0
                    ),
                    asyncio.wait_for(
                        self.hass.async_add_executor_job(
                            _fetch_local_data, uid, account, password, camera_ip, fetch_extras
                        )
                        if uid
                        else _dummy_async(),
                        timeout=20.0,
                    ),
                    return_exceptions=True,
                )
            except Exception as e:
                log_to_file(f"[CuboAICoordinator] Error gathering alerts/state/local for {device_id}: {e}")
                alerts_data, state_data, local_data = [], None, {}

            # An expired token must bubble up so _async_update_data refreshes it
            self._raise_if_unauthorized(alerts_data, state_data)

            # 2. Camera State
            if isinstance(state_data, BaseException):
                log_to_file(f"[CuboAICoordinator] State fetch failed for {device_id}: {state_data}")
            elif state_data:
                cam_data["camera_state"] = state_data

            if isinstance(local_data, BaseException):
                log_to_file(f"[CuboAICoordinator] Local data fetch failed for {device_id}: {local_data}")
            elif local_data:
                if local_data:
                    old_local_merged = old_local.copy()
                    old_local_merged.update(local_data)
                    cam_data["local"] = old_local_merged

                    fetched_ip = local_data.get("wifi_ip")
                    current_ip = self._entry.options.get(f"camera_ip_{device_id}")
                    if fetched_ip and not current_ip:
                        new_options = dict(self._entry.options)
                        new_options[f"camera_ip_{device_id}"] = fetched_ip
                        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
                        _LOGGER.info(f"Automatically updated camera IP for {device_id} to {fetched_ip} in options")

            # 3. Alerts Processing
            if isinstance(alerts_data, BaseException):
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
                    await self.hass.async_add_executor_job(self._cleanup_old_images, device_id, self.max_saved_photos)

                if alert_dicts:
                    latest = max(alert_dicts, key=lambda a: a.get("ts", 0) or 0)
                    cam_data["latest_alert"] = latest.get("type", "Unknown")

                cam_data["alerts"] = alert_dicts

            result["cameras"][device_id] = cam_data

        return result
