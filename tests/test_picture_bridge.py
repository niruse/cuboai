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
  Object.defineProperty(st, 'setProperty', { value(k, v) { st[String(k).replace(/-([a-z])/g, (m, c) => c.toUpperCase())] = v; } });
  return st; };
class El {
  constructor(tag) { this.tag = tag; this.style = makeStyle(); this.children = []; this.parentNode = null; this.textContent = '';
    this.width = 300; this.height = 150; this.picture = null; this.naturalWidth = 0; this.naturalHeight = 0; this.ls = {}; }
  addEventListener(t, fn) { (this.ls[t] = this.ls[t] || []).push(fn); }
  fire(t) { (this.ls[t] || []).forEach((f) => f()); }
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
const doc = { hidden: false, created: [], createElement: (t) => { const el = new El(t); doc.created.push(el); return el; } };
// The camera's snapshot arriving: an <img> the card asked for finishes loading.
const imgs = () => doc.created.filter((e) => e.tag === 'img');
const loadImg = (img, w, h, picture = 'poster') => { img.naturalWidth = w; img.naturalHeight = h; img.picture = picture; img.fire('load'); };
// localStorage, with a switch for a browser that refuses it.
const LS = new Map(); let LS_THROWS = false;
const lsGuard = () => { if (LS_THROWS) throw new Error('storage denied'); };
Object.defineProperty(globalThis, 'localStorage', { configurable: true, writable: true, value: {
  getItem: (k) => { lsGuard(); return LS.has(k) ? LS.get(k) : null; },
  setItem: (k, v) => { lsGuard(); LS.set(k, String(v)); },
  removeItem: (k) => { lsGuard(); LS.delete(k); } } });
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
    this.currentTime = 0; this.isConnected = true; this.paused = false;
    this.style = makeStyle(); this.poster = ''; this.posterRatio = 0; }
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
// The layout rule of a browser, modelled: the height of the video's box at a
// given width. An explicit aspect-ratio decides; else the stream's own size;
// else the poster's while no source is attached; else 300x150.
const boxH = (v, width = 400) => { let ratio = 2;
  const m = /^([\d.]+) \/ ([\d.]+)$/.exec(String(v.style.aspectRatio || ''));
  if (m) ratio = Number(m[1]) / Number(m[2]);
  else if (v.videoWidth > 0 && v.videoHeight > 0) ratio = v.videoWidth / v.videoHeight;
  else if (!v._src && !v._so && v.posterRatio > 0) ratio = v.posterRatio;
  return Math.round((width / ratio) * 100) / 100; };
// The card as cuboaiDressVideo sees it: a player holding the video.
const card = (v, id, poster) => ({ content: { shadowRoot: null, querySelector: (s) => (s === 'video' ? v : null) },
  _liveDeviceId: id, _dvrPlaying: false, _cuboPosterUrl: poster });
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
    # utf-8: the label's text ("Reconnecting…") comes back through stdout.
    done = subprocess.run(["node", str(script)], capture_output=True, text=True, encoding="utf-8", check=True)
    return json.loads(done.stdout)


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
    recording switch not quieting it. Since the first-load fix also: the early
    install right after the player is put on the page removed (back to the
    0.5-1 s in which the video is uncovered and unsized), the early install
    marking the video as initialised (the tick's own init would then be
    skipped), the tick's fallback call removed or put behind the speaker-button
    gate, the box's shape passed through the player's `style` option (static,
    and the player evaluates it as a template), or a version bump."""
    src = _src()
    init = src[src.index('video.dataset.cuboInit = "true";') :][:2500]
    assert init.index("cuboaiSteerMsePlayback(video);") < init.index("cuboaiBridgeGap(video, this._liveDeviceId);")
    show = src[src.index("const showEntity = (entityId, muted) => {") :][:1200]
    assert show.index("bridged.__cuboBridge.quiet();") < show.index("this.content.setConfig(cfg);")
    early = src[src.index("this.appendChild(this.content);") :][:400]
    assert "cuboaiDressVideo(this)" in early
    assert "dataset.cuboInit" not in early
    tick = src[src.index("const volumeIcon = root.querySelector('.volume');") :]
    assert tick.index("cuboaiDressVideo(this);") < tick.index("if ((video || audio) && volumeIcon)")
    helper = _helper()
    assert "function cuboaiPinPictureBox(" in helper and "function cuboaiDressVideo(" in helper
    config = src[src.index("function cuboaiWebrtcConfig(") :]
    config = config[: config.index("\n}\n")]
    assert "style:" not in config and "mode: 'webrtc,mse'" in config
    assert "const CARD_VERSION = 'v2.6.67';" in src


# =============================================================================
# Handover to a card Home Assistant rebuilt
# =============================================================================
#
# Recorded on iOS 26 (screen recording + card diagnostics): at a Wi-Fi -> 5G
# switch Home Assistant reconnects and rebuilds the YAML dashboard's cards -- a
# new card, a new empty <video>, black while its stream starts. The old card's
# picture lived inside the old card. It is now kept per camera for the page.


def test_a_rebuilt_card_starts_from_the_old_cards_picture(tmp_path):
    """Changed on purpose with the first-load fix. This test used to demand
    'Reconnecting…' 700 ms after every handover; that label on a dashboard that
    was simply opened is the reported bug. Here the old card had been showing
    its picture for 3 s only, so the new card is opening: no label at 700 ms,
    'Connecting…' after 4 s. (A card rebuilt while the old one had already
    lost its stream, or after the old one had been live for a while, keeps
    'Reconnecting…': see the next two tests.)

    Kill: no handover (the reported black), 'Reconnecting…' on a card that
    never showed its own picture, a slow open never explained, or the
    handed-over picture kept after the new card's own is on screen."""
    got = _node(
        """
const old = new Media(); new El('div').appendChild(old); cuboaiBridgeGap(old, 'CAM1');
old.srcObject = 'MSE'; play(old, 'room', 3);
const v = new Media(); new El('div').appendChild(v); cuboaiBridgeGap(v, 'CAM1');
const at0 = shown(v); advance(700); const at700 = shown(v);
advance(3200); const at3900 = shown(v);
advance(200); const at4100 = shown(v); const text = v.__cuboBridge.label.textContent;
v.srcObject = 'MSE2'; arrive(v, 'live', 400);
console.log(JSON.stringify({ at0, at700, at3900, at4100, text, after: shown(v) }));
""",
        tmp_path,
    )
    assert got["at0"] == ["block", "none", "room"], "the rebuilt card started black"
    assert got["at700"] == ["block", "none", "room"], "an opening card was announced as a reconnect"
    assert got["at3900"] == ["block", "none", "room"]
    assert got["at4100"] == ["block", "block", "room"], "a slow open was never explained"
    assert got["text"] == "Connecting…"
    assert got["after"][:2] == ["none", "none"]


