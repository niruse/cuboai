import asyncio
import json
import logging
from homeassistant.helpers.entity import Entity
from .const import DOMAIN
from .utils import log_to_file
from .api.cuboai_functions import (
    get_camera_profiles_raw,
    get_n_alerts_paged,       # <-- This does the "get up to N alerts, paged" logic
    get_subscription_info,
    get_camera_state,
    download_image,
    refresh_access_token_only,
    load_refresh_token,
    save_refresh_token,
    load_access_token,
    save_access_token,
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    device_id = entry.data["device_id"]
    access_token = entry.data["access_token"]
    refresh_token = entry.data["refresh_token"]
    baby_name = entry.data["baby_name"]
    user_agent = entry.data["user_agent"]
    download_images = entry.options.get("download_images", entry.data.get("download_images", True))

    sensors = [
        CuboBabyInfoSensor(
            hass=hass,
            entry=entry,
            name=f"CuboAI Baby Info {baby_name}",
            device_id=device_id,
            baby_name=baby_name,
            access_token=access_token,
            refresh_token=refresh_token,
            user_agent=user_agent,
        ),
        CuboLastAlertSensor(
            hass=hass,
            entry=entry,
            device_id=device_id,
            access_token=access_token,
            refresh_token=refresh_token,
            user_agent=user_agent,
            name=f"CuboAI Last Alert {baby_name}",
            download_images=download_images
        ),
        CuboSubscriptionSensor(
            hass=hass,
            entry=entry,
            access_token=access_token,
            refresh_token=refresh_token,
            user_agent=user_agent,
            name=f"CuboAI Subscription {baby_name}"
        ),
        CuboCameraStateSensor(
            hass=hass,
            entry=entry,
            device_id=device_id,
            access_token=access_token,
            refresh_token=refresh_token,
            user_agent=user_agent,
            name=f"CuboAI Camera State {baby_name}"
        ),
    ]
    async_add_entities(sensors, update_before_add=True)

class CuboBaseSensor(Entity):
    def __init__(self, hass, entry, access_token, refresh_token, user_agent):
        self.hass = hass
        self._entry = entry
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._user_agent = user_agent

    async def _load_latest_tokens(self):
        # Load tokens from disk via executor to avoid blocking the event loop
        latest_access, latest_refresh = await asyncio.gather(
            self.hass.async_add_executor_job(load_access_token),
            self.hass.async_add_executor_job(load_refresh_token),
        )
        if latest_access:
            self._access_token = latest_access
        if latest_refresh:
            self._refresh_token = latest_refresh

    async def _external_refresh_token(self):
        # Always reload the latest tokens before refreshing
        await self._load_latest_tokens()
        access_token, refresh_token, _ = await self.hass.async_add_executor_job(
            refresh_access_token_only,
            self._refresh_token,
            self._user_agent,
        )
        self._access_token = access_token
        self._refresh_token = refresh_token
        # Save tokens back to disk via executor as well
        await asyncio.gather(
            self.hass.async_add_executor_job(save_access_token, access_token),
            self.hass.async_add_executor_job(save_refresh_token, refresh_token),
        )

class CuboBabyInfoSensor(CuboBaseSensor):
    def __init__(self, hass, entry, name, device_id, baby_name, access_token, refresh_token, user_agent):
        super().__init__(hass, entry, access_token, refresh_token, user_agent)
        self._name = name
        self._device_id = device_id
        self._baby_name = baby_name
        self._state = None
        self._attributes = {}

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return f"cuboai_baby_info_{self._device_id}"

    @property
    def state(self):
        return self._baby_name

    @property
    def extra_state_attributes(self):
        return self._attributes

    async def async_update(self):
        import traceback
        try:
            await self._load_latest_tokens()
            try:
                profiles = await self.hass.async_add_executor_job(
                    get_camera_profiles_raw, self._access_token, self._user_agent
                )
            except Exception as e:
                if "401" in str(e) or "Unauthorized" in str(e).lower():
                    log_to_file(f"Access token expired in BabyInfoSensor: {e}")
                    await self._external_refresh_token()
                    profiles = await self.hass.async_add_executor_job(
                        get_camera_profiles_raw, self._access_token, self._user_agent
                    )
                else:
                    raise
            found = False
            for item in profiles:
                if item["device_id"] == self._device_id:
                    profile = json.loads(item.get("profile", "{}"))
                    birth_date = profile.get("birth")
                    gender = profile.get("gender")
                    gender_text = "male" if gender == 0 else "female" if gender == 1 else "unknown"
                    self._attributes = {
                        "baby": profile.get("baby"),
                        "birth": birth_date,
                        "gender": gender_text,
                        "device_id": self._device_id
                    }
                    found = True
                    break
            if not found:
                self._attributes = {
                    "baby": None,
                    "birth": None,
                    "gender": None,
                    "device_id": self._device_id
                }
        except Exception as e:
            log_to_file(f"Failed to update Cubo baby profile info: {e}\n{traceback.format_exc()}")
            self._attributes = {
                "baby": None,
                "birth": None,
                "gender": None,
                "device_id": self._device_id
            }

class CuboLastAlertSensor(CuboBaseSensor):
    """
    Sensor that exposes the latest CuboAI alerts for a specific device.
    - Pulls the latest N alerts using get_n_alerts_paged
    - Parses params into a dict (not a JSON string)
    - Downloads alert images to /config/www/cuboai_images if enabled
    - Cleans up old images per device (keeps the latest 5)
    - Chooses the state based on the newest alert by ts
    """

    def __init__(
        self,
        hass,
        entry,
        device_id,
        access_token,
        refresh_token,
        user_agent,
        name="CuboAI Last Alert",
        download_images=True,
    ):
        super().__init__(hass, entry, access_token, refresh_token, user_agent)
        self._device_id = device_id
        self._name = name
        self._state = None
        self._attributes = {}
        self._attr_extra_state_attributes = self._attributes

        # Paths for image storage and web access
        self._images_dir = "/config/www/cuboai_images"
        self._web_base = "/local/cuboai_images"

        # Default behavior can be overridden via options
        self._default_download_images = download_images

    # ---------- Config helpers ----------

    @property
    def download_images(self) -> bool:
        # Prefer options over data, fallback to default ctor value
        return self._entry.options.get(
            "download_images",
            self._entry.data.get("download_images", self._default_download_images),
        )

    @property
    def hours_back(self) -> int:
        # Allow narrowing the window via options for closer parity with manual tests
        return int(self._entry.options.get("hours_back", self._entry.data.get("hours_back", 12)))

    @property
    def max_alerts(self) -> int:
        # How many alerts to keep in attributes
        return int(self._entry.options.get("alerts_count", self._entry.data.get("alerts_count", 5)))

    # ---------- Entity basics ----------

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return f"cuboai_last_alert_{self._device_id}"

    @property
    def state(self):
        return self._state

    @property
    def extra_state_attributes(self):
        return self._attributes

    # ---------- Update logic ----------

    async def async_update(self):
        import os
        from pathlib import Path
        import json as _json
        import traceback

        try:
            # Ensure we use the newest tokens saved on disk
            await self._load_latest_tokens()

            # Fetch alerts, with refresh fallback on 401
            try:
                alerts = await self.hass.async_add_executor_job(
                    get_n_alerts_paged,
                    self._device_id,
                    self._access_token,
                    self._user_agent,
                    self.max_alerts,
                    self.hours_back,
                )
            except Exception as e:
                if "401" in str(e) or "unauthorized" in str(e).lower():
                    log_to_file(f"[CuboLastAlertSensor] Access token expired: {e}")
                    await self._external_refresh_token()
                    alerts = await self.hass.async_add_executor_job(
                        get_n_alerts_paged,
                        self._device_id,
                        self._access_token,
                        self._user_agent,
                        self.max_alerts,
                        self.hours_back,
                    )
                else:
                    raise

            log_to_file(f"[CuboLastAlertSensor] Fetched alerts raw: {_json.dumps(alerts, ensure_ascii=False)[:2000]}")

            alert_dicts = []
            downloaded_filenames = []

            if alerts:
                # Ensure image dir exists if downloads are enabled
                exists = await self.hass.async_add_executor_job(os.path.exists, self._images_dir)
                if self.download_images and not exists:
                    try:
                        # run makedirs in executor to avoid blocking the loop
                        await self.hass.async_add_executor_job(os.makedirs, self._images_dir, True)
                        log_to_file(f"[CuboLastAlertSensor] Created images dir: {self._images_dir}")
                    except Exception as e:
                        log_to_file(f"[CuboLastAlertSensor] Failed to create images dir: {e}")

                for alert in alerts:
                    # params can arrive as dict already if get_n_alerts_paged normalized it
                    params = alert.get("params")
                    if isinstance(params, str):
                        try:
                            params = _json.loads(params)
                        except Exception:
                            # Leave as string if not valid JSON
                            pass

                    local_image_path = None
                    if self.download_images and alert.get("image"):
                        filename = f"{self._device_id}_{alert.get('id')}.jpg"
                        try:
                            await self.hass.async_add_executor_job(
                                download_image,
                                alert.get("image"),
                                self._access_token,
                                self._user_agent,
                                self._images_dir,
                                filename,
                            )
                            local_image_path = f"{self._web_base}/{filename}"
                            downloaded_filenames.append(filename)
                            log_to_file(f"[CuboLastAlertSensor] Downloaded image: {local_image_path}")
                        except Exception as e:
                            log_to_file(f"[CuboLastAlertSensor] Image download failed: {e}")
                            local_image_path = None

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

                # Cleanup old images per device, keep only the latest 5

                def _cleanup_images(dir_path, prefix):
                    # Runs in executor, safe to use blocking I/O here
                    from pathlib import Path
                    files = sorted(
                        Path(dir_path).glob(f"{prefix}_*.jpg"),
                        key=lambda f: f.stat().st_mtime,
                        reverse=True,
                    )
                    for old_file in files[5:]:
                        try:
                            old_file.unlink(missing_ok=True)
                        except Exception:
                            pass
                
                # inside async_update
                if self.download_images:
                    try:
                        await self.hass.async_add_executor_job(_cleanup_images, self._images_dir, self._device_id)
                    except Exception as e:
                        log_to_file(f"[CuboLastAlertSensor] Error cleaning images: {e}")

                # Choose latest by ts
                latest = max(alert_dicts, key=lambda a: a.get("ts", 0) or 0)
                self._state = latest.get("type", "Unknown")
                self._attributes = {"alerts": alert_dicts}
                self._attr_extra_state_attributes = self._attributes

                log_to_file(
                    f"[CuboLastAlertSensor] State set to: {self._state}. "
                    f"Attributes count: {len(alert_dicts)}"
                )
            else:
                self._state = "No alerts"
                self._attributes = {"alerts": []}
                self._attr_extra_state_attributes = self._attributes
                log_to_file("[CuboLastAlertSensor] No alerts found in window.")

        except Exception as e:
            err_msg = f"[CuboLastAlertSensor] Error updating alerts: {e}\n{traceback.format_exc()}"
            log_to_file(err_msg)
            self._state = "Error"
            self._attributes = {"alerts": []}
            self._attr_extra_state_attributes = self._attributes



class CuboSubscriptionSensor(CuboBaseSensor):
    def __init__(self, hass, entry, access_token, refresh_token, user_agent, name="CuboAI Subscription"):
        super().__init__(hass, entry, access_token, refresh_token, user_agent)
        self._name = name
        self._state = None
        self._attributes = {}

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return "cuboai_subscription"

    @property
    def state(self):
        return self._state

    @property
    def extra_state_attributes(self):
        return self._attributes

    async def async_update(self):
        import traceback
        try:
            await self._load_latest_tokens()
            try:
                data = await self.hass.async_add_executor_job(
                    get_subscription_info, self._access_token, self._user_agent
                )
            except Exception as e:
                if "401" in str(e) or "Unauthorized" in str(e).lower():
                    log_to_file(f"Access token expired in SubscriptionSensor: {e}")
                    await self._external_refresh_token()
                    data = await self.hass.async_add_executor_job(
                        get_subscription_info, self._access_token, self._user_agent
                    )
                else:
                    raise
            if data:
                self._state = data.get("status", "unknown")
                self._attributes = data
            else:
                self._state = "No subscription"
                self._attributes = {}
        except Exception as e:
            self._state = "Error"
            self._attributes = {}
            log_to_file(f"Error fetching CuboAI subscription: {e}\n{traceback.format_exc()}")

class CuboCameraStateSensor(CuboBaseSensor):
    def __init__(self, hass, entry, device_id, access_token, refresh_token, user_agent, name="CuboAI Camera State"):
        super().__init__(hass, entry, access_token, refresh_token, user_agent)
        self._device_id = device_id
        self._name = name
        self._state = None
        self._attributes = {}

    @property
    def name(self):
        return self._name

    @property
    def unique_id(self):
        return f"cuboai_camera_state_{self._device_id}"

    @property
    def state(self):
        return self._state

    @property
    def extra_state_attributes(self):
        return self._attributes

    async def async_update(self):
        import traceback
        try:
            await self._load_latest_tokens()
            try:
                data = await self.hass.async_add_executor_job(
                    get_camera_state, self._device_id, self._access_token, self._user_agent
                )
            except Exception as e:
                if "401" in str(e) or "Unauthorized" in str(e).lower():
                    log_to_file(f"Access token expired in CameraStateSensor: {e}")
                    await self._external_refresh_token()
                    data = await self.hass.async_add_executor_job(
                        get_camera_state, self._device_id, self._access_token, self._user_agent
                    )
                else:
                    raise
            if data:
                self._state = data.get("state", "unknown")
                self._attributes = data
            else:
                self._state = "Unknown"
                self._attributes = {}
        except Exception as e:
            self._state = "Error"
            self._attributes = {}
            log_to_file(f"Error fetching CuboAI camera state: {e}\n{traceback.format_exc()}")