import logging

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    cameras = entry.data.get("cameras", [])
    if not cameras and "device_id" in entry.data:
        cameras = [{"device_id": entry.data["device_id"], "baby_name": entry.data["baby_name"]}]

    switches = []
    for camera in cameras:
        if "uid" in camera:
            switches.append(CuboSleepModeSwitch(coordinator, camera, entry.options))
            switches.append(CuboStatusLEDSwitch(coordinator, camera, entry.options))
            switches.append(CuboFlipScreenSwitch(coordinator, camera, entry.options))
            switches.append(CuboBabyPresenceSwitch(coordinator, camera, entry.options))

    if switches:
        async_add_entities(switches)


def _set_sleep_mode(uid, account, password, camera_ip, on: bool):
    """Synchronous function to set sleep mode."""
    try:
        from .tutk.cuboai_messages import build_set_sleep_mode
        from .tutk.cuboai_session import get_session

        with get_session(
            uid,
            account,
            password,
            camera_ip=camera_ip if camera_ip else None,
            defer_stream_start=False,
            defer_video_start_late=False,
            auto_discover_lib=True,
        ) as sess:
            if hasattr(sess, "ioctl"):
                sess.ioctl(*build_set_sleep_mode(on))
            else:
                sess._cubo_set(build_set_sleep_mode(on)[1])
    except Exception as e:
        _LOGGER.error(f"Failed to set sleep mode: {e}")


class CuboSleepModeSwitch(CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator, camera, options):
        super().__init__(coordinator)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]
        self._uid = camera["uid"]
        self._account = camera["account"]
        self._password = camera["password"]
        self._camera_ip = options.get(f"camera_ip_{self._device_id}", "") or camera.get("camera_ip")

        self._attr_name = f"{self._baby_name} Sleep Mode"
        self._attr_unique_id = f"cuboai_sleep_mode_{self._device_id}"
        self._attr_icon = "mdi:sleep"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("sleep_mode_on", False)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    async def async_turn_on(self, **kwargs):
        await self.hass.async_add_executor_job(
            _set_sleep_mode, self._uid, self._account, self._password, self._camera_ip, True
        )
        cam = self.coordinator.data.setdefault("cameras", {}).setdefault(self._device_id, {})
        cam.setdefault("local", {})["sleep_mode_on"] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self.hass.async_add_executor_job(
            _set_sleep_mode, self._uid, self._account, self._password, self._camera_ip, False
        )
        cam = self.coordinator.data.setdefault("cameras", {}).setdefault(self._device_id, {})
        cam.setdefault("local", {})["sleep_mode_on"] = False
        self.async_write_ha_state()


def _set_status_led(uid, account, password, camera_ip, on: bool):
    """Synchronous function to set status led."""
    try:
        import struct

        from .tutk.cuboai_messages import IOTYPE_USER_SET_STATUS_LIGHT_ON_OFF_REQ
        from .tutk.cuboai_session import get_session

        with get_session(
            uid,
            account,
            password,
            camera_ip=camera_ip if camera_ip else None,
            defer_stream_start=False,
            defer_video_start_late=False,
            auto_discover_lib=True,
        ) as sess:
            payload = struct.pack("<III", 0, 1 if on else 0, 0)
            if hasattr(sess, "ioctl"):
                sess.ioctl(IOTYPE_USER_SET_STATUS_LIGHT_ON_OFF_REQ, payload)
            else:
                sess._send_ioc(IOTYPE_USER_SET_STATUS_LIGHT_ON_OFF_REQ, payload)
    except Exception as e:
        _LOGGER.error(f"Failed to set status led: {e}")


class CuboStatusLEDSwitch(CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator, camera, options):
        super().__init__(coordinator)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]
        self._uid = camera["uid"]
        self._account = camera["account"]
        self._password = camera["password"]
        self._camera_ip = options.get(f"camera_ip_{self._device_id}", "") or camera.get("camera_ip")

        self._attr_name = f"{self._baby_name} Status LED"
        self._attr_unique_id = f"cuboai_status_led_{self._device_id}"
        self._attr_icon = "mdi:led-on"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("status_led_on", False)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    async def async_turn_on(self, **kwargs):
        await self.hass.async_add_executor_job(
            _set_status_led, self._uid, self._account, self._password, self._camera_ip, True
        )
        cam = self.coordinator.data.setdefault("cameras", {}).setdefault(self._device_id, {})
        cam.setdefault("local", {})["status_led_on"] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self.hass.async_add_executor_job(
            _set_status_led, self._uid, self._account, self._password, self._camera_ip, False
        )
        cam = self.coordinator.data.setdefault("cameras", {}).setdefault(self._device_id, {})
        cam.setdefault("local", {})["status_led_on"] = False
        self.async_write_ha_state()


