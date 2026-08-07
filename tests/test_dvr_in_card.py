"""Playback belongs in the card you already have.

The DVR work added a second camera entity for recorded footage, which meant
adding a second card to a dashboard to watch it -- so the card that shows the
baby and the card that shows the baby ten minutes ago were different cards.
The scrub bar now repoints the picture that is already on screen.

Two things have to hold for that, and both are guarded here:

- the recording entity publishes ``device_id`` and ``dvr`` so the card can pair
  it with the live camera by ATTRIBUTE. Issue #89 is what happens when a card
  identifies a CuboAI camera by its entity id instead, and this entity has the
  same naming problem: no ``_attr_has_entity_name``, so the id is
  ``camera.<baby>_recording`` with no ``cuboai_`` anywhere in it.
- nothing re-applies the live config while a recording is playing. FOUR call
  sites push the live entity back into the child, and any one of them would
  drop the viewer back to now mid-scrub. This test found the fourth: the first
  build of this feature guarded three and missed the one that creates the
  picture, which fires when a dashboard is navigated away from and back.
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest


class _FakeCoordinatorEntity:
    def __init__(self, coordinator):
        self.coordinator = coordinator


class _FakeCamera:
    def __init__(self):
        pass


def _install_camera_mocks():
    components = ModuleType("homeassistant.components")
    camera_mod = ModuleType("homeassistant.components.camera")
    camera_mod.Camera = _FakeCamera
    camera_mod.CameraEntityFeature = MagicMock()
    camera_mod.StreamType = MagicMock()
    coordinator_mod = ModuleType("homeassistant.helpers.update_coordinator")
    coordinator_mod.CoordinatorEntity = _FakeCoordinatorEntity
    aiohttp_client_mod = ModuleType("homeassistant.helpers.aiohttp_client")
    aiohttp_client_mod.async_get_clientsession = MagicMock(side_effect=RuntimeError("no session in tests"))
    sys.modules.setdefault("homeassistant.components", components)
    sys.modules.setdefault("homeassistant.components.camera", camera_mod)
    sys.modules.setdefault("homeassistant.helpers.update_coordinator", coordinator_mod)
    sys.modules.setdefault("homeassistant.helpers.aiohttp_client", aiohttp_client_mod)


_install_camera_mocks()

from custom_components.cuboai import camera as camera_platform  # noqa: E402

CARD = Path(__file__).parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js"


def _make_recording():
    coordinator = MagicMock()
    rec = camera_platform.CuboRecordingCamera(coordinator, {"device_id": "DEV1", "baby_name": "Mia"})
    rec.hass = MagicMock()
    rec.async_write_ha_state = MagicMock()
    return rec


def _card_code():
    """The card without comment lines.

    Comments here legitimately quote the strings the guards forbid while
    explaining why -- judging them too is how a guard on this project once
    failed against its own explanation.
    """
    return "\n".join(
        line for line in CARD.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("//")
    )


# =============================================================================
# The attribute contract the pairing depends on
# =============================================================================


class TestRecordingAttributeContract:
    def test_publishes_what_the_card_pairs_on(self):
        attrs = _make_recording().extra_state_attributes
        assert attrs["dvr"] is True
        assert attrs["device_id"] == "DEV1"
        assert attrs["uid"] == attrs["device_id"]

    def test_device_id_is_there_before_anything_is_playing(self):
        """The card has to find this entity in order to start playback, so the
        pairing keys cannot appear only once playback is under way."""
        rec = _make_recording()
        assert rec._source is None
        attrs = rec.extra_state_attributes
        assert attrs["device_id"] == "DEV1" and attrs["dvr"] is True
        assert attrs["playing_from"] is None

    def test_the_entity_is_available_before_anything_is_playing(self):
        """This is the one that matters, and asserting the attributes dict was
        not enough to catch it.

        Home Assistant does not publish state attributes for an entity that
        reports unavailable. Reporting unavailable until a moment is requested
        therefore hid `device_id` and `dvr` from the frontend entirely, so the
        card could not find the entity it needs in order to start the playback
        that would have made it available. The Python property below returned
        the right dict the whole time; the live entity showed `unavailable`
        with `attributes: {}`.
        """
        rec = _make_recording()
        assert rec._source is None
        assert rec.available is True

    def test_playing_from_is_reported_once_set(self):
        rec = _make_recording()
        rec.set_playback("rtsp://127.0.0.1:8554/x", 1_770_000_000)
        attrs = rec.extra_state_attributes
        assert attrs["playing_from"].startswith("2026-")
        assert attrs["device_id"] == "DEV1"


# =============================================================================
# Static guards on the card
# =============================================================================


class TestCardKeepsPlaybackInPlace:
    def test_pairs_by_attribute_never_by_entity_id(self):
        code = _card_code()
        assert "cuboaiFindRecordingState" in code
        assert "attrs.dvr !== true" in code
        # The #89 mistake, in either spelling.
        assert "camera.cuboai_" not in code
        # Not a substring check on "_recording": the SERVICE is called
        # play_recording, and an earlier version of this guard failed on that.
        assert "endsWith('_recording')" not in code
        assert 'startsWith("camera.cuboai' not in code

    def test_the_picture_is_repointed_rather_than_a_second_card_required(self):
        code = _card_code()
        # The child element's config is rewritten in place -- that IS the fix.
        assert "const showEntity" in code
        assert "this.content.setConfig(cfg)" in code

    def test_nothing_snaps_back_to_live_while_a_recording_plays(self):
        """Each of these re-applies the LIVE entity to the child. Any one left
        unguarded ends playback the moment the config is touched."""
        lines = _card_code().splitlines()
        sites = [i for i, line in enumerate(lines) if "this.content.setConfig(webrtcConfig)" in line]
        assert len(sites) == 4, f"call sites moved: {sites}"
        for i in sites:
            # The guard sits on the call line or just above it -- two of these
            # retarget the config, two skip the call outright.
            window = "\n".join(lines[max(0, i - 3) : i + 1])
            assert "_dvrPlaying" in window, lines[i]

    def test_there_is_a_way_back_to_live(self):
        """Dragging the playhead onto the right-hand edge is unhittable on a
        phone, so returning to live cannot be the only exit."""
        code = _card_code()
        assert "const goLive" in code
        assert "liveBtn.addEventListener('click', goLive)" in code

    def test_the_bar_matches_what_the_camera_actually_keeps(self):
        """Measured, not taken from the docs.

        Probed against a real camera on 2026-08-07, one request an hour: 48h
        back returns frames, 56h does not. The service description claims
        "about 72 hours", and a 72h bar is a third dead space that fails
        silently when you scrub into it -- while a 12h bar, which is what this
        started as, hid three quarters of what is there.
        """
        code = _card_code()
        hours = re.search(r"Number\(this\._config\.timeline_hours\) \|\| (\d+)", code)
        assert hours, "the span default is gone"
        span = int(hours.group(1))
        assert 24 <= span <= 48, f"span is {span}h; 48h is the measured edge"

    def test_a_moment_can_be_typed_not_only_aimed_at(self):
        """At phone width the bar is ~366px across two days -- about eight
        minutes per pixel, which cannot express "02:14"."""
        code = _card_code()
        assert "datetime-local" in code
        assert "const goToPicked" in code
        # Wired up, not merely declared: deleting the listener left a Go button
        # that did nothing and every other assertion here still passed.
        assert "go.addEventListener('click', goToPicked)" in code
        # Bounded by what the camera still holds, or it offers moments that
        # seek into nothing and time out after 30 seconds.
        assert "when.min = asLocalInput" in code and "when.max = asLocalInput" in code
        assert "Older than the camera still holds" in code

    def test_seconds_are_reachable_without_a_native_seconds_picker(self):
        """iOS renders datetime-local as a wheel with hours and minutes and no
        seconds, whatever `step` says, so `step=1` alone does not give anyone
        on a phone second-level control."""
        code = _card_code()
        assert "const nudge" in code
        for label in ("'-1m'", "'-10s'", "'+10s'", "'+1m'"):
            assert label in code, label
        assert "b.addEventListener('click', () => nudge(secs))" in code
        # On screen, not merely constructed: dropping the append left every
        # other assertion here passing and no buttons in the card.
        assert "bar.appendChild(fine)" in code
        # Four taps must be one seek: each call restarts a producer that takes
        # seconds to connect and seek. Pinned as a pair, because asserting
        # clearTimeout on its own was satisfied by the one in
        # disconnectedCallback while the debounce itself was gone.
        assert "clearTimeout(this._dvrNudge);\n          this._dvrNudge = setTimeout" in code

    def test_playback_works_on_a_card_with_no_device_id(self):
        """Regression, self-inflicted: splitting commit() into playFrom()
        dropped the `!dev` guard, so an unpinned card asked the service for
        device_id "" and got "No CuboAI camera with device_id ''". The card
        already resolved an entity to put a picture on screen; the id comes
        from that entity's attributes."""
        code = _card_code()
        assert "const pinned = (this._config || {}).device_id;" in code
        assert "attrsOf(rec).device_id || attrsOf(liveCam).device_id" in code
        assert "'No CuboAI camera found'" in code

    def test_the_picker_and_the_playhead_share_one_path(self):
        """Two ways in must not become two implementations of playback."""
        code = _card_code()
        assert "const playFrom" in code
        assert code.count("play_recording'") == 1, "playback is requested from more than one place"

        seconds = re.search(r"Number\(this\._config\.timeline_play_seconds\) \|\| (\d+)", code)
        assert seconds and int(seconds.group(1)) >= 600, seconds and seconds.group(1)

    def test_readiness_waits_for_the_seek_not_for_the_entity(self):
        """The entity is always available -- it has to be, or its attributes
        never reach the frontend. So its state says nothing about whether the
        producer has connected and seeked yet, and waiting on availability
        would pass instantly and swap the card to a black picture."""
        code = _card_code()
        assert "st.attributes.playing_from" in code
        assert "st.state !== 'unavailable'" not in code

    def test_tick_spacing_adapts_to_the_span(self):
        """A fixed 15-minute tick is 288 ticks across three days."""
        code = _card_code()
        assert "STEPS.find" in code and "MIN_PX" in code


