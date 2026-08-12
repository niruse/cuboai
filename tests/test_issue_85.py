"""Regression guards for issue #85: ONE live-view stream name per camera.

Each camera used to declare two go2rtc streams -- ``cuboai_<dev_id>`` and
``cuboai_combined_<dev_id>`` -- and each one ran its own ``exec:`` copy of the
pure-python TUTK video engine. Two stream NAMES mean two concurrent TUTK
sessions against a single camera as soon as two consumers attach. A Cubo Plus
(CB02) tolerates that; a Cubo 3 (SW05) does not -- the second session serves no
tracks, so HomeKit gets nothing and shows "No Response".

The fix is to declare only ``cuboai_combined_<dev_id>`` and point every
consumer at it.

There is also a WRONG fix that must never come back. Commit bb4bf13 kept both
streams and made combined's video a cross-stream reference
(``ffmpeg:cuboai_<dev_id>#video=...``); it was reverted in 3942771. The
reference does resolve and producer reuse does begin, but the nested ffmpeg has
a fixed 5-SECOND RTSP read timeout while the python engine needs ~10s from
cold, so a cold start times out and go2rtc reports ``producer(None) medias=[]
tracks=[]``. It only appeared to work when the stream happened to be warm.
The self-referencing ffmpeg INSIDE the combined stream is a different thing and
is safe: it is dialed after the exec producer in that same stream, so its
DESCRIBE always hits an already-warm producer.

These tests judge CODE only: comment lines are stripped before the textual
guards run, because the comments in this repo deliberately quote the very
strings the guards forbid.
"""

import asyncio
import re
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

# =============================================================================
# HA module scaffolding needed to import camera.py and sensor.py
# (conftest mocks the top-level packages; these are the submodules they touch)
# =============================================================================


class _FakeCoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


class _FakeCamera:
    def __init__(self):
        pass


def _install_platform_mocks():
    components = ModuleType("homeassistant.components")
    camera_mod = ModuleType("homeassistant.components.camera")
    camera_mod.Camera = _FakeCamera
    camera_mod.CameraEntityFeature = MagicMock()
    camera_mod.StreamType = MagicMock()
    sensor_mod = ModuleType("homeassistant.components.sensor")
    sensor_mod.SensorDeviceClass = MagicMock()
    sensor_mod.SensorEntity = object
    coordinator_mod = ModuleType("homeassistant.helpers.update_coordinator")
    coordinator_mod.CoordinatorEntity = _FakeCoordinatorEntity
    aiohttp_client_mod = ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client_mod.async_get_clientsession = MagicMock(side_effect=RuntimeError("no session in tests"))
    for name, mod in (
        ("homeassistant.components", components),
        ("homeassistant.components.camera", camera_mod),
        ("homeassistant.components.sensor", sensor_mod),
        ("homeassistant.helpers.update_coordinator", coordinator_mod),
        ("homeassistant.helpers.aiohttp_client", aiohttp_client_mod),
    ):
        sys.modules.setdefault(name, mod)


_install_platform_mocks()

from custom_components.cuboai.const import DOMAIN  # noqa: E402
from custom_components.cuboai.go2rtc import Go2RTCManager  # noqa: E402
from custom_components.cuboai.sensor import CuboWebRTCStreamSensor  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
COMPONENT = REPO / "custom_components" / "cuboai"
GO2RTC_PY = COMPONENT / "go2rtc.py"

DEV_IDS = ("CB02AAA", "SW05BBB")

# The one live-view stream name per camera. Any other cuboai_* stream in the
# generated config must be one of the out-of-scope side streams.
ALLOWED_PREFIXES = ("cuboai_combined_", "cuboai_speaker_", "cuboai_dvr_")


# =============================================================================
# Helpers
# =============================================================================


def _resolve(options=None):
    """Run the real stream planner and return the generated streams dict."""
    mgr = Go2RTCManager(MagicMock())
    mgr._cameras = [
        {"device_id": "CB02AAA", "uid": "u1", "account": "a1", "password": "p1"},
        {"device_id": "SW05BBB", "uid": "u2", "account": "a2", "password": "p2"},
    ]
    mgr._options = options or {}
    mgr._streams = {}
    asyncio.run(mgr._resolve_codecs())
    return mgr._streams


