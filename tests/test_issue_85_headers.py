"""#85, 2026-10-22: the transcode arrived on time and HomeKit still said No Response.

1. SPS/PPS only in the SDP. ffmpeg's RTSP output into go2rtc sets the
   global-header flag, so libx264 put the parameter sets in the SDP and not in
   front of keyframes. HomeKit copies the video into RTP for the Apple device,
   which never sees that SDP. Measured on a live box: 0 of 7 keyframes carried
   SPS/PPS; with repeat-headers=1 (or dump_extra) every one did. The camera's
   own H.264 always carries them, which is why native H.264 played.
2. The diagnostics verdict called a conversion "falling further" behind from
   ffmpeg's speed=, which is an average since the start: a late start kept it
   at 0.86x long after the conversion ran at 1.02x.
"""

from unittest.mock import MagicMock

import pytest

from custom_components.cuboai import diagnostics as diag
from custom_components.cuboai import go2rtc as g2


def test_the_software_transcode_repeats_sps_pps_on_every_keyframe():
    """In the same -x264-params as level and keyint: libx264 applies those last,
    after go2rtc's template. Kill: repeat-headers dropped or moved out."""
    args = g2.h264_transcode_args({})
    params = args.split("-x264-params ", 1)[1].split()[0]
    assert params.split(":") == ["level=4.0", "keyint=15", "repeat-headers=1"]
    assert g2.h264_transcode_args({"h264_resolution": "720p"}).count("repeat-headers=1") == 1


def test_the_vaapi_encoder_repeats_them_too():
    """h264_vaapi has no repeat-headers; the bitstream filter adds the parameter
    sets after any encoder. It is in the template go2rtc uses AND in the startup
    test, so the test proves the filter exists in this ffmpeg.
    Kill: dump_extra dropped from VAAPI_H264_ARGS."""
    assert "-bsf:v dump_extra" in g2.VAAPI_H264_ARGS
    assert g2.VAAPI_H264_ARGS.startswith("-c:v h264_vaapi ")


def test_the_vaapi_startup_test_runs_the_filter(monkeypatch):
    monkeypatch.setattr(g2.sys, "platform", "linux")
    monkeypatch.setattr(g2, "_render_device_ok", lambda device: True)
    g2._VAAPI_PASSED.clear()
    run = MagicMock(return_value=MagicMock(returncode=0))
    monkeypatch.setattr(g2.subprocess, "run", run)
    assert g2._probe_vaapi("/dev/dri/renderD128", "1080p")
    argv = run.call_args.args[0]
    assert argv[argv.index("-bsf:v") + 1] == "dump_extra"
    g2._VAAPI_PASSED.clear()


def _line(video_s, elapsed_s, speed):
    t = f"00:00:{video_s:05.2f}" if video_s < 60 else f"00:{int(video_s // 60):02d}:{video_s % 60:05.2f}"
    e = f"0:00:{elapsed_s:05.2f}" if elapsed_s < 60 else f"0:{int(elapsed_s // 60):02d}:{elapsed_s % 60:05.2f}"
    return diag.parse_progress(f"frame=  763 fps= 14 q=25.0 size=N/A time={t} bitrate=N/A speed={speed}x elapsed={e}")


def test_a_late_start_that_keeps_up_is_not_called_slow():
    """The reporter's reading: 0.86x as an average, 54 s of video in 62 s, then
    1.02x. Eight seconds behind is the start, not a machine falling behind.
    Kill: STARTUP_LAG_S check removed."""
    facts = diag.transcode_facts([_line(53.91, 62.5, 0.862), _line(53.91, 52.63, 1.02)])
    assert facts["slow"] is None
    assert facts["slowest_speed"] == 0.862  # still reported, just not a verdict


@pytest.mark.parametrize(
    "video,elapsed,speed,slow",
    [
        (43.0, 80.0, 0.53, True),  # the original #85 Pi 4: 37 s behind and growing
        (50.0, 60.0, 0.83, False),  # exactly STARTUP_LAG_S behind
        (50.0, 60.5, 0.83, True),  # past it
        (100.0, 105.0, 0.95, False),  # above SLOW_SPEED anyway
    ],
)
def test_slow_means_far_behind_and_below_the_threshold(video, elapsed, speed, slow):
    """Kill: the lag compared the wrong way, or >= instead of >."""
    facts = diag.transcode_facts([_line(video, elapsed, speed)])
    assert (facts["slow"] is not None) is slow


def test_without_elapsed_the_lag_comes_from_speed():
    """ffmpeg before 7.x prints no elapsed=: wall time is video time / speed.
    60 s at 0.8x = 75 s, 15 s behind. Kill: elapsed None treated as no lag."""
    p = diag.parse_progress("frame=  900 fps= 15 q=23.0 size=N/A time=00:01:00.00 bitrate=N/A speed=0.8x")
    assert diag._lag_s(p) == pytest.approx(15.0)
    assert diag.transcode_facts([p])["slow"] is p
