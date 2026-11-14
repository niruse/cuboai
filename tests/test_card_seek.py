"""The card's -15 / +15 s buttons under Now Playing seek inside the playing song.

The position HA reports is as of `media_position_updated_at`, so the card adds the time since
then; the target never goes before the start or past 1 s before the end (the song then plays
out and the next one follows, gaplessly). Lullabies and songs without a known length show no
controls. Every test names the mutation it kills.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CARD = Path(__file__).resolve().parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _helpers():
    src = CARD.read_text(encoding="utf-8")
    return src[
        src.index("function cuboaiSpeakerPosition") : src.index("// The collection song's name for a playing URL")
    ]


def _run(body):
    out = subprocess.run(
        ["node", "-"], input=_helpers() + body, capture_output=True, text=True, encoding="utf-8", check=True
    )
    return json.loads(out.stdout)


AT = "2026-11-14T12:00:00+00:00"
T0 = "Date.parse('2026-11-14T12:00:00Z')"


def test_the_position_runs_on_from_when_ha_last_reported_it():
    """30 s at the report, 4.5 s later: 34.5 s. Kill: ignore media_position_updated_at."""
    r = _run(
        f"""
const a = {{media_position: 30, media_duration: 96, media_position_updated_at: '{AT}'}};
console.log(JSON.stringify([cuboaiSpeakerPosition(a, {T0} + 4500), cuboaiSpeakerPosition(a, {T0} + 999000)]));
"""
    )
    assert r == [34.5, 96]


def test_no_position_without_a_known_length():
    """A lullaby (no position) or an unknown length: no controls. Kill: show them anyway."""
    r = _run(
        f"""
console.log(JSON.stringify([
  cuboaiSpeakerPosition({{}}, {T0}),
  cuboaiSpeakerPosition({{media_position: 3, media_duration: null}}, {T0}),
  cuboaiSpeakerPosition(null, {T0}),
  cuboaiSeekTarget({{}}, {T0}, 15),
]));
"""
    )
    assert r == [None, None, None, None]


def test_seek_targets_stay_inside_the_song():
    """+15 / -15 from the live position, clamped to [0, length - 1].

    Kill: no clamp at the start, or at the end (a seek past the end)."""
    r = _run(
        f"""
const a = (p) => ({{media_position: p, media_duration: 96, media_position_updated_at: '{AT}'}});
console.log(JSON.stringify([
  cuboaiSeekTarget(a(30), {T0} + 2000, 15),
  cuboaiSeekTarget(a(30), {T0} + 2000, -15),
  cuboaiSeekTarget(a(5), {T0}, -15),
  cuboaiSeekTarget(a(90), {T0}, 15),
]));
"""
    )
    assert r == [47, 17, 0, 95]


def test_time_is_shown_as_minutes_and_seconds():
    """Kill: no zero padding (1:5 instead of 1:05)."""
    r = _run("console.log(JSON.stringify([cuboaiFmtTime(65.9), cuboaiFmtTime(0), cuboaiFmtTime(600)]));")
    assert r == ["1:05", "0:00", "10:00"]


def test_the_buttons_call_media_seek_and_lullabies_get_no_controls():
    """Wiring: the buttons seek the speaker by 15 s; a lullaby passes null (no controls); the
    tick is cleared when the card leaves the page.

    Kill: unbind a button, show controls for a lullaby, or leak the 1 s timer."""
    src = CARD.read_text(encoding="utf-8")
    assert "seekBackBtn.addEventListener('click', () => seekBy(-15))" in src
    assert "seekFwdBtn.addEventListener('click', () => seekBy(15))" in src
    assert "callService('media_player', 'media_seek'" in src
    # the bar: drag previews, release seeks, the tick never fights a finger on it
    assert "seekBar.addEventListener('input'" in src and "seekBar.addEventListener('change'" in src
    assert "if (this._seekDragging) return true;" in src
    assert 'id="nowLeft"' in src and "'-' + cuboaiFmtTime(Math.max(0, dur - pos))" in src
    assert "this._updateSeekControls(isLullabyPlaying ? null : speakerState);" in src
    assert "if (this._seekTick) { clearInterval(this._seekTick); this._seekTick = null; }\n" in src.replace(
        "\r\n", "\n"
    )
