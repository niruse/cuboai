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
    box behind the page: a 2 px transparent one was measured to leave the window black. Its
    place in the card is kept by a marker, not by its neighbour: the card moves its overlays
    about every tick, and the picture once came back at the bottom of the card.

    Kill: leave the player in the detached card; park a card that is on the page, not floating,
    or playing a recording; hide the holder with display:none or opacity; put the player back
    next to a neighbour instead of at its marker; leave the marker behind; or never put it back."""
    got = _node(
        """
const out = {};
const mk = () => {
  const c = makeCard();
  const body = { kids: [], appendChild(n) { this.kids.push(n); n.parentNode = this; } };
  doc.body = body;
  doc.createElement = () => ({ style: {}, kids: [], appendChild(n) { this.kids.push(n); n.parentNode = this; },
    remove() { body.kids = body.kids.filter(k => k !== this); this.removed = true; } });
  c.kids = [c.player, { name: 'overlay' }, { name: 'music' }];
  c.kids.forEach(k => { k.parentNode = c; });
  doc.createComment = () => ({ mark: true, remove() { c.kids = c.kids.filter(k => k !== this); this.parentNode = null; } });
  Object.defineProperty(c, 'firstChild', { get() { return c.kids[0] || null; } });
  c.insertBefore = (n, ref) => { c.kids = c.kids.filter(k => k !== n); c.kids.splice(c.kids.indexOf(ref), 0, n); n.parentNode = c; };
  c.appendChild = (n) => { c.kids = c.kids.filter(k => k !== n); c.kids.push(n); n.parentNode = c; };
  c.order = () => c.kids.map(k => (k === c.player ? 'player' : k.mark ? 'mark' : k.name));
  c.video.webkitPresentationMode = 'picture-in-picture';
  return { c, body };
};
const leave = (c, body) => { body.kids[0].appendChild(c.player); c.kids = c.kids.filter(k => k !== c.player); };
let { c, body } = mk();
out.onPage = cuboaiParkFloating(c);                       // the card is on the page: nothing to do
c.isConnected = false;
out.parked = cuboaiParkFloating(c); leave(c, body);
out.holderCss = body.kids[0].style.cssText;
out.inHolder = c.player.parentNode === body.kids[0];
out.whileAway = c.order();
c.kids = [c.kids[2], c.kids[0], c.kids[1]];               // the card shuffles its other children meanwhile
out.again = cuboaiParkFloating(c);                         // already parked, still floating
c.isConnected = true;
out.back = cuboaiParkFloating(c); out.order = c.order(); out.holderGone = body.kids.length === 0;

({ c, body } = mk()); c.isConnected = false; cuboaiParkFloating(c); leave(c, body);
c.video.webkitPresentationMode = 'inline';                 // the window was closed, the card still away
out.closed = cuboaiParkFloating(c); out.closedOrder = c.order();

({ c, body } = mk()); c.isConnected = false; c.video.webkitPresentationMode = 'inline';
out.notFloating = cuboaiParkFloating(c);
({ c, body } = mk()); c.isConnected = false; c._dvrPlaying = true;
out.recording = cuboaiParkFloating(c);
({ c, body } = mk()); c.isConnected = false; cuboaiParkFloating(c); leave(c, body);
c.kids = c.kids.filter(k => { if (k.mark) k.parentNode = null; return !k.mark; });   // the marker was lost
c.isConnected = true; cuboaiParkFloating(c); out.noMark = c.order();
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
    assert got["whileAway"] == ["mark", "overlay", "music"], "no marker was left where the player was"
    assert got["again"] is None, "a parked player was moved again"
    assert got["back"] == "unparked" and got["holderGone"], "the player was not put back"
    assert got["order"] == ["music", "player", "overlay"], (
        "the player did not go back to its marker, or the marker stayed"
    )
    assert got["closed"] == "unparked" and got["closedOrder"] == ["player", "overlay", "music"], (
        "a closed window left it parked"
    )
    assert got["notFloating"] is None and got["recording"] is None, "parked a player that is not a live floating window"
    assert got["noMark"] == ["player", "overlay", "music"], (
        "with its marker lost the player was not put first in the card"
    )
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


