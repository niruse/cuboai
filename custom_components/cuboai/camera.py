import logging

from homeassistant.components.camera import Camera
from homeassistant.core import callback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]

    cameras = entry.data.get("cameras", [])
    if not cameras and "device_id" in entry.data:
        cameras = [{"device_id": entry.data["device_id"], "baby_name": entry.data["baby_name"]}]

    camera_entities = []
    for camera in cameras:
        if "uid" in camera:
            camera_entities.append(CuboLocalCamera(coordinator, camera))

    if camera_entities:
        async_add_entities(camera_entities)


class CuboLocalCamera(CoordinatorEntity, Camera):
    def __init__(self, coordinator, camera):
        super().__init__(coordinator)
        Camera.__init__(self)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]

        self._attr_name = f"{self._baby_name} Local Camera"
        self._attr_unique_id = f"cuboai_local_camera_{self._device_id}"
        self._attr_is_streaming = True

    @property
    def extra_state_attributes(self):
        rtsp_port = self.coordinator.config_entry.options.get(
            "rtsp_port", self.coordinator.config_entry.data.get("rtsp_port", 8555)
        )
        return {"device_id": self._device_id, "uid": self._device_id, "rtsp_port": rtsp_port}

    @property
    def supported_features(self) -> int:
        from homeassistant.components.camera import CameraEntityFeature

        features = CameraEntityFeature.STREAM
        # Dynamically add WEB_RTC if the current HA version supports it
        if hasattr(CameraEntityFeature, "WEB_RTC"):
            features |= CameraEntityFeature.WEB_RTC
        return features

    @property
    def frontend_stream_type(self) -> str | None:
        """Return the type of stream supported by this camera."""
        from homeassistant.components.camera import StreamType

        # If WebRTC is supported, force WebRTC on frontend to avoid HLS HEVC failure
        return getattr(StreamType, "WEB_RTC", "web_rtc")

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Return a still image response from the camera."""
        # 1. Try to get a LIVE snapshot from go2rtc API
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        url = f"http://127.0.0.1:1985/api/frame.jpeg?src=cuboai_{self._device_id}"
        try:
            session = async_get_clientsession(self.hass)
            # 5 second timeout so we don't hang HA if camera is offline
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=5)) as resp:
                if resp.status == 200:
                    image_bytes = await resp.read()
                    if len(image_bytes) > 1000:  # Ensure it's a real image, not an empty file
                        return image_bytes
        except Exception as e:
            _LOGGER.debug(f"Failed to get live snapshot from go2rtc: {e}")

        # 2. Fall back to the last alert image if live stream is unavailable
        cam = self.coordinator.data.get("cameras", {}).get(self._device_id, {})
        alerts = cam.get("alerts", [])
        if alerts and len(alerts) > 0:
            latest_alert = alerts[0]
            alert_id = latest_alert.get("id")
            if alert_id:
                import os

                filename = f"{self._device_id}_{alert_id}.jpg"
                local_path = os.path.join(self.coordinator._images_dir, filename)
                try:
                    import aiofiles
                    import aiofiles.os

                    if await aiofiles.os.path.exists(local_path):
                        async with aiofiles.open(local_path, "rb") as f:
                            return await f.read()
                except Exception as e:
                    _LOGGER.error(f"Failed to read local camera thumbnail: {e}")
        return None

    async def stream_source(self) -> str | None:
        """Return the stream source."""
        # This connects to our internal go2rtc instance via RTSP
        # We use the combined stream to support two-way audio (microphone)
        rtsp_port = self.coordinator.config_entry.options.get(
            "rtsp_port", self.coordinator.config_entry.data.get("rtsp_port", 8555)
        )
        return f"rtsp://127.0.0.1:{rtsp_port}/cuboai_combined_{self._device_id}"

    async def _go2rtc_webrtc_offer(self, offer_sdp: str) -> str | None:
        """POST the WebRTC offer to the internal go2rtc and return the answer SDP."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        # We use the combined stream to enable the WebRTC native two-way audio mic button
        url = f"http://127.0.0.1:1985/api/webrtc?src=cuboai_combined_{self._device_id}"
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(url, data=offer_sdp, headers={"Content-Type": "application/sdp"}) as resp:
                if resp.status == 200:
                    return await resp.text()
                _LOGGER.error(f"go2rtc returned status {resp.status} for WebRTC offer")
        except Exception as e:
            _LOGGER.error(f"Failed to handle WebRTC offer: {e}")
        return None

    async def async_handle_web_rtc_offer(self, offer_sdp: str) -> str | None:
        """Handle the WebRTC offer (legacy API, HA < 2024.11)."""
        return await self._go2rtc_webrtc_offer(offer_sdp)

    async def async_handle_async_webrtc_offer(self, offer_sdp: str, session_id: str, send_message) -> None:
        """Handle the WebRTC offer (async API, HA 2024.11+; the legacy path was removed in 2025.x)."""
        try:
            from homeassistant.components.camera.webrtc import WebRTCAnswer, WebRTCError
        except ImportError:
            # Very old HA without the async WebRTC API — legacy handler covers it.
            return

        answer = await self._go2rtc_webrtc_offer(offer_sdp)
        if answer:
            send_message(WebRTCAnswer(answer))
        else:
            send_message(WebRTCError("go2rtc_error", "go2rtc did not return a WebRTC answer"))

    async def async_on_webrtc_candidate(self, session_id: str, candidate) -> None:
        """go2rtc's sync /api/webrtc exchange is non-trickle; remote candidates are not needed."""

    @callback
    def close_webrtc_session(self, session_id: str) -> None:
        """Nothing to clean up: the exchange is stateless on our side."""

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }
