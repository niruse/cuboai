import logging

from homeassistant.components.number import NumberEntity, RestoreNumber
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .utils import retry_camera_command

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


class CuboLullabyTimerNumber(CoordinatorEntity, RestoreNumber):
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

        # Camera-native timer: only the durations the camera firmware supports
        # (0 = repeat forever). Card playback uses Play Time + an HA-sent stop
        # instead, so it is not limited to these values.
        self._attr_native_min_value = 0
        self._attr_native_max_value = 60
        self._attr_native_step = 30
        self._attr_native_unit_of_measurement = "min"

        # Restored across reloads; used only until the camera tells us its own value.
        self._timer_value = 30
        #: A value the user picked that has not yet been pushed to the camera. It
        #: wins over the camera's reading so a selection made before pressing play
        #: is not overwritten by the next poll; it clears once the camera agrees.
        self._pending = None

    async def async_added_to_hass(self) -> None:
        """Restore the last timer value after a reload or HA restart.

        Options changes (e.g. toggling debug logs) reload the whole
        integration; without restore the timer silently resets.
        """
        await super().async_added_to_hass()
        try:
            last = await self.async_get_last_number_data()
            if last is not None and last.native_value is not None:
                self._timer_value = int(last.native_value)
        except Exception:
            _LOGGER.debug("Could not restore lullaby timer value", exc_info=True)

    @property
    def _camera_minutes(self):
        """The sleep timer the CAMERA reports, in minutes, or None if unknown.

        From GET_LULLABY_SCHEDULE (timer_mode @8): 0 = repeat forever,
        1800 = 30 min, 3600 = 60 min.
        """
        data = self.coordinator.data or {}
        cam = data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("lullaby_timer_minutes")

    @property
    def native_value(self):
        """What the camera is actually set to, unless a newer local pick is pending.

        This used to return a purely local value that started at a hardcoded 30 and
        was never reconciled with the camera — so a lullaby the camera was repeating
        indefinitely still showed "30 min" in Home Assistant. The camera reports its
        real timer on a response the coordinator already reads.
        """
        cam_minutes = self._camera_minutes
        if self._pending is not None:
            if cam_minutes is not None and int(cam_minutes) == int(self._pending):
                self._pending = None      # the camera caught up
            else:
                return self._pending
        if cam_minutes is not None:
            return int(cam_minutes)
        return self._timer_value

    @property
    def extra_state_attributes(self):
        cam_minutes = self._camera_minutes
        data = self.coordinator.data or {}
        cam = data.get("cameras", {}).get(self._device_id, {})
        return {
            "camera_timer_minutes": cam_minutes,
            "camera_timer_mode": cam.get("local", {}).get("lullaby_timer_name"),
            "pending_change": self._pending,
            # True while Home Assistant is showing a value the camera has not adopted.
            "differs_from_camera": (
                self._pending is not None
                and cam_minutes is not None
                and int(cam_minutes) != int(self._pending)
            ),
        }

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
        # Held as pending until the camera reports this value back, so the pick
        # survives the next poll instead of snapping to the camera's old setting.
        self._pending = int(value)
        self.async_write_ha_state()

        # Notify the lullaby player: if a NATIVE lullaby is playing it pushes
        # the new duration to the camera; card sessions (Play Time) ignore it.
        from homeassistant.helpers.dispatcher import async_dispatcher_send

        async_dispatcher_send(self.hass, f"cuboai_lullaby_timer_changed_{self._device_id}", self._timer_value)


class CuboSpeakerTimerNumber(CoordinatorEntity, RestoreNumber):
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

    async def async_added_to_hass(self) -> None:
        """Restore the last Play Time after a reload or HA restart.

        Without this, any options change (e.g. enabling debug logs) reloads
        the integration and resets Play Time to 0 (= Infinite), so a queued
        playlist plays a single pass and stops instead of looping.
        """
        await super().async_added_to_hass()
        try:
            last = await self.async_get_last_number_data()
            if last is not None and last.native_value is not None:
                self._timer_value = int(last.native_value)
        except Exception:
            _LOGGER.debug("Could not restore speaker play time", exc_info=True)

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

        @retry_camera_command("Night light brightness command")
        def _set_brightness():
            # Imported here so the (potentially heavy) tutk modules load in the
            # executor thread, not the event loop.
            from .tutk.cuboai_messages import CuboAIClient
            from .tutk.cuboai_session import get_session

            with get_session(
                self._uid,
                self._account,
                self._password,
                camera_ip=self._camera_ip if self._camera_ip else None,
                defer_stream_start=False,
                defer_video_start_late=False,
                # auto_discover_lib=False: pure is the guaranteed backend everywhere in this
                # integration. An auto-discovered libIOTCAPIs_ALL (async_ensure_dependencies
                # downloads one into libs/<arch>/) would otherwise put SOME sessions on the
                # native backend while switch.py/light.py and the streamers stay pure. Only an
                # explicit lib_path/CUBOAI_LIB selects native.
                auto_discover_lib=False,
            ) as sess:
                client = CuboAIClient(sess)
                client.set_brightness(bright_pct)

        await self.hass.async_add_executor_job(_set_brightness)
        await self.coordinator.async_request_refresh()
