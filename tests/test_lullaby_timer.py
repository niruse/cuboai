"""The Lullaby Timer must report what the camera is actually set to.

`CuboLullabyTimerNumber` used to return a purely local value: it started at a
hardcoded 30, was restored across reloads, and was never reconciled with the
camera. So a lullaby the camera was repeating indefinitely still showed as
"30 min" in Home Assistant — the mismatch that prompted this.

The camera reports its real sleep timer on GET_LULLABY_SCHEDULE (timer_mode @8:
0 = repeat forever, 1800 = 30 min, 3600 = 60 min), which the coordinator already
calls for the volume and was discarding. It now surfaces it, and the entity
reads it, keeping a locally-picked value as `pending` only until the camera
confirms — so a pick made before pressing play is not clobbered by the next poll.

Also covers the coupled-write hazard: SET_LULLABY_VOL_DURATION carries volume
AND timer in one struct, so supplying only one and defaulting the other to a
constant silently changed the field the user did not touch.
"""

import importlib
import importlib.util
import os
import struct
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# The real utils module, so @retry_camera_command stays a real decorator.
_uspec = importlib.util.spec_from_file_location(
    "custom_components.cuboai.utils", os.path.join(_ROOT, "custom_components", "cuboai", "utils.py")
)
_utils = importlib.util.module_from_spec(_uspec)
sys.modules["custom_components.cuboai.utils"] = _utils
_uspec.loader.exec_module(_utils)

for _m in ("custom_components.cuboai.number", "custom_components.cuboai.media_player"):
    sys.modules.pop(_m, None)
number = importlib.import_module("custom_components.cuboai.number")
media_player = importlib.import_module("custom_components.cuboai.media_player")


DEVICE = "SW05TESTDEVICE01"


def _entity(camera_local):
    e = object.__new__(number.CuboLullabyTimerNumber)
    e.coordinator = MagicMock()
    e.coordinator.data = {"cameras": {DEVICE: {"local": camera_local}}}
    e._device_id = DEVICE
    e._timer_value = 30          # the old hardcoded local default
    e._pending = None
    e.async_write_ha_state = lambda: None
    return e


def test_reports_repeat_when_the_camera_is_on_repeat():
    """The exact reported symptom: camera indefinite, HA said 30."""
    e = _entity({"lullaby_timer_minutes": 0, "lullaby_timer_name": "repeat"})
    assert e.native_value == 0
    assert e.extra_state_attributes["camera_timer_mode"] == "repeat"


def test_reports_the_cameras_thirty_minute_timer():
    e = _entity({"lullaby_timer_minutes": 30, "lullaby_timer_name": "30min"})
    assert e.native_value == 30


def test_falls_back_to_the_restored_local_value_when_the_camera_is_silent():
    e = _entity({})
    assert e.native_value == 30


def test_a_fresh_pick_survives_the_next_poll():
    """Selecting 60 while the camera still says repeat must show 60, not 0."""
    e = _entity({"lullaby_timer_minutes": 0})
    e._pending = 60
    assert e.native_value == 60
    assert e.extra_state_attributes["differs_from_camera"] is True


def test_pending_clears_once_the_camera_agrees():
    e = _entity({"lullaby_timer_minutes": 60})
    e._pending = 60
    assert e.native_value == 60
    assert e._pending is None, "pending should clear when the camera catches up"
    assert e.extra_state_attributes["differs_from_camera"] is False


def test_camera_value_wins_again_after_the_pending_clears():
    e = _entity({"lullaby_timer_minutes": 60})
    e._pending = 60
    assert e.native_value == 60                      # reading it clears pending
    e.coordinator.data["cameras"][DEVICE]["local"]["lullaby_timer_minutes"] = 0
    assert e.native_value == 0


# --- the coupled write -------------------------------------------------------
#
# SET_LULLABY_VOL_DURATION (2438) is the camera's ONLY write for the lullaby
# volume and sleep timer, and its 140-byte struct carries BOTH fields with no
# field mask — there is no volume-only or timer-only opcode. Every write is
# therefore a read-modify-write, and these tests decode the REAL payload the
# real builder produces rather than mocking it.

