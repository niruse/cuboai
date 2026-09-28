"""The speaker follows the card's audio setting ("Initial Audio State").

Reported 2026-09-28: "Need verify speakers works with the default setting set in
card". Since v2.4.0 the card started every player muted and brought the sound
up on the first interaction — but that unmute is switched off on Apple WebKit
(the native speaker owns mute there), so on an iPhone the setting was ignored:
the camera always opened muted, even on "Always Unmuted" or a remembered
unmute. On Apple the setting is now the starting state, as in v2.3.x. Every
test names the mutation it kills.
"""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

CARD = Path(__file__).resolve().parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js"
APPLE = "Apple Computer, Inc."
GOOGLE = "Google Inc."

needs_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _src() -> str:
    return CARD.read_text(encoding="utf-8")


def _helpers() -> str:
    src = _src()
    return src[src.index("function cuboaiWantUnmuted(") : src.index("// Single source of truth for the webrtc-camera")]


def _node(body: str, tmp_path) -> dict:
    script = tmp_path / "speaker.js"
    script.write_text(_helpers() + body, encoding="utf-8")
    return json.loads(subprocess.run(["node", str(script)], capture_output=True, text=True, check=True).stdout)


@needs_node
def test_what_each_audio_setting_asks_for(tmp_path):
    """Kill: 'unmuted'/'muted' not fixed, 'remember' ignoring the shared
    (cross-device) choice or letting this browser's copy override it, or a
    first visit (nothing saved) starting with sound."""
    got = _node(
        """
const W = cuboaiWantUnmuted;
console.log(JSON.stringify({
  unmuted: [W('unmuted', true, 'true'), W('unmuted', undefined, null)],
  muted: [W('muted', false, 'false'), W('muted', undefined, null)],
  shared_sound: W('remember', false, 'true'),
  shared_silence: W('remember', true, 'false'),
  saved_sound: W('remember', undefined, 'false'),
  saved_silence: W('remember', undefined, 'true'),
  first_visit: W('remember', undefined, null),
}));
""",
        tmp_path,
    )
    assert got["unmuted"] == [True, True]
    assert got["muted"] == [False, False]
    assert got["shared_sound"] is True and got["shared_silence"] is False, "the other devices' choice lost"
    assert got["saved_sound"] is True and got["saved_silence"] is False
    assert got["first_visit"] is False


@needs_node
def test_an_iphone_starts_in_the_state_the_setting_asks_for(tmp_path):
    """Kill: Apple started muted regardless (the v2.4.0-v2.6.44 bug: the
    setting ignored on an iPhone), or the muted start dropped elsewhere (the
    speaker button vanished when a browser blocked unmuted autoplay)."""
    got = _node(
        f"""
const S = cuboaiStartMuted;
console.log(JSON.stringify({{
  apple_sound: S(true, {APPLE!r}), apple_silence: S(false, {APPLE!r}),
  other_sound: S(true, {GOOGLE!r}), other_silence: S(false, {GOOGLE!r}),
  no_vendor: S(true, undefined),
}}));
""",
        tmp_path,
    )
    assert got["apple_sound"] is False, "an iPhone that should have sound starts muted"
    assert got["apple_silence"] is True
    assert got["other_sound"] is True and got["other_silence"] is True
    assert got["no_vendor"] is True


def test_the_card_starts_the_player_from_the_setting():
    """Kill: the helpers bypassed (a hard-coded muted start), or the setting's
    default changed from 'remember'."""
    src = _src()
    setup = src[src.index("const savedMuted = localStorage.getItem(`cuboai_muted_${deviceId}`);") :][:900]
    assert "const defaultMuteState = this._config?.default_mute_state || 'remember';" in setup
    assert (
        "const wantUnmuted = cuboaiWantUnmuted(defaultMuteState, this._getSharedSetting(deviceId, 'muted'), savedMuted);"
        in setup
    )
    assert "this.isMuted = cuboaiStartMuted(wantUnmuted, navigator.vendor);" in setup
    assert not re.search(r"this\.isMuted = true;\s*this\._wantUnmuted", src), "a hard-coded muted start is back"


def test_the_starting_state_reaches_the_player():
    """webrtc-camera only ever sets `muted` from its config (`if
    (this.config.muted) this.video.muted = true;`), so the card's starting state
    must be what it passes. Kill: the config built without the card's mute."""
    src = _src()
    cfg = src[src.index("function cuboaiWebrtcConfig(") :][:1500]
    assert "muted: isMuted," in cfg
    assert src.count("cuboaiWebrtcConfig(found, this.isMuted)") >= 2
