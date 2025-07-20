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

    def _load_latest_tokens(self):
        latest_access = load_access_token() or self._access_token
        latest_refresh = load_refresh_token() or self._refresh_token
        self._access_token = latest_access
        self._refresh_token = latest_refresh

    async def _external_refresh_token(self):
        self._load_latest_tokens()
        access_token, refresh_token, _ = await self.hass.async_add_executor_job(
            refresh_access_token_only,
            self._refresh_token,
            self._user_agent,
        )
        self._access_token = access_token
        self._refresh_token = refresh_token
        save_access_token(access_token)
        save_refresh_token(refresh_token)

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
            self._load_latest_tokens()
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
    def __init__(self, hass, entry, device_id, access_token, refresh_token, user_agent, name="CuboAI Last Alert", download_images=True):
        super().__init__(hass, entry, access_token, refresh_token, user_agent)
        self._device_id = device_id
        self._name = name
        self._state = None
        self._attributes = {}
        
    @property
    def download_images(self):
        return self._entry.options.get("download_images", self._entry.data.get("download_images", True))

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

    async def async_update(self):
        import traceback
        import os
        from pathlib import Path

        images_dir = "/config/www/cuboai_images"
        web_base = "/local/cuboai_images"
        try:
            self._load_latest_tokens()
            try:
                alerts = await self.hass.async_add_executor_job(
                    get_n_alerts_paged, self._device_id, self._access_token, self._user_agent, 5, 12
                )
            except Exception as e:
                if "401" in str(e) or "Unauthorized" in str(e).lower():
                    log_to_file(f"Access token expired in LastAlertSensor: {e}")
                    await self._external_refresh_token()
                    alerts = await self.hass.async_add_executor_job(
                        get_n_alerts_paged, self._device_id, self._access_token, self._user_agent, 5, 12
                    )
                else:
                    raise

            log_to_file(f"Fetched alerts: {json.dumps(alerts, indent=2)}")
            alert_dicts = []
            downloaded_filenames = []

            if alerts:
                for alert in alerts:
                    local_image_path = None
                    if self.download_images and alert.get("image"):
                        filename = f"{self._device_id}_{alert.get('id')}.jpg"
                        try:
                            await self.hass.async_add_executor_job(
                                download_image, alert.get("image"), self._access_token, self._user_agent, images_dir, filename
                            )
                            local_image_path = f"{web_base}/{filename}"
                            downloaded_filenames.append(filename)
                            log_to_file(f"Downloaded image to: {local_image_path}")
                        except Exception as e:
                            log_to_file(f"Image download failed: {e}")
                            local_image_path = None

                    alert_dicts.append({
                        "type": alert.get("type"),
                        "created": alert.get("created"),
                        "params": alert.get("params"),
                        "image": local_image_path,
                        "id": alert.get("id"),
                    })

                # 🔁 Cleanup old images: keep only the 5 most recent
                try:
                    all_images = sorted(
                        Path(images_dir).glob(f"{self._device_id}_*.jpg"),
                        key=lambda f: f.stat().st_mtime,
                        reverse=True
                    )
                    for old_file in all_images[5:]:  # Keep latest 5
                        log_to_file(f"Deleting old image: {old_file.name}")
                        old_file.unlink()
                except Exception as e:
                    log_to_file(f"Error cleaning up old images: {e}")

                latest = alert_dicts[0]
                self._state = latest.get("type", "Unknown")
                self._attributes = {"alerts": alert_dicts}
                self._attr_extra_state_attributes = self._attributes
                log_to_file(f"Set state: {self._state}, attributes: {json.dumps(self._attributes)}")
            else:
                self._state = "No alerts"
                self._attributes = {"alerts": []}
                self._attr_extra_state_attributes = self._attributes
                log_to_file("No alerts found.")
        except Exception as e:
            err_msg = f"Error fetching Cubo alerts: {e}\n{traceback.format_exc()}"
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
            self._load_latest_tokens()
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
            self._load_latest_tokens()
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