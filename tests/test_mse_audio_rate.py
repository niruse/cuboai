"""Sound and picture must keep going on MSE — the path a phone takes away from
home, where WebRTC can't connect (mobile data through a Cloudflare tunnel).

video-rtc's MSE handler (WebRTC Camera 3.6.1) broke both there:
- it sets `playbackRate = seconds buffered ahead` (floor 0.1) on every
  fragment, so over bursty delivery the speed swings 0.1x-3x; the stream carried
  a song continuously while the iPhone played one-second fragments of it;
- it trims the SourceBuffer to 5 s, and MSE removes video up to the NEXT
  keyframe (every 4 s on this camera), so a trim can delete the frames being
  played; an earlier speed clamp that kept a cushion therefore froze every few
  seconds.
The card owns the speed (three fixed values, hysteresis, from a timer) and
guards the trim. Every test names the mutation it kills.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CARD = Path(__file__).resolve().parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js"
CAMERA_GOP_S = 4.0  # measured keyframe interval of the camera's stream
VIDEO_RTC_TRIM_S = 5.0  # video-rtc keeps the last 5 s and jumps ahead past it

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _src() -> str:
    return CARD.read_text(encoding="utf-8")


def _const(name: str) -> float:
    return float(re.search(rf"const {name} = ([\d.]+);", _src()).group(1))


def _helper_src() -> str:
    src = _src()
    return src[src.index("const CUBOAI_TRIM_KEEP_S") : src.index("class CuboAICameraCardEditor")]


# A stand-in for the browser: HTMLMediaElement with a prototype playbackRate
# accessor (like the real one), TimeRanges, and timers the test drives by hand.
_PRELUDE = """
class HTMLMediaElement {}
Object.defineProperty(HTMLMediaElement.prototype, 'playbackRate', {
  configurable: true,
  get() { return this._rate === undefined ? 1 : this._rate; },
  set(v) { this._rate = v; this._writes = (this._writes || 0) + 1; },
});
const timers = new Map(); let nextId = 1;
globalThis.setInterval = (fn, ms) => { const id = nextId++; timers.set(id, fn); return id; };
globalThis.clearInterval = (id) => { timers.delete(id); };
const runTimers = (n = 1) => { for (let i = 0; i < n; i++) for (const fn of [...timers.values()]) fn(); };
const ranges = (...pairs) => ({ length: pairs.length, start: (i) => pairs[i][0], end: (i) => pairs[i][1] });
const makeVideo = () => {
  const v = Object.create(HTMLMediaElement.prototype);
  v.isConnected = true; v.paused = false; v.currentTime = 0; v.buffered = ranges(); v.srcObject = null;
  return v;
};
class MediaStream {}
class FakeSourceBuffer { remove(s, e) { (this.calls = this.calls || []).push([s, e]); return 'removed'; } }
"""


def _node(body: str, tmp_path) -> dict:
    script = tmp_path / "mse.js"
    script.write_text(_PRELUDE + _helper_src() + body, encoding="utf-8")
    out = subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)


def test_speeds_stay_where_audio_plays_and_the_cushion_is_ordered():
    """Kill: a speed moved outside 0.5-2x, the thresholds reordered (no
    hysteresis band), or a target cushion that video-rtc's 5 s jump would cut."""
    slow, fast = _const("CUBOAI_RATE_SLOW"), _const("CUBOAI_RATE_FAST")
    assert 0.5 < slow < 1.0 < fast < 2.0
    low, low_exit = _const("CUBOAI_AHEAD_LOW"), _const("CUBOAI_AHEAD_LOW_EXIT")
    high_exit, high = _const("CUBOAI_AHEAD_HIGH_EXIT"), _const("CUBOAI_AHEAD_HIGH")
    assert 1.0 <= low < low_exit < high_exit < high < VIDEO_RTC_TRIM_S


def test_the_trim_keeps_two_keyframe_intervals():
    """MSE removes video up to the next keyframe, so less than two GOPs behind
    the playhead can take the playing GOP. Kill: the keep margin shrunk."""
    assert _const("CUBOAI_TRIM_KEEP_S") >= 2 * CAMERA_GOP_S


