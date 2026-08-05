"""Tests for issue #89: the custom card could never find its camera entity.

Root cause: the card located its camera by pattern-matching the entity id
(``camera.cuboai_*_local_camera`` plus a "baby name" token sliced out of the
speaker's entity id). camera.py sets only ``_attr_name`` and never
``_attr_has_entity_name``, so the id is whatever HA composes from the device and
entity names — which varies by install, HA version and user renames, and which
HA's ``_2`` duplicate suffix breaks outright. When the match failed the card fell
through to a hardcoded ``rtsp://127.0.0.1:8555/...`` URL; on HA OS that port
belongs to HA's own go2rtc WebRTC listener, which accepts then tears the
connection down: "connection reset by peer".

Covers:
- the backend attribute contract the new card matcher depends on
- a static guard that the old entity-id filter and hardcoded port cannot return
- the card's cuboaiFindCameraState matcher, executed in Node (skipped without node)
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest

# =============================================================================
# Extra HA module scaffolding (beyond conftest) needed to import camera.py.
# Same pattern as tests/test_issue_84.py.
# =============================================================================


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
    aiohttp_client_mod.async_get_clientsession = MagicMock(side_effect=RuntimeError("no session in tests"))
    sys.modules.setdefault("homeassistant.components", components)
    sys.modules.setdefault("homeassistant.components.camera", camera_mod)
    sys.modules.setdefault("homeassistant.helpers.update_coordinator", coordinator_mod)
    sys.modules.setdefault("homeassistant.helpers.aiohttp_client", aiohttp_client_mod)


_install_camera_mocks()

from custom_components.cuboai import camera as camera_platform  # noqa: E402
from custom_components.cuboai.const import DOMAIN  # noqa: E402

CARD = Path(__file__).parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js"


def _make_camera(options=None, data=None):
    coordinator = MagicMock()
    coordinator.config_entry.entry_id = "entry1"
    coordinator.config_entry.options = options if options is not None else {}
    coordinator.config_entry.data = data if data is not None else {}
    cam = camera_platform.CuboLocalCamera(coordinator, {"device_id": "DEV1", "baby_name": "Mia"})
    cam.hass = MagicMock()
    cam.hass.data = {DOMAIN: {"entry1": {}}}
    cam.hass.async_add_executor_job = AsyncMock()
    return cam


# =============================================================================
# 1. Backend attribute contract
#
# The card now matches on the device_id ATTRIBUTE. If extra_state_attributes
# ever stops publishing it, the matcher goes blind and #89 silently returns —
# so these assertions are the real regression guard for the fix.
# =============================================================================


class TestCameraAttributeContract:
    def test_publishes_keys_the_card_matches_on(self):
        attrs = _make_camera().extra_state_attributes
        for key in ("device_id", "uid", "rtsp_port"):
            assert key in attrs, f"card matcher depends on the {key!r} attribute"

    def test_device_id_and_uid_alias_the_same_device(self):
        # The matcher probes uid as a secondary key; today it aliases device_id.
        attrs = _make_camera().extra_state_attributes
        assert attrs["device_id"] == "DEV1"
        assert attrs["uid"] == attrs["device_id"]

    def test_rtsp_port_follows_the_healed_port(self):
        cam = _make_camera(options={"rtsp_port": 8555})
        cam.hass.data[DOMAIN]["rtsp_port_effective"] = 8557
        assert cam.extra_state_attributes["rtsp_port"] == 8557

    def test_rtsp_port_falls_back_options_then_data_then_default(self):
        assert _make_camera(options={"rtsp_port": 9001}).extra_state_attributes["rtsp_port"] == 9001
        assert _make_camera(data={"rtsp_port": 9002}).extra_state_attributes["rtsp_port"] == 9002
        assert _make_camera().extra_state_attributes["rtsp_port"] == 8555

    def test_attributes_do_not_raise_before_go2rtc_starts(self):
        # The window between entity creation and _resolve_ports publishing.
        cam = _make_camera()
        cam.hass.data = {}
        assert cam.extra_state_attributes["rtsp_port"] == 8555


# =============================================================================
# 2. Static guard on the card
#
# This is a grep test, not a behavioural one — stated plainly because the repo
# has no JS test runner and ruff does not read .js. It pins exactly the two
# mistakes that caused #89 so they cannot be reintroduced.
# =============================================================================


class TestCardHasNoStalePortOrEntityIdFilter:
    def _code_lines(self):
        """Card source minus comment lines — the fix is explained in comments
        that legitimately mention 8555 and camera.cuboai_."""
        out = []
        for line in CARD.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
                continue
            out.append(line)
        return out

    def test_no_hardcoded_rtsp_port(self):
        offenders = [ln for ln in self._code_lines() if "8555" in ln]
        assert not offenders, f"hardcoded RTSP port is back (#89): {offenders}"

    def test_no_fabricated_rtsp_url(self):
        offenders = [ln for ln in self._code_lines() if "rtsp://" in ln]
        assert not offenders, f"card must resolve via the entity, not a URL (#89): {offenders}"

    def test_no_camera_entity_id_filter(self):
        offenders = [ln for ln in self._code_lines() if "camera.cuboai_" in ln]
        assert not offenders, f"entity-id filter is back and can never match (#89): {offenders}"

    def test_matcher_declared_once_and_used_everywhere(self):
        src = CARD.read_text(encoding="utf-8")
        assert src.count("function cuboaiFindCameraState(") == 1
        # render path, editor, and both setConfig branches
        assert src.count("cuboaiFindCameraState(") >= 5
        assert src.count("function cuboaiWebrtcConfig(") == 1


# =============================================================================
# 3. The matcher itself, executed in Node
# =============================================================================

_HARNESS = """
const fs = require('fs');
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => undefined, define: () => {} };
globalThis.window = globalThis;
globalThis.document = { createElement: () => ({ style: {}, appendChild() {}, addEventListener() {} }) };
const src = fs.readFileSync(process.argv[2], 'utf8');
const find = new Function(src + '\\n;return cuboaiFindCameraState;')();
const cases = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
console.log(JSON.stringify(cases.map(c => {
  const r = find(c.hass, c.deviceId);
  return r ? r.entityId : null;
})));
"""


def _cam(device_id, rtsp_port=8557, **extra):
    return {"attributes": {"device_id": device_id, "rtsp_port": rtsp_port, **extra}}


def _run_matcher(tmp_path, cases):
    harness = tmp_path / "harness.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    payload = tmp_path / "cases.json"
    payload.write_text(json.dumps(cases), encoding="utf-8")
    proc = subprocess.run(
        [shutil.which("node"), str(harness), str(CARD), str(payload)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"node failed: {proc.stderr}"
    return json.loads(proc.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
class TestCardMatcherInNode:
    def test_matching_behaviour(self, tmp_path):
        cases = [
            # Exact device_id match on the real entity id shape.
            {"deviceId": "DEV1", "hass": {"states": {"camera.mia_local_camera": _cam("DEV1")}}},
            # The #89 fix: a renamed entity must still resolve.
            {"deviceId": "DEV1", "hass": {"states": {"camera.zzz_renamed": _cam("DEV1")}}},
            # HA's duplicate "_2" suffix must still resolve.
            {"deviceId": "DEV1", "hass": {"states": {"camera.mia_local_camera_2": _cam("DEV1")}}},
            # uid is accepted as a secondary key.
            {"deviceId": "UID9", "hass": {"states": {"camera.mia_local_camera": _cam("DEV1", uid="UID9")}}},
            # Two cameras and a device_id matching neither: refuse rather than
            # show the wrong baby (the old code took the first arbitrary match).
            {
                "deviceId": "NOPE",
                "hass": {"states": {"camera.a_local_camera": _cam("DEV1"), "camera.b_local_camera": _cam("DEV2")}},
            },
            # Unpinned card with exactly one CuboAI camera: safe to use it.
            {"deviceId": None, "hass": {"states": {"camera.mia_local_camera": _cam("DEV1")}}},
            # Nothing to match.
            {"deviceId": "DEV1", "hass": {"states": {}}},
            # A foreign camera without our attributes is never claimed.
            {"deviceId": None, "hass": {"states": {"camera.front_door": {"attributes": {}}}}},
        ]

        assert _run_matcher(tmp_path, cases) == [
            "camera.mia_local_camera",
            "camera.zzz_renamed",
            "camera.mia_local_camera_2",
            "camera.mia_local_camera",
            None,
            "camera.mia_local_camera",
            None,
            None,
        ]

    def test_survives_missing_hass(self, tmp_path):
        cases = [{"deviceId": "DEV1", "hass": None}, {"deviceId": "DEV1", "hass": {}}]
        assert _run_matcher(tmp_path, cases) == [None, None]
