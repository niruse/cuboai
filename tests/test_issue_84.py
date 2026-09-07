"""Tests for issue #84: go2rtc port-1985 conflict handling and duplicate camera profiles.

Covers:
- get_camera_profiles dedupes profiles sharing a device_id (duplicate unique_ids)
- Go2RTCManager resolves a fallback API port when 1985 is taken and publishes it
- Go2RTCManager.is_running reflects subprocess state
- Camera entities stop offering stream sources when go2rtc is not running
"""

import asyncio
import json
import socket
import sys
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aioresponses import aioresponses

from custom_components.cuboai.api import async_api

# =============================================================================
# Extra HA module scaffolding (beyond conftest) needed to import camera.py
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
    # Raises inside the (best-effort) pre-warm block, which camera.py swallows
    aiohttp_client_mod.async_get_clientsession = MagicMock(side_effect=RuntimeError("no session in tests"))
    sys.modules.setdefault("homeassistant.components", components)
    sys.modules.setdefault("homeassistant.components.camera", camera_mod)
    sys.modules.setdefault("homeassistant.helpers.update_coordinator", coordinator_mod)
    sys.modules.setdefault("homeassistant.helpers.aiohttp_client", aiohttp_client_mod)


_install_camera_mocks()

from custom_components.cuboai import camera as camera_platform  # noqa: E402
from custom_components.cuboai import go2rtc as go2rtc_module  # noqa: E402
from custom_components.cuboai.const import DOMAIN  # noqa: E402


def _make_hass():
    """A hass mock whose executor runs functions inline and whose data is a real dict."""
    hass = MagicMock()
    hass.data = {}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    return hass


# =============================================================================
# Duplicate camera profiles → duplicate unique_ids
# =============================================================================


class TestDuplicateProfiles:
    @pytest.mark.asyncio
    async def test_get_camera_profiles_dedupes_same_device_id(self):
        """Two baby profiles on one camera must yield ONE camera dict."""
        payload = {
            "data": [
                {
                    "device_id": "TESTDEVICE001234",
                    "license_id": "UID123",
                    "dev_admin_id": "admin",
                    "dev_admin_pwd": "pwd",
                }
            ],
            "profiles": [
                {"device_id": "TESTDEVICE001234", "profile": json.dumps({"baby": "Dragon"})},
                {"device_id": "TESTDEVICE001234", "profile": json.dumps({"baby": "Draco Room"})},
            ],
        }
        with aioresponses() as m:
            m.get(f"{async_api.API_BASE}/user/cameras", payload=payload)
            m.get(
                f"{async_api.API_BASE}/camera/state?device_id=TESTDEVICE001234",
                payload={"state": "online"},
                repeat=True,
            )
            cameras = await async_api.get_camera_profiles("token", "agent")

        assert len(cameras) == 1
        # The newest (last) profile wins
        assert cameras[0]["baby_name"] == "Draco Room"
        assert cameras[0]["device_id"] == "TESTDEVICE001234"

    def test_stored_duplicates_are_removed(self):
        """The setup-time healing keeps one entry per device_id."""
        stored = [
            {"device_id": "A", "baby_name": "Dragon"},
            {"device_id": "A", "baby_name": "Draco Room"},
            {"device_id": "B", "baby_name": "Mia"},
        ]
        unique = list({c.get("device_id", id(c)): c for c in stored}.values())
        assert [c["device_id"] for c in unique] == ["A", "B"]
        assert unique[0]["baby_name"] == "Draco Room"


# =============================================================================
# API port fallback (1985 busy)
# =============================================================================


class TestApiPortFallback:
    @pytest.mark.asyncio
    async def test_resolve_ports_falls_back_when_1985_busy(self):
        hass = _make_hass()
        manager = go2rtc_module.Go2RTCManager(hass)
        manager.update_streams([], {})

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
            blocker.bind(("0.0.0.0", 1985))
            blocker.listen(1)
            with patch.object(
                sys.modules["custom_components.cuboai.utils"],
                "find_available_port",
                side_effect=lambda start_port, max_port=8600: start_port,
            ):
                await manager._resolve_ports()

        assert manager._api_port != 1985
        assert hass.data[DOMAIN]["api_port_effective"] == manager._api_port
        assert hass.data[DOMAIN]["rtsp_port_effective"] == manager._rtsp_port

    @pytest.mark.asyncio
    async def test_resolve_ports_keeps_1985_when_free(self):
        hass = _make_hass()
        manager = go2rtc_module.Go2RTCManager(hass)
        manager.update_streams([], {})

        await manager._resolve_ports()

        assert manager._api_port == 1985
        assert hass.data[DOMAIN]["api_port_effective"] == 1985

    def test_is_running_reflects_process_state(self):
        manager = go2rtc_module.Go2RTCManager(_make_hass())
        assert manager.is_running is False

        manager.process = MagicMock()
        manager.process.returncode = None
        assert manager.is_running is True

        manager.process.returncode = 1
        assert manager.is_running is False


