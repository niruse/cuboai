import asyncio
import logging
import os
import socket
import sys

import yaml
from homeassistant.core import HomeAssistant

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


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

            backchannel_script = os.path.join(script_dir, "cuboai_stream_backchannel.py")

            # Use the exact interpreter HA runs under: a bare "python3" resolves to the
            # system interpreter on venv installs, which lacks av/yt-dlp and fails imports.
            py = sys.executable or "python3"

            # The 1st stream runs the pure-python engine which outputs native A/V MPEG-TS.
            # The 2nd stream uses ffmpeg to seamlessly transcode the AAC audio to Opus for WebRTC compatibility.
            self._streams[f"cuboai_{dev_id}"] = [
                f"exec:{env_vars}{py} {video_script}#{{killsignal=SIGTERM}}",
                f"ffmpeg:cuboai_{dev_id}#video=copy#audio=opus",
            ]

            # The speaker stream is isolated so the media_player entity can securely cast TTS or audio files to it
            self._streams[f"cuboai_speaker_{dev_id}"] = [
                f"exec:{env_vars}{py} {backchannel_script}#{{killsignal=SIGTERM}}#backchannel=1#audio=pcma"
            ]

            # The combined stream: video from the main camera stream + backchannel for two-way audio.
            # go2rtc writes incoming WebRTC microphone audio (PCMA) directly to the backchannel exec's stdin.
            # The backchannel script reads from pipe:0 (stdin) in alaw format and sends it to the camera speaker.
            self._streams[f"cuboai_combined_{dev_id}"] = [
                f"exec:{env_vars}{py} {video_script}#{{killsignal=SIGTERM}}",
                f"ffmpeg:cuboai_combined_{dev_id}#video=copy#audio=opus",
                f"exec:{env_vars}{py} {backchannel_script}#{{killsignal=SIGTERM}}#backchannel=1#audio=pcma",
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

        Only a holder that (a) answers the go2rtc API and (b) serves
        cuboai_* streams is touched, and processes are matched by our exact
        binary path — a foreign go2rtc is left alone (the port fallback in
        _resolve_ports handles it instead).
        """
        import aiohttp
        from homeassistant.helpers.aiohttp_client import async_get_clientsession

        if await self.hass.async_add_executor_job(_port_bindable, api_port):
            return  # port is free — nothing is squatting on it
        try:
            session = async_get_clientsession(self.hass)
            async with session.get(
                f"http://127.0.0.1:{api_port}/api/streams",
                timeout=aiohttp.ClientTimeout(total=3),
            ) as resp:
                if resp.status != 200:
                    return
                streams = await resp.json(content_type=None)
        except Exception:
            return  # not a go2rtc API — leave the holder alone
        if not isinstance(streams, dict) or not any(str(s).startswith("cuboai_") for s in streams):
            return  # a foreign go2rtc — the API port fallback covers it

        killed = await self.hass.async_add_executor_job(self._terminate_stale_processes)
        if killed:
            _LOGGER.warning(
                "Terminated %d orphaned CuboAI go2rtc process(es) from a previous "
                "Home Assistant run that were still holding port %s.",
                killed,
                api_port,
            )

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
        desired_api = 1985

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
        return rtsp_port, webrtc_port

    async def _generate_config(self):
        """Generate the go2rtc.yaml file."""
        rtsp_port = getattr(self, "_rtsp_port", None) or self._options.get("rtsp_port", 8555)
        webrtc_port = getattr(self, "_webrtc_port", 8556)
        api_port = getattr(self, "_api_port", 1985)
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

        # NVR mode: protect the RTSP listener with credentials so external
        # recorders (HiLook/Hikvision, Synology, Frigate, ...) can consume the
        # stream securely. go2rtc applies the credentials to internal
        # consumers (its ffmpeg re-encoders) automatically.
        if self._options.get("nvr_enabled") and self._options.get("nvr_password"):
            config["rtsp"]["username"] = self._options.get("nvr_username") or "cuboai"
            config["rtsp"]["password"] = self._options["nvr_password"]
        if "streams" not in config:
            config["streams"] = {}

        # Overwrite our streams, keeping any other streams (e.g. from user)
        for k, v in self._streams.items():
            config["streams"][k] = v

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
        await self._reclaim_stale_instance(1985)

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
