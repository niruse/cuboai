"""set_verified(): tell "applied", "accepted but not applied", "couldn't check" and "failed" apart.

A SET that returns a ..._RESP only proves the camera answered. set_verified(name, value)
performs the SET, reads the value back through the paired GET (via get_during_stream, so it
never races a running stream's socket) and reports one of four outcomes. Which setters can be
verified, and how, is the SET_READBACK table in cuboai_messages; setters that cannot be are
listed with the reason in SET_READBACK_UNSUPPORTED and raise instead of pretending.

No wire traffic: a stub session supplies the SET result and the read-back.
"""

import importlib.util
import inspect
import os
import sys

_TUTK = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk"))
if _TUTK not in sys.path:
    sys.path.insert(0, _TUTK)  # set_verified uses the tutk scripts' flat `import cuboai_messages`

import cuboai_messages as cm  # noqa: E402

_spec = importlib.util.spec_from_file_location("live_pure_set_verified", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

# Entries whose SET->GET round trip has been observed on real hardware (dated notes next to each
# in SET_READBACK). An entry may only claim 'live' if it is listed here.
LIVE_CONFIRMED = {"status_light", "light_brightness", "cry_sensitivity", "cough_sensitivity"}


class _Stub(cp.TUTKDirectSession):
    """No I/O: the SET records its call, the GET returns canned state."""

    def __init__(self, get_result, set_raises=False):
        self.calls = []
        self._get_result = get_result
        self._set_raises = set_raises

    def set_night_light(self, on):
        self.calls.append(("set_night_light", on))
        if self._set_raises:
            raise TimeoutError("IOCTL failed after retries")
        return (0x110E, b"")

    def get_during_stream(self, name, timeout=2.5):
        self.calls.append(("get", name))
        return self._get_result


def test_registry_entries_resolve():
    for name, (setter, kwarg, get, _field, _coerce, conf) in cm.SET_READBACK.items():
        assert hasattr(cp.TUTKDirectSession, setter), (name, setter)
        params = inspect.signature(getattr(cp.TUTKDirectSession, setter)).parameters
        assert kwarg in params or any(p.kind == p.VAR_KEYWORD for p in params.values()), (name, kwarg)
        assert get in cm.GET_METHODS, (name, get)
        assert conf in ("live", "static"), (name, conf)


def test_registry_fields_exist_in_the_parsed_get():
    for name, (_setter, _kwarg, get, field, _coerce, _conf) in cm.SET_READBACK.items():
        parsed = cm.GET_METHODS[get][2](bytes(200))
        assert field in parsed, (name, get, field)


def test_only_evidenced_entries_claim_live():
    live = {name for name, entry in cm.SET_READBACK.items() if entry[5] == "live"}
    assert live == LIVE_CONFIRMED


def test_verifiable_and_unsupported_are_disjoint():
    assert not set(cm.SET_READBACK) & {k[4:] for k in cm.SET_READBACK_UNSUPPORTED}


def test_applied():
    s = _Stub({"on": True})
    r = s.set_verified("night_light", True)
    assert r["status"] == "applied" and r["verified"] is True
    assert ("get", "get_night_light") in s.calls


def test_accepted_but_not_applied():
    r = _Stub({"on": False}).set_verified("night_light", True)
    assert r["status"] == "not_applied" and r["verified"] is False and r["set_ok"] is True


def test_unverified_never_reports_true():
    for got in (None, {"other": 1}, {"on": None}):
        r = _Stub(got).set_verified("night_light", True)
        assert r["status"] == "unverified" and r["verified"] is None and r["set_ok"] is True


def test_set_failed_skips_the_read_back():
    s = _Stub({"on": True}, set_raises=True)
    r = s.set_verified("night_light", True)
    assert r["status"] == "set_failed" and r["set_ok"] is False and r["verified"] is None
    assert not any(c[0] == "get" for c in s.calls)


def test_unsupported_setter_raises():
    import pytest

    with pytest.raises(ValueError, match="read-back"):
        _Stub({"on": True}).set_verified("lullaby", 1)