# =============================================================================
# A user-pinned rtsp_port is sticky: it must not self-heal off the port an NVR
# stores (the port kept hopping 8557->8558 and stranding the recorder)
# =============================================================================


class TestPinnedRtspPortIsSticky:
    @pytest.mark.asyncio
    async def test_pinned_port_is_kept_when_bindable(self):
        hass = _make_hass()
        manager = go2rtc_module.Go2RTCManager(hass)
        manager.update_streams([], {"rtsp_port": 8557})
        # every port bindable -> the pinned port must be used verbatim
        with patch.object(go2rtc_module, "_port_bindable", return_value=True):
            await manager._resolve_ports()
        assert manager._rtsp_port == 8557
        assert hass.data[DOMAIN]["rtsp_port_effective"] == 8557

    @pytest.mark.asyncio
    async def test_pinned_port_falls_back_and_logs_only_on_a_foreign_conflict(self, caplog):
        hass = _make_hass()
        manager = go2rtc_module.Go2RTCManager(hass)
        manager.update_streams([], {"rtsp_port": 8557})
        # 8557 stays unbindable through the probe (a genuine foreign holder,
        # since start() has already reclaimed our own instance) -> hop + error.
        bindable = lambda p: p != 8557  # noqa: E731
        with (
            patch.object(go2rtc_module, "_port_bindable", side_effect=bindable),
            patch.object(
                sys.modules["custom_components.cuboai.utils"],
                "find_available_port",
                side_effect=lambda start_port, max_port=8600: start_port,
            ),
            caplog.at_level("ERROR"),
        ):
            await manager._resolve_ports()
        assert manager._rtsp_port == 8558
        assert any("Configured RTSP port 8557" in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_default_8555_still_self_heals_without_error(self, caplog):
        hass = _make_hass()
        manager = go2rtc_module.Go2RTCManager(hass)
        manager.update_streams([], {})  # no rtsp_port -> default 8555
        bindable = lambda p: p != 8555  # noqa: E731
        with (
            patch.object(go2rtc_module, "_port_bindable", side_effect=bindable),
            patch.object(
                sys.modules["custom_components.cuboai.utils"],
                "find_available_port",
                side_effect=lambda start_port, max_port=8600: start_port,
            ),
            caplog.at_level("ERROR"),
        ):
            await manager._resolve_ports()
        assert manager._rtsp_port == 8556
        # the loud "your NVR URL will change" error is for PINNED ports only
        assert not any("Configured RTSP port" in r.message for r in caplog.records)

    def test_start_waits_on_the_pinned_desired_port_but_not_on_8555(self):
        """The fix that stops the hop lives in start(): it must wait for the
        pinned port to release (so our own dying instance can't force a hop),
        and must NOT wait on the 8555 default (HA's own go2rtc owns it forever,
        waiting would burn the timeout every start)."""
        import inspect

        src = inspect.getsource(go2rtc_module.Go2RTCManager.start)
        assert 'desired_rtsp = int(self._options.get("rtsp_port", 8555))' in src
        assert "if desired_rtsp != 8555:" in src
        assert "_wait_for_port_free(desired_rtsp, timeout=30.0)" in src


# =============================================================================
# Camera entities go quiet when go2rtc is down
# =============================================================================


def _make_camera(manager):
    coordinator = MagicMock()
    coordinator.config_entry.entry_id = "entry1"
    coordinator.config_entry.options = {}
    coordinator.config_entry.data = {}
    cam = camera_platform.CuboLocalCamera(coordinator, {"device_id": "DEV1", "baby_name": "Mia"})
    cam.hass = MagicMock()
    cam.hass.data = {DOMAIN: {"entry1": {"go2rtc": manager} if manager else {}}}
    return cam


class TestCameraGating:
    @pytest.mark.asyncio
    async def test_stream_source_none_when_go2rtc_down(self):
        dead = MagicMock()
        dead.is_running = False
        cam = _make_camera(dead)
        assert await cam.stream_source() is None

    @pytest.mark.asyncio
    async def test_stream_source_none_when_no_manager(self):
        cam = _make_camera(None)
        assert await cam.stream_source() is None

    @pytest.mark.asyncio
    async def test_stream_source_uses_effective_ports_when_running(self):
        alive = MagicMock()
        alive.is_running = True
        cam = _make_camera(alive)
        cam.hass.data[DOMAIN]["rtsp_port_effective"] = 8557
        cam.hass.data[DOMAIN]["api_port_effective"] = 1986

        source = await cam.stream_source()

        assert source == "rtsp://127.0.0.1:8557/cuboai_combined_DEV1"
        assert cam._go2rtc_api_base() == "http://127.0.0.1:1986"

    @pytest.mark.asyncio
    async def test_camera_image_skips_live_snapshot_when_down(self):
        dead = MagicMock()
        dead.is_running = False
        cam = _make_camera(dead)
        # No alerts either → returns None, and crucially no HTTP call was made
        cam.coordinator.data = {"cameras": {}}
        assert await cam.async_camera_image() is None


# =============================================================================
# The WebRTC Camera integration's URL must follow a port self-heal
# =============================================================================


class TestWebRTCIntegrationUrlFollowsThePort:
    """A port hop broke ONLY the custom card, and took hours to spot.

    Every consumer inside this integration reads `api_port_effective`, so a
    self-heal is invisible to them. AlexxIT's WebRTC Camera integration stores
    the go2rtc URL as a fixed string in its config entry — when our API port
    moved 1985 -> 1986 the card showed 'Cannot connect to host <ip>:1985'
    while HLS, snapshots and HomeKit all kept working, because they go via the
    RTSP port instead.
    """

    def _mgr_with_entry(self, url):
        from unittest.mock import MagicMock

        from custom_components.cuboai.go2rtc import Go2RTCManager

        mgr = Go2RTCManager(MagicMock())
        entry = MagicMock()
        entry.data = {"url": url} if url else {}
        entry.entry_id = "abc"
        mgr.hass.config_entries.async_entries.return_value = [entry]
        return mgr, entry

    def test_a_stale_url_on_our_port_range_is_rewritten(self):
        mgr, entry = self._mgr_with_entry("http://192.168.1.50:1985")
        asyncio.run(mgr._sync_webrtc_integration_url(1986))
        args, kwargs = mgr.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["url"] == "http://192.168.1.50:1986"

    def test_a_correct_url_is_left_alone(self):
        mgr, _ = self._mgr_with_entry("http://192.168.1.50:1986")
        asyncio.run(mgr._sync_webrtc_integration_url(1986))
        mgr.hass.config_entries.async_update_entry.assert_not_called()

    def test_someone_elses_go2rtc_is_never_touched(self):
        """A URL outside the range we hand out belongs to another server."""
        for url in ("http://otherhost:1984", "http://192.168.1.99:8555", "http://nas.local:2986"):
            mgr, _ = self._mgr_with_entry(url)
            asyncio.run(mgr._sync_webrtc_integration_url(1986))
            mgr.hass.config_entries.async_update_entry.assert_not_called(), url

    def test_no_url_means_its_own_embedded_server(self):
        mgr, _ = self._mgr_with_entry(None)
        asyncio.run(mgr._sync_webrtc_integration_url(1986))
        mgr.hass.config_entries.async_update_entry.assert_not_called()

    def test_missing_integration_is_not_an_error(self):
        from unittest.mock import MagicMock

        from custom_components.cuboai.go2rtc import Go2RTCManager

        mgr = Go2RTCManager(MagicMock())
        mgr.hass.config_entries.async_entries.side_effect = Exception("unknown domain")
        asyncio.run(mgr._sync_webrtc_integration_url(1986))  # must not raise

    def test_the_sync_is_actually_wired_into_port_resolution(self):
        """Guard the CALL SITE, not just the helper.

        The helper can be perfect and still never run. A mutation that deletes
        the call from _resolve_ports left every other test in this class green,
        which is exactly how a fix silently stops working.
        """
        import inspect

        from custom_components.cuboai.go2rtc import Go2RTCManager

        src = inspect.getsource(Go2RTCManager._resolve_ports)
        assert "_sync_webrtc_integration_url(api_port)" in src