def _source_files():
    """Every shipped file that could name a go2rtc stream, plus the tests."""
    files = []
    files += sorted(COMPONENT.rglob("*.py"))
    files += sorted((COMPONENT / "www").rglob("*.js"))
    files += sorted((COMPONENT / "bin").rglob("*.yaml"))
    files += sorted((REPO / "tests").glob("*.py"))
    return files


def _strip_comments(text: str, suffix: str) -> str:
    """Drop whole-line comments. Judge CODE, not prose.

    Only full-line comments are removed: '#' is load-bearing INSIDE go2rtc
    source strings ('#video=copy', '#{killsignal=SIGTERM}'), so stripping
    inline would mangle the very lines under test.
    """
    kept = []
    for line in text.splitlines():
        s = line.strip()
        if suffix == ".js":
            if s.startswith("//") or s.startswith("*") or s.startswith("/*"):
                continue
        elif s.startswith("#"):
            continue
        kept.append(line)
    return "\n".join(kept)


def _code(path: Path) -> str:
    return _strip_comments(path.read_text(encoding="utf-8"), path.suffix)


_NOT_ALLOWED = r"(?!combined_|speaker_|dvr_)"

# A plain-stream reference in any of the shapes this repo actually writes.
FORBIDDEN_PATTERNS = {
    # f-string stream name built straight off the prefix: cuboai_{dev_id}
    "f-string stream name": re.compile(r"cuboai_\{"),
    # go2rtc source referencing another stream by name
    "ffmpeg: stream reference": re.compile(r"ffmpeg:cuboai_" + _NOT_ALLOWED),
    # go2rtc API query parameter
    "?src= stream reference": re.compile(r"src=cuboai_" + _NOT_ALLOWED),
    # RTSP URL path (only inside an rtsp:// URL -- /cuboai_*.json config paths
    # and /path/to/cuboai_stream_video.py docstrings are not stream names)
    "rtsp:// stream path": re.compile(r"rtsp://[^\s\"']*?/cuboai_" + _NOT_ALLOWED),
    # bare declaration/lookup of the deleted stream in the yaml sample
    "yaml stream key": re.compile(r"^\s*-?\s*cuboai_" + _NOT_ALLOWED + r"[A-Z0-9_]+:?\s*$", re.M),
}


# =============================================================================
# 1. The video engine runs from exactly ONE stream declaration
# =============================================================================


class TestSingleVideoEngine:
    def test_video_engine_launched_from_exactly_one_stream(self):
        """The whole bug: two stream names each spawning the TUTK engine."""
        streams = _resolve()
        for dev_id in DEV_IDS:
            owners = [
                name
                for name, sources in streams.items()
                if dev_id in name and any("cuboai_stream_video.py" in s and s.startswith("exec:") for s in sources)
            ]
            assert owners == [f"cuboai_combined_{dev_id}"], (
                f"{dev_id}: the video engine must be launched by exactly one stream "
                f"(cuboai_combined_{dev_id}); got {owners}"
            )

    def test_go2rtc_source_spawns_video_script_once(self):
        """Source-level twin of the above: one `{video_script}` exec in code."""
        code = _code(GO2RTC_PY)
        assert code.count("{video_script}") == 1, (
            "custom_components/cuboai/go2rtc.py must launch the video engine from exactly one stream declaration"
        )

    def test_only_the_three_known_streams_are_declared(self):
        """No fourth stream name may appear for a camera."""
        streams = _resolve()
        expected = {f"{p}{d}" for p in ALLOWED_PREFIXES for d in DEV_IDS}
        assert set(streams) == expected


# =============================================================================
# 2. No cross-stream ffmpeg: reference (the reverted bb4bf13 approach)
# =============================================================================


