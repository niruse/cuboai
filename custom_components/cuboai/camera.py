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

    # Modern HA detects WebRTC support by CLASS introspection (whether
    # async_handle_async_webrtc_offer is overridden) — properties and feature
    # flags can't hide it. So the WebRTC handlers live on a subclass that is
    # only instantiated when the entry opts in (HEVC models, where frontend
    # HLS can't play the video). Default is the plain HLS class: H264+AAC
    # plays natively, no opus transcode, no WebRTC session churn.
    cls = CuboLocalCameraWebRTC if entry.options.get("frontend_webrtc") else CuboLocalCamera

    camera_entities = []
    recording_cameras = []
    for camera in cameras:
        if "uid" in camera:
            camera_entities.append(cls(coordinator, camera))
            # A second entity for recorded footage off the camera's own DVR.
            # It stays idle until cuboai.play_recording points it at a moment.
            recording = CuboRecordingCamera(coordinator, camera)
            recording_cameras.append(recording)
            camera_entities.append(recording)

    # The play_recording service needs to reach these instances to point them
    # at a stream, so keep them alongside the entry's other runtime objects.
    hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})[
        "recording_cameras"
    ] = recording_cameras

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

    def _effective_rtsp_port(self) -> int:
        """The port go2rtc actually bound (it self-heals on conflicts, e.g.
        HA's built-in go2rtc holding 8555), falling back to the configured one."""
        return self.hass.data.get(DOMAIN, {}).get("rtsp_port_effective") or self.coordinator.config_entry.options.get(
            "rtsp_port", self.coordinator.config_entry.data.get("rtsp_port", 8555)
        )

    def _go2rtc_api_base(self) -> str:
        """Base URL of OUR go2rtc API, using the port it actually bound
        (it self-heals to a free port when 1985 is taken, issue #84)."""
        port = self.hass.data.get(DOMAIN, {}).get("api_port_effective", 1985)
        return f"http://127.0.0.1:{port}"

    def _go2rtc_ready(self) -> bool:
        """Whether OUR go2rtc subprocess is alive.

        Every live-stream/snapshot request must be gated on this: if our
        go2rtc never started, the API port may belong to a foreign or
        orphaned go2rtc, and each request would spawn a fresh TUTK producer
        there — the endless 'Using native library' loop of issue #84.
        """
        entry_id = self.coordinator.config_entry.entry_id
        manager = self.hass.data.get(DOMAIN, {}).get(entry_id, {}).get("go2rtc")
        return manager is not None and manager.is_running

    @property
    def extra_state_attributes(self):
        # h264_transcode lets the card editor reflect/toggle the per-camera
        # H.264 transcode option (issue #85).
        h264 = self._device_id in (self.coordinator.config_entry.options.get("h264_cameras") or [])
        return {
            "device_id": self._device_id,
            "uid": self._device_id,
            "rtsp_port": self._effective_rtsp_port(),
            "h264_transcode": h264,
        }

    @property
    def supported_features(self) -> int:
        from homeassistant.components.camera import CameraEntityFeature

        return CameraEntityFeature.STREAM

    @property
    def frontend_stream_type(self) -> str | None:
        """HLS: the camera delivers H264+AAC, which HLS carries natively
        (sound included, no transcode). The WebRTC path needs an AAC→Opus
        ffmpeg transcode inside go2rtc that keeps EOF-ing — every death kills
        the WebRTC session, so the more-info view lags and reconnects in a
        loop ("Received event for unknown subscription"). WebRTC lives on the
        CuboLocalCameraWebRTC subclass for HEVC models (frontend_webrtc
        option)."""
        from homeassistant.components.camera import StreamType

        return getattr(StreamType, "HLS", "hls")

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """Return a still image response from the camera."""
        # 1. Try to get a LIVE snapshot from go2rtc API — only when OUR go2rtc
        # is actually running (otherwise the port may be a stranger's, #84)
        if self._go2rtc_ready():
            import aiohttp
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            url = f"{self._go2rtc_api_base()}/api/frame.jpeg?src=cuboai_{self._device_id}"
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

    def _debug_logs_enabled(self) -> bool:
        return bool(self.coordinator.config_entry.options.get("enable_debug_logs"))

    async def _log_stream_diag(self, moment: str) -> None:
        """Dump go2rtc's live view of the combined stream into the HA log.

        Gated on 'Enable debug logs'. This is the decisive evidence for
        #85-type reports (H.265 camera fails in HomeKit/HLS): it shows which
        codec go2rtc actually offers on each producer track (HEVC vs H.264 —
        i.e. whether the transcode is really applied), whether the producers
        are delivering bytes at all, and whether the RTSP consumer (HA stream
        worker / HomeKit) received anything or gave up empty-handed.
        """
        try:
            import json
            import re

            import aiohttp
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            session = async_get_clientsession(self.hass)
            async with session.get(
                f"{self._go2rtc_api_base()}/api/streams?src=cuboai_combined_{self._device_id}",
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                raw = await resp.text()
            # exec producer URLs embed the TUTK credentials — redact BEFORE logging
            redacted = re.sub(r"(CUBOAI_(?:UID|ACCOUNT|PASSWORD)=)[^\s\"\\]+", r"\1XXX", raw)
            try:
                data = json.loads(redacted)

                def _fmt(item):
                    src = str(item.get("source") or item.get("url") or item.get("remote_addr") or "")[:80]
                    medias = "; ".join(item.get("medias") or [])
                    # per-track codec + packet counters — 'hevc:0pkts' vs
                    # 'h264:2395pkts' is the whole diagnosis in one glance
                    tracks = []
                    for r in (item.get("receivers") or []) + (item.get("senders") or []):
                        codec = (r.get("codec") or {}).get("codec_name")
                        tracks.append(f"{codec}:{r.get('packets')}pkts")
                    return (
                        f"({item.get('format_name')} src={src!r} medias=[{medias}] "
                        f"tracks=[{', '.join(tracks)}] recv={item.get('bytes_recv')} send={item.get('bytes_send')})"
                    )

                parts = [
                    f"{kind[:-1]}{_fmt(item)}" for kind in ("producers", "consumers") for item in data.get(kind) or []
                ]
                summary = " | ".join(parts) if parts else redacted[:1500]
            except ValueError:
                summary = redacted[:1500]
            _LOGGER.info("[stream diag %s] cuboai_combined_%s: %s", moment, self._device_id, summary)
        except Exception as e:
            _LOGGER.info("[stream diag %s] cuboai_combined_%s: probe failed: %s", moment, self._device_id, e)

    async def stream_source(self) -> str | None:
        """Return the stream source."""
        # go2rtc failed to start (port conflict, missing binary, ...): report
        # "no stream source" once instead of letting the stream worker hammer
        # ports that may belong to another process (issue #84).
        if not self._go2rtc_ready():
            _LOGGER.warning("CuboAI go2rtc is not running — no stream source for %s", self._device_id)
            return None

        # This connects to our internal go2rtc instance via RTSP
        # We use the combined stream to support two-way audio (microphone)
        rtsp_port = self._effective_rtsp_port()

        # When NVR mode protects the RTSP listener, HA must authenticate too
        auth = ""
        opts = self.coordinator.config_entry.options
        if opts.get("nvr_enabled") and opts.get("nvr_password"):
            from urllib.parse import quote

            auth = f"{quote(opts.get('nvr_username') or 'cuboai', safe='')}:{quote(opts['nvr_password'], safe='')}@"

        # Pre-warm the go2rtc producer: on a cold start the pure-python engine
        # needs several seconds to connect to the camera and deliver the first
        # HEVC keyframe — longer than the HLS stream worker's demux timeout,
        # which then logs "Error demuxing stream (Operation timed out)" and
        # retries. Requesting a frame first blocks until the producer is live,
        # so the RTSP consumer gets packets immediately. Best-effort only.
        import time

        t0 = time.monotonic()
        try:
            import aiohttp
            from homeassistant.helpers.aiohttp_client import async_get_clientsession

            session = async_get_clientsession(self.hass)
            async with session.get(
                f"{self._go2rtc_api_base()}/api/frame.jpeg?src=cuboai_combined_{self._device_id}",
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                await resp.read()
                if resp.status == 200:
                    _LOGGER.debug("Stream pre-warm for %s ready in %.1fs", self._device_id, time.monotonic() - t0)
                else:
                    # A failed pre-warm is the precursor of the stream worker's
                    # "Error demuxing stream while finding first packet" timeout —
                    # surface it at warning so the sequence shows without debug logging.
                    _LOGGER.warning(
                        "Stream pre-warm for %s returned HTTP %s after %.1fs — the producer "
                        "delivered no frame; the RTSP consumer will likely time out. Turn on "
                        "'Enable debug logs' in the CuboAI options and check "
                        "custom_components/cuboai/bin/go2rtc.log.",
                        self._device_id,
                        resp.status,
                        time.monotonic() - t0,
                    )
        except Exception as e:
            _LOGGER.warning(
                "Stream pre-warm for %s failed after %.1fs (%s) — the producer delivered no "
                "frame; the RTSP consumer will likely time out. Turn on 'Enable debug logs' "
                "in the CuboAI options and check custom_components/cuboai/bin/go2rtc.log.",
                self._device_id,
                time.monotonic() - t0,
                e,
            )

        # With debug logs on, capture go2rtc's view of the stream right now
        # (post-pre-warm: producer codecs — is the H.264 transcode applied?)
        # and again 20s later (did the RTSP consumer actually receive data,
        # or time out empty-handed?). See #85.
        if self._debug_logs_enabled():
            await self._log_stream_diag("at stream request")

            from homeassistant.helpers.event import async_call_later

            @callback
            def _delayed_diag(_now):
                self.hass.async_create_task(self._log_stream_diag("+20s"))

            async_call_later(self.hass, 20, _delayed_diag)

        return f"rtsp://{auth}127.0.0.1:{rtsp_port}/cuboai_combined_{self._device_id}"

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }


class CuboLocalCameraWebRTC(CuboLocalCamera):
    """WebRTC-frontend variant, used only when the entry sets frontend_webrtc.

    HA decides a camera supports native WebRTC by checking whether the CLASS
    overrides async_handle_async_webrtc_offer — so these handlers must not
    exist on the default (HLS) class at all, or every HA frontend prefers
    WebRTC and lands on the fragile AAC→Opus transcode. HEVC models need this
    variant because frontend HLS cannot play HEVC video.
    """

    @property
    def supported_features(self) -> int:
        from homeassistant.components.camera import CameraEntityFeature

        features = CameraEntityFeature.STREAM
        if hasattr(CameraEntityFeature, "WEB_RTC"):
            features |= CameraEntityFeature.WEB_RTC
        return features

    @property
    def frontend_stream_type(self) -> str | None:
        from homeassistant.components.camera import StreamType

        return getattr(StreamType, "WEB_RTC", "web_rtc")

    async def _go2rtc_webrtc_offer(self, offer_sdp: str) -> str | None:
        """POST the WebRTC offer to the internal go2rtc and return the answer SDP."""
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        if not self._go2rtc_ready():
            _LOGGER.error("CuboAI go2rtc is not running — cannot answer the WebRTC offer")
            return None

        # We use the combined stream to enable the WebRTC native two-way audio mic button
        url = f"{self._go2rtc_api_base()}/api/webrtc?src=cuboai_combined_{self._device_id}"
        try:
            session = async_get_clientsession(self.hass)
            async with session.post(url, data=offer_sdp, headers={"Content-Type": "application/sdp"}) as resp:
                # go2rtc answers 200 or 201 (Created) depending on version
                if resp.status in (200, 201):
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


class CuboRecordingCamera(CoordinatorEntity, Camera):
    """Recorded footage from the camera's own on-board DVR.

    Distinct from the live camera in one important way: there is nothing to
    show until someone asks for a moment. `cuboai.play_recording` publishes a
    playback stream for a chosen time, and this entity plays it; before that it
    reports unavailable rather than an empty picture.

    The DVR producer runs in its own process against its own engine copy (see
    tutk/playback_engine), so nothing here affects the live stream.
    """

    _attr_icon = "mdi:rewind"

    def __init__(self, coordinator, camera):
        super().__init__(coordinator)
        Camera.__init__(self)
        self._device_id = camera["device_id"]
        self._baby_name = camera["baby_name"]
        self._attr_name = f"{self._baby_name} Recording"
        self._attr_unique_id = f"cuboai_recording_camera_{self._device_id}"
        #: Set by the play_recording service; None means nothing requested yet.
        self._source: str | None = None
        self._playing_from: int | None = None

    @property
    def supported_features(self) -> int:
        from homeassistant.components.camera import CameraEntityFeature

        return CameraEntityFeature.STREAM

    @property
    def frontend_stream_type(self) -> str | None:
        """HLS, matching the live entity: the DVR producer muxes the same
        MPEG-TS (HEVC/H264 + AAC) the live path does."""
        from homeassistant.components.camera import StreamType

        return getattr(StreamType, "HLS", "hls")

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        """A still from the recorded stream, via go2rtc's frame endpoint."""
        if self._source is None:
            return None
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        port = self.hass.data.get(DOMAIN, {}).get("api_port_effective", 1985)
        url = f"http://127.0.0.1:{port}/api/frame.jpeg?src=cuboai_dvr_{self._device_id}"
        try:
            session = async_get_clientsession(self.hass)
            # Generous: the producer has to connect to the camera and seek
            # before the first recorded frame exists.
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if len(data) > 1000:
                        return data
        except Exception as err:  # noqa: BLE001 - no frame is not an error here
            _LOGGER.debug("No recorded frame yet for %s: %s", self._device_id, err)
        return None

    @property
    def available(self) -> bool:
        """Always available, even with nothing loaded.

        Reporting unavailable until a moment is requested reads well and is
        unusable: Home Assistant does not publish state attributes for an
        unavailable entity, so `device_id` and `dvr` never reached the
        frontend, and the card could not find the entity it needs in order to
        start the playback that would have made it available. The state is
        `idle` with no picture until `play_recording` points it somewhere,
        which is what a DVR does anyway.
        """
        return True

    @property
    def extra_state_attributes(self):
        """Carries `device_id` for the same reason the live camera does.

        The card has to pair this entity with the live one it is already
        showing, and issue #89 was precisely what happens when a card matches a
        camera by guessing at its entity id: `_attr_has_entity_name` is not set
        here either, so this entity is `camera.<baby>_recording` with no
        `cuboai_` in it, and any prefix test would match nothing on every
        install. `dvr` marks it as the playback entity so the pairing survives
        a rename.
        """
        attrs = {
            "device_id": self._device_id,
            "uid": self._device_id,
            "dvr": True,
            "playing_from": None,
        }
        if self._playing_from is not None:
            import datetime as dt

            attrs["playing_from"] = dt.datetime.fromtimestamp(
                self._playing_from, dt.UTC
            ).isoformat()
        return attrs

    def set_playback(self, source: str | None, start_epoch: int | None) -> None:
        """Point this entity at a published playback stream (or clear it)."""
        self._source = source
        self._playing_from = start_epoch
        self.async_write_ha_state()

    async def stream_source(self) -> str | None:
        return self._source

    @property
    def device_info(self):
        return {
            "identifiers": {(DOMAIN, self._device_id)},
            "name": f"CuboAI {self._baby_name}",
            "manufacturer": "CuboAI",
            "model": "Baby Monitor",
        }
