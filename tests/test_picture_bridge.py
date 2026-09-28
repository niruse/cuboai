"""The last picture bridges the black gap while the player changes stream.

Reported 2026-09-28: "small black screen while changing" (Wi-Fi <-> mobile data),
then "Wi-Fi then 5G: black 2 s; back to Wi-Fi: black 1 s", then "first image,
then black screen, then image". What an iPhone does: at Wi-Fi -> 5G the WebRTC
video pauses on its last frame, video-rtc reconnects (which it may delay up to
15 s) and attaches MSE, and the picture is black until the camera's next
keyframe; it apparently fires no timeupdate while playing WebRTC; and a new
WebRTC stream reports its first frame before the frame is on screen. So the
card copies the picture about once a second while the video really plays (three
triggers), shows the copy from the moment a WebRTC stream ends or the source is
swapped, holds it for as long as the reconnect may take, and hides it once the
new picture has settled on screen. A stale or let-go copy is never shown: a
frozen picture must not pass for live. Every test names the mutation it kills.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CARD = Path(__file__).resolve().parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _src() -> str:
    return CARD.read_text(encoding="utf-8")


def _helper() -> str:
    src = _src()
    return src[src.index("const CUBOAI_BRIDGE_MAX_MS") : src.index("// Single source of truth for the webrtc-camera")]


_PRELUDE = r"""
let NOW = 0, tid = 1; const timers = new Map();
globalThis.performance = { now: () => NOW };
globalThis.setTimeout = (fn, ms) => { const id = tid++; timers.set(id, { fn, at: NOW + (Number(ms) || 0) }); return id; };
globalThis.clearTimeout = (id) => { timers.delete(id); };
globalThis.setInterval = (fn, ms) => { const id = tid++; timers.set(id, { fn, at: NOW + ms, every: ms }); return id; };
globalThis.clearInterval = (id) => { timers.delete(id); };
const advance = (ms) => { const end = NOW + ms; for (;;) { let next = null;
  for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
  if (!next) break; const [id, t] = next; NOW = t.at;
  if (t.every) t.at += t.every; else timers.delete(id); t.fn(); } NOW = end; };
const log = [];
// style.cssText parses into properties, as in a browser.
const makeStyle = () => { const st = {}; Object.defineProperty(st, 'cssText', { set(t) {
  for (const part of String(t).split(';')) { const i = part.indexOf(':'); if (i < 0) continue;
    const k = part.slice(0, i).trim().replace(/-([a-z])/g, (m, c) => c.toUpperCase()); st[k] = part.slice(i + 1).trim(); } } });
  return st; };
class El {
  constructor(tag) { this.tag = tag; this.style = makeStyle(); this.children = []; this.parentNode = null; this.textContent = '';
    this.width = 300; this.height = 150; this.picture = null; }
  insertBefore(c, ref) { c.parentNode = this; const i = ref ? this.children.indexOf(ref) : -1;
    if (i < 0) this.children.push(c); else this.children.splice(i, 0, c); return c; }
  appendChild(c) { return this.insertBefore(c, null); }
  // A canvas: what was drawn is what it holds. An ended, stalled or empty
  // player draws black; a canvas drawn onto another copies its picture.
  getContext() { const el = this; return {
    drawImage(src) { if (src.drawThrows) throw new Error('tainted');
      if (src instanceof El) { el.picture = src.picture; return; }
      const what = src.black ? 'BLACK' : src.frameId; log.push('draw:' + what); el.picture = what; },
    getImageData(x, y, w, h) { const d = new Array(w * h * 4).fill(0);
      if (el.picture && el.picture !== 'BLACK') for (let i = 0; i < d.length; i += 4) { d[i] = d[i + 1] = d[i + 2] = 90; d[i + 3] = 255; }
      return { data: d }; } }; }
}
const doc = { hidden: false, createElement: (t) => new El(t) };
const nextSibling = { configurable: true, get() { const k = this.parentNode.children; return k[k.indexOf(this) + 1] || null; } };
Object.defineProperty(El.prototype, 'nextSibling', nextSibling);
class Track {
  constructor() { this.readyState = 'live'; this.muted = false; this.ls = {}; }
  addEventListener(t, fn) { (this.ls[t] = this.ls[t] || []).push(fn); }
  end() { this.readyState = 'ended'; (this.ls.ended || []).forEach((f) => f()); }
}
class Stream { constructor(name) { this.name = name; this.track = new Track(); } getVideoTracks() { return [this.track]; } toString() { return this.name; } }
class Media {
  constructor() { this.tag = 'video'; this._src = ''; this._so = null; this.readyState = 0; this.videoWidth = 0; this.videoHeight = 0;
    this.listeners = {}; this.frameCbs = []; this.ownerDocument = doc; this.frameId = 0; this.black = false;
    this.currentTime = 0; this.isConnected = true; this.paused = false; }
  addEventListener(t, fn, o) { (this.listeners[t] = this.listeners[t] || []).push({ fn, once: o && o.once }); }
  fire(t) { const ls = this.listeners[t] || []; this.listeners[t] = ls.filter((l) => !l.once); ls.forEach((l) => l.fn()); }
  requestVideoFrameCallback(cb) { this.frameCbs.push(cb); return this.frameCbs.length; }
  present() { const cbs = this.frameCbs; this.frameCbs = []; cbs.forEach((cb) => cb()); }
}
Object.defineProperty(Media.prototype, 'nextSibling', nextSibling);
Object.defineProperty(Media.prototype, 'src', { configurable: true, get() { return this._src; },
  set(v) { log.push('src=' + (v ? 'new' : "''")); this._src = v; this.readyState = 0; this.black = true; } });
