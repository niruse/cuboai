"""Night-light brightness must come from the camera, not a constant.

`local["brightness"]` was READ in two places — the Night Light Brightness number
and the light entity's `brightness` property — and WRITTEN in none. Nothing in
the integration called GET_LIGHT_STYLE, so the key never existed:

  * the number returned its hardcoded fallback of 100 no matter what the camera
    was set to (observed live: the entity sat at exactly 100);
  * the light entity returned None for brightness while advertising
    ColorMode.BRIGHTNESS.

The camera reports it on GET_LIGHT_STYLE at offset 24 (percent), a read-back
that round-trips correctly in both directions. The coordinator now polls it, and
the number reports unknown rather than a constant when it has no value.

Same shape as the lullaby-timer defect: an entity presenting a local guess as if
it were camera state.
"""

import importlib
import importlib.util
import os
import struct
import sys
from unittest.mock import MagicMock, patch

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

sys.modules.pop("custom_components.cuboai.coordinator", None)
coordinator = importlib.import_module("custom_components.cuboai.coordinator")
number = importlib.import_module("custom_components.cuboai.number")

GET_LIGHT_STYLE_REQ = 4366
DEVICE = "SW05TESTDEVICE01"


def _light_style_response(brightness):
    raw = bytearray(64)
    struct.pack_into("<i", raw, 24, brightness)
    return bytes(raw)


def _fetch(brightness=None, raises=False):
    """Run _fetch_local_data with a session that answers GET_LIGHT_STYLE."""
    def _ioctl(op, payload=b""):
        if op == GET_LIGHT_STYLE_REQ:
            if raises:
                raise RuntimeError("timeout")
            return op + 1, _light_style_response(brightness)
        return op + 1, bytes(64)

    sess = MagicMock()
    sess.ioctl.side_effect = _ioctl
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=sess)
    ctx.__exit__ = MagicMock(return_value=False)
    with patch("custom_components.cuboai.tutk.cuboai_session.get_session", return_value=ctx), \
         patch("custom_components.cuboai.tutk.cuboai_messages.CuboAIClient", return_value=MagicMock()):
        return coordinator._fetch_local_data("uid", "acct", "pw", "192.0.2.10")


def test_the_poll_now_reports_the_cameras_brightness():
    assert _fetch(brightness=3)["brightness"] == 3


def test_a_different_brightness_is_reported_verbatim():
    assert _fetch(brightness=78)["brightness"] == 78


def test_a_failed_read_leaves_the_key_unset_so_the_last_value_carries():
    assert "brightness" not in _fetch(raises=True)


def _entity(local):
    e = object.__new__(number.CuboNightLightBrightnessNumber)
    e.coordinator = MagicMock()
    e.coordinator.data = {"cameras": {DEVICE: {"local": local}}}
    e._device_id = DEVICE
    return e


def test_entity_reports_the_cameras_value():
    assert _entity({"brightness": 3}).native_value == 3


def test_entity_reports_unknown_rather_than_a_hardcoded_hundred():
    """The bug: with no value at all the entity used to claim 100."""
    assert _entity({}).native_value is None


def test_entity_does_not_round_a_real_hundred_away():
    assert _entity({"brightness": 100}).native_value == 100
