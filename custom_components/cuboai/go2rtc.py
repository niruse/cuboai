import asyncio
import logging
import os

import yaml
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)


class Go2RTCManager:
    """Manages the internal go2rtc subprocess for CuboAI local streaming."""

    def __init__(self, hass: HomeAssistant):
        self.hass = hass
        self.process: asyncio.subprocess.Process | None = None
        self._config_path = os.path.join(os.path.dirname(__file__), "bin", "go2rtc.yaml")
        self._binary_path = os.path.join(os.path.dirname(__file__), "bin", "go2rtc")
        self._streams = {}

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

            # The 1st stream runs the pure-python engine which outputs native A/V MPEG-TS.
            # The 2nd stream uses ffmpeg to seamlessly transcode the AAC audio to Opus for WebRTC compatibility.
            self._streams[f"cuboai_{dev_id}"] = [
                f"exec:{env_vars}python3 {video_script}#{{killsignal=SIGTERM}}",
                f"ffmpeg:cuboai_{dev_id}#video=copy#audio=opus",
            ]

            # The speaker stream is isolated so the media_player entity can securely cast TTS or audio files to it
            self._streams[f"cuboai_speaker_{dev_id}"] = [
                f"exec:{env_vars}python3 {backchannel_script}#{{killsignal=SIGTERM}}#backchannel=1#audio=pcma"
            ]

            # The combined stream: video from the main camera stream + backchannel for two-way audio.
            # go2rtc writes incoming WebRTC microphone audio (PCMA) directly to the backchannel exec's stdin.
            # The backchannel script reads from pipe:0 (stdin) in alaw format and sends it to the camera speaker.
            self._streams[f"cuboai_combined_{dev_id}"] = [
                f"exec:{env_vars}python3 {video_script}#{{killsignal=SIGTERM}}",
                f"ffmpeg:cuboai_combined_{dev_id}#video=copy#audio=opus",
                f"exec:{env_vars}python3 {backchannel_script}#{{killsignal=SIGTERM}}#backchannel=1#audio=pcma",
            ]

    async def _generate_config(self):
        """Generate the go2rtc.yaml file."""
        config = {
            "api": {
                "listen": ":1985",  # Use alternate port to avoid conflict with HA add-on
            },
            "rtsp": {
                "listen": ":8555",
            },
            "webrtc": {
                "listen": ":8556",
            },
        }
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
        if not os.path.exists(self._binary_path):
            _LOGGER.error("go2rtc binary not found at %s. Stream cannot start.", self._binary_path)
            return

        await self._resolve_codecs()
        await self._generate_config()

        if self.process:
            await self.stop()

        # Ensure binary is executable
        try:
            os.chmod(self._binary_path, 0o755)
        except Exception:
            pass

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
