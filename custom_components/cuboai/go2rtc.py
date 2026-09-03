import asyncio
import logging
import os
import socket
import sys

import yaml
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


#: The go2rtc API port the integration aims for. Not user-configurable (unlike
#: rtsp_port), but NOT a promise either: _resolve_ports self-heals to a nearby
#: free port when it's taken and publishes the result as api_port_effective in
#: hass.data — which every consumer (camera entities, sensors, the card) reads
#: instead of assuming this value. Code touching the API port must use this
#: constant or api_port_effective, never a bare 1985.
DESIRED_API_PORT = 1985


def _port_bindable(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("0.0.0.0", port))
            return True
        except OSError:
            return False


class Go2RTCManager:
    """Manages the internal go2rtc subprocess for CuboAI local streaming."""

    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.process: asyncio.subprocess.Process | None = None
        self._config_path = os.path.join(os.path.dirname(__file__), "bin", "go2rtc.yaml")
        self._binary_path = os.path.join(os.path.dirname(__file__), "bin", "go2rtc")
        self._streams = {}
        self._cameras = []
        self._options = {}

    def update_streams(self, cameras: list[dict], options: dict = None):
        """Update the streams list based on configured cameras. The actual resolution happens in start()."""
        self._cameras = cameras
        self._options = options or {}
        self._streams = {}

    async def _resolve_codecs(self):
        """Resolve video codecs for all cameras asynchronously."""
        script_dir = os.path.join(os.path.dirname(__file__), "tutk")
        video_script = os.path.join(script_dir, "cuboai_stream_video.py")
        debug_logs = bool(self._options.get("enable_debug_logs"))

        for cam in self._cameras:
            dev_id = cam.get("device_id")
            uid = cam.get("uid", "")
            account = cam.get("account", "")
            pwd = cam.get("password", "")
            camera_ip = self._options.get(f"camera_ip_{dev_id}", "") or cam.get("camera_ip", "")

            # We MUST set CUBOAI_MUX_AUDIO=1 so the upstream engine embeds the AAC audio into the MPEG-TS stream
            env_vars = f"env CUBOAI_UID={uid} CUBOAI_ACCOUNT={account} CUBOAI_PASSWORD={pwd} CUBOAI_MUX_AUDIO=1 "
            if camera_ip:
                env_vars += f"CUBOAI_CAMERA_IP={camera_ip} "
            if debug_logs:
                # Stream-engine diagnostics on the producers' stderr, which go2rtc
                # forwards into go2rtc.log at exec:debug (enabled in _generate_config):
                # VERBOSE = a [health] line every 10s (fps/bitrate/loss/recovery/keyframe
                # completeness); LOG_FRAMEINFO = one FICENSUS line per assembled AU
                # (codec_id, kind, keyframe flag, trailer bytes) capped at the first 300
                # AUs — enough to see the whole startup of an unknown camera model
                # (issue #85) without growing the log forever.
                env_vars += (
                    "CUBOAI_VERBOSE=1 CUBOAI_VERBOSE_INTERVAL=10 CUBOAI_LOG_FRAMEINFO=1 CUBOAI_LOG_FRAMEINFO_MAX=300 "
                )

            backchannel_script = os.path.join(script_dir, "cuboai_stream_backchannel.py")

            # Use the exact interpreter HA runs under: a bare "python3" resolves to the
            # system interpreter on venv installs, which lacks av/yt-dlp and fails imports.
            py = sys.executable or "python3"

            # H.265/HEVC cameras (e.g. Cubo 3 / SW05) can't be consumed by HomeKit
            # or HA's stream/HLS path — both are H.264-only, so the passthrough
            # 'video=copy' stream fails with 'demuxing ... timed out' / HomeKit
            # 'No Response' (#85). When the per-camera 'force_h264' option is on,
            # go2rtc transcodes the video to H.264. Default off keeps the
            # efficient passthrough for native-H.264 cameras (Cubo 2 / CB02).
            force_h264 = dev_id in (self._options.get("h264_cameras") or [])
            video_codec = "h264" if force_h264 else "copy"

            # One line that answers "is the H.264 toggle actually applied to
            # THIS camera?" (#85: 'the combined stream is not being converted').
            # INFO with debug logs on so it shows without touching HA's logger
            # config; DEBUG otherwise.
            (_LOGGER.info if debug_logs else _LOGGER.debug)(
                "go2rtc stream plan for %s: video=%s (h264_transcode %s, option h264_cameras=%s), audio=opus+aac",
                dev_id,
                video_codec,
                "ON" if force_h264 else "off",
                self._options.get("h264_cameras") or [],
            )

            # Offer BOTH Opus and AAC audio on every playable stream. The card
            # listens over WebRTC (which negotiates Opus) with an MSE fallback,
            # and MSE — Safari/iOS especially — CANNOT decode Opus; it needs
            # AAC. AAC also keeps HLS/HomeKit consumers working. go2rtc takes
            # multiple audio codecs as REPEATED params (#audio=opus#audio=aac),
            # NOT a comma.
            audio_codecs = "#audio=opus#audio=aac"

            # The speaker stream is isolated so the media_player entity can securely cast TTS or audio files to it
            self._streams[f"cuboai_speaker_{dev_id}"] = [
                f"exec:{env_vars}{py} {backchannel_script}#{{killsignal=SIGTERM}}#backchannel=1#audio=pcma"
            ]

            # INVARIANT (#85): there is EXACTLY ONE live-view stream name per
            # camera, and it is this one. Every consumer — snapshots, HLS,
            # HomeKit, the card's WebRTC and MSE paths, the WebRTC sensor's
            # attributes, NVR URLs — must name cuboai_combined_<dev_id>.
            #
            # NEVER declare a second live-view stream (e.g. a plain
            # "cuboai_<dev_id>") that also runs the video exec. Two stream
            # NAMES mean two concurrent TUTK sessions against one camera as
            # soon as two consumers attach. A Cubo Plus (CB02) tolerates that;
            # a Cubo 3 (SW05) does not — the second session serves no tracks,
            # so HomeKit gets nothing and shows "No Response".
            #
            # And do NOT "share" the producer by pointing one stream's source
            # at another with a cross-stream ffmpeg: reference. That was tried
            # (commit bb4bf13) and reverted (3942771): the reference does
            # resolve and reuse does begin, but the nested ffmpeg defaults to
            # a 5-SECOND RTSP dial timeout while the pure-python engine needs
            # ~10s from cold, so on a cold start it times out and go2rtc
            # reports producer(None) medias=[] tracks=[]. One live stream
            # name remains the rule; the sanctioned self/cross references
            # below additionally carry #timeout=20 (go2rtc's ffmpeg source
            # honors it since 1.9.x) so a cold engine can no longer starve
            # them (#85 round 4: HomeKit DESCRIBE -> 404 when the pre-warm
            # had failed and the nested ffmpeg lost the 5s race).
            #
            # The three sources below are ordered and must stay ordered:
            #   1. exec: the pure-python engine, native A/V MPEG-TS producer.
            #   2. ffmpeg: a SELF-referencing transcode (same stream name).
            #      This one is safe precisely because it is dialed AFTER the
            #      exec producer within this same stream, so its DESCRIBE
            #      always hits an already-warm producer — the 5s-vs-10s race
            #      above cannot happen. It gives HomeKit/HLS H.264 when
            #      video_codec is h264, and Opus (WebRTC) + AAC (MSE/Safari).
            #   3. exec: the backchannel for two-way audio. go2rtc writes the
            #      incoming WebRTC microphone audio (PCMA) straight to this
            #      process's stdin; the script reads pipe:0 as alaw and sends
            #      it to the camera speaker.
            self._streams[f"cuboai_combined_{dev_id}"] = [
                f"exec:{env_vars}{py} {video_script}#{{killsignal=SIGTERM}}",
                f"ffmpeg:cuboai_combined_{dev_id}#video={video_codec}{audio_codecs}#timeout=20",
                f"exec:{env_vars}{py} {backchannel_script}#{{killsignal=SIGTERM}}#backchannel=1#audio=pcma",
            ]

            # HomeKit/HLS H.264 output for an HEVC camera (issue #85, part 2).
            #
            # The transcode leg inside cuboai_combined_ is NOT enough on its
            # own: that stream also carries the camera's NATIVE video from the
            # exec producer, and a plain RTSP consumer (HA's stream worker,
            # which is what HomeKit rides) takes what is offered first — the
            # HEVC. The reporter's own diagnostics show it exactly:
            #     producer(mpegts)      tracks=[hevc:264pkts, aac:274pkts]
            #     producer(ffmpeg h264) tracks=[]  recv=None      <- never used
            #     consumer(rtsp ...)    tracks=[hevc:264pkts, aac:274pkts]
            # So the toggle was applied, ffmpeg was spawned with libx264, and
            # HomeKit still got HEVC and said "No Response".
            #
            # A consumer that must have H.264 therefore needs a stream whose
            # ONLY video is H.264. This one qualifies — and it does NOT
            # violate the one-engine invariant above, because it declares no
            # exec: its single source is a CROSS-stream reference that reuses
            # the combined stream's existing producer. Two guards keep its
            # cold start honest (#85 round 4 — a fresh HomeKit request hit
            # DESCRIBE -> 404 because this producer had nothing yet):
            #   1. camera.stream_source() pre-warms the combined stream AND
            #      then THIS stream, so by the time the URL is handed out the
            #      transcode has already found an IDR and is emitting H.264.
            #   2. #timeout=20 on the reference (default 5s): even when the
            #      pre-warm fails or expires, the nested ffmpeg now outlasts
            #      the engine's ~10s cold start plus the up-to-one-GOP wait
            #      for a decodable keyframe instead of dying into a 404.
            if force_h264:
                self._streams[f"cuboai_h264_{dev_id}"] = [
                    f"ffmpeg:cuboai_combined_{dev_id}#video=h264#audio=aac#timeout=20",
                ]

            # Recorded footage from the camera's own DVR. Declared here rather
            # than added over go2rtc's API, which rejects `exec:` sources (it
            # would be a remote-execution hole). Which moment to play is passed
            # through the state file the play_recording service writes, so this
            # entry never changes. The producer only runs while something is
            # watching, so an idle DVR stream costs nothing.
            playback_script = os.path.join(script_dir, "cuboai_stream_playback.py")
            state_file = self.hass.config.path(f"cuboai_playback_{dev_id}.json")
            # The self-referencing ffmpeg leg matters here exactly like on the
            # live stream: the DVR exec emits H264 + AAC only, and with no Opus
            # on offer the WebRTC negotiation fails — every viewer fell back to
            # MSE, which iOS renders as a BLACK picture (time advancing, no
            # frames). With Opus offered, playback rides the same WebRTC path
            # the live view already uses on every platform. (Safe per the #85
            # invariant: a SELF-reference dials after this stream's own exec,
            # so its DESCRIBE always hits a warm producer.)
            self._streams[f"cuboai_dvr_{dev_id}"] = [
                f"exec:{env_vars}CUBOAI_PLAY_STATE={state_file} {py} {playback_script}#{{killsignal=SIGTERM}}",
                f"ffmpeg:cuboai_dvr_{dev_id}#video=copy{audio_codecs}#timeout=20",
            ]

    @property
    def is_running(self) -> bool:
        """Whether the go2rtc subprocess is alive. Camera entities consult
        this before talking to the API port: when go2rtc failed to start,
        that port may belong to a FOREIGN process, and blindly firing
        frame/stream requests at it can spawn TUTK producers in an orphaned
        instance in an endless loop (issue #84)."""
        return self.process is not None and self.process.returncode is None

    async def _reclaim_stale_instance(self, api_port: int) -> None:
        """Terminate an orphaned go2rtc from a previous HA run.

        A go2rtc child can outlive a hard-crashed HA process and keep holding
        our ports. Because it still serves the cuboai_* streams from its old
        config, every camera snapshot/stream request respawns TUTK exec
        producers inside the ORPHAN — the endless 'Using native library'
        loop that piles up processes until the host locks up (issue #84).

        A responsive holder is identified by (a) answering the go2rtc API and
        (b) serving cuboai_* streams. A holder that does NOT answer is not
        automatically foreign: a DYING go2rtc (mid-teardown with exec children,
        e.g. across an HA restart while a phone was streaming) holds the port
        with its API already dead — observed live, and treating it as foreign
        meant hopping ports on restart, stranding the WebRTC frontend. Process
        termination matches our EXACT binary path, so sweeping in that case is
        safe: a genuinely foreign holder never matches and is left alone (the
        port fallback in _resolve_ports covers it).
        """
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        if await self.hass.async_add_executor_job(_port_bindable, api_port):
            return  # port is free — nothing is squatting on it
        streams = None
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                f"http://127.0.0.1:{api_port}/api/streams",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                if resp.status == 200:
                    streams = await resp.json(content_type=None)
        except Exception:
            streams = None  # unresponsive: free port, foreign process, or OUR dying instance
        if isinstance(streams, dict) and not any(str(s).startswith("cuboai_") for s in streams):
            return  # answers the API but with foreign streams — leave it alone

        killed = await self.hass.async_add_executor_job(self._terminate_stale_processes)
        if killed:
            _LOGGER.warning(
                "Terminated %d orphaned CuboAI go2rtc process(es) from a previous "
                "Home Assistant run that were still holding port %s.",
                killed,
                api_port,
            )

    async def _wait_for_port_free(self, port: int, timeout: float = 5.0) -> bool:
        """Wait (up to timeout) for a TCP port to become bindable.

        Returns True as soon as it is free (instantly if already free), False
        if still held after the timeout — in which case _resolve_ports falls
        back to another port (a genuinely foreign holder).
        """
        import asyncio

        steps = max(1, int(timeout / 0.25))
        for _ in range(steps):
            if await self.hass.async_add_executor_job(_port_bindable, port):
                return True
            await asyncio.sleep(0.25)
        return await self.hass.async_add_executor_job(_port_bindable, port)

    def _terminate_stale_processes(self) -> int:
        """SIGTERM (then SIGKILL) every process running our go2rtc binary.

        Runs in an executor. Linux /proc only — on other platforms there is
        nothing to reclaim because HAOS/container is the deployment target.
        """
        import signal
        import time

        def _pids() -> list[int]:
            pids = []
            try:
                entries = os.listdir("/proc")
            except OSError:
                return pids
            for name in entries:
                if not name.isdigit():
                    continue
                pid = int(name)
                if self.process and self.process.pid == pid:
                    continue
                try:
                    with open(f"/proc/{pid}/cmdline", "rb") as f:
                        argv0 = f.read().split(b"\0", 1)[0].decode(errors="replace")
                except OSError:
                    continue
                if argv0 == self._binary_path:
                    pids.append(pid)
            return pids

        stale = _pids()
        for pid in stale:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if stale:
            time.sleep(1.0)
            for pid in _pids():  # anything that ignored SIGTERM
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        return len(stale)

    async def _resolve_ports(self):
        """Resolve the ports go2rtc will ACTUALLY be able to bind.

        On Home Assistant OS the built-in go2rtc already occupies TCP 8555
        (its WebRTC listener), so our RTSP listener silently failed to bind
        while the API kept answering — every RTSP consumer then got
        'connection reset by peer' (issue #80). The API port needs the same
        treatment: with :1985 taken, go2rtc's whole streaming API is dead and
        the frame/WebRTC requests land on a stranger's socket (issue #84).
        Verify the configured ports are free BEFORE starting and self-heal to
        nearby free ones, then publish the effective ports so the
        camera/sensor attributes (and therefore the card) all point at the
        right place.
        """
        from .utils import find_available_port

        desired_rtsp = int(self._options.get("rtsp_port", 8555))
        desired_webrtc = 8556
        desired_api = DESIRED_API_PORT

        def _resolve():
            rtsp = desired_rtsp
            if not _port_bindable(rtsp):
                rtsp = find_available_port(start_port=desired_rtsp + 1)
            webrtc = desired_webrtc
            if webrtc == rtsp or not _port_bindable(webrtc):
                webrtc = find_available_port(start_port=desired_webrtc + 2)
            api = desired_api
            if not _port_bindable(api):
                api = find_available_port(start_port=desired_api + 1, max_port=desired_api + 100)
            return rtsp, webrtc, api

        rtsp_port, webrtc_port, api_port = await self.hass.async_add_executor_job(_resolve)

        if rtsp_port != desired_rtsp:
            _LOGGER.warning(
                "RTSP port %s is already in use (typically Home Assistant's built-in "
                "go2rtc WebRTC listener) — using port %s instead. The camera and card "
                "follow automatically via the rtsp_port attribute.",
                desired_rtsp,
                rtsp_port,
            )
        if api_port != desired_api:
            _LOGGER.warning(
                "go2rtc API port %s is already in use by another process — using "
                "port %s instead. The camera, sensors and card follow automatically.",
                desired_api,
                api_port,
            )

        self._rtsp_port = rtsp_port
        self._webrtc_port = webrtc_port
        self._api_port = api_port
        # Single source of truth for every port consumer (camera
        # stream_source/snapshots, entity attributes, and through them the card).
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        domain_data["rtsp_port_effective"] = rtsp_port
        domain_data["api_port_effective"] = api_port
        await self._sync_webrtc_integration_url(api_port)
        return rtsp_port, webrtc_port

    async def _sync_webrtc_integration_url(self, api_port: int) -> None:
        """Follow our API port in the WebRTC Camera integration's stored URL.

        AlexxIT's WebRTC Camera integration can be pointed at this go2rtc by
        URL, and that URL is a FIXED string in its config entry. Our own
        consumers read `api_port_effective` and follow a self-heal
        automatically — that integration cannot, so a hop leaves the card
        showing 'Cannot connect to host <ip>:1985' while every other camera
        surface in Home Assistant keeps working (verified live: the hop broke
        only the card).

        Only a URL that clearly points at US is touched: the port must be in
        the range this manager hands out. A URL on any other port belongs to a
        different go2rtc and is left alone.
        """
        try:
            from urllib.parse import urlparse, urlunparse

            entries = self.hass.config_entries.async_entries("webrtc")
        except Exception:
            return  # integration not installed — nothing to keep in sync

        for entry in entries:
            url = (entry.data or {}).get("url")
            if not url:
                continue  # using its own embedded go2rtc, not ours
            try:
                parsed = urlparse(url)
                port = parsed.port
            except Exception:
                continue
            if port is None or not (DESIRED_API_PORT <= port <= DESIRED_API_PORT + 100):
                continue  # not a port we manage: someone else's server
            if port == api_port:
                continue  # already correct
            new_netloc = f"{parsed.hostname}:{api_port}"
            if parsed.username:
                cred = parsed.username + (f":{parsed.password}" if parsed.password else "")
                new_netloc = f"{cred}@{new_netloc}"
            new_url = urlunparse(parsed._replace(netloc=new_netloc))
            _LOGGER.warning(
                "WebRTC Camera integration points at %s but our go2rtc API self-healed to "
                "port %s — updating it to %s so the card keeps working.",
                url,
                api_port,
                new_url,
            )
            self.hass.config_entries.async_update_entry(entry, data={**entry.data, "url": new_url})
            self.hass.async_create_task(self.hass.config_entries.async_reload(entry.entry_id))

    async def _generate_config(self):
        """Generate the go2rtc.yaml file."""
        rtsp_port = getattr(self, "_rtsp_port", None) or self._options.get("rtsp_port", 8555)
        webrtc_port = getattr(self, "_webrtc_port", 8556)
        api_port = getattr(self, "_api_port", DESIRED_API_PORT)
        config = {
            "api": {
                # All interfaces: the frontend card / webrtc integration reach this
                # API via the HA host's LAN IP, so it cannot be localhost-only.
                # Alternate port avoids conflict with the HA go2rtc add-on.
                "listen": f":{api_port}",
            },
            "rtsp": {
                "listen": f":{rtsp_port}",
            },
            "webrtc": {
                "listen": f":{webrtc_port}",
            },
        }

        if self._options.get("enable_debug_logs"):
            # exec:debug is the load-bearing one — go2rtc logs each stderr line of an
            # exec producer at debug level on the exec module, so the Python stream
            # engine's diagnostics ([mpegts] codec line, [clean_gop] desync/resync,
            # [health] metrics, FICENSUS, connection errors) all land in go2rtc.log.
            # rtsp/streams at debug show consumer negotiation, producer start/stop and
            # probe results. NOTE: exec:debug also logs the producer command lines,
            # which contain the camera's TUTK credentials (same as this yaml file) —
            # users must redact go2rtc.log before sharing, see the issue template.
            config["log"] = {
                "level": "info",
                "exec": "debug",
                "rtsp": "debug",
                "streams": "debug",
                # ffmpeg:debug logs the full ffmpeg command go2rtc spawns for the
                # transcode producer — decisive for #85-type reports: it shows
                # whether the H.264 transcode (libx264) is actually in the
                # pipeline or the stream is a plain copy.
                "ffmpeg": "debug",
            }

        # NVR mode: protect the RTSP listener with credentials so external
        # recorders (HiLook/Hikvision, Synology, Frigate, ...) can consume the
        # stream securely. go2rtc applies the credentials to internal
        # consumers (its ffmpeg re-encoders) automatically.
        if self._options.get("nvr_enabled") and self._options.get("nvr_password"):
            config["rtsp"]["username"] = self._options.get("nvr_username") or "cuboai"
            config["rtsp"]["password"] = self._options["nvr_password"]
        # This file is fully REGENERATED here, not merged into: `config` is built
        # from scratch above and `_write` truncates. That is what makes a stale
        # cuboai_* entry impossible — a stream we no longer generate simply is not
        # written again. (An earlier comment here described a merge, and a loop
        # tried to prune stale entries from `config["streams"]`; since that key was
        # always empty at that point, the loop could never run. Removed rather than
        # made real: the file lives inside the integration directory and is ours to
        # own, and re-reading it would risk resurrecting exactly the stale entries
        # the loop was meant to remove.)
        config["streams"] = dict(self._streams)
        _LOGGER.info("go2rtc config written with streams: %s", sorted(self._streams))

        def _write():
            os.makedirs(os.path.dirname(self._config_path), exist_ok=True)
            with open(self._config_path, "w") as f:
                yaml.dump(config, f)

        await self.hass.async_add_executor_job(_write)

    async def start(self):
        """Start the go2rtc subprocess."""

        def _binary_ready() -> bool:
            if not os.path.exists(self._binary_path):
                return False
            # Ensure binary is executable
            try:
                os.chmod(self._binary_path, 0o755)
            except Exception:
                pass
            return True

        if not await self.hass.async_add_executor_job(_binary_ready):
            _LOGGER.error("go2rtc binary not found at %s. Stream cannot start.", self._binary_path)
            return

        # Stop any previous instance FIRST so the port probe below doesn't
        # mistake our own listeners for a conflict and hop ports on reload.
        if self.process:
            await self.stop()

        # Reclaim ports from an orphaned go2rtc of a previous HA run before
        # probing, so we bind our usual ports instead of hopping (issue #84).
        # The desired port is not the only place an orphan can sit: a previous
        # run may itself have SELF-HEALED to another port (api_port_effective),
        # and an orphan there would otherwise never be reclaimed — it would
        # keep its TUTK sessions against the camera forever while we happily
        # bind the desired port.
        await self._reclaim_stale_instance(DESIRED_API_PORT)
        last_effective = self.hass.data.get(DOMAIN, {}).get("api_port_effective")
        if last_effective and last_effective != DESIRED_API_PORT:
            await self._reclaim_stale_instance(last_effective)

        # A just-terminated go2rtc (orphan kill, or our own stop() on reload)
        # needs a moment for the OS to RELEASE the port — probing immediately
        # sees it still bound and hops (e.g. 1985→1986), which strands the
        # WebRTC frontend and HomeKit on the old port ("Cannot connect to
        # …:1985" — their go2rtc URL is fixed config that knows nothing of the
        # hop). 15s, not the 5s default: with live consumers attached (a phone
        # streaming), the dying instance holds the port well past 5s — observed
        # live on a reload. Returns instantly when the port is already free, so
        # the longer ceiling costs nothing on a clean start.
        await self._wait_for_port_free(DESIRED_API_PORT, timeout=15.0)

        # The SAME race applies to the RTSP port, and it hurts more: an NVR
        # stores the port in its channel config, so a hop is permanent and
        # silent — the recorder simply reports the host unreachable forever
        # (observed live: a restart moved RTSP 8557 -> 8558 and stayed there,
        # while the integration's own sensors correctly followed the new port
        # and only the external recorder was stranded).
        #
        # Wait only for the port our OWN previous instance was using. Waiting
        # on the desired port unconditionally would burn the full timeout on
        # every start for the common case where 8555 belongs to Home
        # Assistant's built-in go2rtc and is never coming free.
        previous_rtsp = self.hass.data.get(DOMAIN, {}).get("rtsp_port_effective")
        if previous_rtsp:
            await self._wait_for_port_free(int(previous_rtsp), timeout=15.0)

        await self._resolve_ports()
        await self._resolve_codecs()
        await self._generate_config()

        log_file_path = os.path.join(os.path.dirname(self._config_path), "go2rtc.log")
        _LOGGER.info("Starting internal go2rtc streaming server (log: %s)...", log_file_path)
        try:
            debug_logs = self._options.get("enable_debug_logs", False)
            if debug_logs:

                def _open_log():
                    if os.path.exists(log_file_path) and os.path.getsize(log_file_path) > 2 * 1024 * 1024:
                        backup_path = f"{log_file_path}.1"
                        if os.path.exists(backup_path):
                            os.remove(backup_path)
                        os.rename(log_file_path, backup_path)
                    return open(log_file_path, "a")

                log_file = await self.hass.async_add_executor_job(_open_log)
            else:
                log_file = asyncio.subprocess.DEVNULL

            self.process = await asyncio.create_subprocess_exec(
                self._binary_path, "-config", self._config_path, stdout=log_file, stderr=log_file
            )
            # The child holds its own duplicated fd; close the parent's copy so
            # reloads don't leak one file handle per restart.
            if debug_logs and hasattr(log_file, "close"):
                log_file.close()
            _LOGGER.info(f"go2rtc started with PID {self.process.pid}")

            # Health check — wait a moment then verify process is still alive
            await asyncio.sleep(1)
            if self.process.returncode is not None:
                _LOGGER.error(
                    "go2rtc exited immediately with code %s — check %s", self.process.returncode, log_file_path
                )
                self.process = None
        except Exception as e:
            _LOGGER.error(f"Failed to start go2rtc: {e}")

    async def stop(self):
        """Stop the go2rtc subprocess."""
        if self.process:
            _LOGGER.info("Stopping internal go2rtc streaming server...")
            try:
                self.process.terminate()
                await asyncio.wait_for(self.process.wait(), timeout=5.0)
            except TimeoutError:
                self.process.kill()
            except ProcessLookupError:
                pass
            finally:
                self.process = None
                _LOGGER.info("go2rtc stopped.")

    # ── DVR playback ──────────────────────────────────────────────────────
    #
    # Recorded footage is served the same way as live: a go2rtc `exec:` producer
    # writing MPEG-TS. The difference is that a playback stream is transient —
    # it exists for one rewind request — so it is published through go2rtc's
    # REST API at request time instead of living in go2rtc.yaml.

    def playback_stream_name(self, device_id: str) -> str:
        """The go2rtc stream name a camera's recorded playback is served on."""
        return f"cuboai_dvr_{device_id}"

    def playback_rtsp_url(self, device_id: str) -> str:
        """Where Home Assistant should play the recorded stream from.

        Uses the port go2rtc actually bound, not the configured one: it
        self-heals to a free port when the configured one is taken (HA's own
        go2rtc often holds 8555), and pointing at the configured port then
        reaches nothing — no producer is ever spawned and no frames arrive.
        This is the same resolution the live camera entity uses.
        """
        rtsp_port = (
            self.hass.data.get(DOMAIN, {}).get("rtsp_port_effective")
            or getattr(self, "_rtsp_port", None)
            or self._options.get("rtsp_port", 8555)
        )
        return f"rtsp://127.0.0.1:{rtsp_port}/{self.playback_stream_name(device_id)}"

    async def async_start_playback(self, cam: dict, start_epoch: int, seconds: int) -> str:
        """Request playback of `start_epoch` (UTC) for `cam`; returns its URL.

        Writes the moment to the camera's state file, which the already-declared
        DVR producer reads when a viewer connects. Any producer still running
        for a previous request is stopped first, so the next viewer gets the
        moment just asked for rather than the tail of the old one.
        """
        import json

        dev_id = cam.get("device_id")
        state_path = self.hass.config.path(f"cuboai_playback_{dev_id}.json")
        payload = {"start_epoch": int(start_epoch), "seconds": int(seconds)}

        def _write() -> None:
            with open(state_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)

        await self.hass.async_add_executor_job(_write)
        # Deliberately NOT deleting the stream here. go2rtc's DELETE removes the
        # stream itself — not just its producer — and this one is declared in
        # the config, so deleting it made every later request 404 until go2rtc
        # restarted. go2rtc already starts a fresh producer, which reads the
        # file just written, when the next viewer connects.
        _LOGGER.info("Playback requested for %s from %s (%ss)", dev_id, start_epoch, seconds)
        return self.playback_rtsp_url(dev_id)

    async def async_stop_playback(self, device_id: str) -> None:
        """Clear a camera's playback request.

        Note this does NOT call go2rtc's DELETE: that removes the declared
        stream rather than just its producer, and it would then 404 until
        go2rtc restarted. Removing the state file is enough — the producer
        stops at end of footage, and without a request it refuses to start.
        """
        state_path = self.hass.config.path(f"cuboai_playback_{device_id}.json")

        def _remove() -> None:
            try:
                os.remove(state_path)
            except FileNotFoundError:
                pass

        await self.hass.async_add_executor_job(_remove)
