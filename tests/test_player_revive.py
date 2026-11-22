"""A player stuck after a network change is dialled again.

Reported 2026-09-28 (iOS 26, recorded by the card's diagnostics): starting on
Wi-Fi and switching to 5G, the WebRTC stream ended and no new source came for
25 s -- black. webrtc-camera asks Home Assistant to sign a new stream URL at the
moment Home Assistant's own connection is down; the request fails ("error") or
never answers ("Loading.."), and the player never tries again. The card now
dials once more when the player has had neither a stream nor a connection
attempt for a few seconds and Home Assistant is connected. Every test names the
mutation it kills.
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
    a = src.index("const CUBOAI_REVIVE_AFTER_MS")
    return src[a : src.index("// Single source of truth for the webrtc-camera", a)]


_PRELUDE = r"""
const doc = { hidden: false };
const makeCard = (o = {}) => {
  const mode = { innerText: o.mode === undefined ? 'MSE' : o.mode };
  const player = { isConnected: true, ws: null, pc: null, dials: 0, stops: 0,
    onconnect() { this.dials += 1; this.dialMode = mode.innerText; },
    ondisconnect() { this.stops += 1; this.ws = null; this.pc = null; },
    querySelector: (s) => (s === '.mode' ? mode : null) };
  const video = { __cuboBridge: { played: o.played !== false }, currentTime: 0, paused: false, readyState: 0, ownerDocument: doc };
  return { content: player, _hass: { connection: { connected: o.connected !== false } }, _cuboMedia: () => ({ video }), mode, player, video,
    isConnected: true };
};
// Ticks every 500 ms, as the card does, from `from` to `to` (inclusive); returns what the revive did.
const tick = (c, from, to, each) => { const did = []; for (let t = from; t <= to; t += 500) { if (each) each(t);
  const r = cuboaiRevivePlayer(c, t); if (r) did.push([t, r]); } return did; };
"""


def _node(body: str, tmp_path) -> dict:
    script = tmp_path / "revive.js"
    script.write_text(_PRELUDE + _helper() + body, encoding="utf-8")
    return json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout)


def test_a_stuck_player_is_dialled_again(tmp_path):
    """Kill: no revive at all, dialling before the player had its chance (it
    reconnects by itself within a second), or a hung 'Loading..' left in place
    (webrtc-camera refuses every retry while it is up)."""
    got = _node(
        """
const out = {};
const c = makeCard({ mode: 'Loading..' });
out.at0 = cuboaiRevivePlayer(c, 0); out.at2 = cuboaiRevivePlayer(c, 2000);
out.at3 = cuboaiRevivePlayer(c, 3100);
out.dials = c.player.dials; out.dialMode = c.player.dialMode;
const e = makeCard({ mode: 'error' }); cuboaiRevivePlayer(e, 0); out.error = cuboaiRevivePlayer(e, 3100);
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["at0"] is None and got["at2"] is None, "dialled before the player had its own chance"
    assert got["at3"] == "Loading.." and got["dials"] == 1
    assert got["dialMode"] == "", "'Loading..' was not cleared, so webrtc-camera would refuse the dial"
    assert got["error"] == "error"


