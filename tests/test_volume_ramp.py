"""--volume-ramp SECS fades the lullaby volume to --volume instead of jumping.

The CLI reads the current level (GET_LULLABY_SCHEDULE), then steps toward the target on a
wall-clock schedule, so the ramp finishes in about SECS however slowly the camera answers each
SET; --volume-ramp-step is the minimum spacing between SETs. Without a readable current level, or
when already at the target, it falls back to one direct SET.

Runs the real cuboai_validate.main() against a fake transport that records every volume SET.
"""

import importlib.util
import os
import sys
from types import SimpleNamespace

import pytest

_TUTK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk"))
if _TUTK not in sys.path:
    sys.path.insert(0, _TUTK)  # the CLI uses the tutk scripts' flat imports

_spec = importlib.util.spec_from_file_location(
    "cuboai_validate_ramp_under_test", os.path.join(_TUTK, "cuboai_validate.py")
)
cv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cv)

_UID = "A" * 20


class _Transport:
    def __init__(self, current):
        self.current = current
        self.session_hdr = b""

    def connect(self):
        return None

    def disconnect(self):
        return None

    def ioctl(self, io, payload):
        if io == 2440:  # GET_LULLABY_SCHEDULE
            if self.current is None:
                return (0, b"")
            return (cv.IOTYPE_USER_GET_LULLABY_SCHEDULE_RESP, b"\0" * 140)
        return (io | 1, b"")


def _run(monkeypatch, current, argv):
    sets = []
    monkeypatch.setattr(cv, "get_session", lambda *a, **k: _Transport(current))
    monkeypatch.setattr(
        cv.LullabySchedule, "parse", lambda data: SimpleNamespace(volume=current, timer_mode=cv.LULLABY_TIMER_REPEAT)
    )
    monkeypatch.setattr(cv, "build_set_lullaby_vol_duration", lambda v, t: sets.append(v) or (0x0990, b""))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "cuboai_validate.py",
            "--uid",
            _UID,
            "--account",
            "a",
            "--password",
            "p",
            "--camera-ip",
            "127.0.0.1",
            "--no-status",
            *argv,
        ],
    )
    for k in [k for k in os.environ if k.startswith("CUBOAI_")]:
        monkeypatch.delenv(k)
    cv.main()
    return sets


def test_ramp_steps_up_monotonically_to_the_target(monkeypatch):
    sets = _run(monkeypatch, 20, ["--volume", "30", "--volume-ramp", "0.4", "--volume-ramp-step", "0.05"])
    assert len(sets) >= 3
    assert sets == sorted(sets) and len(set(sets)) == len(sets)
    assert 20 < sets[0] and sets[-1] == 30


def test_ramp_steps_down(monkeypatch):
    sets = _run(monkeypatch, 40, ["--volume", "30", "--volume-ramp", "0.4", "--volume-ramp-step", "0.05"])
    assert sets == sorted(sets, reverse=True) and sets[-1] == 30


def test_already_at_target_sets_once(monkeypatch):
    assert _run(monkeypatch, 30, ["--volume", "30", "--volume-ramp", "0.4"]) == [30]


def test_unknown_current_level_sets_directly(monkeypatch):
    assert _run(monkeypatch, None, ["--volume", "30", "--volume-ramp", "0.4"]) == [30]


def test_without_ramp_it_is_one_set(monkeypatch):
    assert _run(monkeypatch, 20, ["--volume", "30"]) == [30]


@pytest.mark.parametrize(
    "argv,msg",
    [
        (["--volume-ramp", "2"], "--volume-ramp requires --volume"),
        (["--volume", "30", "--volume-ramp", "0"], "--volume-ramp must be > 0"),
        (["--volume", "30", "--volume-ramp", "2", "--volume-ramp-step", "0"], "--volume-ramp-step must be > 0"),
    ],
)
def test_bad_arguments_are_rejected(monkeypatch, capsys, argv, msg):
    with pytest.raises(SystemExit) as e:
        _run(monkeypatch, 20, argv)
    assert e.value.code == 2 and msg in capsys.readouterr().err