GET_SCHED_REQ, GET_SCHED_RESP, SET_VOL_DUR_REQ = 2440, 2441, 2438


def _echo(volume, timer_seconds):
    """A GET_LULLABY_SCHEDULE response: timer_mode @8, volume @12, playing @16."""
    raw = bytearray(144)
    struct.pack_into("<I", raw, 8, timer_seconds)
    struct.pack_into("<I", raw, 12, volume)
    struct.pack_into("<I", raw, 16, 1)
    return bytes(raw)


def _run_cmd(cmd_type, volume, timer, cam_volume=70, cam_timer=1800, echo_fails=False):
    """Drive _execute_lullaby_cmd and decode the SET payload it actually built."""
    sent = {}

    def _ioctl(op, payload):
        if op == GET_SCHED_REQ:
            if echo_fails:
                raise RuntimeError("timeout")
            return GET_SCHED_RESP, _echo(cam_volume, cam_timer)
        if op == SET_VOL_DUR_REQ:
            sent["timer"] = struct.unpack_from("<I", payload, 4)[0]
            sent["volume"] = struct.unpack_from("<I", payload, 8)[0]
            sent["len"] = len(payload)
        return op + 1, b""

    sess = MagicMock()
    sess.ioctl.side_effect = _ioctl
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=sess)
    ctx.__exit__ = MagicMock(return_value=False)

    with patch("custom_components.cuboai.tutk.cuboai_session.get_session", return_value=ctx), \
         patch("custom_components.cuboai.tutk.cuboai_messages.CuboAIClient", return_value=MagicMock()):
        media_player._execute_lullaby_cmd(
            "uid", "acct", "pw", "192.0.2.10", cmd_type, "SOME-UUID", volume, timer
        )
    return sent


def test_the_write_is_the_140_byte_coupled_struct():
    sent = _run_cmd("volume", volume=80, timer=None)
    assert sent["len"] == 140, "SET_LULLABY_VOL_DURATION is a fixed 140-byte struct"


def test_setting_volume_alone_preserves_the_cameras_timer():
    sent = _run_cmd("volume", volume=80, timer=None, cam_timer=1800)
    assert sent["volume"] == 80
    assert sent["timer"] == 1800, "changing the volume must not reset the sleep timer"


def test_setting_the_timer_alone_preserves_the_cameras_volume():
    sent = _run_cmd("volume", volume=None, timer=60, cam_volume=70)
    assert sent["timer"] == 3600
    assert sent["volume"] == 70, "changing the timer must not reset the volume"


def test_zero_minutes_still_means_repeat_forever():
    sent = _run_cmd("volume", volume=None, timer=0, cam_timer=1800)
    assert sent["timer"] == 0


def test_both_supplied_are_both_honoured():
    sent = _run_cmd("volume", volume=25, timer=30)
    assert (sent["volume"], sent["timer"]) == (25, 1800)


def test_minutes_are_converted_to_the_cameras_seconds_encoding():
    assert _run_cmd("volume", None, 30)["timer"] == 1800
    assert _run_cmd("volume", None, 60)["timer"] == 3600


def test_an_unreadable_echo_still_writes_the_supplied_field():
    sent = _run_cmd("volume", volume=80, timer=None, echo_fails=True)
    assert sent["volume"] == 80
    assert sent["timer"] == 0, "with no echo to preserve, the timer falls back to repeat"


# --- play must not reset the volume -----------------------------------------

def test_play_keeps_the_cameras_volume_when_none_is_supplied():
    """Both callers pass volume=None; play must not silently reset it to 50."""
    sent = _run_cmd("play", volume=None, timer=None, cam_volume=70)
    assert sent["volume"] == 70


def test_play_honours_an_explicit_volume():
    sent = _run_cmd("play", volume=20, timer=None, cam_volume=70)
    assert sent["volume"] == 20


def test_play_with_no_timer_is_repeat_forever():
    """The card path relies on this: HA enforces the duration and sends the stop."""
    sent = _run_cmd("play", volume=None, timer=None, cam_timer=1800)
    assert sent["timer"] == 0


def test_play_converts_minutes_to_the_cameras_seconds_encoding():
    sent = _run_cmd("play", volume=None, timer=30)
    assert sent["timer"] == 1800
