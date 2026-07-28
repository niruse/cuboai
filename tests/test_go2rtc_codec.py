"""Tests for per-camera H.264 transcoding selection (issue #85).

H.265/HEVC cameras (Cubo 3 / SW05) can't be consumed by HomeKit or HA's
stream/HLS path. When a camera is listed in the `h264_cameras` option, go2rtc
must transcode its video to H.264 (`video=h264`); otherwise it stays an
efficient passthrough (`video=copy`).
"""

import asyncio
from unittest.mock import MagicMock

from custom_components.cuboai.go2rtc import Go2RTCManager


def _resolve(options):
    mgr = Go2RTCManager(MagicMock())
    mgr._cameras = [
        {"device_id": "CB02AAA", "uid": "u1", "account": "a1", "password": "p1"},
        {"device_id": "SW05BBB", "uid": "u2", "account": "a2", "password": "p2"},
    ]
    mgr._options = options
    mgr._streams = {}
    asyncio.run(mgr._resolve_codecs())
    return mgr._streams


def _combined_ffmpeg(streams, dev_id):
    line = next(s for s in streams[f"cuboai_combined_{dev_id}"] if s.startswith("ffmpeg:"))
    return line


class TestH264Transcode:
    def test_default_is_passthrough_copy(self):
        streams = _resolve({})
        assert "video=copy" in _combined_ffmpeg(streams, "CB02AAA")
        assert "video=copy" in _combined_ffmpeg(streams, "SW05BBB")

    def test_selected_camera_transcodes_to_h264(self):
        streams = _resolve({"h264_cameras": ["SW05BBB"]})
        # H.265 camera -> transcode
        assert "video=h264" in _combined_ffmpeg(streams, "SW05BBB")
        # the other camera stays passthrough
        assert "video=copy" in _combined_ffmpeg(streams, "CB02AAA")

    def test_h264_camera_also_offers_aac_audio_for_homekit(self):
        streams = _resolve({"h264_cameras": ["SW05BBB"]})
        line = _combined_ffmpeg(streams, "SW05BBB")
        # go2rtc multi-codec syntax is REPEATED params, not a comma
        assert "#audio=opus#audio=aac" in line  # Opus for WebRTC, AAC for HLS/HomeKit
        assert "audio=opus,aac" not in line
        # passthrough camera keeps opus-only
        assert "#audio=opus" in _combined_ffmpeg(streams, "CB02AAA")
        assert "aac" not in _combined_ffmpeg(streams, "CB02AAA")

    def test_main_stream_also_transcodes_when_selected(self):
        streams = _resolve({"h264_cameras": ["SW05BBB"]})
        main = next(s for s in streams["cuboai_SW05BBB"] if s.startswith("ffmpeg:"))
        assert "video=h264" in main
