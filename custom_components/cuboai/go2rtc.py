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

    async def _resolve_ports(self):
        """Resolve the ports go2rtc will ACTUALLY be able to bind.

        On Home Assistant OS the built-in go2rtc already occupies TCP 8555
        (its WebRTC listener), so our RTSP listener silently failed to bind
        while the API kept answering — every RTSP consumer then got
        'connection reset by peer' (issue #80). Verify the configured ports
        are free BEFORE starting and self-heal to nearby free ones, then
        publish the effective RTSP port so the camera/sensor attributes (and
        therefore the card) all point at the right place.
        """
        from .utils import find_available_port

        desired_rtsp = int(self._options.get("rtsp_port", 8555))
        desired_webrtc = 8556

        def _resolve():
            rtsp = desired_rtsp
            if not _port_bindable(rtsp):
                rtsp = find_available_port(start_port=desired_rtsp + 1)
            webrtc = desired_webrtc
            if webrtc == rtsp or not _port_bindable(webrtc):
                webrtc = find_available_port(start_port=desired_webrtc + 2)
            api_ok = _port_bindable(1985)
            return rtsp, webrtc, api_ok

        rtsp_port, webrtc_port, api_ok = await self.hass.async_add_executor_job(_resolve)

        if rtsp_port != desired_rtsp:
            _LOGGER.warning(
                "RTSP port %s is already in use (typically Home Assistant's built-in "
                "go2rtc WebRTC listener) — using port %s instead. The camera and card "
                "follow automatically via the rtsp_port attribute.",
                desired_rtsp,
                rtsp_port,
            )
        if not api_ok:
            _LOGGER.error(
                "go2rtc API port 1985 is already in use by another process — "
                "CuboAI streaming will not work until it is freed."
            )

        self._rtsp_port = rtsp_port
        self._webrtc_port = webrtc_port
        # Single source of truth for every rtsp_port consumer (camera
        # stream_source, entity attributes, and through them the card).
        self.hass.data.setdefault(DOMAIN, {})["rtsp_port_effective"] = rtsp_port
        return rtsp_port, webrtc_port

    async def _generate_config(self):
        """Generate the go2rtc.yaml file."""
        rtsp_port = getattr(self, "_rtsp_port", None) or self._options.get("rtsp_port", 8555)
        webrtc_port = getattr(self, "_webrtc_port", 8556)
        config = {
            "api": {
                # All interfaces: the frontend card / webrtc integration reach this
                # API via the HA host's LAN IP, so it cannot be localhost-only.
                # Alternate port avoids conflict with the HA go2rtc add-on.
                "listen": ":1985",
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