def test_a_new_card_takes_in_the_card_whose_video_is_floating(tmp_path):
    """Measured 2026-11-21 on an iPhone: back on the dashboard with the floating window open,
    Home Assistant's new card started a second stream, so the picture showed twice, and that
    stream had no frame for 8 s. The new card now finds the old card (off the page, its player
    parked for the floating window, same configuration) and hosts it instead.

    Kill: start a second player next to a floating one; take in a card for another camera, a
    card that is on the page, one whose window is closed, or one that is not parked; or take a
    card in twice."""
    got = _node(
        """
const out = {};
const mk = (cfg, o = {}) => { const c = makeCard(); c.content = o.noContent ? null : c.player; c._config = cfg;
  c.isConnected = !!o.onPage; c._parked = o.parked === false ? null : {}; if (o.noContent) c._parked = null;
  c.video.webkitPresentationMode = o.closed ? 'inline' : 'picture-in-picture'; cuboaiRegisterCard(c); return c; };
const fresh = (cfg) => { const c = makeCard(); c.content = null; c._config = cfg; cuboaiRegisterCard(c); return c; };
const A = { entity: 'camera.a' }, B = { entity: 'camera.b' };
const old = mk(A);
out.same = cuboaiFloatingHost(fresh({ entity: 'camera.a' })) === old;
out.otherCamera = cuboaiFloatingHost(fresh(B));
const busy = fresh(A); busy.content = {}; out.hasPlayer = cuboaiFloatingHost(busy);
const hosting = fresh(A); hosting._proxy = old; out.alreadyHosting = cuboaiFloatingHost(hosting);
old.isConnected = true; out.onPage = cuboaiFloatingHost(fresh(A)); old.isConnected = false;
old.video.webkitPresentationMode = 'inline'; out.closed = cuboaiFloatingHost(fresh(A));
old.video.webkitPresentationMode = 'picture-in-picture';
old._parked = null; out.notParked = cuboaiFloatingHost(fresh(A)); old._parked = {};
out.stillFound = cuboaiFloatingHost(fresh(A)) === old;
out.empty = cuboaiFloatingHost(null);
console.log(JSON.stringify(out));
""",
        tmp_path,
    )
    assert got["same"] and got["stillFound"], "the card with the floating video was not found"
    assert got["otherCamera"] is None, "took in a card for another camera"
    assert got["hasPlayer"] is None and got["alreadyHosting"] is None, (
        "a card that already has a picture took another in"
    )
    assert got["onPage"] is None, "took in a card that is on the page"
    assert got["closed"] is None and got["notParked"] is None, "took in a card whose video is not in a floating window"
    assert got["empty"] is None


def test_the_card_hosts_the_floating_card_before_it_builds_a_player():
    """Wiring: the check comes before the card builds anything, a hosting card passes Home
    Assistant's updates and configuration on, and the hosted card's player goes back at once.

    Kill: build a player first; stop passing hass on; or leave the player parked."""
    src = _src()
    cls = src.index("class CuboAICameraCard extends HTMLElement {")
    setter = src[src.index("  set hass(hass) {", cls) :]
    host = setter.index("const host = cuboaiFloatingHost(this);")
    assert setter.index("if (this._proxy) { this._proxy.hass = hass; return; }") < host
    assert host < setter.index("let deviceId = this._config?.device_id;")
    block = setter[host : host + 500]
    assert "this.appendChild(host);" in block and "cuboaiParkFloating(host)" in block and "host.hass = hass;" in block
    set_config = src[src.index("  setConfig(config) {", cls) :][:700]
    assert "this._proxy.setConfig(config);" in set_config
    # The hosted card gets its first update while it is still off the page: the timeline must not
    # be appended again then (it landed below the music, 2026-11-21).
    assert "if (this.dvrBar && this.dvrBar.parentNode !== this) this.appendChild(this.dvrBar);" in src
    assert "!this.dvrBar.isConnected" not in src


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
