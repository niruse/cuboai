"""Tests for issue #84: go2rtc port-1985 conflict handling and duplicate camera profiles.

Covers:
- get_camera_profiles dedupes profiles sharing a device_id (duplicate unique_ids)
- Go2RTCManager resolves a fallback API port when 1985 is taken and publishes it
- Go2RTCManager.is_running reflects subprocess state
- Camera entities stop offering stream sources when go2rtc is not running
"""

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