def test_a_card_rebuilt_during_a_reconnect_still_says_reconnecting(tmp_path):
    """The old card had already lost its stream and was holding its picture
    when Home Assistant rebuilt the cards (Wi-Fi -> 5G): the new card carries
    on the same reconnect. Kill: the continuity check removed (a real network
    switch losing today's label), or continuity granted to every handover (the
    second card: its old card was playing)."""
    got = _node(
        """
const old = new Media(); new El('div').appendChild(old); cuboaiBridgeGap(old, 'CAM1');
const rtc = new Stream('RTC'); old.srcObject = rtc; play(old, 'room', 3);
rtc.track.end();
const v = new Media(); new El('div').appendChild(v); cuboaiBridgeGap(v, 'CAM1');
const at0 = shown(v); advance(700); const at700 = shown(v); const text = v.__cuboBridge.label.textContent;
const old2 = new Media(); new El('div').appendChild(old2); cuboaiBridgeGap(old2, 'CAM2');
old2.srcObject = new Stream('RTC'); play(old2, 'hall', 3);
const w = new Media(); new El('div').appendChild(w); cuboaiBridgeGap(w, 'CAM2'); advance(700);
console.log(JSON.stringify({ at0, at700, text, playing: shown(w) }));
""",
        tmp_path,
    )
    assert got["at0"] == ["block", "none", "room"]
    assert got["at700"] == ["block", "block", "room"], "a card rebuilt mid-reconnect lost its label"
    assert got["text"] == "Reconnecting…"
    assert got["playing"] == ["block", "none", "hall"]


