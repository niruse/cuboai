import logging

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

OPTIONS_MAP = {
    "Auto": 0,
    "On": 1,
    "Off": 2,
}
REVERSE_MAP = {v: k for k, v in OPTIONS_MAP.items()}

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    cameras = entry.data.get("cameras", [])
    if not cameras and "device_id" in entry.data:
        cameras = [{"device_id": entry.data["device_id"], "baby_name": entry.data["baby_name"]}]

    entities = []
    for camera in cameras:
        if "uid" in camera:
            entities.append(CuboNightVisionSelect(coordinator, camera, entry.options))

    if entities:
        async_add_entities(entities)

def _set_night_vision(uid, account, password, camera_ip, mode: int):
    """Synchronous function to set night vision."""
    try:
        from .tutk.cuboai_messages import build_get_hw_control, build_set_hw_control
        from .tutk.cuboai_session import get_session

        with get_session(uid, account, password, camera_ip=camera_ip if camera_ip else None, defer_stream_start=False, defer_video_start_late=False, auto_discover_lib=True) as sess:
            if hasattr(sess, 'ioctl'):
                resp_type, raw = sess.ioctl(*build_get_hw_control())
                sess.ioctl(*build_set_hw_control(raw, night_vision_mode=mode))
            else:
                resp_type, raw = sess._send_ioc(*build_get_hw_control())
                sess._send_ioc(*build_set_hw_control(raw, night_vision_mode=mode))
    except Exception as e:
        _LOGGER.error(f"Failed to set night vision: {e}")

class CuboNightVisionSelect(CoordinatorEntity, SelectEntity):
    def __init__(self, coordinator, camera, options):
        super().__init__(coordinator)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]
        self._uid = camera["uid"]
        self._account = camera["account"]
        self._password = camera["password"]
        self._camera_ip = options.get(f"camera_ip_{self._device_id}", "") or camera.get("camera_ip")

        self._attr_name = f"{self._baby_name} Night Vision"
        self._attr_unique_id = f"cuboai_night_vision_{self._device_id}"
        self._attr_icon = "mdi:theme-light-dark"
        self._attr_entity_category = EntityCategory.CONFIG
        self._attr_options = list(OPTIONS_MAP.keys())

    @property
    def current_option(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        val = cam.get("local", {}).get("night_vision")
        return REVERSE_MAP.get(val)

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    async def async_select_option(self, option: str) -> None:
        mode_int = OPTIONS_MAP.get(option)
        if mode_int is not None:
            await self.hass.async_add_executor_job(
                _set_night_vision, self._uid, self._account, self._password, self._camera_ip, mode_int
            )
            cam = self.coordinator.data.setdefault("cameras", {}).setdefault(self._device_id, {})
            cam.setdefault("local", {})["night_vision"] = mode_int
            self.async_write_ha_state()
