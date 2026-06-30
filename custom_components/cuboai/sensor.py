import logging
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    
    cameras = entry.data.get("cameras", [])
    if not cameras and "device_id" in entry.data:
        cameras = [{"device_id": entry.data["device_id"], "baby_name": entry.data["baby_name"]}]

    sensors = []
    for camera in cameras:
        device_id = camera["device_id"]
        baby_name = camera["baby_name"]
        
        sensors.extend(
            [
                CuboBabyInfoSensor(coordinator, device_id, baby_name),
                CuboLastAlertSensor(coordinator, device_id, baby_name),
                CuboSessionHistorySensor(coordinator, device_id, baby_name),
                CuboCameraStateSensor(coordinator, device_id, baby_name),
                CuboWebRTCStreamSensor(coordinator, device_id, baby_name),
            ]
        )
        if "uid" in camera:
            sensors.extend(
                [
                    CuboTemperatureSensor(coordinator, device_id, baby_name),
                    CuboHumiditySensor(coordinator, device_id, baby_name),
                    CuboAIFirmwareSensor(coordinator, device_id, baby_name),
                    CuboCryDetectSensor(coordinator, device_id, baby_name),
                    CuboCoughDetectSensor(coordinator, device_id, baby_name),
                    CuboSleepSafetySensor(coordinator, device_id, baby_name),
                    CuboWifiSensor(coordinator, device_id, baby_name),
                    CuboWifiSSIDSensor(coordinator, device_id, baby_name),
                    CuboStandTypeSensor(coordinator, device_id, baby_name),
                    CuboMatBPMSensor(coordinator, device_id, baby_name),
                    CuboMatStateSensor(coordinator, device_id, baby_name),
                    CuboMatBatterySensor(coordinator, device_id, baby_name),
                    CuboThermometerSensor(coordinator, device_id, baby_name),
                    CuboThermometerBatterySensor(coordinator, device_id, baby_name),
                    CuboConnectionModeSensor(coordinator, device_id, baby_name),
                    CuboConnectedUsersSensor(coordinator, device_id, baby_name),
                    CuboIPAddressSensor(coordinator, device_id, baby_name),
                    CuboMACAddressSensor(coordinator, device_id, baby_name),
                    CuboWiFiRSSISensor(coordinator, device_id, baby_name),
                    CuboWiFiNoiseSensor(coordinator, device_id, baby_name),
                    CuboWiFiChannelSensor(coordinator, device_id, baby_name),
                    CuboTempAlertHighSensor(coordinator, device_id, baby_name),
                    CuboTempAlertLowSensor(coordinator, device_id, baby_name),
                    CuboHumiAlertHighSensor(coordinator, device_id, baby_name),
                    CuboHumiAlertLowSensor(coordinator, device_id, baby_name),
                    CuboFeverAlertHighSensor(coordinator, device_id, baby_name),
                    CuboFeverAlertLowSensor(coordinator, device_id, baby_name),
                    CuboCrySensitivitySensor(coordinator, device_id, baby_name),
                ]
            )

        sensors.append(CuboLastUpdateSensor(coordinator, device_id, baby_name))

    sensors.append(CuboSubscriptionSensor(coordinator, entry.entry_id))
    
    # Ensure global media library sensor is only created once even with multiple cameras
    if "cuboai_media_library_added" not in hass.data:
        hass.data["cuboai_media_library_added"] = True
        sensors.append(CuboMediaLibrarySensor(hass))
    
    # We do not need update_before_add=True because Coordinator already fetched data during async_setup_entry
    async_add_entities(sensors)


class CuboBabyInfoSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Baby Info {baby_name}"
        self._attr_unique_id = f"cuboai_baby_info_{device_id}"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        profile = cam.get("profile", {})
        return profile.get("baby", self._baby_name)

    @property
    def extra_state_attributes(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        profile = cam.get("profile", {})
        return {
            "baby": profile.get("baby"),
            "birth": profile.get("birth"),
            "gender": profile.get("gender"),
            "device_id": profile.get("device_id"),
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboMediaLibrarySensor(SensorEntity):
    def __init__(self, hass):
        self.hass = hass
        self._attr_name = "CuboAI Media Library"
        self._attr_unique_id = "cuboai_media_library_global"
        self._attr_icon = "mdi:music-box-multiple"
        self._attr_native_value = "active"

    async def async_added_to_hass(self):
        """Run when entity about to be added to hass."""
        self.hass.data["cuboai_media_library_entity_id"] = self.entity_id
        from homeassistant.helpers.dispatcher import async_dispatcher_connect
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, "cuboai_media_library_updated", self.async_write_ha_state
            )
        )

    @property
    def extra_state_attributes(self):
        if "cuboai_media_library_instance" in self.hass.data:
            library = self.hass.data["cuboai_media_library_instance"]
            data = library.get_data()
            return {
                "custom_songs": data.get("custom_songs", []),
                "playlists": data.get("playlists", []), "last_update": self.hass.data.get("cuboai_media_library_update_time", 0)
            }
        return {"custom_songs": [], "playlists": []}

class CuboLastAlertSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Last Alert {baby_name}"
        self._attr_unique_id = f"cuboai_last_alert_{device_id}"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("latest_alert", "No alerts")

    @property
    def extra_state_attributes(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        alerts = cam.get("alerts", [])
        return {"alerts": alerts}

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }


class CuboSessionHistorySensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Session History {baby_name}"
        self._attr_unique_id = f"cuboai_session_history_{device_id}"
        self._attr_icon = "mdi:history"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        alerts = cam.get("alerts", [])
        return len(alerts) if alerts is not None else 0

    @property
    def extra_state_attributes(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        alerts = cam.get("alerts", [])
        history = []
        if alerts:
            for a in alerts:
                history.append({
                    "type": a.get("type", "unknown"),
                    "time": a.get("created", ""),
                    "image_url": a.get("image", "")
                })
        return {"alerts": history}

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }


class CuboCameraStateSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Camera State {baby_name}"
        self._attr_unique_id = f"cuboai_camera_state_{device_id}"
        self._attr_device_class = SensorDeviceClass.ENUM
        self._attr_options = ["online", "offline", "unknown"]

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        raw = str(cam.get("camera_state", {}).get("state", "unknown")).lower()
        if raw in ("disconnect", "disconnected", "offline"):
            return "offline"
        return raw if raw == "online" else "unknown"

    @property
    def extra_state_attributes(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        state = cam.get("camera_state", {})
        return {
            "timestamp": state.get("ts"),
            "current_state": state.get("state")
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }


class CuboSubscriptionSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, entry_id):
        super().__init__(coordinator)
        self._entry_id = entry_id
        self._attr_name = "CuboAI Subscription"
        self._attr_unique_id = f"cuboai_subscription_{entry_id}"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        sub = self.coordinator.data.get("subscription")
        if sub:
            return sub.get("status", "unknown")
        return "No subscription"

    @property
    def extra_state_attributes(self):
        sub = self.coordinator.data.get("subscription")
        if not sub: return {}
        return {
            "status": sub.get("status"),
            "kind": sub.get("kind"),
            "service_id": sub.get("service_id"),
            "device_id": sub.get("device_id"),
            "platform": sub.get("platform"),
            "service_start_date": sub.get("service_start_date"),
            "service_end_date": sub.get("service_end_date"),
            "auto_renewal": sub.get("auto_renewal"),
            "order_id": sub.get("order_id")
        }


class CuboLastUpdateSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Last Update {baby_name}"
        self._attr_unique_id = f"cuboai_last_update_{device_id}"
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        import datetime
        val = self.coordinator.data.get("last_updated")
        if val:
            return datetime.datetime.fromisoformat(val)
        return None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboTemperatureSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Temperature {baby_name}"
        self._attr_unique_id = f"cuboai_temperature_{device_id}"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = "°C"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("temperature")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboHumiditySensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Humidity {baby_name}"
        self._attr_unique_id = f"cuboai_humidity_{device_id}"
        self._attr_device_class = SensorDeviceClass.HUMIDITY
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("humidity")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboAIFirmwareSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Firmware {baby_name}"
        self._attr_unique_id = f"cuboai_firmware_{device_id}"
        self._attr_icon = "mdi:information-outline"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("firmware_version", "Unknown")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboCryDetectSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Cry Detection Status {baby_name}"
        self._attr_unique_id = f"cuboai_cry_detect_{device_id}"
        self._attr_icon = "mdi:baby-face-outline"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        val = cam.get("local", {}).get("cry_detect")
        return "On" if val else "Off" if val is False else "Unknown"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboCoughDetectSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Cough Detection Status {baby_name}"
        self._attr_unique_id = f"cuboai_cough_detect_{device_id}"
        self._attr_icon = "mdi:account-voice"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        val = cam.get("local", {}).get("cough_detect")
        return "On" if val else "Off" if val is False else "Unknown"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboSleepSafetySensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Sleep Safety Status {baby_name}"
        self._attr_unique_id = f"cuboai_sleep_safety_{device_id}"
        self._attr_icon = "mdi:shield-check"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        val = cam.get("local", {}).get("sleep_safety")
        return "On" if val else "Off" if val is False else "Unknown"

    @property
    def extra_state_attributes(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return {
            "raw_value": cam.get("local", {}).get("sleep_safety_raw")
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }





class CuboWebRTCStreamSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI WebRTC Stream {baby_name}"
        self._attr_unique_id = f"cuboai_webrtc_stream_{device_id}"
        self._attr_icon = "mdi:cctv"

    @property
    def native_value(self):
        return f"cuboai_{self._device_id}"

    @property
    def extra_state_attributes(self):
        return {
            "go2rtc_server": "http://127.0.0.1:1985",
            "rtsp_url": f"rtsp://127.0.0.1:8555/cuboai_{self._device_id}",
            "web_player_url": f"http://127.0.0.1:1985/stream.html?src=cuboai_{self._device_id}",
            "stream_id": f"cuboai_{self._device_id}"
        }

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }


class CuboWifiSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI WiFi Quality {baby_name}"
        self._attr_unique_id = f"cuboai_wifi_{device_id}"
        self._attr_icon = "mdi:wifi"
        self._attr_native_unit_of_measurement = "%"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("wifi_quality")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboWifiSSIDSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI WiFi SSID {baby_name}"
        self._attr_unique_id = f"cuboai_ssid_{device_id}"
        self._attr_icon = "mdi:wifi-cog"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("ssid")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboConnectionModeSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Connection Mode {baby_name}"
        self._attr_unique_id = f"cuboai_conn_mode_{device_id}"
        self._attr_icon = "mdi:network"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("connection_mode")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboConnectedUsersSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Connected Users {baby_name}"
        self._attr_unique_id = f"cuboai_users_{device_id}"
        self._attr_icon = "mdi:account-group"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("connected_users")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboMatBPMSensor(CoordinatorEntity, SensorEntity):

    @property
    def available(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        state = cam.get("local", {}).get("mat_state")
        return state is not None and state != 0
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Mat BPM {baby_name}"
        self._attr_unique_id = f"cuboai_mat_bpm_{device_id}"
        self._attr_icon = "mdi:heart-pulse"
        self._attr_native_unit_of_measurement = "bpm"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("mat_bpm")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboMatStateSensor(CoordinatorEntity, SensorEntity):

    @property
    def available(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        state = cam.get("local", {}).get("mat_state")
        return state is not None and state != 0
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Mat State {baby_name}"
        self._attr_unique_id = f"cuboai_mat_state_{device_id}"
        self._attr_icon = "mdi:bed"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        val = cam.get("local", {}).get("mat_state")
        return str(val) if val is not None else None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboThermometerSensor(CoordinatorEntity, SensorEntity):

    @property
    def available(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("smart_temp") is not None
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Thermometer Temperature {baby_name}"
        self._attr_unique_id = f"cuboai_smart_temp_{device_id}"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = "°C"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("smart_temp")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboStandTypeSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Stand Type {baby_name}"
        self._attr_unique_id = f"cuboai_stand_{device_id}"
        self._attr_icon = "mdi:human-cane"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("stand_type")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboMatBatterySensor(CoordinatorEntity, SensorEntity):

    @property
    def available(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        state = cam.get("local", {}).get("mat_state")
        return state is not None and state != 0
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Mat Battery {baby_name}"
        self._attr_unique_id = f"cuboai_mat_battery_{device_id}"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_native_unit_of_measurement = "%"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("mat_battery")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboThermometerBatterySensor(CoordinatorEntity, SensorEntity):

    @property
    def available(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("smart_temp") is not None
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Thermometer Battery {baby_name}"
        self._attr_unique_id = f"cuboai_smart_temp_battery_{device_id}"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_native_unit_of_measurement = "%"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("smart_temp_battery")

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }


class CuboIPAddressSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI IP Address {baby_name}"
        self._attr_unique_id = f"cuboai_ip_{device_id}"
        self._attr_icon = "mdi:ip-network"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("wifi_ip")

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboMACAddressSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI MAC Address {baby_name}"
        self._attr_unique_id = f"cuboai_mac_{device_id}"
        self._attr_icon = "mdi:network"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("wifi_mac")

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboWiFiRSSISensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI WiFi RSSI {baby_name}"
        self._attr_unique_id = f"cuboai_rssi_{device_id}"
        self._attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
        self._attr_native_unit_of_measurement = "dBm"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("wifi_rssi")

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboWiFiNoiseSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI WiFi Noise {baby_name}"
        self._attr_unique_id = f"cuboai_noise_{device_id}"
        self._attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
        self._attr_native_unit_of_measurement = "dBm"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("wifi_noise")

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboWiFiChannelSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI WiFi Channel {baby_name}"
        self._attr_unique_id = f"cuboai_channel_{device_id}"
        self._attr_icon = "mdi:router-wireless"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("wifi_channel")

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboTempAlertHighSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Temp Alert High {baby_name}"
        self._attr_unique_id = f"cuboai_temp_alert_high_{device_id}"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = "°C"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("temp_alert_high")

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboTempAlertLowSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Temp Alert Low {baby_name}"
        self._attr_unique_id = f"cuboai_temp_alert_low_{device_id}"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = "°C"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("temp_alert_low")

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboHumiAlertHighSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Humidity Alert High {baby_name}"
        self._attr_unique_id = f"cuboai_humi_alert_high_{device_id}"
        self._attr_device_class = SensorDeviceClass.HUMIDITY
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("humi_alert_high")

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboHumiAlertLowSensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Humidity Alert Low {baby_name}"
        self._attr_unique_id = f"cuboai_humi_alert_low_{device_id}"
        self._attr_device_class = SensorDeviceClass.HUMIDITY
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("humi_alert_low")

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboFeverAlertHighSensor(CoordinatorEntity, SensorEntity):

    @property
    def available(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("fever_alert_high") is not None
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Fever Alert High {baby_name}"
        self._attr_unique_id = f"cuboai_fever_alert_high_{device_id}"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = "°C"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("fever_alert_high")

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboFeverAlertLowSensor(CoordinatorEntity, SensorEntity):

    @property
    def available(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("fever_alert_low") is not None
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Fever Alert Low {baby_name}"
        self._attr_unique_id = f"cuboai_fever_alert_low_{device_id}"
        self._attr_device_class = SensorDeviceClass.TEMPERATURE
        self._attr_native_unit_of_measurement = "°C"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("fever_alert_low")

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

class CuboCrySensitivitySensor(CoordinatorEntity, SensorEntity):
    def __init__(self, coordinator, device_id, baby_name):
        super().__init__(coordinator)
        self._device_id = device_id
        self._baby_name = baby_name
        self._attr_name = f"CuboAI Cry Sensitivity {baby_name}"
        self._attr_unique_id = f"cuboai_cry_sensitivity_{device_id}"
        self._attr_icon = "mdi:tune"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        val = cam.get("local", {}).get("cry_detect_sensitivity")
        if val == 1:
            return "High"
        if val == 2:
            return "Medium"
        if val == 3:
            return "Low"
        return val

    @property
    def device_info(self):
        return {
            "identifiers": {("cuboai", self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

