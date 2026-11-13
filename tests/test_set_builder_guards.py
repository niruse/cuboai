"""Read-modify-write SET builders must refuse a missing or short GET response.

These builders copy the camera's current settings out of the raw GET response
into a fixed-size SET payload (`payload[a:b] = get_resp_bytes[c:d]`). On a short
buffer that slice assignment silently SHRINKS the bytearray, so the builder used
to return a malformed, wrong-length SET with no error, and that SET writes device
state. build_set_cry_detect and build_set_detection_zone_v2 returned short
payloads; build_set_hw_policy raised a bare IndexError instead.

Each builder now checks its input up front and raises a clear ValueError. The two
build-from-zero builders (cough setting, lullaby schedule) are deliberately not
guarded: they document degrading to a zero base on missing input.
"""

import importlib.util
import os
import sys

import pytest

_TUTK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "custom_components", "cuboai", "tutk")
_spec = importlib.util.spec_from_file_location(
    "cuboai_messages_guards_under_test", os.path.join(_TUTK, "cuboai_messages.py")
)
msgs = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = msgs
_spec.loader.exec_module(msgs)

# (builder, minimum GET length, SET payload length, one override)
RMW = [
    ("build_set_cry_detect", 40, 40, {"enabled": True}),
    ("build_set_detection_zone_v2", 28, 84, {"x_min": 0.1}),
    ("build_set_hw_policy", 44, 48, {"temp_low": 19}),
    ("build_set_hw_control", 96, 96, {"mic_level": 5}),
    ("build_set_sleep_safety_setting", 24, 20, {"safety_alert": True}),
]
IDS = [r[0] for r in RMW]


@pytest.mark.parametrize("name,min_len,full_len,kw", RMW, ids=IDS)
@pytest.mark.parametrize("with_override", [True, False], ids=["override", "no-override"])
def test_short_get_raises(name, min_len, full_len, kw, with_override):
    fn = getattr(msgs, name)
    with pytest.raises(ValueError, match=name):
        fn(b"\x00" * (min_len - 1), **(kw if with_override else {}))


@pytest.mark.parametrize("name,min_len,full_len,kw", RMW, ids=IDS)
def test_none_get_raises(name, min_len, full_len, kw):
    with pytest.raises(ValueError, match=name):
        getattr(msgs, name)(None, **kw)


@pytest.mark.parametrize("name,min_len,full_len,kw", RMW, ids=IDS)
def test_full_get_builds_exact_length(name, min_len, full_len, kw):
    _code, payload = getattr(msgs, name)(bytes(range(256))[: max(min_len, 96)], **kw)
    assert len(payload) == full_len


@pytest.mark.parametrize("name,min_len,full_len,kw", RMW, ids=IDS)
def test_exactly_min_len_is_accepted(name, min_len, full_len, kw):
    _code, payload = getattr(msgs, name)(bytes(min_len), **kw)
    assert len(payload) == full_len


def test_build_from_zero_builders_are_not_guarded():
    _code, p = msgs.build_set_cough_setting(None, enabled=True)
    assert len(p) == 16
    _code, p = msgs.build_set_lullaby_schedule(volume=5, get_resp_bytes=None)
    assert len(p) == 140
