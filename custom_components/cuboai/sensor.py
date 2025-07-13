import json
import logging
from datetime import datetime
from homeassistant.helpers.entity import Entity
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

def log_to_file(msg):
    try:
        with open("/config/cuboai_last_alert_debug.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now()} - {msg}\n")
    except Exception as e:
        _LOGGER.error(f"Failed to log to file: {e}")

async def async_setup_entry(hass, entry, async_add_entities):
    device_id = entry.data["device_id"]
    access_token = entry.data["access_token"]
    baby_name = entry.data["baby_name"]
    user_agent = entry.data["user_agent"]
    download_images = entry.options.get("download_images", entry.data.get("download_images", True))

    sensors = [
        CuboBabyInfoSensor(
            name=f"CuboAI Baby Info {baby_name}",
            device_id=device_id,
            baby_name=baby_name,
            access_token=access_token,
            user_agent=user_agent,
        ),
        CuboLastAlertSensor(
            device_id=device_id,
            access_token=access_token,
            user_agent=user_agent,
            name=f"CuboAI Last Alert {baby_name}",
            download_images=download_images
        ),
        CuboSubscriptionSensor(
            access_token=access_token,
            user_agent=user_agent,
            name=f"CuboAI Subscription {baby_name}"
        ),
        CuboCameraStateSensor(
            device_id=device_id,
            access_token=access_token,
            user_agent=user_agent,
            name=f"CuboAI Camera State {baby_name}"
        ),
    ]
    async_add_entities(sensors, update_before_add=True)

class CuboBabyInfoSensor(Entity):
    def __init__(self, name, device_id, baby_name, access_token, user_agent):
        self._name = name
        self._device_id = device_id
        self._baby_name = baby_name
        self._access_token = access_token
        self._user_agent = user_agent
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
        from .api.cuboai_functions import get_camera_profiles_raw
        try:
            profiles = await self.hass.async_add_executor_job(
                get_camera_profiles_raw, self._access_token, self._user_agent
            )
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
            _LOGGER.error("Failed to update Cubo baby profile info: %s", e)
            self._attributes = {
                "baby": None,
                "birth": None,
                "gender": None,
                "device_id": self._device_id
            }

class CuboLastAlertSensor(Entity):
    def __init__(self, device_id, access_token, user_agent, name="CuboAI Last Alert", download_images=True):
        self._device_id = device_id
        self._access_token = access_token
        self._user_agent = user_agent
        self._name = name
        self._state = None
        self._attributes = {}
        self.download_images = download_images

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
        from .api.cuboai_functions import get_recent_alerts, download_image
        try:
            log_to_file("Starting CuboLastAlertSensor update...")
            images_dir = "/config/www/cuboai_images"
            web_base = "/local/cuboai_images"

            alerts = await self.hass.async_add_executor_job(
                get_recent_alerts, self._device_id, self._access_token, self._user_agent, 12, 5
            )
            log_to_file(f"Fetched alerts: {json.dumps(alerts, indent=2)}")

            alert_dicts = []
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
            _LOGGER.error(err_msg)
            self._state = "Error"
            self._attributes = {"alerts": []}
            self._attr_extra_state_attributes = self._attributes

class CuboSubscriptionSensor(Entity):
    def __init__(self, access_token, user_agent, name="CuboAI Subscription"):
        self._access_token = access_token
        self._user_agent = user_agent
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
        from .api.cuboai_functions import get_subscription_info
        import traceback
        try:
            data = await self.hass.async_add_executor_job(
                get_subscription_info, self._access_token, self._user_agent
            )
            if data:
                self._state = data.get("status", "unknown")
                self._attributes = data
            else:
                self._state = "No subscription"
                self._attributes = {}
        except Exception as e:
            self._state = "Error"
            self._attributes = {}
            _LOGGER.error(f"Error fetching CuboAI subscription: {e}\n{traceback.format_exc()}")

class CuboCameraStateSensor(Entity):
    def __init__(self, device_id, access_token, user_agent, name="CuboAI Camera State"):
        self._device_id = device_id
        self._access_token = access_token
        self._user_agent = user_agent
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
        from .api.cuboai_functions import get_camera_state
        import traceback
        try:
            data = await self.hass.async_add_executor_job(
                get_camera_state, self._device_id, self._access_token, self._user_agent
            )
            if data:
                self._state = data.get("state", "unknown")
                self._attributes = data
            else:
                self._state = "Unknown"
                self._attributes = {}
        except Exception as e:
            self._state = "Error"
            self._attributes = {}
            _LOGGER.error(f"Error fetching CuboAI camera state: {e}\n{traceback.format_exc()}")