def test_the_revive_stays_out_of_the_way(tmp_path):
    """Kill: dialling while the player is connected or connecting (ws/pc), while
    Home Assistant is still offline (the signing would fail again), for a card
    that is not on the page, during the card's own recording switch, or before
    anything ever played (a first load has its own dial)."""
    got = _node(
        """
const run = (c) => { cuboaiRevivePlayer(c, 0); cuboaiRevivePlayer(c, 5000); return c.player.dials; };
const out = {};
let c = makeCard(); c.player.ws = {}; out.ws = run(c);
c = makeCard(); c.player.pc = {}; out.pc = run(c);
c = makeCard({ connected: false }); out.offline = run(c);
c = makeCard(); c.player.isConnected = false; out.detached = run(c);
c = makeCard(); c._dvrRedial = 7; out.recording = run(c);
c = makeCard({ played: false }); out.firstLoad = run(c);
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got == {"ws": 0, "pc": 0, "offline": 0, "detached": 0, "recording": 0, "firstLoad": 0}


def test_the_revive_waits_for_home_assistant_and_does_not_hammer(tmp_path):
    """Kill: the dead time reset while Home Assistant is offline (the dial would
    wait another 3 s after it is back), or a dial on every tick."""
    got = _node(
        """
const c = makeCard({ connected: false });
cuboaiRevivePlayer(c, 0); cuboaiRevivePlayer(c, 4000);
c._hass.connection.connected = true; const back = cuboaiRevivePlayer(c, 4500);
for (let t = 5000; t <= 7000; t += 500) cuboaiRevivePlayer(c, t);
const within3s = c.player.dials;
cuboaiRevivePlayer(c, 7600);
console.log(JSON.stringify({ back, within3s, after: c.player.dials }));
""",
        tmp_path,
    )
    assert got["back"] == "MSE", "the revive waited again after Home Assistant came back"
    assert got["within3s"] == 1
    assert got["after"] == 2


def test_a_recovered_player_resets_the_clock(tmp_path):
    """Kill: dead time carried over a recovery (a later short gap dialled at
    once, racing the player's own reconnect)."""
    got = _node(
        """
const c = makeCard(); cuboaiRevivePlayer(c, 0); cuboaiRevivePlayer(c, 2000);
c.player.ws = {}; cuboaiRevivePlayer(c, 2500);
c.player.ws = null; const early = cuboaiRevivePlayer(c, 3500);
console.log(JSON.stringify({ early, dials: c.player.dials }));
""",
        tmp_path,
    )
    assert got["early"] is None and got["dials"] == 0


def test_the_card_runs_the_revive_on_its_tick():
    """Kill: the revive not called from the card's attach interval."""
    src = _src()
    tick = src[src.index("this.attachInterval = setInterval(() => {") :][:400]
    assert "cuboaiRevivePlayer(this, Date.now())" in tick
    assert "cuboaiStopReplaced(this, Date.now())" in tick
    cls = src.index("class CuboAICameraCard extends HTMLElement {")
    set_config = src[src.index("  setConfig(config) {", cls) :][:120]
    assert "cuboaiRegisterCard(this);" in set_config


# =============================================================================
# The watchdog: no moving picture for 10 s
# =============================================================================


def test_a_hung_connection_is_restarted_from_scratch(tmp_path):
    """The card HA re-created 2 s before the switch: its stream connection hung
    and it never showed a picture. Kill: no watchdog, a restart before 10 s,
    dialling while the old connection still stands, or no dial after it."""
    got = _node(
        """
const c = makeCard({ played: false }); c.player.ws = { hung: true };
// The old connection takes a second to close after the disconnect.
c.player.ondisconnect = function () { this.stops += 1; const self = this; closeAt = 11000; };
let closeAt = null;
const did = tick(c, 0, 13000, (t) => { if (closeAt !== null && t >= closeAt) c.player.ws = null; });
console.log(JSON.stringify({ did, stops: c.player.stops, dials: c.player.dials }));
""",
        tmp_path,
    )
    assert [d[0] for d in got["did"]] == [10000, 11000], "dialled while the old connection still stood"
    assert got["did"][0][1] == "stuck:MSE" and got["did"][1][1] == "restarted:MSE"
    assert got["stops"] == 1 and got["dials"] == 1


def test_a_moving_or_paused_picture_is_never_restarted(tmp_path):
    """Kill: restarting a stream that plays (currentTime moving), or one the
    user paused (a picture is there)."""
    got = _node(
        """
const a = makeCard(); a.player.ws = {}; const moving = tick(a, 0, 30000, () => { a.video.currentTime += 0.5; });
const b = makeCard(); b.player.ws = {}; b.video.paused = true; b.video.readyState = 4; const paused = tick(b, 0, 30000);
console.log(JSON.stringify({ moving, paused }));
""",
        tmp_path,
    )
    assert got == {"moving": [], "paused": []}


def test_the_watchdog_leaves_alone_what_it_cannot_help(tmp_path):
    """Kill: restarting while HA is offline, while the page is in the
    background (the hidden time never counts), a card off the page, or during
    the card's own recording playback. Back from the background a picture that
    does not move gets the short resume window, not zero and not the full 10 s."""
    got = _node(
        """
const out = {};
let c = makeCard({ connected: false }); c.player.ws = {}; out.offline = tick(c, 0, 15000).length;
c._hass.connection.connected = true; out.back = tick(c, 15500, 16000).map((d) => d[1]);
doc.hidden = true; c = makeCard(); c.player.ws = {}; out.hidden = tick(c, 0, 20000).length;
doc.hidden = false; out.visibleAgain = tick(c, 20500, 29500).map((d) => d[0]);
c = makeCard(); c.player.ws = {}; c.player.isConnected = false; out.offPage = tick(c, 0, 20000).length;
c = makeCard(); c.player.ws = {}; c._dvrPlaying = true; out.recording = tick(c, 0, 20000).length;
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["offline"] == 0
    assert got["back"] == ["stuck:MSE", "restarted:MSE"], "HA back: the overdue restart should run at once"
    assert got["hidden"] == 0, "time in the background counted as stuck"
    assert got["visibleAgain"][0] == 23500, got["visibleAgain"]
    assert got["offPage"] == 0 and got["recording"] == 0


def test_a_restart_is_not_repeated_before_the_new_stream_had_its_chance(tmp_path):
    """Kill: restarting every tick while the new stream is still starting."""
    got = _node(
        """
const c = makeCard({ played: false }); c.player.ws = {};
c.player.onconnect = function () { this.dials += 1; this.ws = { again: true }; };
const did = tick(c, 0, 19500);
console.log(JSON.stringify({ restarts: did.filter((d) => d[1].startsWith('stuck')).map((d) => d[0]) }));
""",
        tmp_path,
    )
    assert got["restarts"] == [10000], got["restarts"]


# =============================================================================
# A card Home Assistant replaced stops its stream
# =============================================================================


def test_a_replaced_card_stops_its_player(tmp_path):
    """Kill: the hidden old card's stream left running next to the new card's
    (a second stream on mobile data), stopped too soon (a card only moved),
    stopped with no other card showing the camera (another dashboard view:
    background audio must go on), stopped for another camera's card, or
    never allowed to run again once back on the page."""
    got = _node(
        """
const old = makeCard(); old._liveDeviceId = 'CAM1'; cuboaiRegisterCard(old);
const fresh = makeCard(); fresh._liveDeviceId = 'CAM1'; cuboaiRegisterCard(fresh);
const other = makeCard(); other._liveDeviceId = 'CAM2'; cuboaiRegisterCard(other);
const out = {};
old.isConnected = false; cuboaiStopReplaced(old, 0); out.at9s = cuboaiStopReplaced(old, 9000);
out.at10s = cuboaiStopReplaced(old, 10000); out.stops = old.player.stops; out.again = cuboaiStopReplaced(old, 20000);
old.isConnected = true; cuboaiStopReplaced(old, 21000); out.reset = old._stoppedAsReplaced;
const lone = makeCard(); lone._liveDeviceId = 'CAM3'; cuboaiRegisterCard(lone); lone.isConnected = false;
cuboaiStopReplaced(lone, 0); out.lone = cuboaiStopReplaced(lone, 20000);   // cards for CAM1 and CAM2 are showing
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["at9s"] is False
    assert got["at10s"] is True and got["stops"] == 1
    assert got["again"] is False, "stopped twice"
    assert got["reset"] is False
    assert got["lone"] is False, "a card on another view lost its background stream"


# =============================================================================
# Back from a locked iPhone: black picture, no sound
# =============================================================================


def test_a_phone_back_from_lock_presses_play_on_the_video_ios_paused(tmp_path):
    """Reported 2026-11-22: lock the iPhone, unlock -- black, no sound. iOS
    froze the app (no ticks at all) and paused the video; a paused video with a
    picture used to pass for the user's pause and was never touched. Kill: no
    resume on a tick gap, no play() on resume, play() also without a resume."""
    got = _node(
        """
const c = makeCard(); c.player.pc = {};
c.video.paused = true; c.video.readyState = 4; c.video.plays = 0;
c.video.play = function () { this.plays += 1; return Promise.resolve(); };
tick(c, 0, 5000);                       // the user's own pause: left alone
const before = c.video.plays;
tick(c, 65000, 65000);                  // first tick after a 60 s freeze
console.log(JSON.stringify({ before, after: c.video.plays }));
""",
        tmp_path,
    )
    assert got["before"] == 0, "the user's pause was overridden"
    assert got["after"] == 1, "the video iOS paused was not played again"


def test_a_refused_play_after_lock_retries_muted(tmp_path):
    """iOS refuses sound without a tap: play muted so the picture moves (the
    speaker button brings the sound back). Kill: no muted retry."""
    got = _node(
        """
const c = makeCard(); c.player.pc = {};
c.video.paused = true; c.video.readyState = 4; c.video.muted = false; c.video.calls = [];
c.video.play = function () { this.calls.push(this.muted);
  return this.muted ? Promise.resolve() : Promise.reject({ name: 'NotAllowedError' }); };
tick(c, 0, 0); tick(c, 60000, 60000);
setTimeout(() => console.log(JSON.stringify({ calls: c.video.calls, muted: c.video.muted })), 0);
""",
        tmp_path,
    )
    assert got == {"calls": [False, True], "muted": True}


def test_a_frozen_picture_after_lock_is_restarted_quickly(tmp_path):
    """The WebRTC connection died during the lock but the player still holds
    it: restart after 3 s, not the 10 s watchdog -- and only once per resume.
    Kill: no fast path, the fast path repeating, the fast path without a resume."""
    got = _node(
        """
const c = makeCard({ played: false }); c.player.pc = {};
c.player.onconnect = function () { this.dials += 1; this.pc = { again: true }; };
let n = 0; tick(c, 0, 2000, () => { c.video.currentTime = ++n; });   // playing before the lock
const did = tick(c, 60000, 80000).filter((d) => d[1].startsWith('stuck')).map((d) => d[0]);
console.log(JSON.stringify({ did }));
""",
        tmp_path,
    )
    assert got["did"] == [63000, 73000], got["did"]


def test_a_page_hidden_and_shown_counts_as_a_resume(tmp_path):
    """Where timers keep running while hidden (no tick gap), the hidden spell
    alone marks the resume. A blink (under 3 s) does not. Kill: hidden time
    ignored, every blink treated as a lock."""
    got = _node(
        """
const mk = () => { const c = makeCard(); c.player.pc = {}; c.video.paused = true; c.video.readyState = 4; c.video.plays = 0;
  c.video.play = function () { this.plays += 1; return Promise.resolve(); }; return c; };
let c = mk(); tick(c, 0, 1000); doc.hidden = true; tick(c, 1500, 6000); doc.hidden = false; tick(c, 6500, 6500);
const long = c.video.plays;
c = mk(); tick(c, 0, 1000); doc.hidden = true; tick(c, 1500, 2000); doc.hidden = false; tick(c, 2500, 2500);
console.log(JSON.stringify({ long, blink: c.video.plays }));
""",
        tmp_path,
    )
    assert got == {"long": 1, "blink": 0}
