"""Issue #118: a card with no picture and no error.

The picture is AlexxIT's ``webrtc-camera`` element, and the card only waits
for it with ``customElements.whenDefined``. When WebRTC Camera's JavaScript
never loads, that wait never ends: the camera is found, the scrub bar and the
badges are drawn, and there is simply no video -- the badges fall onto the
scrub bar. The card now says what is missing after a grace period, and the
message goes away by itself once the player arrives.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CARD = Path(__file__).parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js"

_HARNESS = """
const fs = require('fs');
globalThis.HTMLElement = class {};
globalThis.window = globalThis;
globalThis.customElements = { get: () => undefined, define: () => {}, whenDefined: () => new Promise(() => {}) };
globalThis.document = { createElement: () => ({ style: {} }) };
// The grace period is 10 s; run timers by hand instead of waiting.
let timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
globalThis.clearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].fn = null; };
const fire = () => { const due = timers; timers = []; due.forEach((t) => t.fn && t.fn()); };
new Function(fs.readFileSync(process.argv[2], 'utf8') +
  ';globalThis.__w = cuboaiWatchForPlayer; globalThis.__a = cuboaiPlayerArrived; globalThis.__ms = CUBOAI_PLAYER_WAIT_MS;')();

function makeCard() {
  const card = { children: [] };
  card.dvrBar = { parentNode: card, id: 'bar' };
  card.children.push(card.dvrBar);
  card.insertBefore = (el, ref) => {
    el.parentNode = card;
    el.remove = () => { card.children = card.children.filter((c) => c !== el); };
    const i = ref ? card.children.indexOf(ref) : -1;
    if (i < 0) card.children.push(el); else card.children.splice(i, 0, el);
  };
  return card;
}
const missing = { get: () => undefined };
const loaded = { get: () => class {} };
const doc = { createElement: () => ({ style: {} }) };
const out = {};

// Player never loads: one message, above the scrub bar, after the grace period.
let card = makeCard();
__w(card, missing, doc);
out.waitMs = timers.length ? timers[0].ms : null;
out.constMs = __ms;
out.beforeGrace = card.children.length;
__w(card, missing, doc);          // set hass runs again and again
out.timersArmed = timers.length;
fire();
out.after = card.children.map((c) => c.id || 'notice');
out.text = card._noPlayerEl ? card._noPlayerEl.innerHTML : '';
__w(card, missing, doc); fire();  // re-armed later: still one message
out.afterRearm = card.children.length;

// The player turns up after the message: the message goes.
__a(card);
out.afterArrival = card.children.map((c) => c.id || 'notice');

// The player turns up inside the grace period: no message ever.
card = makeCard();
__w(card, missing, doc);
__a(card);
fire();
out.arrivedInTime = card.children.length;

// Already loaded, or the picture is already there: nothing is armed.
card = makeCard();
__w(card, loaded, doc);
card.content = {};
__w(card, missing, doc);
out.armedWhenLoaded = timers.length;

console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_missing_player_is_reported_not_silent(tmp_path):
    (tmp_path / "card.js").write_text(CARD.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "h.js").write_text(_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [shutil.which("node"), str(tmp_path / "h.js"), str(tmp_path / "card.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)

    assert got["waitMs"] == got["constMs"] == 10000
    assert got["beforeGrace"] == 1
    assert got["timersArmed"] == 1
    assert got["after"] == ["notice", "bar"]
    assert "WebRTC Camera" in got["text"]
    assert "HACS" in got["text"]
    assert "reload" in got["text"]
    assert got["afterRearm"] == 2
    assert got["afterArrival"] == ["bar"]
    assert got["arrivedInTime"] == 1
    assert got["armedWhenLoaded"] == 0


def test_the_wait_is_watched_where_the_picture_is_created():
    js = CARD.read_text(encoding="utf-8")
    create = js.index("this.content = document.createElement('webrtc-camera');")
    wait = js.rindex("customElements.whenDefined('webrtc-camera').then(() => {", 0, create)
    assert "cuboaiWatchForPlayer(this);" in js[wait - 200 : wait]
    assert "cuboaiPlayerArrived(this);" in js[wait:create]
    disconnected = js[js.index("disconnectedCallback() {") :]
    assert "clearTimeout(this._playerWait)" in disconnected[: disconnected.index("super.disconnectedCallback")]
