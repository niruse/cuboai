"""Two-way audio in the card: the microphone path, executed in Node.

The card captures the microphone, resamples it to 16 kHz 16-bit mono and
streams it over Home Assistant's own websocket to the `cuboai/talk` command:
binary messages of [handler id byte] + PCM, 640 samples (40 ms) each, and a
1-byte message at the end. The pure helpers are sliced out of the card; the
tap-to-stop flow runs the real card methods against fakes of the browser
(AudioContext, getUserMedia) and of the Home Assistant connection. Nothing
here opens a socket or plays a sound. Every test names the mutation it kills.
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


def _helpers() -> str:
    src = _src()
    return src[src.index("// ── cuboai mic helpers ──") : src.index("// ── end mic helpers ──")]


def _run(script: str, tmp_path) -> dict:
    path = tmp_path / "mic.js"
    path.write_text(script, encoding="utf-8")
    proc = subprocess.run(["node", str(path), str(CARD)], capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


def _helper_run(body: str, tmp_path) -> dict:
    return _run(_helpers() + "\n" + body, tmp_path)


# =============================================================================
# The pure helpers
# =============================================================================


def test_the_resampler_is_continuous_across_pieces_and_accurate(tmp_path):
    """A 440 Hz tone at every rate a phone or browser uses, fed in pieces of
    random size. Kill: the position or the previous sample reset per piece
    (clicks at every piece boundary), or the wrong step (pitch shift)."""
    got = _helper_run(
        r"""
let seed = 7; const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
const out = {};
for (const inRate of [48000, 44100, 24000, 22050, 16000, 8000]) {
  const f = 440, N = inRate * 2;
  const x = new Float32Array(N);
  for (let i = 0; i < N; i++) x[i] = 0.5 * Math.sin(2 * Math.PI * f * i / inRate);
  const rs = cuboaiMicResampler(inRate, CUBOAI_MIC_RATE); const parts = []; let o = 0;
  while (o < N) { const n = Math.min(N - o, 1 + Math.floor(rnd() * 3000)); parts.push(rs(x.subarray(o, o + n))); o += n; }
  const y = [].concat(...parts.map((p) => Array.from(p)));
  const one = Array.from(cuboaiMicResampler(inRate, CUBOAI_MIC_RATE)(x));
  let maxErr = 0, diff = 0;
  for (let k = 0; k < y.length; k++) maxErr = Math.max(maxErr, Math.abs(y[k] / 32768 - 0.5 * Math.sin(2 * Math.PI * f * k / 16000)));
  for (let k = 0; k < y.length; k++) diff = Math.max(diff, Math.abs(y[k] - one[k]));
  out[inRate] = { len: y.length, one: one.length, maxErr, diff, pieces: parts.length };
}
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    # Linear interpolation's own bound for a 440 Hz tone: tiny from 22 kHz up,
    # ~0.0075 from an 8 kHz source.
    limits = {"48000": 5e-4, "44100": 1e-3, "24000": 2e-3, "22050": 2e-3, "16000": 5e-4, "8000": 1e-2}
    for rate, r in got.items():
        assert r["pieces"] > 10, rate
        assert abs(r["len"] - 32000) <= 2, (rate, r)
        assert r["len"] == r["one"], (rate, r)
        assert r["diff"] <= 1, f"{rate}: piece boundaries changed the output ({r['diff']} LSB)"
        assert r["maxErr"] < limits[rate], (rate, r)


def test_the_resampler_clamps_instead_of_wrapping(tmp_path):
    """A hot microphone (AGC overshoot) must saturate. Kill: the clamp
    removed -- Int16Array wraps 1.5 * 32767 round to a large NEGATIVE value, a
    full-scale crack in the nursery."""
    got = _helper_run(
        r"""
const rs = cuboaiMicResampler(16000, 16000);
const empty = rs(new Float32Array(0)).length;
const y = Array.from(rs(new Float32Array([2, -2, 1.5, -1.5, 1, -1, 0.5, 0])));
console.log(JSON.stringify({ empty, y }));
""",
        tmp_path,
    )
    assert got["empty"] == 0
    assert got["y"][:6] == [32767, -32768, 32767, -32768, 32767, -32768]
    assert got["y"][6] == 16383


