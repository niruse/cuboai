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

def _run_volume_cmd(volume, timer, sched_volume=70, sched_timer=1800):
    """Drive _execute_lullaby_cmd's 'volume' branch and capture the frame built."""
    client = MagicMock()
    client.get_lullaby_schedule.return_value = MagicMock(volume=sched_volume, timer_mode=sched_timer)
    sess = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=sess)
    ctx.__exit__ = MagicMock(return_value=False)
    seen = {}

    def _build(vol, tmr, correlation_id=0):
        seen["volume"], seen["timer"] = vol, tmr
        return 2438, b""

    with patch("custom_components.cuboai.tutk.cuboai_session.get_session", return_value=ctx), \
         patch("custom_components.cuboai.tutk.cuboai_messages.CuboAIClient", return_value=client), \
         patch("custom_components.cuboai.tutk.cuboai_messages.build_set_lullaby_vol_duration", _build):
        media_player._execute_lullaby_cmd(
            "uid", "acct", "pw", "192.0.2.10", "volume", None, volume, timer
        )
    return seen


def test_setting_volume_alone_preserves_the_cameras_timer():
    seen = _run_volume_cmd(volume=80, timer=None, sched_timer=1800)
    assert seen["volume"] == 80
    assert seen["timer"] == 1800, "changing the volume must not reset the sleep timer"


def test_setting_the_timer_alone_preserves_the_cameras_volume():
    seen = _run_volume_cmd(volume=None, timer=60, sched_volume=70)
    assert seen["timer"] == 3600
    assert seen["volume"] == 70, "changing the timer must not reset the volume"


def test_zero_minutes_still_means_repeat_forever():
    seen = _run_volume_cmd(volume=None, timer=0, sched_timer=1800)
    assert seen["timer"] == 0


def test_both_supplied_are_both_honoured():
    seen = _run_volume_cmd(volume=25, timer=30)
    assert (seen["volume"], seen["timer"]) == (25, 1800)


def test_unreadable_schedule_still_writes_something_sane():
    client = MagicMock()
    client.get_lullaby_schedule.side_effect = RuntimeError("timeout")
    sess = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=sess)
    ctx.__exit__ = MagicMock(return_value=False)
    seen = {}

    def _build(vol, tmr, correlation_id=0):
        seen["volume"], seen["timer"] = vol, tmr
        return 2438, b""

    with patch("custom_components.cuboai.tutk.cuboai_session.get_session", return_value=ctx), \
         patch("custom_components.cuboai.tutk.cuboai_messages.CuboAIClient", return_value=client), \
         patch("custom_components.cuboai.tutk.cuboai_messages.build_set_lullaby_vol_duration", _build):
        media_player._execute_lullaby_cmd("uid", "acct", "pw", "192.0.2.10", "volume", None, 80, None)
    assert seen["volume"] == 80
    assert seen["timer"] == 0


# --- play must not reset the volume -----------------------------------------

def _run_play_cmd(volume, timer, sched_volume=70):
    client = MagicMock()
    client.get_lullaby_schedule.return_value = MagicMock(volume=sched_volume, timer_mode=0)
    sess = MagicMock()
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=sess)
    ctx.__exit__ = MagicMock(return_value=False)
    seen = {}

    def _build(vol, tmr, correlation_id=0):
        seen["volume"], seen["timer"] = vol, tmr
        return 2438, b""

    with patch("custom_components.cuboai.tutk.cuboai_session.get_session", return_value=ctx), \
         patch("custom_components.cuboai.tutk.cuboai_messages.CuboAIClient", return_value=client), \
         patch("custom_components.cuboai.tutk.cuboai_messages.build_set_lullaby_vol_duration", _build):
        media_player._execute_lullaby_cmd(
            "uid", "acct", "pw", "192.0.2.10", "play", "SOME-UUID", volume, timer
        )
    return seen


def test_play_keeps_the_cameras_volume_when_none_is_supplied():
    """Both callers pass volume=None; play must not silently reset it to 50."""
    seen = _run_play_cmd(volume=None, timer=None, sched_volume=70)
    assert seen["volume"] == 70


def test_play_honours_an_explicit_volume():
    seen = _run_play_cmd(volume=20, timer=None, sched_volume=70)
    assert seen["volume"] == 20


def test_play_with_no_timer_is_repeat_forever():
    """The card path relies on this: HA enforces the duration and sends the stop."""
    seen = _run_play_cmd(volume=None, timer=None)
    assert seen["timer"] == 0


def test_play_converts_minutes_to_the_cameras_seconds_encoding():
    seen = _run_play_cmd(volume=None, timer=30)
    assert seen["timer"] == 1800
