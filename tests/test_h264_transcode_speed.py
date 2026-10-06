"""The H.264 transcode has to keep up with the camera (issue #85, 2.6.47).

On a Raspberry Pi 4 the `cuboai_h264_<id>` conversion ran at 0.53-0.66x real
time, so the picture fell further behind the longer it was open and HomeKit
showed "No Response". Two causes, both measured from the reporter's log:

* ffmpeg's RTSP output is constant-frame-rate. The Cubo 3's HEVC was guessed
  at 29.92 fps while the camera sends 15, so more than half of what libx264
  encoded were repeated frames (dup=604 of 1120).
* 1080p is more than that machine can convert; 720p (what HomeKit asked for)
  ran smoothly in the reporter's own test.

So the transcode encodes every camera frame once, has a keyframe every 15
frames (go2rtc's 50 frames was 5 s at 10 fps, all of it a black wait for a new
viewer), and its size is an option. Every test names the mutation it kills.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.cuboai import config_flow as cf
from custom_components.cuboai import go2rtc as g2
from custom_components.cuboai.const import (
    DOMAIN,
    H264_KEYINT,
    H264_RESOLUTION_DEFAULT,
    H264_RESOLUTIONS,
    OPT_H264_RESOLUTION,
    h264_resolution,
)

HEVC_CAM, H264_CAM = "SW05BBB", "CB02AAA"
FFMPEG_8 = "ffmpeg version 8.1.2 Copyright (c) 2000-2026 the FFmpeg developers\nbuilt with gcc 15.2.0 (Alpine 15.2.0)\n"
FFMPEG_44 = "ffmpeg version 4.4.2-0ubuntu0.22.04.1 Copyright (c) 2000-2021 the FFmpeg developers\n"


@pytest.fixture(autouse=True)
def _fresh_version_probe(monkeypatch):
    """Each test asks ffmpeg afresh: the answer is cached per Home Assistant run."""
    monkeypatch.setattr(g2, "_FPS_FLAG", None)


def _resolve(options=None, version_text=FFMPEG_8):
    """The real stream planner, with an ffmpeg that answers `version_text`."""
    hass = MagicMock()

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    mgr = g2.Go2RTCManager(hass)
    mgr._cameras = [
        {"device_id": H264_CAM, "uid": "u1", "account": "a1", "password": "p1"},
        {"device_id": HEVC_CAM, "uid": "u2", "account": "a2", "password": "p2"},
    ]
    mgr._options = {"h264_cameras": [HEVC_CAM], **(options or {})}
    mgr._streams = {}
    with patch.object(g2, "_ffmpeg_version_text", lambda: version_text):
        asyncio.run(mgr._resolve_codecs())
    return mgr._streams, hass


def _h264_source(options=None, version_text=FFMPEG_8):
    streams, _ = _resolve(options, version_text)
    (source,) = streams[f"cuboai_h264_{HEVC_CAM}"]
    return source


# =============================================================================
# Every camera frame once
# =============================================================================


def test_every_camera_frame_is_encoded_once():
    """Kill: the passthrough flag dropped (ffmpeg pads to its guessed 30 fps)."""
    src = _h264_source()
    assert "#raw=-fps_mode passthrough " in src
    assert "-vsync" not in src


def test_an_ffmpeg_older_than_5_1_gets_the_option_it_knows():
    """-fps_mode does not exist before 5.1; an unknown option would stop the
    transcode altogether. Kill: the version check removed."""
    src = _h264_source(version_text=FFMPEG_44)
    assert "#raw=-vsync passthrough " in src
    assert "-fps_mode" not in src


@pytest.mark.parametrize(
    ("banner", "flag"),
    [
        (FFMPEG_8, g2.FPS_MODE_PASSTHROUGH),
        ("ffmpeg version n7.1 Copyright", g2.FPS_MODE_PASSTHROUGH),
        ("ffmpeg version 5.1.6-0+deb12u1 Copyright", g2.FPS_MODE_PASSTHROUGH),
        ("ffmpeg version 5.0.3 Copyright", g2.VSYNC_PASSTHROUGH),
        (FFMPEG_44, g2.VSYNC_PASSTHROUGH),
        ("ffmpeg version N-118411-g1c3a2b2f8d Copyright", g2.FPS_MODE_PASSTHROUGH),
        ("", g2.FPS_MODE_PASSTHROUGH),
    ],
)
def test_the_version_decides_the_option(banner, flag):
    """5.1 is the boundary; an unreadable version (git build, no ffmpeg) is
    taken as current. Kill: the comparison flipped or moved off 5.1."""
    assert g2.fps_passthrough_flag(banner) == flag


def test_ffmpeg_is_asked_once_per_run():
    """Kill: the cache removed (a subprocess on every stream rebuild)."""
    _, hass = _resolve()
    mgr = g2.Go2RTCManager(hass)
    mgr._cameras, mgr._options, mgr._streams = [{"device_id": HEVC_CAM}], {"h264_cameras": [HEVC_CAM]}, {}
    asyncio.run(mgr._resolve_codecs())
    assert hass.async_add_executor_job.await_count == 1


def test_ffmpeg_is_not_asked_when_no_camera_transcodes():
    """Kill: the probe moved out of the transcode branch."""
    _, hass = _resolve({"h264_cameras": []})
    hass.async_add_executor_job.assert_not_awaited()


def test_a_failed_probe_assumes_a_current_ffmpeg():
    """A probe error must never stop the streams. Kill: the except removed."""
    hass = MagicMock()
    hass.async_add_executor_job = AsyncMock(side_effect=OSError("no ffmpeg"))
    mgr = g2.Go2RTCManager(hass)
    mgr._cameras, mgr._options, mgr._streams = [{"device_id": HEVC_CAM}], {"h264_cameras": [HEVC_CAM]}, {}
    asyncio.run(mgr._resolve_codecs())
    assert "-fps_mode passthrough" in mgr._streams[f"cuboai_h264_{HEVC_CAM}"][0]


def test_the_real_probe_survives_a_missing_ffmpeg():
    """Kill: OSError not caught in _ffmpeg_version_text."""
    with patch.object(g2.subprocess, "run", side_effect=FileNotFoundError("ffmpeg")):
        assert g2._ffmpeg_version_text() == ""


# =============================================================================
# Keyframes and the HomeKit limits
# =============================================================================


def test_a_keyframe_every_15_frames_and_level_4_0():
    """go2rtc's template sets -g 50 and -level:v 4.1 AFTER our arguments, so
    both go through -x264-params, which libx264 applies last. Kill: keyint or
    the level dropped from x264-params."""
    assert H264_KEYINT == 15
    assert "-profile:v high -x264-params level=4.0:keyint=15" in _h264_source()


# =============================================================================
# The size option
# =============================================================================


def test_1080p_is_the_default_cap():
    """Kill: the default changed (every Cubo 3 user's picture would shrink)."""
    src = _h264_source()
    assert "scale='min(1920,iw)':'min(1080,ih)':force_original_aspect_ratio=decrease" in src
    assert H264_RESOLUTION_DEFAULT == "1080p"


def test_720p_caps_the_transcode_at_720p():
    """Kill: the option ignored by the stream planner."""
    src = _h264_source({OPT_H264_RESOLUTION: "720p"})
    assert "scale='min(1280,iw)':'min(720,ih)':force_original_aspect_ratio=decrease" in src
    assert "1920" not in src


@pytest.mark.parametrize("value", [None, "", "4k", 720])
def test_an_unknown_size_falls_back_to_1080p(value):
    """A hand-edited or future value must not break the stream. Kill: the
    membership check in h264_resolution() removed (KeyError)."""
    assert h264_resolution({OPT_H264_RESOLUTION: value}) == "1080p"
    assert "min(1920,iw)" in _h264_source({OPT_H264_RESOLUTION: value})


def test_only_the_transcode_changes():
    """The combined stream (card, NVR) and an untoggled camera are untouched.
    Kill: the arguments applied to every stream."""
    streams, _ = _resolve({OPT_H264_RESOLUTION: "720p"})
    for name, sources in streams.items():
        if name != f"cuboai_h264_{HEVC_CAM}":
            assert not any("fps_mode" in s or "min(1280" in s for s in sources), name
    assert f"cuboai_h264_{H264_CAM}" not in streams


@pytest.mark.asyncio
async def test_configure_offers_the_size_and_keeps_the_saved_one():
    """Kill: the field removed from Configure, its default changed, or the
    saved value not shown."""
    import voluptuous as vol

    cams = [{"device_id": HEVC_CAM, "baby_name": "B"}]
    for saved, shown in (({}, "1080p"), ({OPT_H264_RESOLUTION: "720p"}, "720p")):
        hass = MagicMock()
        hass.data = {DOMAIN: {}}
        hass.config_entries.async_entries = lambda domain: []
        entry = MagicMock(entry_id="entryA", options=saved, data={"cameras": cams, "all_cameras": cams})
        flow = cf.CuboAIOptionsFlowHandler()
        flow.hass, flow.config_entry = hass, entry
        flow.async_show_form = lambda **kw: kw
        with patch.object(cf, "setup_file_logger", MagicMock()):
            result = await flow.async_step_init()
        schema = result["data_schema"].schema
        keys = [str(k) for k in schema]
        key = next(k for k in schema if str(k) == OPT_H264_RESOLUTION)
        assert keys.index(OPT_H264_RESOLUTION) == keys.index("h264_cameras") + 1
        assert key.default() == shown
        validator = schema[key]
        assert isinstance(validator, vol.In) and list(validator.container) == list(H264_RESOLUTIONS)