def test_every_player_video_gets_the_steering():
    """Installed where the card first takes hold of each <video>. Kill: the
    call removed from the per-video init, or the old setter-driven steering back."""
    src = _src()
    init = src[src.index('video.dataset.cuboInit = "true";') :][:2500]
    assert "cuboaiSteerMsePlayback(video);" in init
    assert "cuboaiRateForBuffer" not in src and "cuboaiClampPlaybackRate" not in src


@needs_node
def test_next_rate_holds_each_speed_until_its_exit(tmp_path):
    """Kill: continuous (non-discrete) speeds, a missing or swapped exit
    threshold, or the null/NaN guard."""
    got = _node(
        """
const R = cuboaiNextRate, S = CUBOAI_RATE_SLOW, F = CUBOAI_RATE_FAST;
const out = {
  n_low: R(1, 1.9), n_mid_lo: R(1, 2.0), n_mid_hi: R(1, 4.2), n_high: R(1, 4.3),
  s_stay: R(S, 2.5), s_exit: R(S, 2.7), s_burst: R(S, 9),
  f_stay: R(F, 3.7), f_exit: R(F, 3.5), f_empty: R(F, 0),
  none: R(S, null), nan: R(F, NaN), undef: R(1, undefined),
};
const seen = new Set(); let r = 1;
for (let i = 0; i <= 2000; i++) { r = R(r, (Math.sin(i / 7) + 1) * 3.5); seen.add(r); }
out.values = [...seen].sort();
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    slow, fast = _const("CUBOAI_RATE_SLOW"), _const("CUBOAI_RATE_FAST")
    assert (got["n_low"], got["n_mid_lo"], got["n_mid_hi"], got["n_high"]) == (slow, 1, 1, fast)
    assert (got["s_stay"], got["s_exit"], got["s_burst"]) == (slow, 1, 1)
    assert (got["f_stay"], got["f_exit"], got["f_empty"]) == (fast, 1, 1)
    assert (got["none"], got["nan"], got["undef"]) == (1, 1, 1)
    assert got["values"] == sorted([slow, 1, fast])


@needs_node
def test_buffered_ahead_follows_the_range_under_the_playhead(tmp_path):
    """Kill: measuring to the last range across a real hole (video-rtc's way),
    not merging hairline gaps, or treating an empty buffer as 0 s."""
    got = _node(
        """
const v = makeVideo(); const A = () => cuboaiBufferedAhead(v);
const out = {};
out.empty = A();
v.buffered = ranges([10, 14]); v.currentTime = 11; out.inside = A();
v.buffered = ranges([10, 14], [14.1, 16]); out.hairline = A();
v.buffered = ranges([10, 14], [15, 20]); out.hole = A();
v.buffered = ranges([10, 14]); v.currentTime = 14; out.at_end = A();
v.buffered = ranges([10, 14]); v.currentTime = 5; out.before = A();
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["empty"] is None
    assert got["inside"] == 3
    assert got["hairline"] == 5
    assert got["hole"] == 3
    assert got["at_end"] == 0
    assert got["before"] == 0


@needs_node
def test_the_trim_never_reaches_the_playing_frames(tmp_path):
    """Runs the guard on a fake SourceBuffer. Kill: the end not pulled back,
    tiny trims not skipped, a prototype-wide patch, or double wrapping."""
    got = _node(
        """
const v = makeVideo(); v.currentTime = 20;
const sb = new FakeSourceBuffer(), other = new FakeSourceBuffer();
cuboaiGuardSourceBuffer(v, sb); const once = sb.remove;
cuboaiGuardSourceBuffer(v, sb);
const out = { wrapped_once: sb.remove === once };
out.ret = sb.remove(0, 15);          // video-rtc: keep the last 5 s
sb.remove(11.5, 15);                 // would free only 0.5 s
sb.remove(0, 5);                     // entirely old: untouched
sb.remove(NaN, 15);
v.currentTime = 30; sb.remove(12, 25);
other.remove(0, 25);
out.calls = sb.calls; out.other = other.calls;
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    keep = _const("CUBOAI_TRIM_KEEP_S")
    assert got["ret"] == "removed"
    assert got["wrapped_once"], "a second install wrapped the guard again"
    assert got["calls"] == [[0, 20 - keep], [0, 5], [12, 30 - keep]]
    assert got["other"] == [[0, 25]], "the guard leaked onto another SourceBuffer"


@needs_node
def test_the_card_owns_the_speed_and_guards_every_new_source(tmp_path):
    """Drives the installed hook through its timer. Kill: video-rtc's writes
    applied, the speed not driven by the real buffer, the guard not installed
    on a SourceBuffer created later (reconnect), a pre-existing speed kept, or
    the hook leaking onto other elements."""
    got = _node(
        """
const v = makeVideo(); v.playbackRate = 3;
cuboaiSteerMsePlayback(v); cuboaiSteerMsePlayback(v);
const out = { start: v.playbackRate, timers: timers.size };
v.playbackRate = 0.1; out.after_player_write = v.playbackRate;
const sb1 = new FakeSourceBuffer();
v.srcObject = { sourceBuffers: [sb1] };
v.buffered = ranges([0, 1]); v.currentTime = 0.5; runTimers(); out.short = v.playbackRate;
v.buffered = ranges([0, 4]); v.currentTime = 1; runTimers(); out.refilled = v.playbackRate;
v.buffered = ranges([0, 7]); v.currentTime = 1.5; runTimers(); out.long = v.playbackRate;
out.sb1 = !!sb1.__cuboTrimGuard;
const sb2 = new FakeSourceBuffer();
v.srcObject = { sourceBuffers: [sb2] }; runTimers(); out.sb2 = !!sb2.__cuboTrimGuard;
v.srcObject = {}; v.buffered = ranges(); runTimers(); out.nothing_buffered = v.playbackRate;
v.buffered = ranges([0, 7]); runTimers(); out.long_again = v.playbackRate;
v.buffered = ranges([0, 4.5]); runTimers(); out.settled = v.playbackRate;
v.srcObject = new MediaStream(); v.buffered = ranges([0, 0.2]); v.currentTime = 0.1;
runTimers(); out.webrtc = v.playbackRate;
const other = makeVideo(); other.playbackRate = 3; out.other = other.playbackRate;
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["start"] == 1 and got["timers"] == 1
    assert got["after_player_write"] == 1, "video-rtc's 'seconds buffered' reached the element"
    assert got["short"] == _const("CUBOAI_RATE_SLOW")
    assert got["refilled"] == 1
    assert got["long"] == _const("CUBOAI_RATE_FAST")
    assert got["sb1"] and got["sb2"]
    assert got["nothing_buffered"] == 1
    assert got["long_again"] == _const("CUBOAI_RATE_FAST")
    assert got["settled"] == 1
    assert got["webrtc"] == 1, "a live WebRTC stream was slowed down"
    assert got["other"] == 3


@needs_node
def test_a_detached_player_keeps_steering_until_it_stops(tmp_path):
    """With `background: true` a detached player keeps playing. Kill: parking
    a playing detached player, never parking a stopped one, or a parked timer
    that player activity can't restart."""
    got = _node(
        """
const v = makeVideo(); cuboaiSteerMsePlayback(v);
const ticks = CUBOAI_MSE_PARK_MS / CUBOAI_MSE_TICK_MS;
v.isConnected = false; runTimers(ticks + 5);
const out = { playing_detached: timers.size };
v.paused = true; runTimers(ticks + 5); out.stopped_detached = timers.size;
v.playbackRate = 2; out.write_while_stopped = timers.size;
v.isConnected = true; v.playbackRate = 2; out.reattached = timers.size;
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["playing_detached"] == 1
    assert got["stopped_detached"] == 0
    assert got["write_while_stopped"] == 0
    assert got["reattached"] == 1