def test_the_chunker_emits_exact_frames_and_keeps_the_rest(tmp_path):
    """Kill: the remainder dropped (a gap every message), frames padded, or
    one buffer reused for every frame (all frames alias the last one)."""
    got = _helper_run(
        r"""
let seed = 3; const rnd = () => (seed = (seed * 1103515245 + 12345) % 2147483648) / 2147483648;
const ch = cuboaiMicChunker(CUBOAI_MIC_CHUNK);
const total = 10007, x = new Int16Array(total).map((_, i) => i % 30000);
const frames = []; let o = 0;
while (o < total) { const n = Math.min(total - o, Math.floor(rnd() * 1500)); frames.push(...ch(x.subarray(o, o + n))); o += n; }
const flat = [].concat(...frames.map((f) => Array.from(f)));
let same = true; for (let i = 0; i < flat.length; i++) if (flat[i] !== x[i]) { same = false; break; }
const rest = ch(new Int16Array(CUBOAI_MIC_CHUNK - (total % CUBOAI_MIC_CHUNK)).fill(-7));
console.log(JSON.stringify({
  n: frames.length, sizes: [...new Set(frames.map((f) => f.length))], same,
  distinct: new Set(frames.map((f) => f.buffer)).size,
  rest: rest.length, restHead: rest.length ? rest[0][0] : null, restTail: rest.length ? rest[0][CUBOAI_MIC_CHUNK - 1] : null,
}));
""",
        tmp_path,
    )
    assert got["n"] == 10007 // 640
    assert got["sizes"] == [640]
    assert got["same"], "frames are not the input, in order"
    assert got["distinct"] == got["n"], "frames share a buffer"
    # The 10007 % 640 samples held back come out first in the next frame.
    assert got["rest"] == 1
    assert got["restHead"] == (10007 // 640) * 640
    assert got["restTail"] == -7


def test_a_frame_is_the_handler_id_then_little_endian_pcm(tmp_path):
    """HA routes a binary message by its first byte. Kill: the id byte
    missing or misplaced, a subarray's offset ignored (the wrong samples
    sent), or ids outside 1..255 let through."""
    got = _helper_run(
        r"""
const big = new Int16Array(700).map((_, i) => i - 350);
const pcm = big.subarray(10, 10 + CUBOAI_MIC_CHUNK);
const f = cuboaiMicFrame(7, pcm);
const dv = new DataView(f.buffer, 1);
const back = []; for (let i = 0; i < CUBOAI_MIC_CHUNK; i++) back.push(dv.getInt16(i * 2, true));
const throws = (fn) => { try { fn(); return false; } catch (e) { return e instanceof RangeError; } };
console.log(JSON.stringify({
  len: f.length, id: f[0], ok: back.every((v, i) => v === pcm[i]), first: back[0],
  bad: [0, 256, -1, 1.5, NaN, undefined, '7'].map((id) => throws(() => cuboaiMicFrame(id, pcm))),
  edge: [1, 255].map((id) => cuboaiMicFrame(id, pcm)[0]),
}));
""",
        tmp_path,
    )
    assert got["len"] == 1 + 2 * 640 == 1281
    assert got["id"] == 7
    assert got["ok"] and got["first"] == -340
    assert all(got["bad"]), got["bad"]
    assert got["edge"] == [1, 255]


def test_the_end_frame_is_exactly_one_byte(tmp_path):
    """A 0-byte binary message makes Home Assistant drop the WHOLE websocket.
    Kill: an empty end frame, or a padded one (HA would treat it as audio)."""
    got = _helper_run(
        r"""
const throws = (fn) => { try { fn(); return false; } catch (e) { return e instanceof RangeError; } };
const e = cuboaiMicEndFrame(9);
console.log(JSON.stringify({ len: e.length, bytes: Array.from(e), zero: throws(() => cuboaiMicEndFrame(0)),
  high: throws(() => cuboaiMicEndFrame(256)) }));
""",
        tmp_path,
    )
    assert got == {"len": 1, "bytes": [9], "zero": True, "high": True}


def test_the_send_gate(tmp_path):
    """Kill: sending into a socket that is not open, or queueing past ~1 s on a
    slow uplink (the voice then arrives ever later)."""
    got = _helper_run(
        r"""
const G = cuboaiMicSendGate, M = CUBOAI_MIC_MAX_BUFFERED;
console.log(JSON.stringify([
  G(null), G(undefined), G({ readyState: 0, bufferedAmount: 0 }), G({ readyState: 2, bufferedAmount: 0 }),
  G({ readyState: 3, bufferedAmount: 0 }), G({ readyState: 1, bufferedAmount: 0 }),
  G({ readyState: 1, bufferedAmount: M }), G({ readyState: 1, bufferedAmount: M + 1 }), M,
]));
""",
        tmp_path,
    )
    assert got[:8] == ["closed", "closed", "closed", "closed", "closed", "ok", "ok", "backlog"]
    assert got[8] == 32768


def test_the_button_state_machine(tmp_path):
    """Kill: a tap while connecting that does not cancel, a late `live` that
    lights up a cancelled talk, a tap while stopping that starts another, or
    a stopped event that skips idle."""
    got = _helper_run(
        r"""
const N = cuboaiMicNextState, out = {};
for (const s of ['idle', 'connecting', 'live', 'stopping', 'bogus'])
  for (const e of ['tap', 'live', 'stop', 'stopped', 'status'])
    out[s + '+' + e] = N(s, e);
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    want = {
        "idle": {"tap": "connecting", "live": "idle", "stop": "idle", "stopped": "idle", "status": "idle"},
        "connecting": {
            "tap": "stopping",
            "live": "live",
            "stop": "stopping",
            "stopped": "connecting",
            "status": "connecting",
        },
        "live": {"tap": "stopping", "live": "live", "stop": "stopping", "stopped": "live", "status": "live"},
        "stopping": {
            "tap": "stopping",
            "live": "stopping",
            "stop": "stopping",
            "stopped": "idle",
            "status": "stopping",
        },
        "bogus": dict.fromkeys(("tap", "live", "stop", "stopped", "status"), "idle"),
    }
    for state, row in want.items():
        for ev, nxt in row.items():
            assert got[f"{state}+{ev}"] == nxt, (state, ev)


def test_error_texts(tmp_path):
    """Real DOMExceptions (they carry a numeric legacy `code`, which must not
    shadow the name), HA's {code, message}, and bare reason strings. Kill: a
    mapping removed, the numeric-code shadowing, or a notice for the user's
    own stop."""
    got = _helper_run(
        r"""
const T = cuboaiMicErrorText, D = (name) => new DOMException('x', name);
console.log(JSON.stringify({
  denied: T(D('NotAllowedError')), security: T(D('SecurityError')),
  none: T(D('NotFoundError')), over: T(D('OverconstrainedError')),
  busyMic: T(D('NotReadableError')), abort: T(D('AbortError')),
  domCode: D('NotFoundError').code,
  speaker: T({ code: 'speaker_busy', message: 'server words' }), talker: T('busy'),
  unknownCode: T({ code: 'something_new', message: 'from the server' }), unknownReason: T('weird_reason'),
  plain: T(new Error('boom')), clientEnd: T('client_end'), removed: T('card_removed'),
  insecure: T('insecure', 'http://192.168.1.5:8123'), insecureNoWhere: T('insecure'),
  codes: ['not_found', 'unauthorized', 'speaker_busy', 'busy', 'spawn_failed', 'camera_unreachable',
          'camera_refused', 'camera_lost', 'camera_timeout', 'internal', 'idle', 'max_duration',
          'timeout', 'unknown_command'].map((c) => [c, T(c)]),
}));
""",
        tmp_path,
    )
    assert got["domCode"] == 8, "the premise: a DOMException has a numeric code"
    assert "not allowed" in got["denied"] and "Settings" in got["denied"]
    assert got["security"] == got["denied"]
    assert got["none"] == got["over"] == "No microphone found."
    assert "busy" in got["busyMic"].lower() and got["abort"] == got["busyMic"]
    assert "music" in got["speaker"].lower() and "server words" not in got["speaker"]
    assert "already talking" in got["talker"]
    assert got["unknownCode"] == "Two-way audio failed: from the server"
    assert "weird_reason" in got["unknownReason"]
    assert got["plain"] == "Two-way audio failed: boom"
    assert got["clientEnd"] == "" and got["removed"] == ""
    assert "https://" in got["insecure"] and "http://192.168.1.5:8123" in got["insecure"]
    assert "HTTPS" in got["insecureNoWhere"]
    texts = dict(got["codes"])
    assert all(texts.values()), texts
    assert len(set(texts.values())) == len(texts), "two codes share a text"


def test_can_capture_only_in_a_secure_context(tmp_path):
    """Kill: the secure-context test dropped (the card would call a
    getUserMedia that does not exist on a plain http:// address)."""
    got = _helper_run(
        r"""
const gum = { getUserMedia() {} };
console.log(JSON.stringify([
  cuboaiMicCanCapture({ isSecureContext: false, navigator: { mediaDevices: gum } }),
  cuboaiMicCanCapture({ isSecureContext: true, navigator: {} }),
  cuboaiMicCanCapture({ isSecureContext: true, navigator: { mediaDevices: {} } }),
  cuboaiMicCanCapture({ isSecureContext: true, navigator: { mediaDevices: gum } }),
  cuboaiMicCanCapture(null),
]));
""",
        tmp_path,
    )
    assert got == [False, False, False, True, False]


def test_the_worklet_batches_1024_frames_and_transfers_them(tmp_path):
    """Runs the worklet source against a fake AudioWorkletGlobalScope. Kill:
    one message per 128-frame quantum (8x the traffic), a copy instead of a
    transfer, samples out of order, or 'stop' ignored."""
    got = _helper_run(
        r"""
const posts = [];
globalThis.AudioWorkletProcessor = class {
  constructor() { this.port = { onmessage: null, postMessage: (m, t) => posts.push({ m: Array.from(m), moved: !!(t && t[0] === m.buffer) }) }; }
};
let Proc = null, name = null;
globalThis.registerProcessor = (n, c) => { name = n; Proc = c; };
new Function(CUBOAI_MIC_WORKLET_SRC)();
const p = new Proc(); const ret = [];
for (let q = 0; q < 17; q++) ret.push(p.process([[new Float32Array(128).map((_, i) => q * 128 + i)]]));
const empty = p.process([[]]);
p.port.onmessage({ data: 'stop' });
const after = p.process([[new Float32Array(128)]]);
for (let q = 0; q < 8; q++) p.process([[new Float32Array(128)]]);
console.log(JSON.stringify({ name, n: posts.length, sizes: posts.map((x) => x.m.length), moved: posts.every((x) => x.moved),
  inOrder: posts.every((x, j) => x.m.every((v, i) => v === j * 1024 + i)), ret: ret.every(Boolean), empty, after }));
""",
        tmp_path,
    )
    assert got["name"] == "cuboai-mic-tap"
    assert got["n"] == 2 and got["sizes"] == [1024, 1024]
    assert got["moved"], "the batch was copied, not transferred"
    assert got["inOrder"]
    assert got["ret"] and got["empty"] is True
    assert got["after"] is False, "'stop' did not end processing"


# =============================================================================
# The flow: the real card methods against a fake browser and a fake HA
# =============================================================================

_PRELUDE = r"""
const fs = require('fs');
const src = fs.readFileSync(process.argv[2], 'utf8');

// A clock the test drives: nothing waits for real time.
let NOW = 0, tid = 1; const timers = new Map();
globalThis.setTimeout = (fn, ms) => { const id = tid++; timers.set(id, { fn, at: NOW + (Number(ms) || 0) }); return id; };
globalThis.clearTimeout = (id) => { timers.delete(id); };
globalThis.setInterval = () => 0; globalThis.clearInterval = () => {};
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise((r) => setImmediate(r)); };
const advance = async (ms) => {
  const end = NOW + ms;
  for (;;) {
    await flush();
    let next = null;
    for (const [id, t] of timers) if (t.at <= end && (!next || t.at < next[1].at)) next = [id, t];
    if (!next) break;
    timers.delete(next[0]); NOW = next[1].at; next[1].fn();
  }
  NOW = end; await flush();
};

const log = [];
class El {
  constructor(tag) { this.tag = tag; this.style = {}; this.attrs = {}; this.children = []; this.parentNode = null; }
  appendChild(c) { c.parentNode = this; this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  animate(frames) { return { frames, cancel() {} }; }
  addEventListener() {}
}
const docListeners = {};
globalThis.document = {
  visibilityState: 'visible', createElement: (t) => new El(t),
  addEventListener: (n, f) => { (docListeners[n] = docListeners[n] || []).push(f); },
  removeEventListener: (n, f) => { docListeners[n] = (docListeners[n] || []).filter((x) => x !== f); },
};
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => undefined, define: () => {}, whenDefined: () => new Promise(() => {}) };
globalThis.window = globalThis;
// Where the card keeps the remembered mute ('remember' mode): observable, so a
// talk that writes a mute setting is caught.
const STORE = new Map();
globalThis.localStorage = {
  getItem: (k) => (STORE.has(k) ? STORE.get(k) : null),
  setItem: (k, v) => { STORE.set(k, String(v)); },
  removeItem: (k) => { STORE.delete(k); },
};
globalThis.isSecureContext = true;
globalThis.location = { origin: 'https://ha.example' };

// Audio: a context, its nodes, a worklet node and a script processor.
const ctxs = [], worklets = [], scripts = [];
let CTX_RATE = 48000, HAS_WORKLET = true, WORKLET_FAILS = false;
class FakeNode { constructor(kind) { this.kind = kind; this.out = []; this.cut = 0; }
  connect(n) { this.out.push(n); return n; } disconnect() { this.cut++; this.out = []; } }
class FakeCtx {
  constructor(...args) {
    log.push('ctx:new'); this.args = args; this.sampleRate = CTX_RATE; this.state = 'suspended';
    this.destination = new FakeNode('destination'); this.resumes = 0; ctxs.push(this);
    this.audioWorklet = HAS_WORKLET ? { addModule: async (url) => {
      log.push('worklet:addModule'); this.moduleUrl = url;
      if (WORKLET_FAILS) throw new DOMException('refused', 'AbortError');
    } } : undefined;
  }
  resume() { log.push('ctx:resume'); this.resumes++; if (this.state !== 'closed') this.state = 'running'; return Promise.resolve(); }
  close() { log.push('ctx:close'); this.state = 'closed'; return Promise.resolve(); }
  createMediaStreamSource(s) { const n = new FakeNode('source'); n.stream = s; this.source = n; return n; }
  createBiquadFilter() { const n = new FakeNode('biquad'); n.frequency = { value: 0 }; n.Q = { value: 0 }; return n; }
  createGain() { const n = new FakeNode('gain'); n.gain = { value: 1 }; return n; }
  createScriptProcessor(...a) { const n = new FakeNode('script'); n.args = a; scripts.push(n); return n; }
}
globalThis.AudioContext = FakeCtx;
globalThis.AudioWorkletNode = class extends FakeNode {
  constructor(ctx, name, opts) { super('worklet'); this.name = name; this.opts = opts; this.posted = [];
    const self = this; this.port = { onmessage: null, postMessage: (m) => self.posted.push(m) }; worklets.push(this); }
};
URL.createObjectURL = () => { log.push('blob:create'); return 'blob:cuboai-test'; };
URL.revokeObjectURL = () => { log.push('blob:revoke'); };

// The microphone. GUM holds scripted outcomes, in call order.
let GUM = [], gumGate = null; const gumCalls = [], tracks = [];
const makeStream = () => {
  const t = { readyState: 'live', onended: null, stop() { if (this.readyState !== 'ended') log.push('track:stop'); this.readyState = 'ended'; } };
  tracks.push(t); return { getTracks: () => [t], getAudioTracks: () => [t] };
};
Object.defineProperty(globalThis, 'navigator', { configurable: true, writable: true, value: {
  vendor: 'Google Inc.', userAgent: 'node',
  mediaDevices: { getUserMedia: async (c) => {
    gumCalls.push(c); log.push('gum'); if (gumGate) await gumGate;
    const r = GUM.length ? GUM.shift() : 'ok'; if (r !== 'ok') throw r; return makeStream();
  } },
} });

// Home Assistant's connection: subscribeMessage as home-assistant-js-websocket
// has it (resolves with an async unsubscribe), and a socket that records.
class FakeSocket { constructor() { this.readyState = 1; this.bufferedAmount = 0; this.sent = []; }
  send(b) { this.sent.push(Array.from(b)); log.push(b.length === 1 ? 'send:end' : 'send:frame'); } }
const conn = {
  socket: new FakeSocket(), listeners: {}, subs: [],
  addEventListener(n, f) { (this.listeners[n] = this.listeners[n] || []).push(f); },
  removeEventListener(n, f) { this.listeners[n] = (this.listeners[n] || []).filter((x) => x !== f); },
  fire(n) { (this.listeners[n] || []).slice().forEach((f) => f()); },
  subscribeMessage(cb, msg, opts) {
    log.push('subscribe'); const s = { cb, msg, opts, unsubs: 0 }; this.subs.push(s);
    return new Promise((resolve, reject) => {
      s.accept = () => resolve(async () => { s.unsubs++; log.push('unsub'); });
      s.refuse = (e) => reject(e);
    });
  },
};

new Function(src + ';globalThis.__Card = CuboAICameraCard; globalThis.__TEXT = CUBOAI_MIC_TEXT;')();

const video = { muted: false, paused: false, plays: 0, play() { this.plays++; this.paused = false; return Promise.resolve(); } };
const volume = { icon: 'mdi:volume-high' };
const makeCard = (config = { device_id: 'DEV1' }) => {
  const card = new __Card();
  card._config = config;
  card._hass = { connection: conn, states: { 'media_player.zuzu_speaker': { attributes: { device_id: 'DEV1' } } } };
  card._speakerEntityId = 'media_player.zuzu_speaker';
  card._liveDeviceId = 'DEV1';   // the live picture was built for this camera
  card.isConnected = true;
  card.isMuted = false;
  card._gestureArms = 0; card._armGestureUnmute = () => { card._gestureArms++; };
  const player = new El('div');
  card.micButton = player.appendChild(new El('ha-icon-button'));
  card.content = { shadowRoot: null, querySelector: (s) => (s === 'video' ? video : s === '.volume' ? volume : null) };
  card._cuboMicPaint();
  return card;
};
const notice = (card) => (card._noticeEl && card._noticeEl.style.display !== 'none' ? card._noticeEl.textContent : null);
const sub = () => conn.subs[conn.subs.length - 1];
let fedAt = 0;
const tone = (n, rate) => { const a = new Float32Array(n); for (let i = 0; i < n; i++) a[i] = 0.5 * Math.sin(2 * Math.PI * 440 * (fedAt + i) / rate); fedAt += n; return a; };
// Push `secs` of a tone through whichever tap the card built.
const feed = (secs) => {
  const ctx = ctxs[ctxs.length - 1], w = worklets[worklets.length - 1], sp = scripts[scripts.length - 1];
  const total = Math.round(ctx.sampleRate * secs);
  for (let done = 0; done < total; done += 1024) {
    const chunk = tone(Math.min(1024, total - done), ctx.sampleRate);
    if (w && w.port.onmessage) w.port.onmessage({ data: chunk });
    else if (sp && sp.onaudioprocess) {
      const outBuf = new Float32Array(chunk.length).fill(9);
      sp.onaudioprocess({ inputBuffer: { getChannelData: () => chunk }, outputBuffer: { getChannelData: () => outBuf } });
      sp.lastOut = outBuf;
    }
  }
};
const frames = (sock = conn.socket) => sock.sent.filter((b) => b.length > 1);
const ends = (sock = conn.socket) => sock.sent.filter((b) => b.length === 1);
const chain = (from) => { const kinds = []; let n = from; while (n && kinds.length < 10) { kinds.push(n.kind); n = n.out[0]; } return kinds; };
// A flow that never settles would exit silently with an empty event loop.
let settled = false;
process.on('exit', () => { if (!settled) { console.error('the flow never finished (awaiting a fake timer?)'); process.exitCode = 1; } });
const run = (fn) => fn().then((out) => { settled = true; console.log(JSON.stringify(out)); },
  (e) => { settled = true; console.error(e && e.stack || e); process.exit(1); });
"""


def _flow(body: str, tmp_path) -> dict:
    return _run(_PRELUDE + "\nrun(async () => {\n" + body + "\n});\n", tmp_path)


def test_a_talk_from_tap_to_stop(tmp_path):
    """The whole happy path at 48 kHz with the AudioWorklet. Kill: the context
    made after an await (iOS never starts it), the talk opened before the
    microphone was captured, frames sent before `started`, a cached socket,
    the backlog drop removed, the end frame not last, capture left running
    after the stop, the unsubscribe skipped, or the speaker touched by the
    talk."""
    got = _flow(
        r"""
const card = makeCard(); const o = {};
const p = card._cuboStartMic();
o.sync = log.slice(); o.ctxArgs = ctxs[0].args.length; o.stateTap = card._micState;
o.btnConnecting = card.micButton.style.backgroundColor;
await flush();
o.order = log.slice();
o.msg = sub().msg; o.opts = sub().opts;
o.gum = gumCalls[0];
o.chain = chain(ctxs[0].source); o.gain = ctxs[0].source.out[0].out[0].out[0].out[0].gain.value;
const lp = ctxs[0].source.out[0]; o.lp = [lp.type, lp.frequency.value, lp.Q.value];
o.muted = [video.muted, card.isMuted, volume.icon];
feed(1); o.beforeStarted = frames().length;
sub().accept(); await flush(); await p;
sub().cb({ type: 'started', handler_id: 7, dry_run: false, max_secs: 120 });
feed(1); const f = frames(); o.frames = f.length; o.sizes = [...new Set(f.map((b) => b.length))]; o.ids = [...new Set(f.map((b) => b[0]))];
const pcm = []; for (const b of f) for (let i = 1; i < b.length; i += 2) pcm.push((b[i] | (b[i + 1] << 8)) << 16 >> 16);
o.peak = Math.max(...pcm.map(Math.abs));
sub().cb({ type: 'live' }); o.stateLive = card._micState; o.noticeLive = notice(card);
o.btnLive = card.micButton.style.backgroundColor;
conn.socket.bufferedAmount = 40000; const n0 = frames().length; feed(0.4);
o.backlogSent = frames().length - n0; conn.socket.bufferedAmount = 0;
const first = conn.socket; conn.socket = new FakeSocket(); feed(0.4);
o.freshSocket = frames(conn.socket).length; o.oldSocketAfter = frames(first).length - frames(first).length;
const tapHandler = worklets[0].port.onmessage;
card._cuboStopMic('client_end');
o.stateStop = card._micState; o.disabled = card.micButton.disabled;
o.track = tracks[0].readyState; o.ctxState = ctxs[0].state; o.portCut = worklets[0].port.onmessage === null;
o.portStop = worklets[0].posted.includes('stop'); o.nodesCut = ctxs[0].source.cut > 0;
o.lastSent = conn.socket.sent[conn.socket.sent.length - 1];
o.restored = [video.muted, card.isMuted, volume.icon];
const nAfter = conn.socket.sent.length; tapHandler({ data: tone(4096, 48000) }); o.sentAfterStop = conn.socket.sent.length - nAfter;
await advance(1000); o.unsubBeforeEnded = sub().unsubs; o.stateWaiting = card._micState;
sub().cb({ type: 'ended', reason: 'client_end', sent: 50, speech: 40, camera: true });
await flush();
o.unsubs = sub().unsubs; o.stateEnd = card._micState; o.noticeEnd = notice(card);
o.listeners = [(conn.listeners.disconnected || []).length, (docListeners.visibilitychange || []).length];
o.btnIdle = card.micButton.style.backgroundColor; o.disabledIdle = card.micButton.disabled;
await advance(200000); o.lateUnsubs = sub().unsubs; o.lateState = card._micState; o.lateNotice = notice(card);
o.subscribes = conn.subs.length;
return o;
""",
        tmp_path,
    )
    # In the tap itself, before any await: the context, resumed, no rate forced.
    assert got["sync"][:2] == ["ctx:new", "ctx:resume"], got["sync"]
    assert got["ctxArgs"] == 0, "a sampleRate was forced on the AudioContext"
    assert got["stateTap"] == "connecting" and got["btnConnecting"].startswith("rgba(255, 160")
    order = got["order"]
    assert order.index("gum") < order.index("worklet:addModule") < order.index("subscribe")
    assert order.index("blob:create") < order.index("worklet:addModule") < order.index("blob:revoke")
    assert got["msg"] == {"type": "cuboai/talk", "device_id": "DEV1", "sample_rate": 16000, "dry_run": False}
    assert got["opts"] == {"resubscribe": False}
    assert got["gum"] == {
        "audio": {"channelCount": 1, "echoCancellation": True, "noiseSuppression": True, "autoGainControl": True}
    }
    assert got["chain"] == ["source", "biquad", "biquad", "worklet", "gain", "destination"]
    assert got["gain"] == 0, "the microphone would play back on the phone"
    assert got["lp"] == ["lowpass", 7200, 0.707]
    assert got["muted"] == [False, False, "mdi:volume-high"], "talking changed the speaker"
    assert got["beforeStarted"] == 0, "audio sent before the server gave a handler id"
    # 1 s at 48 kHz in 1024-frame batches is 48128 samples -> 16042 at 16 kHz.
    assert got["frames"] == 16042 // 640 == 25
    assert got["sizes"] == [1281] and got["ids"] == [7]
    assert 15500 < got["peak"] <= 16384, got["peak"]
    assert got["stateLive"] == "live" and got["btnLive"].startswith("rgba(220, 53")
    assert got["noticeLive"].startswith("Live — speak now")
    assert got["backlogSent"] == 0, "frames queued behind a 1 s backlog"
    assert got["freshSocket"] >= 9, "frames went to a cached socket, not the current one"
    assert got["stateStop"] == "stopping" and got["disabled"] is True
    assert got["track"] == "ended" and got["ctxState"] == "closed"
    assert got["portCut"] and got["portStop"] and got["nodesCut"]
    assert got["lastSent"] == [7], "the end frame is not the last thing sent"
    assert got["restored"] == [False, False, "mdi:volume-high"]
    assert got["sentAfterStop"] == 0
    assert got["unsubBeforeEnded"] == 0 and got["stateWaiting"] == "stopping"
    assert got["unsubs"] == 1 and got["stateEnd"] == "idle"
    assert got["noticeEnd"] is None, "the user's own stop showed a notice"
    assert got["listeners"] == [0, 0], "stop paths still armed after the talk"
    assert got["btnIdle"] == "rgba(0, 0, 0, 0.5)" and got["disabledIdle"] is False
    assert got["lateUnsubs"] == 1 and got["lateState"] == "idle" and got["lateNotice"] is None
    assert got["subscribes"] == 1


def test_without_ended_the_stop_still_unsubscribes_after_4s(tmp_path):
    """Kill: waiting forever for `ended` (the button stuck disabled), or not
    waiting at all (the server's tail cut)."""
    got = _flow(
        r"""
const card = makeCard(); card._cuboStartMic(); await flush();
sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 3, dry_run: false, max_secs: 120 });
card._cuboStopMic('client_end');
await advance(3900); const early = [sub().unsubs, card._micState];
await advance(200); return { early, late: [sub().unsubs, card._micState], ends: ends() };
""",
        tmp_path,
    )
    assert got["early"] == [0, "stopping"]
    assert got["late"] == [1, "idle"]
    assert got["ends"] == [[3]]


def test_a_refused_microphone_never_reaches_the_camera(tmp_path):
    """The talk (and the camera's SPEAKERSTART click) starts only after the
    microphone is captured. Kill: subscribing first, the context left open
    after a denial, or the video muted for a talk that never happened."""
    got = _flow(
        r"""
const card = makeCard(); GUM = [new DOMException('no', 'NotAllowedError')];
await card._cuboStartMic(); await flush();
return { subs: conn.subs.length, notice: notice(card), ctx: ctxs[0].state, state: card._micState,
  video: video.muted, gum: gumCalls.length };
""",
        tmp_path,
    )
    assert got["subs"] == 0
    assert "not allowed" in got["notice"]
    assert got["ctx"] == "closed" and got["state"] == "idle"
    assert got["video"] is False
    assert got["gum"] == 1, "a denial was retried"


def test_constraints_a_device_cannot_meet_fall_back_to_any_microphone(tmp_path):
    """Kill: no retry on OverconstrainedError/TypeError, or a retry on a
    real refusal (NotReadableError: the mic is busy)."""
    got = _flow(
        r"""
const out = {};
for (const err of ['OverconstrainedError', 'TypeError', 'NotReadableError']) {
  const card = makeCard(); gumCalls.length = 0; conn.subs.length = 0;
  GUM = [err === 'TypeError' ? new TypeError('bad constraint') : new DOMException('x', err), 'ok'];
  card._cuboStartMic(); await flush();
  out[err] = { calls: gumCalls.map((c) => JSON.stringify(c)), subs: conn.subs.length, notice: notice(card) };
  if (conn.subs.length) { sub().refuse({ code: 'busy', message: '' }); await flush(); }
  GUM = [];
}
return out;
""",
        tmp_path,
    )
    for err in ("OverconstrainedError", "TypeError"):
        assert got[err]["calls"][1] == '{"audio":true}', got[err]
        assert got[err]["subs"] == 1
    assert len(got["NotReadableError"]["calls"]) == 1 and got["NotReadableError"]["subs"] == 0
    assert "busy" in got["NotReadableError"]["notice"].lower()


def test_without_a_worklet_a_script_processor_taps(tmp_path):
    """Kill: the ScriptProcessor fallback removed (no audio on a browser
    without AudioWorklet), its output not silenced, or a failed addModule not
    falling back (and its Blob URL left alive)."""
    got = _flow(
        r"""
const out = {};
for (const mode of ['missing', 'refused']) {
  HAS_WORKLET = mode !== 'missing'; WORKLET_FAILS = mode === 'refused';
  const card = makeCard(); conn.socket = new FakeSocket();
  card._cuboStartMic(); await flush();
  const ctx = ctxs[ctxs.length - 1], sp = scripts[scripts.length - 1];
  sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 5, dry_run: false, max_secs: 120 });
  feed(1);
  out[mode] = { args: sp.args, chain: chain(ctx.source), frames: frames().length,
    silent: sp.lastOut.every((v) => v === 0), revoked: log.filter((x) => x === 'blob:revoke').length,
    created: log.filter((x) => x === 'blob:create').length };
  card._cuboStopMic('client_end'); sub().cb({ type: 'ended', reason: 'client_end' }); await flush();
  out[mode].handlerCut = sp.onaudioprocess === null;
}
return out;
""",
        tmp_path,
    )
    for mode in ("missing", "refused"):
        r = got[mode]
        assert r["args"] == [2048, 1, 1], mode
        assert r["chain"] == ["source", "biquad", "biquad", "script", "gain", "destination"], mode
        assert r["frames"] == 25, mode
        assert r["silent"], mode
        assert r["handlerCut"], mode
        assert r["revoked"] == r["created"], mode
    assert got["missing"]["created"] == 0 and got["refused"]["created"] == 1


def test_a_16khz_context_needs_no_filter(tmp_path):
    """Kill: the low-pass gate inverted (a 16 kHz context filtered, or a 48 kHz
    one aliasing)."""
    got = _flow(
        r"""
CTX_RATE = 16000; const card = makeCard(); card._cuboStartMic(); await flush();
sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 2, dry_run: false, max_secs: 120 });
feed(1); return { chain: chain(ctxs[0].source), frames: frames().length };
""",
        tmp_path,
    )
    assert got["chain"] == ["source", "worklet", "gain", "destination"]
    # At 1:1 the resampler holds the newest sample back for the next piece.
    assert got["frames"] == (16000 - 1) // 640 == 24


def test_cancel_while_the_permission_prompt_is_open(tmp_path):
    """Tap, then tap again before answering the prompt. Kill: the late stream
    kept (the phone goes on recording), a capture graph built for a talk
    already cancelled, or a talk opened anyway."""
    got = _flow(
        r"""
let open; gumGate = new Promise((r) => { open = r; });
const card = makeCard(); card._cuboStartMic(); await flush();
card._cuboStopMic('client_end'); await flush();
const mid = card._micState;
gumGate = null; open(); await flush();
return { mid, track: tracks[0] && tracks[0].readyState, subs: conn.subs.length, ctx: ctxs[0].state,
  state: card._micState, notice: notice(card), video: video.muted,
  built: log.filter((x) => x === 'blob:create' || x === 'worklet:addModule').length + worklets.length + scripts.length };
""",
        tmp_path,
    )
    assert got["mid"] == "idle"
    assert got["track"] == "ended"
    assert got["subs"] == 0
    assert got["ctx"] == "closed" and got["state"] == "idle"
    assert got["notice"] is None and got["video"] is False
    assert got["built"] == 0, "a capture graph was built for a cancelled talk"


def test_home_assistant_refusals_are_explained(tmp_path):
    """Every refusal code the command sends. Kill: a refusal leaving the
    microphone open, the video muted, or no notice."""
    got = _flow(
        r"""
const out = {};
for (const code of ['not_found', 'unauthorized', 'speaker_busy', 'busy', 'spawn_failed', 'unknown_command']) {
  const card = makeCard(); card._cuboStartMic(); await flush();
  sub().refuse({ code, message: 'server text' }); await flush();
  const t = tracks[tracks.length - 1], c = ctxs[ctxs.length - 1];
  out[code] = { notice: notice(card), want: __TEXT[code], track: t.readyState, ctx: c.state, state: card._micState,
    video: video.muted, unsubs: sub().unsubs };
}
return out;
""",
        tmp_path,
    )
    for code, r in got.items():
        assert r["notice"] == r["want"] and r["want"], code
        assert (r["track"], r["ctx"], r["state"]) == ("ended", "closed", "idle"), code
        assert r["video"] is False and r["unsubs"] == 0, code


def test_no_answer_within_10s_gives_up_and_releases_a_late_talk(tmp_path):
    """Kill: no subscribe timeout (connecting forever), or a talk accepted
    after the timeout left running with nobody holding it."""
    got = _flow(
        r"""
const card = makeCard(); card._cuboStartMic(); await flush();
await advance(9900); const before = card._micState;
await advance(200); const after = card._micState; const said = notice(card);
sub().accept(); await flush();
return { before, after, said, unsubs: sub().unsubs, track: tracks[0].readyState };
""",
        tmp_path,
    )
    assert got["before"] == "connecting"
    assert got["after"] == "idle" and "in time" in got["said"]
    assert got["unsubs"] == 1, "the late talk was not released"
    assert got["track"] == "ended"


def test_every_stop_path(tmp_path):
    """Each way a talk ends without the button. Kill: any stop path unarmed;
    an end frame sent into a dropped connection (whose handler ids are dead);
    the 4 s wait on a dropped connection; an unsubscribe after it (the
    frontend's command ids restart on reconnect: another card's); or
    'suspended' treated as fatal."""
    got = _flow(
        r"""
const out = {};
const cases = {
  disconnected: () => conn.fire('disconnected'),
  hidden: () => { document.visibilityState = 'hidden'; (docListeners.visibilitychange || []).slice().forEach((f) => f()); document.visibilityState = 'visible'; },
  track_ended: () => tracks[tracks.length - 1].onended(),
  interrupted: () => { const c = ctxs[ctxs.length - 1]; c.state = 'interrupted'; c.onstatechange(); },
  card_removed: async (card) => { card.isConnected = false; card.disconnectedCallback(); await advance(0); },
};
for (const [name, trigger] of Object.entries(cases)) {
  conn.socket = new FakeSocket();
  const card = makeCard(); card._cuboStartMic(); await flush();
  sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 4, dry_run: false, max_secs: 120 });
  sub().cb({ type: 'live' });
  // A context that merely suspends is resumed, not stopped.
  const c = ctxs[ctxs.length - 1]; const r0 = c.resumes; c.state = 'suspended'; c.onstatechange();
  const survived = card._micState === 'live' && c.resumes === r0 + 1;
  const deferred = trigger(card);   // only card_removed waits (for its deferred check)
  if (deferred) await deferred;
  const now = card._micState; await flush();
  out[name] = { survived, now, after: card._micState, ends: ends().length, unsubs: sub().unsubs,
    notice: notice(card), track: tracks[tracks.length - 1].readyState, ctx: c.state };
  if (card._micState !== 'idle') { sub().cb({ type: 'ended', reason: 'client_end' }); await flush(); }
  out[name].final = card._micState;
}
return out;
""",
        tmp_path,
    )
    for name, r in got.items():
        assert r["survived"], name
        assert r["now"] == "stopping", name
        assert r["track"] == "ended" and r["ctx"] == "closed", name
        assert r["final"] == "idle", name
    assert got["disconnected"]["ends"] == 0, "an end frame was sent into a dropped connection"
    assert got["disconnected"]["after"] == "idle", "waited for an `ended` that cannot arrive"
    assert got["disconnected"]["unsubs"] == 0, "unsubscribed a command the frontend has dropped"
    assert "connection" in got["disconnected"]["notice"]
    for name in ("hidden", "track_ended", "interrupted", "card_removed"):
        assert got[name]["ends"] == 1, name
    assert "background" in got["hidden"]["notice"]
    assert "microphone" in got["track_ended"]["notice"]
    assert "interrupted" in got["interrupted"]["notice"]


def test_timers_end_a_talk_the_camera_never_starts_or_that_runs_too_long(tmp_path):
    """Kill: the 40 s no-live stop, the 125 s backup, or the stop a second
    before the server's own cap (else frames hit a dropped handler)."""
    got = _flow(
        r"""
const out = {};
const start = async (maxSecs) => {
  conn.socket = new FakeSocket(); const card = makeCard(); card._cuboStartMic(); await flush();
  sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 6, dry_run: false, max_secs: maxSecs });
  return card;
};
let card = await start(120);
await advance(39900); out.at399 = card._micState;
await advance(200); out.at401 = card._micState; out.noLive = notice(card); out.noLiveEnds = ends().length;
await advance(5000);
card = await start(120); sub().cb({ type: 'live' });
await advance(118900); out.at1189 = card._micState;
await advance(200); out.at1191 = card._micState; out.capNotice = notice(card);
await advance(5000);
card = await start(undefined); sub().cb({ type: 'live' });
await advance(124900); out.at1249 = card._micState;
await advance(200); out.at1251 = card._micState;
return out;
""",
        tmp_path,
    )
    assert got["at399"] == "connecting"
    assert got["at401"] == "stopping" and "in time" in got["noLive"] and got["noLiveEnds"] == 1
    assert got["at1189"] == "live"
    assert got["at1191"] == "stopping" and "time limit" in got["capNotice"]
    assert got["at1249"] == "live"
    assert got["at1251"] == "stopping"


def test_a_talk_the_server_ends(tmp_path):
    """`ended` while live (the server's idle timer), and `error` from the
    camera. Kill: an end frame to a talk the server already closed, `error`
    not stopping the capture, or its notice overwritten by the `ended` that
    follows."""
    got = _flow(
        r"""
const out = {};
conn.socket = new FakeSocket();
let card = makeCard(); card._cuboStartMic(); await flush();
sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 8, dry_run: false, max_secs: 120 }); sub().cb({ type: 'live' });
sub().cb({ type: 'ended', reason: 'idle', sent: 1, speech: 1, camera: true }); await flush();
out.ended = { state: card._micState, ends: ends().length, unsubs: sub().unsubs, notice: notice(card), track: tracks[0].readyState };
await advance(9000);
conn.socket = new FakeSocket();
card = makeCard(); card._cuboStartMic(); await flush();
sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 8, dry_run: false, max_secs: 120 });
sub().cb({ type: 'error', code: 'camera_refused', message: 'fixed server text' });
out.error = { now: card._micState, ends: ends().length, track: tracks[1].readyState };
await advance(1000); out.error.waiting = [card._micState, sub().unsubs];
sub().cb({ type: 'ended', reason: 'camera_lost' }); await flush();
out.error.after = [card._micState, sub().unsubs, notice(card)];
return out;
""",
        tmp_path,
    )
    assert got["ended"]["state"] == "idle" and got["ended"]["ends"] == 0 and got["ended"]["unsubs"] == 1
    assert "no audio arrived" in got["ended"]["notice"] and got["ended"]["track"] == "ended"
    err = got["error"]
    assert err["now"] == "stopping" and err["ends"] == 0 and err["track"] == "ended"
    assert err["waiting"] == ["stopping", 0]
    assert err["after"][:2] == ["idle", 1]
    assert "didn't accept" in err["after"][2]


def test_test_mode_asks_for_a_dry_run_and_says_so(tmp_path):
    """Kill: the flag not sent, the TEST MODE notice missing, or audio sent to
    a talk the server did not confirm as a dry run."""
    got = _flow(
        r"""
const out = {};
window.__cuboaiMicDryRun = true;
let card = makeCard(); card._cuboStartMic();
out.connecting = notice(card); await flush();
out.flag = sub().msg.dry_run;
sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 9, dry_run: true, max_secs: 120 });
feed(0.2); sub().cb({ type: 'live' }); out.live = notice(card); out.frames = frames().length;
sub().cb({ type: 'status', sent: 5, speech: 4, rx: 100, dropped: 0 }); out.status = notice(card);
card._cuboStopMic('client_end'); sub().cb({ type: 'ended', reason: 'client_end' }); await flush();
window.__cuboaiMicDryRun = undefined;
conn.socket = new FakeSocket();
card = makeCard({ device_id: 'DEV1', talk_dry_run: true }); card._cuboStartMic(); await flush();
out.yamlFlag = sub().msg.dry_run;
sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 9, dry_run: false, max_secs: 120 });
feed(0.5); await flush();
out.refused = { frames: frames().length, ends: ends().length, unsubs: sub().unsubs, notice: notice(card), state: card._micState };
conn.socket = new FakeSocket();
card = makeCard(); card._cuboStartMic(); await flush();
out.realFlag = sub().msg.dry_run;
return out;
""",
        tmp_path,
    )
    banner = "TEST MODE — nothing plays at the camera"
    assert got["flag"] is True and got["yamlFlag"] is True and got["realFlag"] is False
    assert got["connecting"].startswith(banner)
    assert got["live"].startswith(banner) and "worklet" in got["live"] and "48000" in got["live"]
    assert got["frames"] > 0
    assert got["status"].startswith(banner) and "camera frames 4" in got["status"]
    r = got["refused"]
    assert r["frames"] == 0 and r["ends"] == 0, "audio went to a talk not confirmed as a test"
    assert r["unsubs"] == 1 and r["state"] == "idle" and "TEST MODE" in r["notice"]


def test_late_or_bad_events_change_nothing(tmp_path):
    """Kill: a `live` after a cancel re-lighting the button, a second start
    while one is running, or an out-of-range handler id used for frames."""
    got = _flow(
        r"""
const out = {};
let card = makeCard(); card._cuboStartMic(); await flush();
sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 1, dry_run: false, max_secs: 120 });
await card._cuboStartMic(); out.secondStart = [conn.subs.length, ctxs.length];
card._cuboStopMic('client_end'); sub().cb({ type: 'live' }); out.lateLive = [card._micState, notice(card)];
sub().cb({ type: 'ended', reason: 'client_end' }); await flush();
conn.socket = new FakeSocket();
card = makeCard(); card._cuboStartMic(); await flush();
sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 0, dry_run: false, max_secs: 120 });
feed(0.5); await flush();
out.badId = { frames: frames().length, ends: ends().length, state: card._micState, notice: notice(card) };
return out;
""",
        tmp_path,
    )
    assert got["secondStart"] == [1, 1]
    assert got["lateLive"] == ["stopping", None], "a cancelled talk announced itself live"
    b = got["badId"]
    assert b["frames"] == 0 and b["ends"] == 0 and b["state"] == "idle" and "log" in b["notice"]


def test_talking_never_touches_the_speaker(tmp_path):
    """Reported 2026-09-28: the mic flipped the speaker (mute <-> unmute) — the talk
    used to mute the picture and 'restore' it after, and the restore came out
    the opposite. The mic now leaves the speaker exactly as the user and the
    card's audio setting have it. Kill: any mute, unmute or icon change on the
    way in or out, or anything written to the saved or shared mute setting."""
    got = _flow(
        r"""
const out = {}; const writes = [];
const set = localStorage.setItem; localStorage.setItem = (k, v) => { writes.push(k); return set(k, v); };
const talk = async (card) => { card._cuboStartMic(); await flush(); sub().accept(); await flush();
  sub().cb({ type: 'started', handler_id: 2, dry_run: false, max_secs: 120 }); sub().cb({ type: 'live' }); };
const stop = async (card) => { card._cuboStopMic('client_end'); sub().cb({ type: 'ended', reason: 'client_end' }); await flush(); };
for (const muted of [true, false]) {
  video.muted = muted; volume.icon = muted ? 'mdi:volume-mute' : 'mdi:volume-high';
  const card = makeCard(); card.isMuted = muted;
  await talk(card); const during = [video.muted, card.isMuted, volume.icon];
  await stop(card); await advance(1000); const after = [video.muted, card.isMuted, volume.icon];
  out[muted ? 'muted' : 'sound'] = { during, after };
}
// The user taps the speaker during a talk: that choice simply stands.
video.muted = false; volume.icon = 'mdi:volume-high'; const card = makeCard(); card.isMuted = false;
await talk(card); video.muted = true; volume.icon = 'mdi:volume-mute'; card.isMuted = true;
await stop(card); await advance(1000); out.tap = [video.muted, card.isMuted, volume.icon];
out.writes = writes.filter((k) => /mute/i.test(k));
return out;
""",
        tmp_path,
    )
    assert got["muted"] == {"during": [True, True, "mdi:volume-mute"], "after": [True, True, "mdi:volume-mute"]}
    assert got["sound"] == {"during": [False, False, "mdi:volume-high"], "after": [False, False, "mdi:volume-high"]}
    assert got["tap"] == [True, True, "mdi:volume-mute"], "the end of a talk undid the user's own tap"
    assert got["writes"] == [], "the talk wrote a mute setting"


def test_an_insecure_page_explains_and_touches_no_audio(tmp_path):
    """Kill: the https guard dropped (an AudioContext made and getUserMedia
    called on a page that has none), or the notice without the page's own
    address."""
    got = _flow(
        r"""
isSecureContext = false; location.origin = 'http://192.168.1.5:8123';
navigator.mediaDevices = undefined;
const card = makeCard(); await card._cuboStartMic();
return { ctxs: ctxs.length, notice: notice(card), state: card._micState || 'idle', gum: gumCalls.length };
""",
        tmp_path,
    )
    assert got["ctxs"] == 0 and got["gum"] == 0 and got["state"] == "idle"
    assert "http://192.168.1.5:8123" in got["notice"] and "https://" in got["notice"]


def test_the_command_goes_to_the_cards_own_camera(tmp_path):
    """The command carries the camera's device_id: the pinned one, else the
    camera the live picture was built for. Kill: an unpinned card sending
    nothing or a made-up id; the target re-resolved per tap from the first
    speaker in hass.states (an entry reload re-adds a speaker at the end, and
    the voice would go to the OTHER nursery while this one is on screen); a
    card with no picture yet starting audio."""
    got = _flow(
        r"""
let card = makeCard({ device_id: '' }); card._cuboStartMic(); await flush();
const unpinned = sub().msg.device_id; sub().refuse({ code: 'busy', message: '' }); await flush();
// Camera DEV1's entry reloads: its speaker is re-added after DEV2's.
card = makeCard({ device_id: '' });
card._hass.states = { 'media_player.other_speaker': { attributes: { device_id: 'DEV2' } },
  'media_player.zuzu_speaker': { attributes: { device_id: 'DEV1' } } };
card._speakerEntityId = 'media_player.other_speaker';
card._cuboStartMic(); await flush();
const reordered = sub().msg.device_id; sub().refuse({ code: 'busy', message: '' }); await flush();
card = makeCard({ device_id: 'DEV9' }); card._cuboStartMic(); await flush();
const pinned = sub().msg.device_id; sub().refuse({ code: 'busy', message: '' }); await flush();
const n = ctxs.length;
card = makeCard({ device_id: '' }); card._liveDeviceId = undefined; await card._cuboStartMic();
return { unpinned, reordered, pinned, noCamera: [ctxs.length - n, notice(card)] };
""",
        tmp_path,
    )
    assert got["unpinned"] == "DEV1" and got["pinned"] == "DEV9"
    assert got["reordered"] == "DEV1", "the voice followed the speaker scan, not the picture"
    assert got["noCamera"][0] == 0 and "no CuboAI camera" in got["noCamera"][1]


def test_the_live_picture_fixes_the_mics_camera(tmp_path):
    """Where _liveDeviceId comes from: the camera the picture is built with,
    in `set hass` (the first build) and in setConfig (an unpinned card
    re-detecting). Kill: either assignment removed, or taken from anything
    but that camera entity's own device_id."""
    src = _src()
    build = src[src.index("customElements.whenDefined('webrtc-camera').then(() => {\n        if (!this.content) {") :]
    build = build[: build.index("this.content.setConfig(webrtcConfig);")]
    assert "this._liveDeviceId = cuboaiCameraDeviceId(found);" in build
    redetect = src[src.index("// Fallback to auto-detect.") : src.index("this.config = config;")]
    assert redetect.index("this.content.setConfig(webrtcConfig);") < redetect.index(
        "this._liveDeviceId = cuboaiCameraDeviceId(found);"
    )
    fn = src[src.index("function cuboaiCameraDeviceId") :]
    fn = fn[: fn.index("\n}\n") + 3]
    got = _run(
        fn
        + r"""
console.log(JSON.stringify([
  cuboaiCameraDeviceId({ entityId: 'camera.a', state: { attributes: { device_id: 'DEV7', uid: 'UID7' } } }),
  cuboaiCameraDeviceId({ entityId: 'camera.a', state: { attributes: {} } }),
  cuboaiCameraDeviceId(null),
]));
""",
        tmp_path,
    )
    assert got == ["DEV7", None, None]


def test_test_mode_fails_safe(tmp_path):
    """YAML hands `talk_dry_run: "true"`, `yes`, `on` or `1` over as strings or
    numbers. Kill: only the boolean `true` accepted (each of those would quietly
    start a real, audible talk, the banner simply missing), or `false` / no
    switch at all read as test mode."""
    got = _flow(
        r"""
const out = {};
const values = { bool: true, str: 'true', yes: 'yes', on: 'on', one: 1, zero: 0, empty: null, strFalse: 'false',
  off: false, missing: undefined };
for (const [name, value] of Object.entries(values)) {
  for (const where of ['yaml', 'window']) {
    const config = { device_id: 'DEV1' };
    if (where === 'yaml' && name !== 'missing') config.talk_dry_run = value;
    window.__cuboaiMicDryRun = where === 'window' ? value : undefined;
    const card = makeCard(config); card._cuboStartMic();
    const banner = (notice(card) || '').startsWith('TEST MODE');
    await flush();
    out[`${where}:${name}`] = [sub().msg.dry_run, banner];
    sub().refuse({ code: 'busy', message: '' }); await flush();
  }
}
window.__cuboaiMicDryRun = undefined;
return out;
""",
        tmp_path,
    )
    assert len(got) == 20
    for key, (flag, banner) in got.items():
        want = key.split(":")[1] not in ("off", "missing")
        assert flag is want and banner is want, key


def test_a_drop_during_the_wait_for_ended_ends_it_and_unsubscribes_nothing(tmp_path):
    """The end frame is out (or the server reported an error), and the socket
    drops before `ended` -- a Wi-Fi to 5G handover. The frontend reconnects at
    once and restarts its command ids, so this talk's id may now be another
    card's subscription. Kill: the 'disconnected' listener removed when the stop
    begins (the button sits in 'stopping' for the full 4 s), or the unsubscribe
    still sent afterwards (it would cancel that other subscription)."""
    got = _flow(
        r"""
const out = {};
for (const how of ['stop', 'error']) {
  conn.socket = new FakeSocket();
  const card = makeCard(); card._cuboStartMic(); await flush();
  sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 5, dry_run: false, max_secs: 120 });
  sub().cb({ type: 'live' });
  if (how === 'stop') card._cuboStopMic('client_end');
  else sub().cb({ type: 'error', code: 'camera_lost', message: 'fixed server text' });
  await advance(500);
  const waiting = card._micState;
  conn.fire('disconnected'); conn.socket = new FakeSocket();   // and straight back
  await flush();
  out[how] = { waiting, after: card._micState, unsubs: sub().unsubs, notice: notice(card),
    listeners: (conn.listeners.disconnected || []).length };
  await advance(10000);
  out[how].late = [sub().unsubs, card._micState];
}
return out;
""",
        tmp_path,
    )
    for how, r in got.items():
        assert r["waiting"] == "stopping", how
        assert r["after"] == "idle", f"{how}: waited for an `ended` that can no longer arrive"
        assert r["unsubs"] == 0 and r["late"] == [0, "idle"], f"{how}: unsubscribed after the drop"
        assert r["listeners"] == 0, how
    assert got["stop"]["notice"] is None
    assert got["error"]["notice"] == "Lost the connection to the camera.", "the drop overwrote why the talk ended"


def test_what_changed_during_the_capture_start_is_seen_before_the_talk_opens(tmp_path):
    """The phone locked, another app took the microphone, or the audio was
    interrupted while the permission prompt (or the worklet) was loading: those
    events have fired already, before anything listened. Kill: the state not
    checked when the stop paths are armed (the talk opens anyway -- on a real
    camera a SPEAKERSTART click for nobody -- until the server's idle timer), or
    a context suspended meanwhile not resumed."""
    got = _flow(
        r"""
const out = {};
const gum = navigator.mediaDevices.getUserMedia;
let endTrack = false;
navigator.mediaDevices.getUserMedia = async (c) => {
  const s = await gum(c); if (endTrack) s.getAudioTracks()[0].readyState = 'ended'; return s;
};
const cases = {
  hidden: () => { document.visibilityState = 'hidden'; },
  track_ended: () => { endTrack = true; },
  interrupted: (c) => { c.state = 'interrupted'; },
  suspended: (c) => { c.state = 'suspended'; },
};
for (const [name, change] of Object.entries(cases)) {
  video.muted = false;
  let open; gumGate = new Promise((r) => { open = r; });
  const n = conn.subs.length;
  const card = makeCard(); card._cuboStartMic(); await flush();
  const c = ctxs[ctxs.length - 1]; const r0 = c.resumes;
  change(c);
  gumGate = null; open(); await flush();
  out[name] = { subs: conn.subs.length - n, state: card._micState, notice: notice(card),
    track: tracks[tracks.length - 1].readyState, ctx: c.state, resumed: c.resumes - r0, video: video.muted };
  document.visibilityState = 'visible'; endTrack = false;
  if (conn.subs.length > n) { sub().refuse({ code: 'busy', message: '' }); await flush(); }
}
return out;
""",
        tmp_path,
    )
    for name, word in (("hidden", "background"), ("track_ended", "microphone"), ("interrupted", "interrupted")):
        r = got[name]
        assert r["subs"] == 0, f"{name}: the talk was opened"
        assert (r["state"], r["track"], r["ctx"]) == ("idle", "ended", "closed"), name
        assert word in r["notice"] and r["video"] is False, name
    s = got["suspended"]
    assert s["subs"] == 1 and s["state"] == "connecting" and s["resumed"] == 1


def test_a_card_moved_in_the_dashboard_keeps_talking(tmp_path):
    """A masonry dashboard re-flows its columns when the phone rotates: the card
    is taken out and put straight back. Kill: the release not deferred (the talk
    ends with no word of why), or a card really removed left talking."""
    got = _flow(
        r"""
const card = makeCard(); card._cuboStartMic(); await flush();
sub().accept(); await flush(); sub().cb({ type: 'started', handler_id: 3, dry_run: false, max_secs: 120 });
sub().cb({ type: 'live' });
card.disconnectedCallback();   // and attached again in the same task: still connected
await advance(50);
const moved = [card._micState, ends().length, tracks[0].readyState];
card.isConnected = false; card.disconnectedCallback(); await advance(0);
return { moved, removed: [card._micState, ends().length, tracks[0].readyState] };
""",
        tmp_path,
    )
    assert got["moved"] == ["live", 0, "live"]
    assert got["removed"] == ["stopping", 1, "ended"]


def test_a_refused_talk_says_why_and_leaves_the_sound_on(tmp_path):
    """Kill: a refusal that mutes the room's sound, or a notice other than the
    reason the talk did not happen."""
    got = _flow(
        r"""
video.muted = false; video.paused = false;
const card = makeCard(); card.isMuted = false; card._cuboStartMic(); await flush();
sub().refuse({ code: 'speaker_busy', message: '' }); await flush();
await advance(300);
return { notice: notice(card), muted: video.muted, isMuted: card.isMuted };
""",
        tmp_path,
    )
    assert got["muted"] is False and got["isMuted"] is False
    assert got["notice"] == "The camera speaker is playing. Stop the music first, then talk."


def test_another_devices_mute_during_a_talk_applies_at_once(tmp_path):
    """The other parent mutes the camera (the shared setting) while this phone
    talks: the phone follows it at once, exactly as without a talk, and the end
    of the talk does not undo it. Kill: the change ignored during a talk, or
    reverted when the talk ends (the two devices would disagree)."""
    got = _flow(
        r"""
const hassWith = (muted) => ({ connection: conn, states: {
  'media_player.zuzu_speaker': { state: 'idle', attributes: { device_id: 'DEV1' } },
  'sensor.cuboai_media_library': { state: 'ok', attributes: { settings: { DEV1: { muted } } } } } });
video.muted = false;
const card = makeCard(); card.isMuted = false; card._initialized = true; card._deviceId = 'DEV1';
card.hass = hassWith(false);
card._cuboStartMic(); await flush(); sub().accept(); await flush();
sub().cb({ type: 'started', handler_id: 2, dry_run: false, max_secs: 120 }); sub().cb({ type: 'live' });
card.hass = hassWith(true);   // another device mutes the camera
const during = [video.muted, card.isMuted];
card._cuboStopMic('client_end'); sub().cb({ type: 'ended', reason: 'client_end' }); await flush();
return { during, after: [video.muted, card.isMuted], error: card.innerHTML || null };
""",
        tmp_path,
    )
    assert got["error"] is None
    assert got["during"] == [True, True]
    assert got["after"] == [True, True], "the other device's mute was lost at the end of the talk"


# =============================================================================
# Option talk_mutes_speaker: the room's sound muted while talking
# =============================================================================

_TALK = r"""
const talk = async (card) => { card._cuboStartMic(); await flush(); sub().accept(); await flush();
  sub().cb({ type: 'started', handler_id: 2, dry_run: false, max_secs: 120 }); sub().cb({ type: 'live' }); };
const stopTap = async (card) => { navigator.userActivation = { isActive: true };
  card._cuboStopMic('client_end'); navigator.userActivation = { isActive: false };
  sub().cb({ type: 'ended', reason: 'client_end' }); await flush(); };
const OPT = { device_id: 'DEV1', talk_mutes_speaker: true };
"""


def test_the_option_mutes_the_room_while_talking_and_brings_it_back(tmp_path):
    """Kill: the option ignored (no mute), the mute not undone, a muted room
    unmuted by the end of a talk, the speaker icon set by hand (it must follow
    the video), or anything written to the saved mute setting."""
    got = _flow(
        _TALK
        + r"""
const out = {}; const writes = [];
const set = localStorage.setItem; localStorage.setItem = (k, v) => { writes.push(k); return set(k, v); };
for (const muted of [false, true]) {
  video.muted = muted; volume.icon = 'untouched';
  const card = makeCard(OPT); card.isMuted = muted;
  await talk(card); const during = [video.muted, card.isMuted];
  await stopTap(card); await advance(1000);
  out[muted ? 'muted' : 'sound'] = { during, after: [video.muted, card.isMuted], icon: volume.icon, notice: notice(card) };
}
out.writes = writes.filter((k) => /mute/i.test(k));
return out;
""",
        tmp_path,
    )
    assert got["sound"]["during"] == [True, True], "the option did not mute the room"
    assert got["sound"]["after"] == [False, False], "the sound did not come back"
    assert got["muted"]["during"] == [True, True] and got["muted"]["after"] == [True, True]
    assert got["sound"]["icon"] == got["muted"]["icon"] == "untouched", "the talk mute set the icon by hand"
    assert got["sound"]["notice"] is None
    assert got["writes"] == [], "the talk mute was saved"


def test_a_speaker_tap_during_the_talk_stands(tmp_path):
    """Kill: the end of the talk undoing the user's own speaker tap."""
    got = _flow(
        _TALK
        + r"""
video.muted = false; const card = makeCard(OPT); card.isMuted = false;
await talk(card);
card._mic.talkMute.touched = true; video.muted = true;   // the volume hook marks it; the user chose silence
await stopTap(card);
return { after: [video.muted, card.isMuted] };
""",
        tmp_path,
    )
    assert got["after"] == [True, True]


def test_a_talk_that_ends_by_itself_never_leaves_a_paused_picture(tmp_path):
    """Outside a tap a browser may refuse the unmute and pause the video. Kill:
    the picture left paused and silent, or no hint for the tap that brings the
    sound back."""
    got = _flow(
        _TALK
        + r"""
video.muted = false; video.paused = false; const card = makeCard(OPT); card.isMuted = false;
await talk(card);
sub().cb({ type: 'ended', reason: 'idle' }); await flush();
const restored = video.muted; video.paused = true; const plays = video.plays;
await advance(300);
return { restored, muted: video.muted, isMuted: card.isMuted, played: video.plays > plays, arms: card._gestureArms,
  notice: notice(card) };
""",
        tmp_path,
    )
    assert got["restored"] is False, "the unmute was not even tried"
    assert got["muted"] is True and got["isMuted"] is True and got["played"] and got["arms"] == 1
    assert got["notice"].endswith("Tap 🔈 to hear the room.")


def test_a_refused_microphone_never_mutes_the_room(tmp_path):
    """Kill: the room muted before the microphone was captured (a denied
    permission would silence the monitor for nothing)."""
    got = _flow(
        _TALK
        + r"""
video.muted = false; const card = makeCard(OPT); card.isMuted = false; GUM = [new DOMException('no', 'NotAllowedError')];
await card._cuboStartMic(); await flush();
return { muted: video.muted, isMuted: card.isMuted };
""",
        tmp_path,
    )
    assert got == {"muted": False, "isMuted": False}


def test_another_devices_mute_during_a_muted_talk_is_what_it_brings_back(tmp_path):
    """The other parent mutes the camera while this phone talks with the option
    on. Kill: the room unmuted mid-talk by the sync, or this phone's old state
    restored over the other device's choice."""
    got = _flow(
        _TALK
        + r"""
const hassWith = (muted) => ({ connection: conn, states: {
  'media_player.zuzu_speaker': { state: 'idle', attributes: { device_id: 'DEV1' } },
  'sensor.cuboai_media_library': { state: 'ok', attributes: { settings: { DEV1: { muted } } } } } });
video.muted = false;
const card = makeCard(OPT); card.isMuted = false; card._initialized = true; card._deviceId = 'DEV1';
card.hass = hassWith(false);
await talk(card);
card.hass = hassWith(true);
const during = [video.muted, card.isMuted];
await stopTap(card);
return { during, after: [video.muted, card.isMuted], error: card.innerHTML || null };
""",
        tmp_path,
    )
    assert got["error"] is None
    assert got["during"] == [True, True]
    assert got["after"] == [True, True], "the other device's mute was lost"