class TestNoCrossStreamReference:
    def test_every_ffmpeg_source_references_its_own_stream(self):
        """A cross-stream ffmpeg: hits a COLD producer.

        go2rtc's nested ffmpeg gives up after 5s; the python engine needs ~10s
        from cold. Self-referencing is safe (the producer is already warm),
        anything else is the reverted race.
        """
        streams = _resolve()
        for name, sources in streams.items():
            for src in sources:
                if not src.startswith("ffmpeg:"):
                    continue
                target = src[len("ffmpeg:") :].split("#", 1)[0]
                assert target == name, (
                    f"stream {name} references another stream ({target}) via ffmpeg: -- "
                    "this is the reverted bb4bf13 race (5s ffmpeg timeout vs ~10s cold engine)"
                )

    def test_no_cross_stream_ffmpeg_literal_in_any_source_file(self):
        pattern = FORBIDDEN_PATTERNS["ffmpeg: stream reference"]
        offenders = []
        for path in _source_files():
            if path.name == Path(__file__).name:
                continue
            for i, line in enumerate(_code(path).splitlines(), 1):
                if pattern.search(line):
                    offenders.append(f"{path.relative_to(REPO)}:{i}: {line.strip()}")
        assert not offenders, "cross-stream ffmpeg: reference found:\n" + "\n".join(offenders)


# =============================================================================
# 3. The combined stream keeps all three of its sources, in order
# =============================================================================


class TestCombinedStreamIntact:
    @pytest.mark.parametrize("dev_id", DEV_IDS)
    def test_three_sources_in_order(self, dev_id):
        sources = _resolve()[f"cuboai_combined_{dev_id}"]
        assert len(sources) == 3
        # 1. the pure-python engine producer
        assert sources[0].startswith("exec:")
        assert "cuboai_stream_video.py" in sources[0]
        assert "#{killsignal=SIGTERM}" in sources[0]
        # 2. the SELF-referencing transcode, dialed AFTER the producer above
        assert sources[1].startswith(f"ffmpeg:cuboai_combined_{dev_id}#")
        # 3. two-way audio
        assert sources[2].startswith("exec:")
        assert "cuboai_stream_backchannel.py" in sources[2]

    @pytest.mark.parametrize("dev_id", DEV_IDS)
    def test_backchannel_flags_survive(self, dev_id):
        """Two-way audio: go2rtc only pipes mic audio in with these flags."""
        sources = _resolve()[f"cuboai_combined_{dev_id}"]
        backchannel = next(s for s in sources if "cuboai_stream_backchannel.py" in s)
        assert "#backchannel=1" in backchannel
        assert "#audio=pcma" in backchannel

    def test_transcode_carries_video_codec_and_both_audio_codecs(self):
        """HomeKit needs H.264; MSE/Safari needs AAC; WebRTC needs Opus."""
        default = _resolve()
        forced = _resolve({"h264_cameras": ["SW05BBB"]})
        for dev_id in DEV_IDS:
            line = default[f"cuboai_combined_{dev_id}"][1]
            assert "#video=copy" in line
            # REPEATED params, not a comma -- go2rtc rejects 'audio=opus,aac'
            assert "#audio=opus#audio=aac" in line
        assert "#video=h264" in forced["cuboai_combined_SW05BBB"][1]
        assert "#video=copy" in forced["cuboai_combined_CB02AAA"][1]


# =============================================================================
# 4. No source file still names the deleted stream
# =============================================================================


class TestNoOrphanedConsumers:
    def test_no_source_file_names_the_deleted_stream(self):
        offenders = []
        for path in _source_files():
            if path.name == Path(__file__).name:
                continue
            code = _code(path)
            for label, pattern in FORBIDDEN_PATTERNS.items():
                for match in pattern.finditer(code):
                    line_no = code.count("\n", 0, match.start()) + 1
                    line = code.splitlines()[line_no - 1].strip()
                    offenders.append(f"{path.relative_to(REPO)}:{line_no}: [{label}] {line}")
        assert not offenders, (
            "these still name the deleted cuboai_<dev_id> stream -- point them at "
            "cuboai_combined_<dev_id>:\n" + "\n".join(offenders)
        )

    def test_webrtc_sensor_state_follows_the_one_rule(self):
        """A user-visible sensor STATE, documented in the README as the stream
        ID to embed. Grep patterns aimed at rtsp_url/stream_id miss it.

        It must follow the SAME rule stream_source() uses: advertising the
        combined stream while HomeKit was sent to the H.264 one is what made
        #85 look unfixed after v2.6.10 — the reporter read this sensor to
        check which stream HomeKit received, and it said the wrong thing."""
        sensor = object.__new__(CuboWebRTCStreamSensor)
        sensor._device_id = "DEV1"
        sensor.coordinator = MagicMock()
        sensor.coordinator.config_entry.options = {}
        assert sensor.native_value == "cuboai_combined_DEV1"

        sensor.coordinator.config_entry.options = {"h264_cameras": ["DEV1"]}
        assert sensor.native_value == "cuboai_h264_DEV1"

    def test_webrtc_sensor_attributes_all_point_at_combined(self):
        sensor = object.__new__(CuboWebRTCStreamSensor)
        sensor._device_id = "DEV1"
        sensor.hass = MagicMock()
        sensor.hass.data = {DOMAIN: {"rtsp_port_effective": 8557, "api_port_effective": 1986}}
        sensor.coordinator = MagicMock()
        # nvr_enabled on, so the NVR copy-paste URLs are covered too
        sensor.coordinator.config_entry.options = {"nvr_enabled": True}
        sensor.coordinator.config_entry.data = {}

        attrs = sensor.extra_state_attributes

        for key in ("rtsp_url", "web_player_url", "stream_id", "nvr_rtsp_url", "nvr_rtsp_url_video_only"):
            assert "cuboai_combined_DEV1" in attrs[key], f"{key} = {attrs[key]}"

    def test_snapshot_url_uses_the_combined_stream(self):
        """Camera snapshots fail SOFT -- a wrong stream name here silently
        serves the stale last-alert JPEG instead of a live frame."""
        import inspect

        from custom_components.cuboai import camera as camera_platform

        src = inspect.getsource(camera_platform.CuboLocalCamera.async_camera_image)
        assert "src=cuboai_combined_{self._device_id}" in src
        assert "src=cuboai_{self._device_id}" not in src


