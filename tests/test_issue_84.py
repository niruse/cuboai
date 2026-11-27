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
            {"device_id": "B", "baby_name": "Zuzu"},
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


def _flatten(src: str) -> str:
    """Collapse a source snippet to one space-normalised line.

    These are source pins: they assert the LOGIC is present, so they must not
    break when a long condition is wrapped across lines by the formatter.
    """
    return " ".join(src.split()).replace("( ", "(").replace(" )", ")")


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

    def test_start_reclaims_our_orphan_holding_only_the_pinned_rtsp_port(self):
        """The API-port reclaim misses an orphan of ours that holds only the
        RTSP port (API port free), so a pinned port would hop to desired+1 and
        stay. start() must, when the pinned port is unbindable, kill our own
        binary (which _terminate_stale_processes matches by exact path) before
        the wait — inside the `desired_rtsp != 8555` branch only."""
        import inspect

        src = inspect.getsource(go2rtc_module.Go2RTCManager.start)
        # the guard + the reclaim call live together in the pinned branch
        branch = src[src.index("if desired_rtsp != 8555:") :]
        flat = _flatten(branch)
        # the reclaim is gated on the port being busy, not unconditional
        assert "if not await self.hass.async_add_executor_job(_port_bindable, desired_rtsp)" in flat
        assert "self._terminate_stale_processes" in flat
        # ...and never kills a SECOND config entry's live go2rtc, which runs the
        # very same binary and would otherwise match the path sweep.
        assert "not self._port_held_by_live_sibling(desired_rtsp)" in flat
        assert "self._protected_pids()" in flat


# =============================================================================
# Camera entities go quiet when go2rtc is down
# =============================================================================


def _make_camera(manager):
    coordinator = MagicMock()
    coordinator.config_entry.entry_id = "entry1"
    coordinator.config_entry.options = {}
    coordinator.config_entry.data = {}
    cam = camera_platform.CuboLocalCamera(coordinator, {"device_id": "DEV1", "baby_name": "Zuzu"})
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


def _a_local_non_loopback_ip():
    """An address this machine owns that is not loopback (skip if offline)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("203.0.113.1", 9))  # no packet is sent for UDP connect
            ip = s.getsockname()[0]
        except OSError:
            ip = ""
    if not ip or ip.startswith("127."):
        pytest.skip("no non-loopback IPv4 address on this machine")
    return ip


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

    def test_a_url_naming_this_host_moves_to_loopback(self):
        """The API listens on 127.0.0.1 only, so a URL that names this host by
        a LAN address or name no longer connects, even from HA itself. That is
        what broke the card when the API was first made loopback-only. Kill:
        the host kept as-is, or the rewrite skipped when the port is correct."""
        local_ip = _a_local_non_loopback_ip()
        for url, want in (
            (f"http://{local_ip}:1986", "http://127.0.0.1:1986"),
            (f"http://{local_ip}:1985", "http://127.0.0.1:1986"),
            ("http://localhost:1985", "http://127.0.0.1:1986"),
            (f"http://user:pw@{local_ip}:1986", "http://user:pw@127.0.0.1:1986"),
        ):
            mgr, _ = self._mgr_with_entry(url)
            asyncio.run(mgr._sync_webrtc_integration_url(1986))
            args, kwargs = mgr.hass.config_entries.async_update_entry.call_args
            assert kwargs["data"]["url"] == want, url

    def test_a_loopback_url_on_the_right_port_is_left_alone(self):
        mgr, _ = self._mgr_with_entry("http://127.0.0.1:1986")
        asyncio.run(mgr._sync_webrtc_integration_url(1986))
        mgr.hass.config_entries.async_update_entry.assert_not_called()

    def test_another_machine_keeps_its_host(self):
        """TEST-NET-3 is never assigned to a local interface."""
        mgr, _ = self._mgr_with_entry("http://203.0.113.7:1986")
        asyncio.run(mgr._sync_webrtc_integration_url(1986))
        mgr.hass.config_entries.async_update_entry.assert_not_called()

    def test_a_host_ha_knows_it_is_moves_to_loopback(self):
        """Docker on bridge networking: inside the container the host's LAN IP
        is not bindable and `homeassistant.local` may not resolve, so the bind
        check alone misses them. The internal/external URL settings still name
        this host. (TEST-NET addresses and .example names are never local, so
        only the settings can recognise them.) Kill: drop either source."""
        for attr, value, url in (
            ("internal_url", "http://198.51.100.20:8123", "http://198.51.100.20:1985"),
            ("internal_url", "http://homeassistant.local:8123", "http://homeassistant.local:1985"),
            ("external_url", "https://ha.example.com", "http://HA.example.com:1986"),
        ):
            mgr, _ = self._mgr_with_entry(url)
            mgr.hass.config.internal_url = None
            mgr.hass.config.external_url = None
            setattr(mgr.hass.config, attr, value)
            with patch("custom_components.cuboai.go2rtc._is_this_host", AsyncMock(return_value=False)):
                asyncio.run(mgr._sync_webrtc_integration_url(1986))
            args, kwargs = mgr.hass.config_entries.async_update_entry.call_args
            assert kwargs["data"]["url"] == "http://127.0.0.1:1986", (attr, value)

    def test_an_ipv6_host_keeps_its_brackets(self):
        """A remote IPv6 literal must stay bracketed when only the port moves:
        'http://2001:db8::7:1986' is not a URL. 2001:db8::/32 is documentation
        space, never local."""
        mgr, _ = self._mgr_with_entry("http://[2001:db8::7]:1985")
        mgr.hass.config.internal_url = None
        mgr.hass.config.external_url = None
        asyncio.run(mgr._sync_webrtc_integration_url(1986))
        args, kwargs = mgr.hass.config_entries.async_update_entry.call_args
        assert kwargs["data"]["url"] == "http://[2001:db8::7]:1986"

    def test_a_name_that_never_resolves_does_not_hold_up_the_start(self):
        """The sync runs before go2rtc is spawned, so a resolver that hangs
        (no DNS server, no mDNS in a container) must not delay the start by
        its own timeout. Kill: remove the wait_for."""
        import time

        from custom_components.cuboai import go2rtc as mod

        async def _hang(*_a, **_k):
            await asyncio.sleep(30)

        with patch.object(mod, "_RESOLVE_TIMEOUT", 0.05), patch.object(asyncio.BaseEventLoop, "getaddrinfo", _hang):
            t0 = time.monotonic()
            assert asyncio.run(mod._is_this_host("never-resolves.invalid")) is False
            assert time.monotonic() - t0 < 5

    def test_an_unrecognised_host_is_kept_but_named_in_a_warning(self, caplog):
        """If neither check recognises the host it may still be this HA (an
        unusual Docker setup), so the log tells the user what to change."""
        import logging

        mgr, _ = self._mgr_with_entry("http://203.0.113.7:1986")
        mgr.hass.config.internal_url = None
        mgr.hass.config.external_url = None
        with caplog.at_level(logging.WARNING):
            asyncio.run(mgr._sync_webrtc_integration_url(1986))
        mgr.hass.config_entries.async_update_entry.assert_not_called()
        assert "http://203.0.113.7:1986" in caplog.text
        assert "http://127.0.0.1:1986" in caplog.text

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
