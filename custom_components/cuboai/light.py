import asyncio
import logging
import os
import platform

from homeassistant.components.light import LightEntity, ColorMode, ATTR_BRIGHTNESS
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    
    cameras = entry.data.get("cameras", [])
    if not cameras and "device_id" in entry.data:
        cameras = [{"device_id": entry.data["device_id"], "baby_name": entry.data["baby_name"]}]

    lights = []
    for camera in cameras:
        if "uid" in camera:  # Only add local features if credentials exist
            lights.append(CuboNightLight(coordinator, camera, entry.options))
            
    if lights:
        async_add_entities(lights)

def _set_night_light(uid, account, password, camera_ip, on: bool, brightness: int = None):
    """Synchronous function to set night light."""
    try:
        from .tutk.cuboai_session import get_session
        from .tutk.cuboai_messages import CuboAIClient
        
        with get_session(uid, account, password, camera_ip=camera_ip if camera_ip else None, defer_stream_start=False, defer_video_start_late=False, auto_discover_lib=True) as sess:
            client = CuboAIClient(sess)
            if brightness is not None:
                client.set_brightness(brightness)
            if on is not None:
                client.set_night_light(on)
    except Exception as e:
        _LOGGER.error(f"Failed to set night light: {e}")

class CuboNightLight(CoordinatorEntity, LightEntity):
    def __init__(self, coordinator, camera, options):
        super().__init__(coordinator)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]
        self._uid = camera["uid"]
        self._account = camera["account"]
        self._password = camera["password"]
        self._camera_ip = options.get(f"camera_ip_{self._device_id}", "") or camera.get("camera_ip")
        
        self._attr_name = f"{self._baby_name} Night Light"
        self._attr_unique_id = f"cuboai_night_light_{self._device_id}"
        self._attr_icon = "mdi:lightbulb-night"
        self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
        self._attr_color_mode = ColorMode.BRIGHTNESS

    @property
    def is_on(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        return cam.get("local", {}).get("night_light_on", False)

    @property
    def brightness(self):
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        bright_pct = cam.get("local", {}).get("brightness")
        if bright_pct is not None:
            return int(bright_pct * 255.0 / 100.0)
        return None

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }

    async def async_turn_on(self, **kwargs):
        brightness_ha = kwargs.get(ATTR_BRIGHTNESS)
        brightness_pct = int(brightness_ha / 255.0 * 100) if brightness_ha is not None else None
        await self.hass.async_add_executor_job(
            _set_night_light, self._uid, self._account, self._password, self._camera_ip, True, brightness_pct
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        await self.hass.async_add_executor_job(
            _set_night_light, self._uid, self._account, self._password, self._camera_ip, False, None
        )
        await self.coordinator.async_request_refresh()