# =============================================================================
# 5. Out-of-scope streams stay exactly where they were
# =============================================================================


class TestUntouchedStreams:
    @pytest.mark.parametrize("dev_id", DEV_IDS)
    def test_speaker_stream_unchanged(self, dev_id):
        sources = _resolve()[f"cuboai_speaker_{dev_id}"]
        assert len(sources) == 1
        assert "cuboai_stream_backchannel.py" in sources[0]
        assert "#backchannel=1" in sources[0] and "#audio=pcma" in sources[0]

    @pytest.mark.parametrize("dev_id", DEV_IDS)
    def test_dvr_stream_holds_the_invariants(self, dev_id):
        """The DVR stream gained a transcode leg (Opus for WebRTC — AAC-only
        made negotiation fail and the MSE fallback renders black on iOS), but
        the #85 invariants still hold: exactly ONE exec (the playback script,
        never the live video engine), and the ffmpeg leg is a SELF-reference —
        a cross-stream reference is the 5s-DESCRIBE-vs-10s-start race that
        bb4bf13 shipped and 3942771 reverted."""
        sources = _resolve()[f"cuboai_dvr_{dev_id}"]
        execs = [s for s in sources if s.startswith("exec:")]
        assert len(execs) == 1
        assert "cuboai_stream_playback.py" in execs[0]
        assert "CUBOAI_PLAY_STATE=" in execs[0]
        assert "cuboai_stream_video.py" not in " ".join(sources)
        ffmpegs = [s for s in sources if s.startswith("ffmpeg:")]
        assert len(ffmpegs) == 1
        assert ffmpegs[0].startswith(f"ffmpeg:cuboai_dvr_{dev_id}#")
        assert "#audio=opus#audio=aac" in ffmpegs[0]


# ---------------------------------------------------------------------------
# The regression the reviewers caught in the fix itself
# ---------------------------------------------------------------------------


def test_a_still_never_starts_the_shared_engine():
    """Once every consumer shares one stream, a snapshot becomes the dangerous
    caller: asking go2rtc for a frame attaches a consumer, attaching to a cold
    stream starts the engine, and this call gives up after 5s while the engine
    needs ~10s -- so it would kill, five seconds in, the engine HomeKit, HLS,
    the card and any NVR are all sharing. Home Assistant polls stills
    constantly, so that is the common path.
    """
    src = (Path(__file__).parent.parent / "custom_components" / "cuboai" / "camera.py").read_text(encoding="utf-8")
    code = chr(10).join(line for line in src.splitlines() if not line.strip().startswith("#"))
    assert "_producer_is_warm" in code
    assert "if not await self._producer_is_warm(" in code
    assert "raise _ColdProducer" in code
    assert "except _ColdProducer:" in code
    # The probe must not itself attach a consumer.
    assert "/api/streams?src=cuboai_combined_" in code
    assert "frame.jpeg" in code