# =============================================================================
# The matcher itself, executed in Node
# =============================================================================

_HARNESS = """
const fs = require('fs');
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => undefined, define: () => {} };
globalThis.window = globalThis;
globalThis.document = { createElement: () => ({ style: {}, appendChild(){}, addEventListener(){} }) };
const src = fs.readFileSync(process.argv[2], 'utf8');
const find = new Function(src + ';return cuboaiFindRecordingState;')();
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
console.log(JSON.stringify(cases.map((c) => {
  const hit = find(c.hass, c.deviceId);
  return hit ? hit.entityId : null;
})));
"""


def _live(device_id):
    return {"attributes": {"device_id": device_id, "uid": device_id, "rtsp_port": 8557}}


def _rec(device_id):
    return {"attributes": {"device_id": device_id, "uid": device_id, "dvr": True}}


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_recording_matcher(tmp_path):
    card = tmp_path / "card.js"
    card.write_text(CARD.read_text(encoding="utf-8"), encoding="utf-8")
    harness = tmp_path / "h.js"
    harness.write_text(_HARNESS, encoding="utf-8")

    one = {"camera.mia_local_camera": _live("DEV1"), "camera.mia_recording": _rec("DEV1")}
    two = dict(
        one,
        **{
            "camera.leo_local_camera": _live("DEV2"),
            "camera.leo_recording": _rec("DEV2"),
        },
    )
    cases = [
        # Paired by device_id.
        {"hass": {"states": one}, "deviceId": "DEV1"},
        # Renamed entity: the pairing is on attributes, so it still holds.
        {"hass": {"states": {"camera.zzz": _rec("DEV1")}}, "deviceId": "DEV1"},
        # Two cameras, and the card is pinned to the second one.
        {"hass": {"states": two}, "deviceId": "DEV2"},
        # Two cameras and no pin: guessing would show the wrong baby.
        {"hass": {"states": two}, "deviceId": None},
        # One camera and no pin: unambiguous.
        {"hass": {"states": one}, "deviceId": None},
        # The live camera must never be mistaken for the recording one.
        {"hass": {"states": {"camera.mia_local_camera": _live("DEV1")}}, "deviceId": "DEV1"},
        # An install that has not been reloaded yet.
        {"hass": {"states": {}}, "deviceId": "DEV1"},
        {"hass": None, "deviceId": "DEV1"},
    ]
    payload = tmp_path / "c.json"
    payload.write_text(json.dumps(cases), encoding="utf-8")

    proc = subprocess.run(
        [shutil.which("node"), str(harness), str(card), str(payload)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr

    assert json.loads(proc.stdout) == [
        "camera.mia_recording",
        "camera.zzz",
        "camera.leo_recording",
        None,
        "camera.mia_recording",
        None,
        None,
        None,
    ]


_SPANS_HARNESS = """
const fs = require('fs');
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => undefined, define: () => {} };
globalThis.window = globalThis;
globalThis.document = { createElement: () => ({ style:{}, appendChild(){}, setAttribute(){}, addEventListener(){} }) };
new Function(fs.readFileSync(process.argv[2], 'utf8') + ';globalThis.__TL = CuboAITimelineCard;')();
const card = Object.create(globalThis.__TL.prototype);
const start = new Date(1000000 * 1000), end = new Date(1010000 * 1000);
// A matching reading that begins AFTER the window ends. Home Assistant bounds
// its own reply, so only a card fed a wider range than it asked for sees this.
const points = [
  { lu: 1000500, s: 'in crib' },
  { lu: 1001500, s: 'out' },
  { lu: 1020000, s: 'in crib' },
];
const spans = card._spans({ match: 'in crib' }, points, start, end);
console.log(JSON.stringify({
  spans: spans.map((x) => [x.from / 1000, x.to / 1000]),
  anyNegative: spans.some((x) => x.to <= x.from),
  anyPastEnd: spans.some((x) => x.to > end.getTime()),
}));
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_readings_outside_the_window_are_discarded_not_clamped(tmp_path):
    """Clamping one that begins AFTER the end closed its span at the end,
    running the span backwards. It surfaced as a lane reporting -35% coverage
    once the legend started showing each lane's share of the window.

    Two things now prevent it -- the loop stops at the window's end, and any
    span that still ends before it starts is filtered out -- and they are
    redundant: removing either alone leaves this passing, because the other
    still covers it. Only removing both fails, which is verified. That is the
    right level for this test to sit at: it asserts the outcome, not a line.
    """
    (tmp_path / "card.js").write_text(CARD.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "h.js").write_text(_SPANS_HARNESS, encoding="utf-8")
    proc = subprocess.run(
        [shutil.which("node"), str(tmp_path / "h.js"), str(tmp_path / "card.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    got = json.loads(proc.stdout)

    assert got["anyNegative"] is False, got["spans"]
    assert got["anyPastEnd"] is False, got["spans"]
    assert got["spans"] == [[1000500, 1001500]]