def test_a_card_rebuilt_at_a_network_change_still_says_reconnecting(tmp_path):
    """Wi-Fi -> mobile data on a YAML dashboard: Home Assistant can rebuild the
    cards before the old player has noticed that its WebRTC picture froze (no
    'ended', no new source yet). The old card had been live for a while and was
    on the page a moment ago, so the new card carries on a reconnect and says
    so after 0.6 s, as before the first-load fix. Kill: continuity only from an
    old card already in a gap (this path silent for 4 s, then 'Connecting…'),
    or continuity for a view navigated back to (the old card left the page
    seconds ago), for a page that woke a moment ago even though its old stream
    went on playing, or for an old card that had been live only briefly."""
    got = _node(
        """
let WALL = 1e9; Date.now = () => WALL;
const fresh = (id) => { const n = new Media(); new El('div').appendChild(n); cuboaiBridgeGap(n, id); return n; };
const secs = (m, id, n) => { for (let i = 0; i < n; i++) { play(m, id, 1); WALL += 1000; } };
const out = {};
// (a) live for 30 s, the picture freezes, the card is rebuilt 1.5 s later.
const a = fresh('CAM1'); a.srcObject = new Stream('RTC'); secs(a, 'room', 30);
WALL += 1500; advance(1500);
const a2 = fresh('CAM1'); out.at0 = shown(a2); advance(700); out.at700 = shown(a2); out.text = a2.__cuboBridge.label.textContent;
// (b) the view was left 5 s ago and is opened again.
const b = fresh('CAM2'); b.srcObject = new Stream('RTC'); secs(b, 'hall', 30);
b.isConnected = false; WALL += 5000; advance(5000);
const b2 = fresh('CAM2'); advance(700); out.navigated = shown(b2);
// (c) the page woke 6 s ago; the old stream survived and went on playing.
const c = fresh('CAM3'); c.srcObject = new Stream('RTC'); secs(c, 'yard', 30);
cuboaiPageWoke(); secs(c, 'yard', 6);
const c2 = fresh('CAM3'); advance(700); out.woke = shown(c2);
// ... and 12 s after the wake a rebuild is a reconnect again.
secs(c, 'yard', 6);
const c3 = fresh('CAM3'); advance(700); out.wokeLongAgo = shown(c3);
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["at0"] == ["block", "none", "room"]
    assert got["at700"] == ["block", "block", "room"], "a card rebuilt at a network change lost its label"
    assert got["text"] == "Reconnecting…"
    assert got["navigated"] == ["block", "none", "hall"], "a view opened again was announced as a reconnect"
    assert got["woke"] == ["block", "none", "yard"], "a card rebuilt right after the page woke was a reconnect"
    assert got["wokeLongAgo"] == ["block", "block", "yard"]


def test_no_handover_of_a_stale_or_foreign_picture(tmp_path):
    """Kill: another camera's picture handed over, a picture from minutes ago
    handed over, a player that already plays covered by an old picture, or
    (added with the first-load fix) freshness judged by the page's own clock
    alone: that clock may stand still while the app is in the background, and
    a picture from before the suspend would be handed over as fresh."""
    got = _node(
        """
let WALL = 1e9; Date.now = () => WALL;
const old = new Media(); new El('div').appendChild(old); cuboaiBridgeGap(old, 'CAM1'); old.srcObject = 'A'; play(old, 'room', 2);
WALL += 70000;                                    // the page's clock stood still for 70 s
const slept = new Media(); new El('div').appendChild(slept); cuboaiBridgeGap(slept, 'CAM1'); const sleptAt = shown(slept);
WALL -= 70000;
const fresh = new Media(); new El('div').appendChild(fresh); cuboaiBridgeGap(fresh, 'CAM1'); const freshAt = shown(fresh);
const other = new Media(); new El('div').appendChild(other); cuboaiBridgeGap(other, 'CAM2');
const playing = new Media(); new El('div').appendChild(playing); playing.readyState = 4; playing.videoWidth = 1280; playing.videoHeight = 720;
cuboaiBridgeGap(playing, 'CAM1'); const playingAt = shown(playing);
old.readyState = 1; advance(70000);
const late = new Media(); new El('div').appendChild(late); cuboaiBridgeGap(late, 'CAM1'); advance(700);
console.log(JSON.stringify({ other: shown(other), playing: playingAt, late: shown(late), slept: sleptAt, fresh: freshAt }));
""",
        tmp_path,
    )
    assert got["slept"][0] == "none", "a picture from before a suspend was handed over as fresh"
    assert got["fresh"][0] == "block"
    assert got["other"][0] == "none", "another camera's picture was handed over"
    assert got["playing"][0] == "none", "a playing player was covered by the old picture"
    assert got["late"][:2] == ["none", "none"], "a picture from minutes ago was handed over"


# =============================================================================
# Opening a card is not a reconnect
# =============================================================================
#
# Reported 2026-11 (iPhone screen recording of the app opening the dashboard):
# the snapshot at full width, one black frame, then a narrower and shorter
# picture with "Reconnecting…" over it, then the live picture at full size.
# The player gives its video no size (poster shape -> 300x150 -> stream shape),
# drops the poster when MSE attaches, and the card installed its cover only on
# its first 500 ms tick and announced every handover as a reconnect. `boxH`
# models the browser's layout rule; these tests prove the card's logic against
# that model, not WebKit itself.


def test_a_first_load_shows_one_stable_box_and_no_label(tmp_path):
    """The recording, replayed: poster, MSE without frames, WebRTC takes over.
    Kill: no pin (the box follows poster -> 300x150 -> stream), a pin applied
    only once frames arrive, the shape not remembered per camera, the bridge
    installed late (the first sample uncovered), a label on an open, the whole
    frame not fitted in the box, or a needless second snapshot request."""
    got = _node(
        """
const old = new Media(); new El('div').appendChild(old); cuboaiDressVideo(card(old, 'CAM1'));
old.srcObject = 'MSE'; play(old, 'room', 3); old.fire('resize');
const v = new Media(); v.posterRatio = 16 / 9; new El('div').appendChild(v);
const samples = []; const sample = () => samples.push({ h: boxH(v), s: shown(v) });
const dressed = cuboaiDressVideo(card(v, 'CAM1', '/api/p')); sample();
v.srcObject = 'MSE'; sample();
advance(700); sample();
v.srcObject = new Stream('RTC'); sample();
arrive(v, 'live', 1000); sample();
console.log(JSON.stringify({ dressed, samples, imgs: imgs().length, fit: v.style.objectFit, again: cuboaiDressVideo(card(v, 'CAM1', '/api/p')),
  canvases: v.parentNode.children.filter((c) => c.tag === 'canvas').length }));
""",
        tmp_path,
    )
    assert got["dressed"] is True and got["again"] is True and got["canvases"] == 1
    assert [s["h"] for s in got["samples"]] == [225] * 5, "the picture box changed size while the stream started"
    assert [s["s"][0] for s in got["samples"]] == ["block"] * 4 + ["none"]
    assert all(s["s"][2] == "room" for s in got["samples"][:4])
    assert all(s["s"][1] != "block" for s in got["samples"]), "a label on a card that was simply opened"
    assert got["fit"] == "contain"
    assert got["imgs"] == 0, "the snapshot was requested although nothing needed it"


def test_a_cold_start_is_covered_by_the_snapshot_without_a_label(tmp_path):
    """No card showed this camera before, nothing is remembered. Kill: no
    cold-start cover (the black frame when the player drops its poster), a
    guessed 16:9 box before anything is known, the snapshot stored or reused
    as a 'last picture', the cover counting as 'has played' (the revive fast
    path would change), a live picture covered by a late snapshot, or a black
    snapshot used as a cover."""
    got = _node(
        """
const v = new Media(); v.posterRatio = 4 / 3; new El('div').appendChild(v);
cuboaiDressVideo(card(v, 'CAM1', '/api/p'));
const out = { imgs: imgs().length, src: imgs()[0].src, before: shown(v), pinBefore: v.style.aspectRatio || null, hBefore: boxH(v) };
loadImg(imgs()[0], 1280, 960);
out.covered = shown(v); out.pin = v.style.aspectRatio; out.hAfter = boxH(v);
v.srcObject = 'MSE'; out.hMse = boxH(v); advance(700); out.at700 = shown(v);
advance(3400); out.at4100 = shown(v); out.text = v.__cuboBridge.label.textContent;
out.played = v.__cuboBridge.played; out.kept = cuboaiLastPictures.has('CAM1'); out.stored = LS.size;
arrive(v, 'live', 400); out.after = shown(v);
play(v, 'live', 2); v.srcObject = 'B'; out.laterGap = shown(v);
// The snapshot arrives after the video already shows its picture.
const w = new Media(); new El('div').appendChild(w); cuboaiDressVideo(card(w, 'CAM2', '/api/q'));
w.srcObject = 'MSE'; w.readyState = 4; w.videoWidth = 1280; w.videoHeight = 720; w.black = false; w.frameId = 'live';
loadImg(imgs()[1], 1280, 960); out.late = shown(w);
// A snapshot that is black is no cover.
const b = new Media(); new El('div').appendChild(b); cuboaiDressVideo(card(b, 'CAM3', '/api/r'));
loadImg(imgs()[2], 1280, 960, 'BLACK'); out.black = shown(b);
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["imgs"] == 1 and got["src"] == "/api/p"
    assert got["before"] == ["none", "none", None]
    assert got["pinBefore"] is None, "a shape was guessed before anything was known"
    assert got["covered"] == ["block", "none", "poster"], "a cold start was left uncovered"
    assert got["pin"] == "1280 / 960"
    assert got["hBefore"] == got["hAfter"] == got["hMse"] == 300
    assert got["at700"] == ["block", "none", "poster"]
    assert got["at4100"] == ["block", "block", "poster"] and got["text"] == "Connecting…"
    assert got["played"] is False, "the snapshot cover counted as playback"
    assert got["kept"] is False and got["stored"] == 0, (
        "the snapshot was kept as a last picture or as the camera's shape"
    )
    assert got["after"][:2] == ["none", "none"]
    assert got["laterGap"] == ["block", "none", "live"], "the snapshot came back as a last picture"
    assert got["late"][0] == "none", "a live picture was covered by a late snapshot"
    assert got["black"][0] == "none", "a black snapshot was used as a cover"


def test_a_late_snapshot_cover_lets_go_without_new_events(tmp_path):
    """The snapshot can finish loading after the video's 'loadeddata' and
    'playing' have fired, on a browser without frame callbacks: nothing would
    tell the cover that the picture came. Kill: the cover not looking by itself
    (held over a live picture until the 8 s cap)."""
    got = _node(
        """
const v = new Media(); v.requestVideoFrameCallback = undefined; new El('div').appendChild(v);
cuboaiDressVideo(card(v, 'CAM1', '/api/p'));
v.srcObject = 'MSE'; v.fire('loadeddata'); v.fire('playing');
loadImg(imgs()[0], 1280, 960); const covered = shown(v);
v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.black = false; v.frameId = 'live';
advance(600);
console.log(JSON.stringify({ covered, after: shown(v) }));
""",
        tmp_path,
    )
    assert got["covered"][0] == "block"
    assert got["after"][:2] == ["none", "none"], "the cover stayed over a live picture"


def test_sound_only_mse_does_not_make_a_card_live(tmp_path):
    """On an iPhone MSE plays its sound before its first picture, and already
    reports its size (recorded on iOS 26: readyState 4, 1280 wide, black). That
    marks the card as 'played' (the revive of a dead player depends on it, and
    must not change) but the card has shown nothing. Kill: the label decided
    by 'played', or 'live' taken from readyState and size without looking at
    the frame -- 'Reconnecting…' over black on a first load, or instead of
    'Connecting…' on a rebuilt card that is slow to open."""
    got = _node(
        """
const soundOnly = (m, ms) => { m.readyState = 4; m.videoWidth = 1280; m.videoHeight = 720; m.black = true;
  for (let t = 0; t < ms; t += 250) { advance(250); m.currentTime += 0.25; } };
const { v } = make(); v.srcObject = 'MSE'; soundOnly(v, 2500);
const played = v.__cuboBridge.played; const live = v.__cuboBridge.isLive();
v.src = ''; v.srcObject = null; v.srcObject = new Stream('RTC'); advance(700); const at700 = shown(v);
// A rebuilt card whose own MSE has sound only for more than 4 s.
const old = new Media(); new El('div').appendChild(old); cuboaiBridgeGap(old, 'CAM1'); old.srcObject = 'MSE'; play(old, 'room', 3);
const h = new Media(); new El('div').appendChild(h); cuboaiBridgeGap(h, 'CAM1');
h.srcObject = 'MSE'; soundOnly(h, 4250);
console.log(JSON.stringify({ played, live, at700, slow: shown(h), text: h.__cuboBridge.label.textContent }));
""",
        tmp_path,
    )
    assert got["played"] is True
    assert got["live"] is False, "sound with a known size was taken for a picture"
    assert got["at700"][1] != "block", "sound alone made the card a reconnecting one"
    assert got["slow"] == ["block", "block", "room"] and got["text"] == "Connecting…"


def test_a_snapshot_arriving_over_black_mse_still_covers(tmp_path):
    """Cold start on an iPhone: the snapshot finishes loading after MSE already
    plays its sound and reports its size, with a black picture. Kill: the
    cover refused because the video 'has dimensions' (the black frame left
    uncovered), or that cover labelled as a reconnect."""
    got = _node(
        """
const v = new Media(); new El('div').appendChild(v); cuboaiDressVideo(card(v, 'CAM1', '/api/p'));
v.srcObject = 'MSE'; v.readyState = 4; v.videoWidth = 1280; v.videoHeight = 720; v.black = true;
v.fire('loadeddata'); v.fire('playing');
loadImg(imgs()[0], 1280, 720); const covered = shown(v);
for (let t = 0; t < 1500; t += 250) { advance(250); v.currentTime += 0.25; }
const held = shown(v); const live = v.__cuboBridge.isLive();
v.black = false; v.frameId = 'live'; for (let t = 0; t < 400; t += 40) { advance(40); v.currentTime += 0.04; v.present(); }
console.log(JSON.stringify({ covered, held, live, after: shown(v) }));
""",
        tmp_path,
    )
    assert got["covered"] == ["block", "none", "poster"], "the black frame was left uncovered"
    assert got["held"] == ["block", "none", "poster"] and got["live"] is False
    assert got["after"][:2] == ["none", "none"]


def test_the_snapshot_cover_has_one_deadline(tmp_path):
    """A camera that is offline, or a codec the browser cannot show: the player
    re-dials every 15 s and attaches a new source each time, and no frame ever
    comes. Kill: every re-dial restarting the cover's cap (an old snapshot held
    under 'Connecting…' for ever), or the cover coming back at a later
    re-dial."""
    got = _node(
        """
const v = new Media(); new El('div').appendChild(v); cuboaiDressVideo(card(v, 'CAM1', '/api/p'));
loadImg(imgs()[0], 1280, 960);
const dial = () => { v.src = 'blob:x'; v.srcObject = null; };   // MSE as desktop Chrome and Android attach it
dial(); advance(15000); dial(); advance(9900); const at24900 = shown(v);
advance(200); const at25100 = shown(v);
advance(4900); dial(); advance(700); const later = shown(v);
console.log(JSON.stringify({ at24900, at25100, later }));
""",
        tmp_path,
    )
    assert got["at24900"][0] == "block"
    assert got["at25100"][:2] == ["none", "none"], "a re-dial extended the snapshot cover"
    assert got["later"][:2] == ["none", "none"]


def test_the_players_own_upgrade_is_not_a_reconnect(tmp_path):
    """First load, the other order: MSE shows its picture first, the card is
    live, then the player replaces MSE directly with the WebRTC stream, whose
    first frame takes more than 0.6 s. That is the player's upgrade, not a lost
    stream: the last picture is held without a label. Kill: 'Reconnecting…' on
    that swap, the upgrade never explained if it hangs, or the upgrade's
    silence leaking into real losses -- the player clearing the video, a
    stream ending or being cleared in the middle of an upgrade, or a source
    that had stalled or stopped delivering being replaced (5G -> Wi-Fi)."""
    got = _node(
        """
const out = {};
const v = new Media(); new El('div').appendChild(v); cuboaiDressVideo(card(v, 'CAM1', '/api/p'));
loadImg(imgs()[0], 1280, 720); v.srcObject = 'MSE'; arrive(v, 'mse', 600); out.mseShown = shown(v);
play(v, 'mse', 1);
v.srcObject = new Stream('RTC'); advance(700); out.up700 = shown(v);
advance(3400); out.up4100 = shown(v); out.upText = v.__cuboBridge.label.textContent;
arrive(v, 'live', 1000); out.upDone = shown(v);
play(v, 'live', 2); v.src = ''; v.srcObject = null; advance(700);
out.lost = shown(v); out.lostText = v.__cuboBridge.label.textContent;
// The new stream dies in the middle of the upgrade.
const w = make().v; w.srcObject = 'MSE'; play(w, 'm', 2);
const rtc = new Stream('RTC'); w.srcObject = rtc; advance(300); rtc.track.end(); advance(700);
out.ended = shown(w); out.endedText = w.__cuboBridge.label.textContent;
// The player clears the video in the middle of the upgrade.
const x = make().v; x.srcObject = 'MSE'; play(x, 'm', 2);
x.srcObject = new Stream('RTC'); advance(300); x.src = ''; x.srcObject = null; advance(700);
out.cleared = shown(x); out.clearedText = x.__cuboBridge.label.textContent;
// The old source was not working: stalled MSE, or a WebRTC track gone silent.
const y = make().v; y.srcObject = 'MSE'; play(y, 'm', 2); y.readyState = 1; advance(1500);
y.srcObject = new Stream('RTC'); advance(700); out.stalled = [shown(y), y.__cuboBridge.label.textContent];
const z = make().v; const dead = new Stream('RTC'); z.srcObject = dead; play(z, 'm', 2); dead.track.muted = true;
z.srcObject = 'MSE'; advance(700); out.silent = [shown(z), z.__cuboBridge.label.textContent];
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["mseShown"][:2] == ["none", "none"]
    assert got["up700"] == ["block", "none", "mse"], "the player's own upgrade was announced as a reconnect"
    assert got["up4100"] == ["block", "block", "mse"] and got["upText"] == "Connecting…"
    assert got["upDone"][:2] == ["none", "none"]
    assert got["lost"] == ["block", "block", "live"] and got["lostText"] == "Reconnecting…"
    assert got["ended"] == ["block", "block", "m"] and got["endedText"] == "Reconnecting…"
    assert got["cleared"] == ["block", "block", "m"] and got["clearedText"] == "Reconnecting…"
    assert got["stalled"] == [["block", "block", "m"], "Reconnecting…"]
    assert got["silent"] == [["block", "block", "m"], "Reconnecting…"]


def test_the_diagnostics_keep_the_stream_field(tmp_path):
    """`live` in a diagnostic record is the WebRTC track delivering, as it
    always was; whether the card's own picture has been shown is `shown`.
    Kill: the new field written under the old name (it silently replaced the
    track's state in every 'swap' and 'ended' record)."""
    got = _node(
        """
const { v, rtc } = hookedWhilePlaying({ rvfc: true }); const evs = [];
v.__cuboBridge.onDiag = (e) => evs.push(e);
v.srcObject = 'MSE';
const swap = evs.find((e) => e.ev === 'swap');
console.log(JSON.stringify({ live: swap.live, shown: swap.shown, keys: ['open', 'up', 'box'].every((k) => k in swap) }));
""",
        tmp_path,
    )
    assert got == {"live": True, "shown": False, "keys": True}


def test_a_real_reconnect_after_an_opening_still_says_so(tmp_path):
    """Kill: the opening state leaking into later gaps (a real Wi-Fi -> 5G
    switch left unlabelled), or the label's text left at 'Connecting…'."""
    got = _node(
        """
const old = new Media(); new El('div').appendChild(old); cuboaiBridgeGap(old, 'CAM1');
old.srcObject = 'MSE'; play(old, 'room', 3);
const v = new Media(); new El('div').appendChild(v); cuboaiBridgeGap(v, 'CAM1');
advance(4100); const slow = v.__cuboBridge.label.textContent; const openSlow = v.__cuboBridge.opening;
const rtc = new Stream('RTC'); v.srcObject = rtc; arrive(v, 'live', 1000); const live = shown(v); const openLive = v.__cuboBridge.opening;
play(v, 'live2', 2); rtc.track.end(); advance(700);
console.log(JSON.stringify({ slow, openSlow, live, openLive, at: shown(v), text: v.__cuboBridge.label.textContent }));
""",
        tmp_path,
    )
    assert got["slow"] == "Connecting…" and got["openSlow"] is True
    assert got["live"][:2] == ["none", "none"] and got["openLive"] is False
    assert got["at"] == ["block", "block", "live2"]
    assert got["text"] == "Reconnecting…"


def test_a_picture_seen_but_never_copied_still_makes_the_card_live(tmp_path):
    """A browser that refuses the copy (a tainted draw) never has a last
    picture, but the card did show its own: its stream ending is a reconnect.
    Kill: 'live' learned only from a successful copy, not from the moment the
    card's own picture replaced the cover."""
    got = _node(
        """
const old = new Media(); new El('div').appendChild(old); cuboaiBridgeGap(old, 'CAM1');
old.srcObject = 'MSE'; play(old, 'room', 3);
const v = new Media(); v.drawThrows = true; new El('div').appendChild(v); cuboaiBridgeGap(v, 'CAM1');
advance(4100);
const rtc = new Stream('RTC'); v.srcObject = rtc; arrive(v, 'live', 840); const shownLive = shown(v);
v.readyState = 1; rtc.track.end(); advance(700);
console.log(JSON.stringify({ shownLive, snaps: v.__cuboBridge.snaps, at: shown(v), text: v.__cuboBridge.label.textContent }));
""",
        tmp_path,
    )
    assert got["shownLive"][:2] == ["none", "none"] and got["snaps"] == 0
    assert got["at"][1] == "block" and got["text"] == "Reconnecting…"


def test_a_page_that_woke_treats_the_next_gap_as_an_opening(tmp_path):
    """The app comes back from the background with the same card: its stream
    is gone and it starts again, which is an opening. Kill: the resume of the
    same card announced as a reconnect, wake detection that needs a browser
    event (the timer standing still is the only sign in some web views), a
    frozen last frame copied at the swap taken as proof of a live picture, or
    a wake that silences real reconnects for good."""
    got = _node(
        """
let WALL = 1e9; Date.now = () => WALL;
const out = {};
// (a) the browser said the page is back.
const a = make().v; a.srcObject = 'A'; play(a, 'room', 3);
cuboaiPageWoke();
a.src = ''; a.srcObject = null; a.srcObject = 'MSE'; advance(700); out.woke = shown(a);
const rtcA = new Stream('RTC'); a.srcObject = rtcA; arrive(a, 'live', 1000); play(a, 'live', 2);
rtcA.track.end(); advance(700); out.wokeLater = shown(a); out.wokeText = a.__cuboBridge.label.textContent;
// (b) no event: only the clock jumped while the timers stood still.
const b = make().v; b.srcObject = 'B'; play(b, 'hall', 2); advance(1000);   // the picture stands
WALL += 30000; advance(1000);
b.src = ''; b.srcObject = null; b.srcObject = 'MSE'; advance(700); out.jump = shown(b);
// (c) a jump while the picture keeps playing heals at the next tick.
const c = make().v; const rtcC = new Stream('RTC'); c.srcObject = rtcC; play(c, 'yard', 2);
WALL += 30000; play(c, 'yard', 1, false);
rtcC.track.end(); advance(700); out.healed = shown(c); out.healedText = c.__cuboBridge.label.textContent;
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["woke"] == ["block", "none", "room"], "a resumed card was announced as a reconnect"
    assert got["wokeLater"] == ["block", "block", "live"] and got["wokeText"] == "Reconnecting…"
    assert got["jump"] == ["block", "none", "hall"], "a suspend without a browser event went unnoticed"
    assert got["healed"] == ["block", "block", "yard"] and got["healedText"] == "Reconnecting…"


def test_the_ratio_follows_the_camera_and_storage_is_not_trusted(tmp_path):
    """Kill: text from storage reaching the style unvalidated, a recording's
    shape remembered as the camera's, a storage failure breaking the picture,
    a nonsense size (2x2, zero) pinned, a card without a device id writing to
    storage, the snapshot's shape overriding the stream's, or the shape not
    surviving a page reload."""
    got = _node(
        """
const out = {}; const KEY = 'cuboai_aspect_CAM1';
const dress = (id, poster) => { const v = new Media(); new El('div').appendChild(v); const c = card(v, id, poster); cuboaiDressVideo(c); return { v, c }; };
const size = (v, w, h) => { v.videoWidth = w; v.videoHeight = h; v.fire('resize'); };
const one = dress('CAM1').v; size(one, 640, 480);
out.learned = one.style.aspectRatio; out.stored = LS.get(KEY);
cuboaiAspects.clear(); out.reloaded = dress('CAM1').v.style.aspectRatio;
out.junk = ['1}body{display:none', '0x0', '5x9000', '99999x16', '640 / 480', '1e3x1e3'].map((j, i) => { cuboaiAspects.clear();
  LS.set('cuboai_aspect_J' + i, j); return dress('J' + i).v.style.aspectRatio || null; });
for (let i = 0; i < 6; i++) LS.delete('cuboai_aspect_J' + i);
LS_THROWS = true; cuboaiAspects.clear();
try { const t = dress('CAMT').v; size(t, 640, 360); out.denied = [!!t.__cuboBridge, t.style.aspectRatio]; } catch (e) { out.denied = 'threw'; }
LS_THROWS = false;
const rec = dress('CAM1'); out.recStart = rec.v.style.aspectRatio; rec.c._dvrPlaying = true; size(rec.v, 1280, 720);
out.recording = [rec.v.style.aspectRatio, LS.get(KEY), cuboaiAspects.get('CAM1').w];
const anon = dress(undefined).v; size(anon, 2, 2); const tiny = anon.style.aspectRatio || null;
size(anon, 0, 720); const zero = anon.style.aspectRatio || null; size(anon, 800, 600);
out.anon = [tiny, zero, anon.style.aspectRatio, [...LS.keys()]];
const known = dress('CAM1').v; known.__cuboBox.fromPoster(1920, 1080); out.poster = [known.style.aspectRatio, cuboaiAspects.get('CAM1').from];
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["learned"] == "640 / 480" and got["stored"] == "640x480"
    assert got["reloaded"] == "640 / 480", "the shape was not kept across a page reload"
    assert got["junk"] == [None] * 6, "text from storage reached the style"
    assert got["denied"] == [True, "640 / 360"], "a storage failure broke the picture"
    assert got["recStart"] == "640 / 480"
    assert got["recording"] == ["1280 / 720", "640x480", 640], "a recording was remembered as the camera's shape"
    assert got["anon"] == [None, None, "800 / 600", ["cuboai_aspect_CAM1"]]
    assert got["poster"] == ["640 / 480", "video"], "the snapshot's shape replaced the stream's"


def test_a_camera_changed_in_the_editor_is_not_remembered_as_the_old_one(tmp_path):
    """The card editor re-points the same player and <video> at another camera.
    Kill: the new camera's shape written under the old camera's id (its next
    open pinned to the wrong shape), or the box no longer following the
    picture that is on screen."""
    got = _node(
        """
const v = new Media(); new El('div').appendChild(v); const c = card(v, 'CAM1', '/api/p'); cuboaiDressVideo(c);
v.videoWidth = 640; v.videoHeight = 480; v.fire('resize');
c._liveDeviceId = 'CAM2'; v.videoWidth = 1280; v.videoHeight = 720; v.fire('resize');
v.__cuboBox.fromPoster(800, 800);
console.log(JSON.stringify({ box: v.style.aspectRatio, stored: LS.get('cuboai_aspect_CAM1'), page: cuboaiAspects.get('CAM1'),
  keys: [...LS.keys()], other: cuboaiAspects.has('CAM2') }));
""",
        tmp_path,
    )
    assert got["box"] == "1280 / 720"
    assert got["stored"] == "640x480" and got["page"] == {"w": 640, "h": 480, "from": "video"}
    assert got["keys"] == ["cuboai_aspect_CAM1"] and got["other"] is False


def test_a_recording_switch_ends_and_blocks_the_opening_cover(tmp_path):
    """The user opens a recording while the card is still opening. Kill: the
    snapshot cover left over the recording, or started inside the quiet that
    the card's own switch asks for."""
    got = _node(
        """
const v = new Media(); new El('div').appendChild(v); cuboaiDressVideo(card(v, 'CAM1', '/api/p'));
loadImg(imgs()[0], 1280, 960); const covered = shown(v);
v.__cuboBridge.quiet(); const quieted = shown(v);
loadImg(imgs()[0], 1280, 960); const again = shown(v);
const d = new Media(); new El('div').appendChild(d); const c = card(d, 'CAM2', '/api/q'); c._dvrPlaying = true; cuboaiDressVideo(c);
console.log(JSON.stringify({ covered, quieted, again, imgsForRecording: imgs().length - 1 }));
""",
        tmp_path,
    )
    assert got["covered"][0] == "block"
    assert got["quieted"][:2] == ["none", "none"]
    assert got["again"][:2] == ["none", "none"], "the cover started inside the recording switch's quiet"
    assert got["imgsForRecording"] == 0, "a snapshot was requested for a card that is playing a recording"


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
