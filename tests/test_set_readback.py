"""A SET the camera acknowledges but ignores must not be reported as applied.

`baby_presence_alert` is one of four coupled fields in SET_SLEEP_SAFETY_SETTING
that this camera firmware accepts (`result=0`) and then does not apply. A live
round-trip showed its read-back unchanged in both directions while
`safety_alert`, `cover_alert` and `sensitivity` read back exactly as written in
the same call, so it is the field being ignored, not the transport.

The switch used to write the REQUESTED value straight into the coordinator
cache, so Home Assistant showed a state the camera was not in until the next
poll quietly reverted it. On a baby monitor's presence alert that is the wrong
thing to be quiet about. It now reads the value back and publishes the truth.
"""

import importlib
import importlib.util
import logging
import os
import sys
import types
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _stub(name, **attrs):
    """Register a stub module. conftest mocks `homeassistant` itself, which makes it
    a non-package, so every submodule switch.py imports has to be registered here —
    and the entity base classes must be real classes, not MagicMock attributes."""
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    return mod


def _base(name):
    """A distinct stand-in class per Home Assistant entity base — they are combined
    by multiple inheritance, so they must not collapse into one shared class."""
    return type(name, (), {"__init__": lambda self, *a, **k: None})


_stub("homeassistant.components")
_stub("homeassistant.components.switch", SwitchEntity=_base("SwitchEntity"))
_stub("homeassistant.helpers.restore_state", RestoreEntity=_base("RestoreEntity"))
_stub("homeassistant.helpers.update_coordinator", CoordinatorEntity=_base("CoordinatorEntity"))
_stub("homeassistant.exceptions", HomeAssistantError=RuntimeError)

# conftest replaces custom_components.cuboai.utils with a MagicMock, which would
# turn the @retry_camera_command-decorated helpers into mocks too. Put the real
# module back before loading switch.py so the helpers under test are real.
_utils_path = os.path.join(_ROOT, "custom_components", "cuboai", "utils.py")
_uspec = importlib.util.spec_from_file_location("custom_components.cuboai.utils", _utils_path)
_utils = importlib.util.module_from_spec(_uspec)
sys.modules["custom_components.cuboai.utils"] = _utils
_uspec.loader.exec_module(_utils)

sys.modules.pop("custom_components.cuboai.switch", None)
switch = importlib.import_module("custom_components.cuboai.switch")


class _FakeEntity:
    """The attributes _apply_verified touches on a real switch entity."""

    def __init__(self):
        self.coordinator = MagicMock()
        self.coordinator.data = {"cameras": {}}
        self._device_id = "SW05TESTDEVICE01"
        self.entity_id = "switch.test_baby_presence"
        self._attr_name = "Test Baby Presence"
        self.written = 0

    def async_write_ha_state(self):
        self.written += 1


def _cached(entity, key):
    return entity.coordinator.data["cameras"][entity._device_id]["local"][key]


def test_applied_value_is_published_when_the_camera_honours_it(caplog=None):
    e = _FakeEntity()
    switch._apply_verified(e, "baby_presence", True, True, "the baby-presence alert")
    assert _cached(e, "baby_presence") is True
    assert e.written == 1


def test_ignored_set_publishes_the_cameras_real_state_not_the_request():
    e = _FakeEntity()
    switch._apply_verified(e, "baby_presence", True, False, "the baby-presence alert")
    assert _cached(e, "baby_presence") is False, "the requested value must not be published"
    assert e.written == 1


def test_ignored_set_warns_once_not_on_every_toggle(caplog):
    caplog.set_level(logging.WARNING)
    e = _FakeEntity()
    for _ in range(4):
        switch._apply_verified(e, "baby_presence", True, False, "the baby-presence alert")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one warning, got {len(warnings)}"
    assert "does not apply it" in warnings[0].getMessage()


def test_honoured_set_does_not_warn(caplog):
    caplog.set_level(logging.WARNING)
    e = _FakeEntity()
    switch._apply_verified(e, "baby_presence", False, False, "the baby-presence alert")
    assert [r for r in caplog.records if r.levelno == logging.WARNING] == []


def _session_ctx(client):
    """A get_session(...) context manager whose session exposes .ioctl."""
    sess = MagicMock()
    sess.ioctl.return_value = (0, b"\x00" * 24)
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=sess)
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx, sess


def test_baby_presence_helper_returns_the_readback_not_the_request():
    client = MagicMock()
    client.get_sleep_safety_status.return_value = {"baby_presence_alert": False}
    ctx, sess = _session_ctx(client)
    with patch("custom_components.cuboai.tutk.cuboai_session.get_session", return_value=ctx), \
         patch("custom_components.cuboai.tutk.cuboai_messages.CuboAIClient", return_value=client), \
         patch("custom_components.cuboai.tutk.cuboai_messages.build_get_sleep_safety_setting",
               return_value=(0x2331, b"")), \
         patch("custom_components.cuboai.tutk.cuboai_messages.build_set_sleep_safety_setting",
               return_value=(0x2332, b"")):
        got = switch._set_baby_presence("uid", "acct", "pw", "192.0.2.10", True)
    assert got is False, "helper must report the camera's value, not the requested one"
    client.get_sleep_safety_status.assert_called_once()


def test_readback_failure_falls_back_to_the_requested_value():
    """A failed read-back is not a failed SET — don't invent an 'unknown' state."""
    client = MagicMock()
    client.get_sleep_safety_status.side_effect = RuntimeError("timeout")
    ctx, sess = _session_ctx(client)
    with patch("custom_components.cuboai.tutk.cuboai_session.get_session", return_value=ctx), \
         patch("custom_components.cuboai.tutk.cuboai_messages.CuboAIClient", return_value=client), \
         patch("custom_components.cuboai.tutk.cuboai_messages.build_get_sleep_safety_setting",
               return_value=(0x2331, b"")), \
         patch("custom_components.cuboai.tutk.cuboai_messages.build_set_sleep_safety_setting",
               return_value=(0x2332, b"")):
        got = switch._set_baby_presence("uid", "acct", "pw", "192.0.2.10", True)
    assert got is True
