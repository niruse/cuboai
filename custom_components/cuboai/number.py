import logging

from homeassistant.components.number import NumberEntity
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    cameras = entry.data.get("cameras", [])
    if not cameras and "device_id" in entry.data:
        cameras = [{"device_id": entry.data["device_id"], "baby_name": entry.data["baby_name"]}]

    numbers = []
    for camera in cameras:
        if "uid" in camera:
            numbers.append(CuboLullabyTimerNumber(coordinator, camera, entry.options))
            numbers.append(CuboSpeakerTimerNumber(coordinator, camera, entry.options))
            numbers.append(CuboNightLightBrightnessNumber(coordinator, camera, entry.options))

    if numbers:
        async_add_entities(numbers)


class CuboLullabyTimerNumber(CoordinatorEntity, NumberEntity):
    def __init__(self, coordinator, camera, options):
        super().__init__(coordinator)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]
        self._uid = camera.get("uid")
        self._account = camera.get("account")
        self._password = camera.get("password")
        self._camera_ip = options.get(f"camera_ip_{self._device_id}", "") or camera.get("camera_ip")

        self._attr_name = f"{self._baby_name} Lullaby Timer"
        self._attr_unique_id = f"cuboai_lullaby_timer_{self._device_id}"
        self._attr_icon = "mdi:timer-music"

        self._attr_native_min_value = 0
        self._attr_native_max_value = 60
        self._attr_native_step = 30
        self._attr_native_unit_of_measurement = "min"

        self._timer_value = 30

    @property
    def native_value(self):
        return self._timer_value

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    async def async_set_native_value(self, value: float) -> None:
        self._timer_value = int(value)
        self.async_write_ha_state()

        cam_data = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        vol = cam_data.get("local", {}).get("lullaby_volume", 50)

        from .media_player import _execute_lullaby_cmd

        await self.hass.async_add_executor_job(
            _execute_lullaby_cmd,
            self._uid,
            self._account,
            self._password,
            self._camera_ip,
            "volume",
            None,
            vol,
            self._timer_value,
        )
        await self.coordinator.async_request_refresh()


class CuboSpeakerTimerNumber(CoordinatorEntity, NumberEntity):
    def __init__(self, coordinator, camera, options):
        super().__init__(coordinator)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]

        self._attr_name = f"{self._baby_name} Speaker Play Time"
        self._attr_unique_id = f"cuboai_speaker_timer_{self._device_id}"
        self._attr_icon = "mdi:timer-sand"

        self._attr_native_min_value = 0
        self._attr_native_max_value = 120
        self._attr_native_step = 10
        self._attr_native_unit_of_measurement = "min"

        self._timer_value = 0

    @property
    def native_value(self):
        return self._timer_value

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    async def async_set_native_value(self, value: float) -> None:
        self._timer_value = int(value)
        self.async_write_ha_state()


class CuboNightLightBrightnessNumber(CoordinatorEntity, NumberEntity):
    def __init__(self, coordinator, camera, options):
        super().__init__(coordinator)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]
        self._uid = camera.get("uid")
        self._account = camera.get("account")
        self._password = camera.get("password")
        self._camera_ip = options.get(f"camera_ip_{self._device_id}", "") or camera.get("camera_ip")

        self._attr_name = f"CuboAI {self._baby_name} Night Light Brightness"
        self._attr_unique_id = f"cuboai_night_light_brightness_{self._device_id}"
        self._attr_icon = "mdi:brightness-6"

        self._attr_native_min_value = 1
        self._attr_native_max_value = 100
        self._attr_native_step = 1
        self._attr_native_unit_of_measurement = "%"

    @property
    def native_value(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        bright_pct = cam.get("local", {}).get("brightness")
        if bright_pct is not None:
            return int(bright_pct)
        return 100

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    async def async_set_native_value(self, value: float) -> None:
        bright_pct = int(value)

        from .tutk.cuboai_messages import CuboAIClient
        from .tutk.cuboai_session import get_session

        def _set_brightness():
            with get_session(
                self._uid,
                self._account,
                self._password,
                camera_ip=self._camera_ip if self._camera_ip else None,
                defer_stream_start=False,
                defer_video_start_late=False,
                auto_discover_lib=True,
            ) as sess:
                client = CuboAIClient(sess)
                client.set_brightness(bright_pct)

        await self.hass.async_add_executor_job(_set_brightness)
        await self.coordinator.async_request_refresh()