Object.defineProperty(Media.prototype, 'srcObject', { configurable: true, get() { return this._so; },
  set(v) { log.push('srcObject=' + (v ? String(v) : 'null')); this._so = v; this.readyState = 0; this.black = true;
    if (this.dropOnSwap) this.frameCbs = []; } });
// A source playing picture `id`: currentTime moves, and (unless an iPhone on
// WebRTC is imitated) timeupdate ticks 4 times a second.
const play = (v, id, secs = 1, events = true) => { v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.frameId = id; v.black = false;
  for (let i = 0; i < secs * 4; i++) { advance(250); v.currentTime += 0.25; if (events) v.fire('timeupdate'); } };
// The new source decodes its first picture and plays for `ms`, a frame every 40 ms.
const arrive = (v, id, ms) => { v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.frameId = id; v.black = false;
  v.fire('loadeddata'); v.fire('playing');
  for (let t = 0; t < ms; t += 40) { advance(40); v.currentTime += 0.04; v.present(); } };
const shown = (v) => { const b = v.__cuboBridge; return [b.canvas.style.display, b.label.style.display, b.canvas.picture || null]; };
const make = () => { const host = new El('div'); const v = new Media(); host.appendChild(v); cuboaiBridgeGap(v); return { host, v }; };
// The card usually hooks in while the player is ALREADY playing WebRTC.
const hookedWhilePlaying = (opts) => {
  const host = new El('div'); const v = new Media(); host.appendChild(v);
  const rtc = new Stream('RTC'); v._so = rtc; v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.frameId = 'room'; v.black = false;
  if (!opts.rvfc) v.requestVideoFrameCallback = undefined;
  cuboaiBridgeGap(v);
  return { v, rtc };
};
"""


def _node(body: str, tmp_path) -> dict:
    script = tmp_path / "bridge.js"
    script.write_text(_PRELUDE + _helper() + body, encoding="utf-8")
    return json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout)


def test_wifi_to_5g_is_covered_from_webrtc_end_to_the_settled_mse_picture(tmp_path):
    """Kill: no bridge until the swap, the label for a blink, the hide at the
    new source's first decoded frame (no settle), or the picture kept after."""
    got = _node(
        """
const { host, v } = make();
const out = { order: host.children.map((c) => c.tag) };
const rtc = new Stream('RTC'); v.srcObject = rtc; play(v, 'room', 3);
rtc.track.end(); v.paused = true;          // pc.close(): the iPhone pauses on the last frame
out.atEnd = shown(v);
advance(700); out.label = shown(v);
advance(3000); v.srcObject = 'MSE'; v.paused = false; out.atSwap = shown(v);
v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.frameId = 'live'; v.black = false; v.fire('loadeddata');
out.firstFrame = shown(v);
arrive(v, 'live', 400); out.settled = shown(v);
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["order"] == ["video", "canvas", "div"]
    assert got["atEnd"] == ["block", "none", "room"], "the ended stream left the screen without the picture"
    assert got["label"] == ["block", "block", "room"]
    assert got["atSwap"] == ["block", "block", "room"]
    assert got["firstFrame"][0] == "block", "hidden at the first decoded frame, before it is on screen"
    assert got["settled"][:2] == ["none", "none"]


def test_a_slow_reconnect_is_held_until_the_new_source(tmp_path):
    """video-rtc may wait 15 s before it reconnects. Kill: the 8 s cap started
    at the WebRTC end (the reported 'image, black, image'), or no cap once the
    new source is attached."""
    got = _node(
        """
const { v } = make(); const rtc = new Stream('RTC'); v.srcObject = rtc; play(v, 'room', 2);
rtc.track.end(); v.paused = true;
advance(14000); const at14s = shown(v);
v.srcObject = 'MSE'; v.paused = false;
advance(7900); const beforeCap = shown(v);
advance(200); const afterCap = shown(v);
console.log(JSON.stringify({ at14s, beforeCap, afterCap }));
""",
        tmp_path,
    )
    assert got["at14s"] == ["block", "block", "room"]
    assert got["beforeCap"][0] == "block"
    assert got["afterCap"][:2] == ["none", "none"]


def test_a_reconnect_that_never_comes_is_let_go_for_good(tmp_path):
    """Kill: no wait cap (a frozen picture forever), or a let-go picture shown
    again at a later swap (an old picture passing for live)."""
    got = _node(
        """
const { v } = make(); const rtc = new Stream('RTC'); v.srcObject = rtc; play(v, 'room', 2);
rtc.track.end(); v.paused = true;
advance(25100); const letGo = shown(v);
v.srcObject = 'MSE'; advance(700); const later = shown(v);
console.log(JSON.stringify({ letGo, later }));
""",
        tmp_path,
    )
    assert got["letGo"][:2] == ["none", "none"]
    assert got["later"][:2] == ["none", "block"], "the let-go picture came back, or the gap went unexplained"


def test_back_to_wifi_waits_for_the_webrtc_picture_to_settle(tmp_path):
    """5G -> Wi-Fi: the stalled MSE stream is replaced by WebRTC, whose first
    decoded frames an iPhone reports before they are on screen. Kill: copying
    only at the swap (nothing drawable then), the WebRTC settle removed, or
    frame counting that never lets go when frame callbacks are silent."""
    got = _node(
        """
const { v } = make();
v.srcObject = 'MSE1'; play(v, 'room', 2);
v.readyState = 1; v.black = true;           // stalled: nothing drawable any more
advance(1500);
v.srcObject = new Stream('RTC'); const atSwap = shown(v);
arrive(v, 'rtc', 400); const at400 = shown(v);
arrive(v, 'rtc', 600); const at1000 = shown(v);
const w = make().v; w.requestVideoFrameCallback = undefined; w.srcObject = 'MSE1'; play(w, 'x', 2);
w.srcObject = new Stream('RTC'); w.readyState = 4; w.videoWidth = 1280; w.videoHeight = 720; w.black = false;
w.fire('loadeddata'); advance(1000); const silentFrames = shown(w);
console.log(JSON.stringify({ atSwap, at400, at1000, silentFrames }));
""",
        tmp_path,
    )
    assert got["atSwap"] == ["block", "none", "room"]
    assert got["at400"][0] == "block", "the WebRTC picture was assumed on screen too early"
    assert got["at1000"][:2] == ["none", "none"]
    assert got["silentFrames"][:2] == ["none", "none"], "without frame callbacks the bridge never let go"


def test_a_paused_new_source_is_not_taken_for_a_picture(tmp_path):
    """Kill: hiding while the new source is paused (nothing moving on screen)."""
    got = _node(
        """
const { v } = make(); v.srcObject = 'A'; play(v, 'room', 2);
v.srcObject = 'B'; v.paused = true; arrive(v, 'b', 1000); const paused = shown(v);
v.paused = false; arrive(v, 'b', 400); const playing = shown(v);
console.log(JSON.stringify({ paused, playing }));
""",
        tmp_path,
    )
    assert got["paused"][0] == "block"
    assert got["playing"][:2] == ["none", "none"]


def test_a_new_picture_arriving_quickly_never_shows_the_label(tmp_path):
    """Opening at home: MSE first, WebRTC takes over within the second. Kill:
    'Reconnecting…' flashing on every open (the WebRTC settle outlasts the
    label delay)."""
    got = _node(
        """
const { v } = make(); v.srcObject = 'MSE'; play(v, 'mse', 2);
const label = v.__cuboBridge.label; let everLabel = false;
v.srcObject = new Stream('RTC');
for (let t = 0; t < 1000; t += 40) { if (t === 40) { v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.black = false; v.fire('loadeddata'); v.fire('playing'); }
  advance(40); v.currentTime += 0.04; v.present(); if (label.style.display === 'block') everLabel = true; }
console.log(JSON.stringify({ after: shown(v), everLabel }));
""",
        tmp_path,
    )
    assert got["after"][:2] == ["none", "none"]
    assert got["everLabel"] is False


def test_a_stale_copy_is_never_shown(tmp_path):
    """Kill: a copy from minutes ago shown as the current picture."""
    got = _node(
        """
const { v } = make(); v.srcObject = 'A'; play(v, 'old', 2);
v.readyState = 1; advance(70000);                 // stalled for 70 s, nothing copied since
v.srcObject = 'B'; advance(700);
console.log(JSON.stringify({ at: shown(v) }));
""",
        tmp_path,
    )
    assert got["at"][:2] == ["none", "block"]


def test_an_empty_copy_never_replaces_a_good_one(tmp_path):
    """A player with nothing to show draws black. Kill: that black copied over
    the last good picture."""
    got = _node(
        """
const { v } = make(); v.srcObject = 'A'; play(v, 'room', 2);
v.black = true; for (let i = 0; i < 8; i++) { advance(250); v.currentTime += 0.25; v.fire('timeupdate'); }
console.log(JSON.stringify({ picture: v.__cuboBridge.canvas.picture, empty: v.__cuboBridge.empty }));
""",
        tmp_path,
    )
    assert got["picture"] == "room"
    assert got["empty"] >= 1


def test_the_copy_is_small_fresh_and_cheap(tmp_path):
    """Kill: full-resolution copies, copying on every timeupdate, copying a
    stream that stopped delivering, or copying while the page is hidden."""
    got = _node(
        """
const { v } = make();
const rtc = new Stream('RTC'); v.srcObject = rtc; play(v, 'a', 3);
const draws3s = log.filter((l) => l.startsWith('draw')).length;
const size = [v.__cuboBridge.canvas.width, v.__cuboBridge.canvas.height];
rtc.track.muted = true; play(v, 'frozen', 2);
const whileMuted = v.__cuboBridge.canvas.picture;
rtc.track.muted = false; doc.hidden = true; play(v, 'hidden', 3);
const whileHidden = v.__cuboBridge.canvas.picture;
console.log(JSON.stringify({ draws3s, size, whileMuted, whileHidden }));
""",
        tmp_path,
    )
    assert got["draws3s"] == 3, got["draws3s"]
    assert got["size"] == [960, 540]
    assert got["whileMuted"] == "a", "a stream that stopped delivering was copied"
    assert got["whileHidden"] == "a", "copied while the page was hidden"


def test_the_label_is_outside_the_zoom_layer(tmp_path):
    """Kill: the label placed inside the digital-zoom layer (zooming would move
    it off screen) or the canvas outside it (it would not match the video)."""
    got = _node(
        """
const player = new El('div'); const layer = player.appendChild(new El('div')); const v = new Media(); layer.appendChild(v);
cuboaiBridgeGap(v);
console.log(JSON.stringify({ canvasIn: v.__cuboBridge.canvas.parentNode === layer, labelIn: v.__cuboBridge.label.parentNode === player }));
""",
        tmp_path,
    )
    assert got == {"canvasIn": True, "labelIn": True}


def test_the_cards_own_recording_switch_is_not_bridged(tmp_path):
    """Kill: the frozen live picture labelled 'Reconnecting…' over a switch to
    a recording, or a quiet that never ends."""
    got = _node(
        """
const { v } = make(); v.srcObject = 'LIVE'; play(v, 'live', 2);
v.__cuboBridge.quiet(); v.src = ''; v.srcObject = null; advance(700); const during = shown(v);
v.srcObject = 'REC'; play(v, 'rec', 16);
v.srcObject = 'B'; advance(700); const later = shown(v);
console.log(JSON.stringify({ during, later }));
""",
        tmp_path,
    )
    assert got["during"][:2] == ["none", "none"]
    assert got["later"][0] == "block", "the quiet never ended"


def test_a_disconnect_then_a_new_source_is_one_bridge(tmp_path):
    """video-rtc clears the element (src '' and srcObject null) and connects
    again later. Kill: the picture dropped at the clear, the 8 s cap applied
    before the new source, or never let go."""
    got = _node(
        """
const { v } = make(); v.srcObject = 'LIVE'; play(v, 'live', 2);
v.src = ''; v.srcObject = null; const cleared = shown(v);
advance(9000); v.srcObject = 'MSE2'; const reconnected = shown(v);
arrive(v, 'mse', 400);
console.log(JSON.stringify({ cleared, reconnected, after: shown(v) }));
""",
        tmp_path,
    )
    assert got["cleared"][0] == "block" and got["cleared"][2] == "live"
    assert got["reconnected"][0] == "block", "the 8 s cap ran out while waiting for the new source"
    assert got["after"][:2] == ["none", "none"]


def test_nothing_to_hold_means_no_picture(tmp_path):
    """Kill: a canvas shown over the first load (nothing ever played), a
    drawing error breaking the swap, or a played-but-uncopyable gap left
    unexplained."""
    got = _node(
        """
const { v } = make(); v.srcObject = 'MSE'; advance(700); const first = shown(v);
const w = make().v; w.drawThrows = true; w.srcObject = 'OLD'; play(w, 'x', 2); w.srcObject = 'NEW';
advance(700);
console.log(JSON.stringify({ first, throwing: shown(w), src: String(w.srcObject) }));
""",
        tmp_path,
    )
    assert got["first"][:2] == ["none", "none"]
    assert got["throwing"][:2] == ["none", "block"] and got["src"] == "NEW"


def test_the_hook_is_per_element_and_once(tmp_path):
    """Kill: the accessors patched on the prototype (every media element on the
    page), or a second install adding a second canvas."""
    got = _node(
        """
const { host, v } = make(); cuboaiBridgeGap(v);
const other = new Media(); new El('div').appendChild(other); other.srcObject = 'A'; play(other, 'o', 2); other.srcObject = 'X';
console.log(JSON.stringify({ canvases: host.children.filter((c) => c.tag === 'canvas').length,
  otherDrew: log.some((l) => l === 'draw:o'), own: Object.prototype.hasOwnProperty.call(v, 'srcObject') }));
""",
        tmp_path,
    )
    assert got["canvases"] == 1
    assert got["otherDrew"] is False
    assert got["own"] is True


def test_the_timer_alone_copies_a_player_hooked_while_playing(tmp_path):
    """No frame callbacks, no timeupdate (an iPhone on WebRTC): only the 1 s
    timer can copy. Kill: the timer not started at install, or copying while
    currentTime stands."""
    got = _node(
        """
const { v, rtc } = hookedWhilePlaying({ rvfc: false });
for (let i = 0; i < 12; i++) { advance(250); v.currentTime += 0.25; }
const draws = log.filter((l) => l.startsWith('draw')).length;
advance(1500); const stopped = log.filter((l) => l.startsWith('draw')).length;
advance(3000); const paused = log.filter((l) => l.startsWith('draw')).length;
rtc.track.end();
console.log(JSON.stringify({ draws, stopped, paused, at: shown(v) }));
""",
        tmp_path,
    )
    assert got["draws"] >= 2
    assert got["stopped"] <= got["draws"] + 1
    assert got["paused"] == got["stopped"], "copied while nothing was playing"
    assert got["at"] == ["block", "none", "room"]


def test_frame_callbacks_alone_copy_a_player_hooked_while_playing(tmp_path):
    """Only presented frames (currentTime standing, no timeupdate). Kill: the
    frame loop not armed at install, or not re-armed after each frame."""
    got = _node(
        """
const { v, rtc } = hookedWhilePlaying({ rvfc: true });
for (let i = 0; i < 30; i++) { advance(50); v.present(); }
const draws = log.filter((l) => l.startsWith('draw')).length;
rtc.track.end();
console.log(JSON.stringify({ draws, at: shown(v) }));
""",
        tmp_path,
    )
    assert got["draws"] == 2
    assert got["at"] == ["block", "none", "room"]


def test_timeupdate_alone_copies_before_the_first_timer_tick(tmp_path):
    """WebRTC ends 0.6 s after the card hooked in: only timeupdate has run.
    Kill: the timeupdate trigger removed."""
    got = _node(
        """
const { v, rtc } = hookedWhilePlaying({ rvfc: false });
advance(250); v.currentTime += 0.25; v.fire('timeupdate');
advance(250); v.currentTime += 0.25; v.fire('timeupdate');
advance(100); rtc.track.end();
console.log(JSON.stringify({ at: shown(v) }));
""",
        tmp_path,
    )
    assert got["at"] == ["block", "none", "room"]


def test_a_detached_player_parks_its_timer_and_playback_restarts_it(tmp_path):
    """Kill: the timer kept forever for a player that left the page, or never
    restarted when the same player plays again without a source swap (HA puts
    the card back and the player just calls play())."""
    got = _node(
        """
const { v } = make(); v.srcObject = 'A'; play(v, 'a', 1);
v.isConnected = false; advance(61000); const parked = v.__cuboBridge.poll;
v.isConnected = true; v.fire('play'); const again = !!v.__cuboBridge.poll;
console.log(JSON.stringify({ parked, again }));
""",
        tmp_path,
    )
    assert got["parked"] == 0
    assert got["again"] is True


def test_the_diagnostic_hook_sees_the_switch_and_can_never_break_it(tmp_path):
    """Kill: no report at a swap, a WebRTC end or the hide, or a failing report
    breaking the swap."""
    got = _node(
        """
const { v } = make(); const seen = [];
v.__cuboBridge.onDiag = (i) => { seen.push(i.ev); throw new Error('report failed'); };
const rtc = new Stream('RTC'); v.srcObject = rtc; play(v, 'room', 2);
rtc.track.end(); v.srcObject = 'MSE'; arrive(v, 'mse', 400);
console.log(JSON.stringify({ seen, src: String(v.srcObject), after: shown(v) }));
""",
        tmp_path,
    )
    assert got["seen"] == ["swap", "ended", "swap", "hide"]
    assert got["src"] == "MSE" and got["after"][:2] == ["none", "none"]


def test_every_player_video_gets_the_bridge_and_recordings_quiet_it():
    """Kill: the install removed from the per-video init, or the card's own
    recording switch not quieting it."""
    src = _src()
    init = src[src.index('video.dataset.cuboInit = "true";') :][:2500]
    assert init.index("cuboaiSteerMsePlayback(video);") < init.index("cuboaiBridgeGap(video, this._liveDeviceId);")
    show = src[src.index("const showEntity = (entityId, muted) => {") :][:1200]
    assert show.index("bridged.__cuboBridge.quiet();") < show.index("this.content.setConfig(cfg);")


# =============================================================================
# Handover to a card Home Assistant rebuilt
# =============================================================================
#
# Recorded on iOS 26 (screen recording + card diagnostics): at a Wi-Fi -> 5G
# switch Home Assistant reconnects and rebuilds the YAML dashboard's cards -- a
# new card, a new empty <video>, black while its stream starts. The old card's
# picture lived inside the old card. It is now kept per camera for the page.


def test_a_rebuilt_card_starts_from_the_old_cards_picture(tmp_path):
    """Kill: no handover (the reported black), the label never saying why, or
    the handed-over picture kept after the new card's own is on screen."""
    got = _node(
        """
const old = new Media(); new El('div').appendChild(old); cuboaiBridgeGap(old, 'CAM1');
old.srcObject = 'MSE'; play(old, 'room', 3);
const v = new Media(); new El('div').appendChild(v); cuboaiBridgeGap(v, 'CAM1');
const at0 = shown(v); advance(700); const at700 = shown(v);
v.srcObject = 'MSE2'; arrive(v, 'live', 400);
console.log(JSON.stringify({ at0, at700, after: shown(v) }));
""",
        tmp_path,
    )
    assert got["at0"] == ["block", "none", "room"], "the rebuilt card started black"
    assert got["at700"] == ["block", "block", "room"]
    assert got["after"][:2] == ["none", "none"]


def test_no_handover_of_a_stale_or_foreign_picture(tmp_path):
    """Kill: another camera's picture handed over, a picture from minutes ago
    handed over, or a player that already plays covered by an old picture."""
    got = _node(
        """
const old = new Media(); new El('div').appendChild(old); cuboaiBridgeGap(old, 'CAM1'); old.srcObject = 'A'; play(old, 'room', 2);
const other = new Media(); new El('div').appendChild(other); cuboaiBridgeGap(other, 'CAM2');
const playing = new Media(); new El('div').appendChild(playing); playing.readyState = 4; playing.videoWidth = 1280; playing.videoHeight = 720;
cuboaiBridgeGap(playing, 'CAM1'); const playingAt = shown(playing);
old.readyState = 1; advance(70000);
const late = new Media(); new El('div').appendChild(late); cuboaiBridgeGap(late, 'CAM1'); advance(700);
console.log(JSON.stringify({ other: shown(other), playing: playingAt, late: shown(late) }));
""",
        tmp_path,
    )
    assert got["other"][0] == "none", "another camera's picture was handed over"
    assert got["playing"][0] == "none", "a playing player was covered by the old picture"
    assert got["late"][:2] == ["none", "none"], "a picture from minutes ago was handed over"


def test_the_swap_takes_the_freshest_good_picture(tmp_path):
    """Kill: the swap showing a copy up to a second old when the picture on
    screen was still good."""
    got = _node(
        """
const { v } = make(); v.srcObject = 'MSE'; play(v, 'old', 1);
v.frameId = 'newer'; advance(600);
v.srcObject = new Stream('RTC');
console.log(JSON.stringify({ at: shown(v) }));
""",
        tmp_path,
    )
    assert got["at"] == ["block", "none", "newer"]


def test_sound_before_picture_keeps_the_bridge(tmp_path):
    """Reported on iOS 26 (diagnostics): the new MSE stream plays as soon as its
    sound arrives, while its picture waits for the camera's next keyframe.
    Kill: taking 'playing' for a picture (the bridge dropped onto black)."""
    got = _node(
        """
const { v } = make(); const rtc = new Stream('RTC'); v.srcObject = rtc; play(v, 'room', 2);
rtc.track.end(); v.srcObject = 'MSE';
v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.black = true;   // playing, sound only
v.fire('loadeddata'); v.fire('playing');
for (let t = 0; t < 2000; t += 40) { advance(40); v.currentTime += 0.04; v.present(); }
const soundOnly = shown(v);
v.black = false; v.frameId = 'live'; for (let t = 0; t < 400; t += 40) { advance(40); v.present(); }
console.log(JSON.stringify({ soundOnly, picture: shown(v) }));
""",
        tmp_path,
    )
    assert got["soundOnly"][0] == "block", "the bridge dropped onto a black picture"
    assert got["picture"][:2] == ["none", "none"]


def test_where_frames_are_reported_the_bridge_waits_for_them(tmp_path):
    """Kill: hiding on readyState and time alone on a browser that reports
    presented frames (a decoded frame is not a shown one)."""
    got = _node(
        """
const { v } = make(); v.srcObject = 'A'; play(v, 'room', 2);
for (let i = 0; i < 3; i++) { advance(40); v.present(); }           // frame callbacks work here
v.srcObject = 'B'; v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.black = false; v.frameId = 'b';
v.fire('loadeddata'); v.fire('playing'); advance(1000); const noFrames = shown(v);
for (let i = 0; i < 3; i++) { advance(40); v.present(); }
console.log(JSON.stringify({ noFrames, frames: shown(v) }));
""",
        tmp_path,
    )
    assert got["noFrames"][0] == "block"
    assert got["frames"][:2] == ["none", "none"]


def test_a_frame_callback_dropped_at_the_swap_does_not_strand_the_bridge(tmp_path):
    """A browser may drop frame callbacks pending when the source changes.
    Kill: the hide's frame callback registered only once (at the WebRTC end)."""
    got = _node(
        """
const { v } = make(); v.dropOnSwap = true; const rtc = new Stream('RTC'); v.srcObject = rtc; play(v, 'room', 2);
for (let i = 0; i < 3; i++) { advance(40); v.present(); }
rtc.track.end(); advance(2000); v.srcObject = 'MSE';
arrive(v, 'live', 600);
console.log(JSON.stringify({ at: shown(v) }));
""",
        tmp_path,
    )
    assert got["at"][:2] == ["none", "none"], "the new source's frames were never counted"


def test_swaps_restart_the_frame_loop_without_stacking_it(tmp_path):
    """Kill: the copy loop left dead when a browser drops its callback at a
    swap (every later hide would guess on time alone), or a fresh loop per swap
    with the old ones still running (the copy work multiplying)."""
    got = _node(
        """
const { v } = make(); v.dropOnSwap = true; v.srcObject = 'A'; play(v, 'a', 1);
for (let i = 0; i < 3; i++) { advance(40); v.present(); }
const framesAlive = v.__cuboBridge.frames;
const w = make().v;
for (let k = 0; k < 5; k++) { w.srcObject = 'S' + k; w.readyState = 4; w.videoWidth = 1280; w.videoHeight = 720; w.black = false; w.frameId = 'w';
  for (let i = 0; i < 3; i++) { advance(40); w.present(); } }
const before = w.__cuboBridge.frames; w.present(); const perFrame = w.__cuboBridge.frames - before;
console.log(JSON.stringify({ framesAlive, perFrame }));
""",
        tmp_path,
    )
    assert got["framesAlive"] == 3, "the loop died with the dropped callback"
    assert got["perFrame"] == 1, "loops stacked up across swaps"


def test_a_new_stream_left_paused_is_started(tmp_path):
    """Recorded on iOS 26: after Wi-Fi -> 5G the new MSE stream had frames and
    stayed paused; the bridge waited for a playing picture and let go onto a
    frozen one. Kill: no play() for a paused new source with a picture, play()
    pressed before there is anything to play, or pressed on every poll."""
    got = _node(
        """
const { v } = make(); v.srcObject = 'A'; play(v, 'room', 2);
const calls = [];
v.play = () => { calls.push(NOW); return Promise.resolve().then(() => { v.paused = false; }); };
v.srcObject = 'MSE'; v.paused = true; v.fire('playing'); advance(500); const beforeData = calls.length;
v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.frameId = 'live'; v.black = false; v.fire('loadeddata');
v.play = () => { calls.push(NOW); return Promise.resolve(); };   // the first presses change nothing
advance(3000); const pressed = calls.length;
console.log(JSON.stringify({ beforeData, pressed }));
""",
        tmp_path,
    )
    assert got["beforeData"] == 0, "play pressed before there was a picture"
    assert 2 <= got["pressed"] <= 2, got["pressed"]


def test_a_refused_unmuted_start_plays_muted(tmp_path):
    """The phone refuses sound without a tap (NotAllowedError). Kill: the
    picture left paused, or muting on any other failure (an interrupted load
    is no refusal of sound)."""
    got = _node(
        """
const run = (err) => {
  const { v } = make(); v.srcObject = 'A'; play(v, 'room', 2);
  const tries = [];
  v.play = () => { tries.push(v.muted); if (!v.muted) { const e = new Error('no'); e.name = err; return Promise.reject(e); }
    v.paused = false; return Promise.resolve(); };
  v.muted = false; v.srcObject = 'MSE'; v.paused = true;
  v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.frameId = 'live'; v.black = false; v.fire('loadeddata');
  return new Promise((r) => setImmediate(() => { advance(150); setImmediate(() => r({ tries, muted: v.muted, paused: v.paused })); }));
};
(async () => {
  const refused = await run('NotAllowedError');
  const aborted = await run('AbortError');
  console.log(JSON.stringify({ refused, aborted }));
})();
""",
        tmp_path,
    )
    assert got["refused"]["tries"][:2] == [False, True] and got["refused"]["muted"] is True
    assert got["refused"]["paused"] is False
    assert got["aborted"]["muted"] is False, "muted on a failure that was no refusal of sound"
