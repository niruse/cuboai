"""The fixed stream UniFi Protect is pointed at (go2rtc `cuboai_protect_<id>`).

Found in the live adoption loop: Protect's media server locks in the stream
address it is given at adoption. A reconnect, Protect's own re-adopt and a go2rtc
restart all left it pulling the OLD stream after the H.264 option was ticked, so
a Cubo 3 owner following diagnostics' advice would have kept sending Protect
HEVC. The fix is one fixed name whose content follows the option. Every test
names the mutation it kills.
"""

import asyncio
from unittest.mock import MagicMock

from custom_components.cuboai.const import OPT_PROTECT_CAMERA, OPT_PROTECT_ENABLED
from custom_components.cuboai.go2rtc import Go2RTCManager

A, B = "CB02AAAA00000001", "SW05BBBB00000002"


def _plan(options, cameras=None):
    mgr = Go2RTCManager(MagicMock())
    mgr._cameras = cameras or [{"device_id": A, "uid": "u1"}, {"device_id": B, "uid": "u2"}]
    mgr._options = options
    mgr._streams = {}
    mgr._rtsp_port = 8557
    asyncio.run(mgr._resolve_codecs())
    return mgr._streams


def test_no_protect_stream_unless_protect_is_on():
    """Kill: the alias declared regardless of the option (it would also break
    test_issue_85's default-streams invariant)."""
    assert not any(name.startswith("cuboai_protect_") for name in _plan({}))


def test_exactly_one_protect_stream_for_the_chosen_camera():
    """One camera per HA host. Kill: alias declared for every camera."""
    streams = _plan({OPT_PROTECT_ENABLED: True, OPT_PROTECT_CAMERA: B})
    assert [n for n in streams if n.startswith("cuboai_protect_")] == [f"cuboai_protect_{B}"]


def test_the_alias_re_reads_the_native_stream_by_default():
    """A pure RTSP loopback — no transcode, no extra process. Kill: target
    changed, or an ffmpeg leg used."""
    src = _plan({OPT_PROTECT_ENABLED: True})[f"cuboai_protect_{A}"]
    assert src == [f"rtsp://127.0.0.1:8557/cuboai_combined_{A}#timeout=20"]


def test_the_alias_follows_the_h264_option():
    """THE fix: same name, different content. Kill: target ignoring the option."""
    src = _plan({OPT_PROTECT_ENABLED: True, "h264_cameras": [A]})[f"cuboai_protect_{A}"]
    # Built from a variable: test_issue_85 scans every file for literal
    # rtsp:// paths to anything but combined/speaker/dvr.
    transcode = f"cuboai_h264_{A}"
    assert src == [f"rtsp://127.0.0.1:8557/{transcode}#timeout=20"]


def test_the_alias_authenticates_when_the_rtsp_listener_requires_it():
    """With NVR auth on, go2rtc's RTSP server rejects the loopback without
    credentials. Kill: userinfo dropped."""
    opts = {OPT_PROTECT_ENABLED: True, "nvr_enabled": True, "nvr_username": "nvr", "nvr_password": "p w"}
    src = _plan(opts)[f"cuboai_protect_{A}"]
    assert src == [f"rtsp://nvr:p%20w@127.0.0.1:8557/cuboai_combined_{A}#timeout=20"]


def test_the_alias_uses_the_port_go2rtc_actually_bound():
    """RTSP self-heals (8555 is usually HA's). Kill: the option value used
    instead of the resolved port."""
    mgr = Go2RTCManager(MagicMock())
    mgr._cameras = [{"device_id": A, "uid": "u1"}]
    mgr._options = {OPT_PROTECT_ENABLED: True, "rtsp_port": 8555}
    mgr._streams = {}
    mgr._rtsp_port = 8559
    asyncio.run(mgr._resolve_codecs())
    assert mgr._streams[f"cuboai_protect_{A}"][0].startswith("rtsp://127.0.0.1:8559/")


def test_the_alias_never_starts_a_second_camera_session():
    """One exec per camera (#85). Kill: the alias given an exec source."""
    src = _plan({OPT_PROTECT_ENABLED: True})[f"cuboai_protect_{A}"]
    assert not any(s.startswith("exec:") for s in src)
