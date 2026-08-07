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
    aiohttp_client_mod.async_get_clientsession = MagicMock(
        side_effect=RuntimeError("no session in tests")
    )
    sys.modules.setdefault("homeassistant.components", components)
    sys.modules.setdefault("homeassistant.components.camera", camera_mod)
    sys.modules.setdefault("homeassistant.helpers.update_coordinator", coordinator_mod)
    sys.modules.setdefault("homeassistant.helpers.aiohttp_client", aiohttp_client_mod)


_install_camera_mocks()

from custom_components.cuboai import camera as camera_platform  # noqa: E402

CARD = Path(__file__).parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js"


def _make_recording():
    coordinator = MagicMock()
    rec = camera_platform.CuboRecordingCamera(
        coordinator, {"device_id": "DEV1", "baby_name": "Mia"}
    )
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
        line for line in CARD.read_text(encoding="utf-8").splitlines()
        if not line.strip().startswith("//")
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
        sites = [i for i, line in enumerate(lines)
                 if "this.content.setConfig(webrtcConfig)" in line]
        assert len(sites) == 4, f"call sites moved: {sites}"
        for i in sites:
            # The guard sits on the call line or just above it -- two of these
            # retarget the config, two skip the call outright.
            window = "\n".join(lines[max(0, i - 3):i + 1])
            assert "_dvrPlaying" in window, lines[i]

    def test_there_is_a_way_back_to_live(self):
        """Dragging the playhead onto the right-hand edge is unhittable on a
        phone, so returning to live cannot be the only exit."""
        code = _card_code()
        assert "const goLive" in code
        assert "liveBtn.addEventListener('click', goLive)" in code

    def test_the_bar_covers_the_whole_retention(self):
        """The camera keeps ~72h. A 12h bar left five sixths of the recording
        unreachable, and the bar is the only way in."""
        code = _card_code()
        hours = re.search(r"Number\(this\._config\.timeline_hours\) \|\| (\d+)", code)
        assert hours and int(hours.group(1)) >= 72, hours and hours.group(1)

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
    two = dict(one, **{
        "camera.leo_local_camera": _live("DEV2"),
        "camera.leo_recording": _rec("DEV2"),
    })
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
        capture_output=True, text=True, timeout=60,
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