def _set_flip_screen(uid, account, password, camera_ip, on: bool):
    """Synchronous function to set flip screen."""
    try:
        from .tutk.cuboai_messages import build_get_hw_control, build_set_hw_control
        from .tutk.cuboai_session import get_session

        with get_session(
            uid,
            account,
            password,
            camera_ip=camera_ip if camera_ip else None,
            defer_stream_start=False,
            defer_video_start_late=False,
            auto_discover_lib=True,
        ) as sess:
            if hasattr(sess, "ioctl"):
                resp_type, raw = sess.ioctl(*build_get_hw_control())
                sess.ioctl(*build_set_hw_control(raw, video_v_flip=on))
            else:
                resp_type, raw = sess._send_ioc(*build_get_hw_control())
                sess._send_ioc(*build_set_hw_control(raw, video_v_flip=on))
    except Exception as e:
        _LOGGER.error(f"Failed to set flip screen: {e}")


class CuboFlipScreenSwitch(CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator, camera, options):
        super().__init__(coordinator)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]
        self._uid = camera["uid"]
        self._account = camera["account"]
        self._password = camera["password"]
        self._camera_ip = options.get(f"camera_ip_{self._device_id}", "") or camera.get("camera_ip")

        self._attr_name = f"{self._baby_name} Flip Screen"
        self._attr_unique_id = f"cuboai_flip_screen_{self._device_id}"
        self._attr_icon = "mdi:flip-vertical"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("video_v_flip", False)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    async def async_turn_on(self, **kwargs):
        await self.hass.async_add_executor_job(
            _set_flip_screen, self._uid, self._account, self._password, self._camera_ip, True
        )
        cam = self.coordinator.data.setdefault("cameras", {}).setdefault(self._device_id, {})
        cam.setdefault("local", {})["video_v_flip"] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self.hass.async_add_executor_job(
            _set_flip_screen, self._uid, self._account, self._password, self._camera_ip, False
        )
        cam = self.coordinator.data.setdefault("cameras", {}).setdefault(self._device_id, {})
        cam.setdefault("local", {})["video_v_flip"] = False
        self.async_write_ha_state()


def _set_baby_presence(uid, account, password, camera_ip, on: bool):
    """Synchronous function to set baby presence alert."""
    try:
        from .tutk.cuboai_messages import build_get_sleep_safety_setting, build_set_sleep_safety_setting
        from .tutk.cuboai_session import get_session

        with get_session(
            uid,
            account,
            password,
            camera_ip=camera_ip if camera_ip else None,
            defer_stream_start=False,
            defer_video_start_late=False,
            auto_discover_lib=True,
        ) as sess:
            if hasattr(sess, "ioctl"):
                resp_type, raw = sess.ioctl(*build_get_sleep_safety_setting())
                sess.ioctl(*build_set_sleep_safety_setting(raw, baby_presence_alert=on))
            else:
                resp_type, raw = sess._send_ioc(*build_get_sleep_safety_setting())
                sess._send_ioc(*build_set_sleep_safety_setting(raw, baby_presence_alert=on))
    except Exception as e:
        _LOGGER.error(f"Failed to set baby presence: {e}")


class CuboBabyPresenceSwitch(CoordinatorEntity, SwitchEntity):
    def __init__(self, coordinator, camera, options):
        super().__init__(coordinator)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]
        self._uid = camera["uid"]
        self._account = camera["account"]
        self._password = camera["password"]
        self._camera_ip = options.get(f"camera_ip_{self._device_id}", "") or camera.get("camera_ip")

        self._attr_name = f"{self._baby_name} Baby Presence"
        self._attr_unique_id = f"cuboai_baby_presence_{self._device_id}"
        self._attr_icon = "mdi:baby-face-outline"
        self._attr_entity_category = EntityCategory.CONFIG

    @property
    def is_on(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("baby_presence", False)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    async def async_turn_on(self, **kwargs):
        await self.hass.async_add_executor_job(
            _set_baby_presence, self._uid, self._account, self._password, self._camera_ip, True
        )
        cam = self.coordinator.data.setdefault("cameras", {}).setdefault(self._device_id, {})
        cam.setdefault("local", {})["baby_presence"] = True
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs):
        await self.hass.async_add_executor_job(
            _set_baby_presence, self._uid, self._account, self._password, self._camera_ip, False
        )
        cam = self.coordinator.data.setdefault("cameras", {}).setdefault(self._device_id, {})
        cam.setdefault("local", {})["baby_presence"] = False
        self.async_write_ha_state()
