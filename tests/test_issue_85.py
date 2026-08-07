"""Issue #85: one camera session, however many things are watching.

A Cubo 3 (SW05) streamed fine in Home Assistant and reported "No Response" in
HomeKit at the same moment. The reporter's decisive test was watching it play
on a laptop without a stutter while HomeKit failed alongside it: the camera was
fine, opening it *twice* was not.

Every camera defined two streams, and each spawned its own `exec:` copy of the
video script -- so a dashboard viewer and a HomeKit viewer were two independent
TUTK sessions against the same camera. A Cubo Plus (CB02) tolerates that; a
Cubo 3 does not, and the second session produced nothing at all:

    producer(mpegts) tracks=[hevc:264pkts, aac:274pkts]
    producer(ffmpeg 'video=h264...') tracks=[] recv=None      <- the transcode
    consumer(rtsp ...) tracks=[hevc:264pkts, aac:274pkts]

which is why the H.264 transcode appeared to be the culprit: it sat downstream
of a source session that never delivered, so HomeKit only ever saw HEVC.

The combined stream now references the main stream instead, and go2rtc reuses
that producer.
"""

import re
from pathlib import Path

GO2RTC = Path(__file__).parent.parent / "custom_components" / "cuboai" / "go2rtc.py"


def _stream_block(name_fragment: str) -> str:
    """The list literal assigned to a `self._streams[...]` key."""
    src = GO2RTC.read_text(encoding="utf-8")
    m = re.search(
        r"self\._streams\[f\"" + name_fragment + r"[^\]]*\]\s*=\s*\[(.*?)\]",
        src,
        re.S,
    )
    assert m, f"no stream definition matching {name_fragment!r}"
    return m.group(1)


def test_the_camera_is_opened_once_not_once_per_stream():
    """The invariant the fix rests on. Counted across the whole file, because
    a second `exec:` of the video script anywhere reintroduces the bug."""
    src = GO2RTC.read_text(encoding="utf-8")
    code = "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))
    launches = re.findall(r"exec:[^\"]*\{video_script\}", code)
    assert len(launches) == 1, (
        f"the video engine is launched {len(launches)} times; each one is a separate session against the same camera"
    )


def test_the_combined_stream_reuses_the_main_one():
    block = _stream_block("cuboai_combined_")
    assert "{video_script}" not in block, "combined stream still opens its own session"
    assert "ffmpeg:cuboai_{dev_id}#video=" in block, "combined stream must take its video from the main stream"


def test_two_way_audio_survives_the_change():
    """The backchannel is the whole reason this stream exists separately, and
    it is an `exec:` of a different script -- it must not have been swept up."""
    block = _stream_block("cuboai_combined_")
    assert "{backchannel_script}" in block
    assert "backchannel=1" in block and "audio=pcma" in block


def test_the_transcode_still_reaches_the_combined_stream():
    """HomeKit consumes the combined stream, and on an HEVC camera it needs the
    H.264 variant -- the toggle behind `video_codec`. Dropping it would leave
    #85 fixed in shape and broken in effect."""
    block = _stream_block("cuboai_combined_")
    assert "{video_codec}" in block
    assert "{audio_codecs}" in block, "HomeKit and MSE both need the AAC track"
