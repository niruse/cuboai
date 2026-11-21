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
    tick = src[src.index("this.attachInterval = setInterval(() => {") :][:900]
    assert "cuboaiRevivePlayer(this, Date.now())" in tick
    assert "cuboaiStopReplaced(this, Date.now())" in tick
    assert "cuboaiParkFloating(this" in tick
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
    background (on return it counts afresh), a card off the page, or during
    the card's own recording playback."""
    got = _node(
        """
const out = {};
let c = makeCard({ connected: false }); c.player.ws = {}; out.offline = tick(c, 0, 15000).length;
c._hass.connection.connected = true; out.back = tick(c, 15500, 16000).map((d) => d[1]);
doc.hidden = true; c = makeCard(); c.player.ws = {}; out.hidden = tick(c, 0, 20000).length;
doc.hidden = false; out.visibleAgain = tick(c, 20500, 29500).length;
c = makeCard(); c.player.ws = {}; c.player.isConnected = false; out.offPage = tick(c, 0, 20000).length;
c = makeCard(); c.player.ws = {}; c._dvrPlaying = true; out.recording = tick(c, 0, 20000).length;
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["offline"] == 0
    assert got["back"] == ["stuck:MSE", "restarted:MSE"], "HA back: the overdue restart should run at once"
    assert got["hidden"] == 0 and got["visibleAgain"] == 0, "time in the background counted as stuck"
    assert got["offPage"] == 0 and got["recording"] == 0


def test_a_floating_window_is_revived_although_the_page_is_in_the_background(tmp_path):
    """Reported 2026-11-21 (iPhone app): with the video in a floating window and the app in
    the background, the picture went black after a while and stayed black until the app was
    opened. The revive skipped every page in the background; the floating window is exactly
    where the user is looking then.

    Kill: skip the revive whenever the page is hidden, or revive every hidden page."""
    got = _node(
        """
const out = {};
doc.hidden = true;
let c = makeCard(); c.video.webkitPresentationMode = 'picture-in-picture';
out.iphone = tick(c, 0, 4000).length; out.iphoneDials = c.player.dials;
c = makeCard(); doc.pictureInPictureElement = c.video;
out.chrome = tick(c, 0, 4000).length;
c = makeCard(); c.video._pipVideo = {}; doc.pictureInPictureElement = c.video._pipVideo;
out.canvas = tick(c, 0, 4000).length;
c = makeCard(); doc.pictureInPictureElement = {};          // some other video's window
out.other = tick(c, 0, 4000).length;
doc.pictureInPictureElement = null;
c = makeCard(); c.video.webkitPresentationMode = 'inline';
out.inline = tick(c, 0, 4000).length;
c = makeCard(); c.video.webkitPresentationMode = 'picture-in-picture'; c.player.ws = {};
out.stuck = tick(c, 0, 10500).filter(d => String(d[1]).startsWith('stuck')).length;
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["iphone"] == 1 and got["iphoneDials"] == 1, "a dead stream in an iPhone floating window was not dialled"
    assert got["chrome"] == 1 and got["canvas"] == 1, "a floating window on Chrome was not recognised"
    assert got["other"] == 0 and got["inline"] == 0, "a page in the background with no floating window was revived"
    assert got["stuck"] == 1, "a hung connection behind a floating window was never restarted"


def test_a_replaced_card_keeps_playing_while_its_video_is_floating(tmp_path):
    """Back in the app, Home Assistant builds a new card. The old card's video is the one in the
    floating window, so stopping it as "replaced" would blank the window the user is watching.
    It plays on until the window is closed, and is stopped then.

    Kill: stop a replaced card whose video is floating, or never stop it after the window closes."""
    got = _node(
        """
const out = {};
doc.hidden = false; doc.pictureInPictureElement = null;
const old = makeCard(), fresh = makeCard();
old._liveDeviceId = fresh._liveDeviceId = 'CAM1';
[old, fresh].forEach(cuboaiRegisterCard);
old.video.webkitPresentationMode = 'picture-in-picture';
old.isConnected = false; cuboaiStopReplaced(old, 0);
out.floating = cuboaiStopReplaced(old, 20000); out.stopsFloating = old.player.stops;
old.video.webkitPresentationMode = 'inline';
out.closed = cuboaiStopReplaced(old, 20500); out.stops = old.player.stops;
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["floating"] is False and got["stopsFloating"] == 0, "a card watched in a floating window was stopped"
    assert got["closed"] is True and got["stops"] == 1, "the replaced card was not stopped once its window closed"


def test_a_floating_video_is_kept_in_the_page_when_its_card_leaves(tmp_path):
    """Measured 2026-11-21 on an iPhone with the card's diagnostics: moving to another dashboard
    view took the card off the page, and the floating window went black although the video was
    still connected, playing and counting time. An iPhone draws nothing for a video that is not
    in the page, so the player is parked in a holder on <body> until the card is back or the
    window is closed, and then put back exactly where it was. The holder is a full-size opaque
    box behind the page: a 2 px transparent one was measured to leave the window black.

    Kill: leave the player in the detached card; park a card that is on the page, not floating,
    or playing a recording; hide the holder with display:none; or put the player back in the
    wrong place / never."""
    got = _node(
        """
const out = {};
const mk = () => {
  const c = makeCard();
  const body = { kids: [], appendChild(n) { this.kids.push(n); n.parentNode = this; } };
  doc.body = body;
  doc.createElement = () => ({ style: {}, kids: [], appendChild(n) { this.kids.push(n); n.parentNode = this; },
    remove() { body.kids = body.kids.filter(k => k !== this); this.removed = true; } });
  c.moves = [];
  const after = { parentNode: c };
  c.player.nextSibling = after; c.after = after;
  c.insertBefore = (n, ref) => { c.moves.push(ref === after ? 'before-next' : 'before-?'); n.parentNode = c; };
  c.appendChild = (n) => { c.moves.push('append'); n.parentNode = c; };
  c.video.webkitPresentationMode = 'picture-in-picture';
  return { c, body };
};
let { c, body } = mk();
out.onPage = cuboaiParkFloating(c);                       // the card is on the page: nothing to do
c.isConnected = false;
out.parked = cuboaiParkFloating(c);
out.holderCss = body.kids[0].style.cssText;
out.inHolder = c.player.parentNode === body.kids[0];
out.again = cuboaiParkFloating(c);                         // already parked, still floating
c.isConnected = true;
out.back = cuboaiParkFloating(c); out.moves = c.moves; out.holderGone = body.kids.length === 0;

({ c, body } = mk()); c.isConnected = false; cuboaiParkFloating(c);
c.video.webkitPresentationMode = 'inline';                 // the window was closed, the card still away
out.closed = cuboaiParkFloating(c); out.closedMoves = c.moves;

({ c, body } = mk()); c.isConnected = false; c.video.webkitPresentationMode = 'inline';
out.notFloating = cuboaiParkFloating(c);
({ c, body } = mk()); c.isConnected = false; c._dvrPlaying = true;
out.recording = cuboaiParkFloating(c);
({ c, body } = mk()); c.isConnected = false; c.after.parentNode = null;   // its old neighbour is gone
cuboaiParkFloating(c); c.isConnected = true; cuboaiParkFloating(c); out.noNeighbour = c.moves;
out.empty = cuboaiParkFloating({});
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["onPage"] is None, "a card that is on the page was parked"
    assert got["parked"] == "parked" and got["inHolder"], "the floating video's player was left in the detached card"
    css = got["holderCss"]
    assert "display" not in css and "opacity" not in css, "the holder hides the video in a way that blanks the window"
    assert "width: 160px" in css and "height: 90px" in css and "z-index: -1" in css, (
        "not a full-size box behind the page"
    )
    assert got["again"] is None, "a parked player was moved again"
    assert got["back"] == "unparked" and got["moves"] == ["before-next"] and got["holderGone"], (
        "the player did not go back where it was"
    )
    assert got["closed"] == "unparked" and got["closedMoves"] == ["before-next"], (
        "a closed window left the player parked"
    )
    assert got["notFloating"] is None and got["recording"] is None, "parked a player that is not a live floating window"
    assert got["noNeighbour"] == ["append"], "the player was not put back when its old neighbour was gone"
    assert got["empty"] is None


def test_the_floating_window_button_waits_for_a_picture(tmp_path):
    """Measured 2026-11-21 on an iPhone, three times: the floating-window button pressed while
    the video had no frame yet opened the window and closed it 40 ms later, and after that no
    press worked until the page was reloaded. The card shows a remembered picture at once, so
    the button looked ready. A press before the first frame is swallowed before the player
    sees it, and the button is dimmed until then.

    Kill: let an early press through; swallow a press once there is a picture; leave the button
    dimmed; or add the listener again on every tick."""
    got = _node(
        """
const out = {};
const c = makeCard();
const btn = { style: {}, added: 0, addEventListener(name, fn, capture) { this.added += 1; this.fn = fn; this.capture = capture; } };
c.player.querySelector = (s) => (s === '.pictureinpicture' ? btn : null);
const press = () => { const e = { stopped: false, prevented: false,
  stopImmediatePropagation() { this.stopped = true; }, preventDefault() { this.prevented = true; } }; btn.fn(e); return [e.stopped, e.prevented]; };
c.video.readyState = 1; c.video.videoWidth = 0;
out.early = cuboaiGuardPipButton(c); out.dim = btn.style.opacity; out.capture = btn.capture;
out.earlyPress = press();
c.video.readyState = 4; c.video.videoWidth = 0;            // sound before picture: still no frame
cuboaiGuardPipButton(c); out.soundOnlyPress = press();
c.video.videoWidth = 1920;
out.ready = cuboaiGuardPipButton(c); out.lit = btn.style.opacity; out.readyPress = press();
cuboaiGuardPipButton(c); out.added = btn.added;
c.video.readyState = 0; c.video.videoWidth = 0;            // the player is switching source
cuboaiGuardPipButton(c); out.dimAgain = btn.style.opacity;
const none = makeCard(); out.noButton = cuboaiGuardPipButton(none); out.empty = cuboaiGuardPipButton({});
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["early"] is False and got["dim"] == "0.35" and got["capture"] is True
    assert got["earlyPress"] == [True, True], "a press before the first frame reached the player"
    assert got["soundOnlyPress"] == [True, True], "a press with sound but no picture reached the player"
    assert got["ready"] is True and got["lit"] == "" and got["readyPress"] == [False, False], (
        "a press with a picture was swallowed"
    )
    assert got["added"] == 1, "the listener was added again"
    assert got["dimAgain"] == "0.35", "the button stayed lit while the player had no picture"
    assert got["noButton"] is None and got["empty"] is None


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