def test_the_probe_is_cheaper_than_the_thing_it_guards():
    """A guard that can block as long as the call it protects is not a guard."""
    src = (Path(__file__).parent.parent / "custom_components" / "cuboai" / "camera.py").read_text(encoding="utf-8")
    probe = src[src.index("_producer_is_warm") : src.index("async def async_camera_image")]
    assert "ClientTimeout(total=2)" in probe, "probe should be short"


# =============================================================================
# 6. HomeKit gets H.264, not just a transcode nobody consumes (issue #85, pt 2)
# =============================================================================


class TestHomeKitActuallyReceivesH264:
    """v2.6.0 gave every consumer ONE stream — correct, and not enough.

    That stream also carries the camera's NATIVE video, so a plain RTSP
    consumer (HA's stream worker, which HomeKit rides) takes the HEVC and
    fails to decode it. The reporter's diagnostics showed the transcode leg
    sitting idle with `tracks=[] recv=None` while the RTSP consumer pulled
    `hevc:264pkts`. A consumer that MUST have H.264 needs a stream whose only
    video is H.264.
    """

    def test_no_h264_stream_unless_the_toggle_is_on(self):
        streams = _resolve()
        assert not [k for k in streams if k.startswith("cuboai_h264_")]

    def test_toggled_camera_gets_an_h264_only_stream(self):
        streams = _resolve({"h264_cameras": ["SW05BBB"]})
        sources = streams["cuboai_h264_SW05BBB"]
        assert len(sources) == 1
        assert sources[0].startswith("ffmpeg:cuboai_combined_SW05BBB#")
        assert "video=h264" in sources[0]
        # No second video codec to pick from — that is the whole point.
        assert "video=copy" not in sources[0]
        # The untoggled camera is untouched.
        assert "cuboai_h264_CB02AAA" not in streams

    def test_the_h264_stream_never_spawns_a_second_camera_session(self):
        """The #85 invariant, which the reverted bb4bf13 broke.

        A cross-stream reference is only safe because it declares NO exec: it
        reuses the combined stream's producer. Two execs against one camera is
        the double-session bug this whole issue was about.
        """
        streams = _resolve({"h264_cameras": ["SW05BBB", "CB02AAA"]})
        for dev in ("CB02AAA", "SW05BBB"):
            assert not [s for s in streams[f"cuboai_h264_{dev}"] if s.startswith("exec:")]
            video_execs = [
                s
                for name, srcs in streams.items()
                if name.endswith(dev)
                for s in srcs
                if s.startswith("exec:") and "cuboai_stream_video.py" in s
            ]
            assert len(video_execs) == 1, f"{dev} runs the video engine {len(video_execs)}x"

    def test_stream_source_points_homekit_at_the_h264_stream(self):
        """The plumbing only matters if the consumer is sent to it."""
        code = (Path(__file__).parent.parent / "custom_components" / "cuboai" / "camera.py").read_text(encoding="utf-8")
        assert "live_stream_name(self._device_id, self.coordinator.config_entry.options)" in code
        # and the rule itself resolves the way HomeKit needs
        from custom_components.cuboai.const import live_stream_name

        assert live_stream_name("DEV1", {"h264_cameras": ["DEV1"]}) == "cuboai_h264_DEV1"
        assert live_stream_name("DEV1", {}) == "cuboai_combined_DEV1"

    def test_the_prewarm_stays_on_the_source_stream(self):
        """The single most dangerous line to "tidy up".

        stream_source() pre-warms cuboai_combined_ even when it then hands out
        cuboai_h264_. That looks inconsistent and is the whole reason the
        cross-stream reference is safe: the h264 stream is a transcode OF the
        combined one, so the combined one must be warm before its nested
        ffmpeg dials it. Point the pre-warm at cuboai_h264_ and it dials a COLD
        combined through an ffmpeg that gives up after 5s while the engine
        needs ~10s — precisely the bb4bf13 failure that had to be reverted.
        """
        import inspect

        from custom_components.cuboai import camera as camera_platform

        src = inspect.getsource(camera_platform.CuboLocalCamera.stream_source)
        assert "frame.jpeg?src=cuboai_combined_{self._device_id}" in src
        assert "frame.jpeg?src=cuboai_h264_" not in src
