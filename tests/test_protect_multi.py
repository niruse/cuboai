"""Several cameras in UniFi Protect, each under its own name.

Tested live on a UDM: Protect tells third-party cameras apart by the MAC they
REPORT over ONVIF, not the one on the wire — a second device on the same IP and
another port, reporting its own MAC, was adopted as a second camera next to the
first, both streaming. And Protect names a camera "<Manufacturer> <Model>", so
a Model of "Baby Monitor" made every camera look the same. Every test names
the mutation it kills.
"""

import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from xml.etree import ElementTree as ET

import pytest

from custom_components.cuboai import config_flow as cf
from custom_components.cuboai import onvif_server as onvif
from custom_components.cuboai.const import (
    DOMAIN,
    OPT_PROTECT_CAMERA,
    OPT_PROTECT_CAMERAS,
    OPT_PROTECT_ENABLED,
    OPT_PROTECT_PASSWORD,
    OPT_PROTECT_PORT,
    OPT_PROTECT_PORTS,
    assign_protect_ports,
    protect_camera_ids,
    protect_display_name,
    protect_mac,
    protect_primary_id,
)

from .test_onvif_server import PASSWORD, _call, _device, _digest_token, _req

A, B, C = "CB02AAAA00000001", "SW05BBBB00000002", "CB02CCCC00000003"
CAMS = [
    {"device_id": A, "baby_name": "Noa"},
    {"device_id": B, "baby_name": "Ari"},
    {"device_id": C, "baby_name": "Tal"},
]
HOST_MAC = "d8:3a:dd:00:00:01"
NS = {"tds": onvif.NS_DEVICE, "tt": onvif.NS_SCHEMA}


# =============================================================================
# Which cameras, which ports
# =============================================================================


def test_an_install_from_before_the_multi_select_keeps_its_one_camera():
    """Upgrading must change nothing in Protect. Kill: the legacy fallback
    removed (an old install would expose no camera)."""
    assert protect_camera_ids({}, CAMS) == [A]
    assert protect_camera_ids({OPT_PROTECT_CAMERA: B}, CAMS) == [B]
    assert assign_protect_ports({OPT_PROTECT_CAMERA: B, OPT_PROTECT_PORT: 8899}, CAMS) == {B: 8899}


def test_the_multi_select_exposes_the_chosen_cameras_and_drops_removed_ones():
    """Kill: the selection ignored, or a camera no longer configured kept."""
    assert protect_camera_ids({OPT_PROTECT_CAMERAS: [C, A, "GONE0000"]}, CAMS) == [A, C]
    assert protect_camera_ids({OPT_PROTECT_CAMERAS: []}, CAMS) == []


def test_each_camera_gets_its_own_port_the_primary_the_base():
    """Kill: every camera given the base port."""
    ports = assign_protect_ports({OPT_PROTECT_CAMERAS: [A, B, C]}, CAMS)
    assert ports == {A: 8899, B: 8900, C: 8901}


def test_a_camera_keeps_its_port_when_others_come_and_go():
    """Protect stores ip:port at adoption. Kill: saved ports recomputed."""
    opts = {OPT_PROTECT_CAMERAS: [A, C], OPT_PROTECT_CAMERA: A, OPT_PROTECT_PORTS: {A: 8899, B: 8900, C: 8901}}
    assert assign_protect_ports(opts, CAMS) == {A: 8899, C: 8901}, "C moved when B was unticked"


def test_a_port_once_given_is_never_handed_to_another_camera():
    """B's old port stays B's while B is unticked, so re-ticking B finds it
    where Protect has it. Kill: reserved ports reused for a newcomer."""
    opts = {OPT_PROTECT_CAMERAS: [A, C], OPT_PROTECT_CAMERA: A, OPT_PROTECT_PORTS: {A: 8899, B: 8900}}
    assert assign_protect_ports(opts, CAMS) == {A: 8899, C: 8901}


def test_unticking_the_primary_moves_no_one():
    """Kill: the next camera promoted onto the base port (it would lose the
    address Protect adopted it at)."""
    opts = {OPT_PROTECT_CAMERAS: [B, C], OPT_PROTECT_CAMERA: A, OPT_PROTECT_PORTS: {A: 8899, B: 8900, C: 8901}}
    assert protect_primary_id(opts, CAMS) is None
    assert assign_protect_ports(opts, CAMS) == {B: 8900, C: 8901}


def test_new_cameras_skip_ports_to_avoid():
    """Kill: `avoid` ignored."""
    assert assign_protect_ports({OPT_PROTECT_CAMERAS: [A, B]}, CAMS, avoid=[8900]) == {A: 8899, B: 8901}


def test_changing_the_base_port_moves_only_the_primary():
    """Kill: other cameras following the base port."""
    opts = {
        OPT_PROTECT_CAMERAS: [A, B],
        OPT_PROTECT_CAMERA: A,
        OPT_PROTECT_PORT: 9000,
        OPT_PROTECT_PORTS: {A: 8899, B: 8900},
    }
    assert assign_protect_ports(opts, CAMS) == {A: 9000, B: 8900}


# =============================================================================
# The MAC Protect keys cameras by
# =============================================================================


def test_the_primary_reports_the_hosts_real_mac():
    """A camera adopted before v2.6.41 was adopted with it. Kill: the primary
    given a synthetic MAC (Protect would see a different camera)."""
    assert protect_mac(A, True, HOST_MAC) == HOST_MAC


def test_other_cameras_report_their_own_stable_locally_administered_mac():
    """Kill: all cameras sharing the host MAC (Protect merges them), or the
    MAC changing between calls, or a globally-unique / multicast address."""
    mac_b, mac_c = protect_mac(B, False, HOST_MAC), protect_mac(C, False, HOST_MAC)
    assert mac_b != mac_c and HOST_MAC not in (mac_b, mac_c)
    assert protect_mac(B, False, HOST_MAC) == mac_b
    first = int(mac_b.split(":")[0], 16)
    assert first & 0b10, "not locally administered"
    assert not first & 0b01, "multicast"
    assert len(mac_b.split(":")) == 6


def test_the_mac_never_reveals_the_camera_id():
    assert B.lower() not in protect_mac(B, False, HOST_MAC).replace(":", "")


# =============================================================================
# The name Protect shows
# =============================================================================


def _registry(name_by_user):
    device = SimpleNamespace(name_by_user=name_by_user)
    registry = MagicMock()
    registry.async_get_device = lambda identifiers: device
    return patch("homeassistant.helpers.device_registry.async_get", return_value=registry)


def test_the_name_is_the_cameras_name_from_the_account():
    """Kill: the generic "Baby Monitor" kept."""
    with _registry(None):
        assert protect_display_name(MagicMock(), B, CAMS) == "Ari"


def test_a_rename_in_home_assistant_wins():
    """Kill: name_by_user ignored."""
    with _registry("Nursery"):
        assert protect_display_name(MagicMock(), B, CAMS) == "Nursery"


def test_protects_own_manufacturer_prefix_is_not_doubled():
    """Protect shows "CuboAI <Model>". Kill: the prefix strip removed."""
    with _registry("CuboAI Nursery"):
        assert protect_display_name(MagicMock(), B, CAMS) == "Nursery"


def test_a_broken_registry_falls_back_to_the_account_name():
    """Kill: a registry error escaping into Protect's SOAP answer."""
    with patch("homeassistant.helpers.device_registry.async_get", side_effect=RuntimeError("boom")):
        assert protect_display_name(MagicMock(), B, CAMS) == "Ari"


def test_a_non_text_rename_is_ignored_and_long_names_are_cut():
    """Kill: a MagicMock/None name used; the length cap removed."""
    with _registry(MagicMock()):
        assert protect_display_name(MagicMock(), B, CAMS) == "Ari"
    with _registry("x" * 200):
        assert len(protect_display_name(MagicMock(), B, CAMS)) == 48


def test_the_model_carries_the_name_escaped():
    """Protect names the camera from GetDeviceInformation's Model. Kill: the
    Model hard-coded again, or the name not XML-escaped."""
    device, _ = _device(name="Noa & Ari <3")
    _, root = _call(device, "GetDeviceInformation", onvif.NS_DEVICE)
    assert root.find(".//tds:Model", NS).text == "Noa & Ari <3"
    assert root.find(".//tds:Manufacturer", NS).text == "CuboAI"


def test_the_discovery_name_scope_carries_the_name():
    """Kill: the scope left as "CuboAI"."""
    device, _ = _device(name="Noa Ari")
    _, root = _call(device, "GetScopes", onvif.NS_DEVICE)
    items = [e.text for e in root.iter(f"{{{onvif.NS_SCHEMA}}}ScopeItem")]
    assert "onvif://www.onvif.org/name/Noa%20Ari" in items


# =============================================================================
# The group: one service per camera, over real sockets
# =============================================================================


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _hass():
    hass = MagicMock()
    hass.data = {DOMAIN: {"_ports_by_entry": {"entryA": {"rtsp": 8557, "api": 1985}}}}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    return hass


def _entry(options):
    return SimpleNamespace(
        entry_id="entryA",
        options={OPT_PROTECT_ENABLED: True, OPT_PROTECT_PASSWORD: PASSWORD, **options},
        data={"cameras": CAMS},
    )


def test_the_group_builds_one_service_per_camera_primary_first():
    """Kill: only one service built; the primary flag on the wrong camera."""
    group = onvif.OnvifGroup(_hass(), _entry({OPT_PROTECT_CAMERAS: [A, B], OPT_PROTECT_PORT: 8899}))
    assert {d: s.port for d, s in group.services.items()} == {A: 8899, B: 8900}
    assert group.services[A].primary and not group.services[B].primary
    assert group.for_camera(C) is None


def test_no_camera_selected_is_reported():
    """Kill: the empty-group start_error removed."""
    group = onvif.OnvifGroup(_hass(), _entry({OPT_PROTECT_CAMERAS: []}))
    assert group.services == {} and group.start_error == "no camera selected"


@pytest.mark.asyncio
async def test_two_cameras_answer_on_two_ports_with_their_own_name_and_mac():
    """End to end, the live finding: each port is its own camera to Protect.
    Kill: the name or the MAC not reaching the spec; ports not published."""
    import aiohttp

    pa, pb = _free_port(), _free_port()
    hass = _hass()
    opts = {OPT_PROTECT_CAMERAS: [A, B], OPT_PROTECT_CAMERA: A, OPT_PROTECT_PORT: pa, OPT_PROTECT_PORTS: {B: pb}}
    group = onvif.OnvifGroup(hass, _entry(opts))
    for s in group.services.values():
        s.mac = HOST_MAC  # what _resolve_host finds; patched below so the test is host-independent

    async def fake_resolve(self):
        self.host_ip, self.mac = "127.0.0.1", HOST_MAC

    with (
        patch.object(onvif.OnvifService, "_resolve_host", fake_resolve),
        patch.object(onvif.WsDiscovery, "start", AsyncMock()),
        patch.object(onvif.WsDiscovery, "stop", AsyncMock()),
        patch("homeassistant.helpers.device_registry.async_get", side_effect=RuntimeError),
    ):
        assert await group.start()
        try:
            assert hass.data[DOMAIN]["_ports_by_entry"]["entryA"]["onvif"] == {A: pa, B: pb}
            seen = {}
            async with aiohttp.ClientSession() as session:
                for dev, port in ((A, pa), (B, pb)):
                    answers = []
                    for op in ("GetDeviceInformation", "GetNetworkInterfaces"):
                        async with session.post(
                            f"http://127.0.0.1:{port}/onvif/device_service",
                            data=_req(op, onvif.NS_DEVICE, _digest_token()),
                        ) as resp:
                            assert resp.status == 200
                            answers.append(ET.fromstring(await resp.read()))
                    seen[dev] = (
                        answers[0].find(".//tds:Model", NS).text,
                        answers[1].find(".//tt:HwAddress", NS).text,
                    )
            assert seen[A] == ("Noa", HOST_MAC)
            assert seen[B][0] == "Ari" and seen[B][1] == protect_mac(B, False, HOST_MAC)
        finally:
            await group.stop()
    assert not hass.data[DOMAIN]["_ports_by_entry"]["entryA"]["onvif"]


@pytest.mark.asyncio
async def test_one_cameras_taken_port_does_not_stop_the_others():
    """Kill: a start failure aborting the loop."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    taken, free = blocker.getsockname()[1], _free_port()
    try:
        opts = {
            OPT_PROTECT_CAMERAS: [A, B],
            OPT_PROTECT_CAMERA: A,
            OPT_PROTECT_PORT: taken,
            OPT_PROTECT_PORTS: {B: free},
        }
        group = onvif.OnvifGroup(_hass(), _entry(opts))
        with (
            patch.object(onvif.WsDiscovery, "start", AsyncMock()),
            patch.object(onvif.WsDiscovery, "stop", AsyncMock()),
            patch.object(onvif.OnvifService, "_notify_port_conflict"),
        ):
            assert await group.start() is True
            try:
                assert not group.services[A].running and group.services[B].running
            finally:
                await group.stop()
    finally:
        blocker.close()


@pytest.mark.asyncio
async def test_a_crash_in_one_camera_is_contained():
    """Kill: the per-camera try/except removed."""
    group = onvif.OnvifGroup(_hass(), _entry({OPT_PROTECT_CAMERAS: [A, B]}))
    group.services[A].start = AsyncMock(side_effect=RuntimeError("boom"))
    group.services[B].start = AsyncMock(return_value=True)
    assert await group.start() is True
    assert group.services[A].start_error


# =============================================================================
# Configure
# =============================================================================


def _hass_flow(other_entries=()):
    hass = MagicMock()
    hass.data = {DOMAIN: {}}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    hass.config_entries.async_entries = lambda domain: list(other_entries)
    return hass


def _on(**kw):
    return {OPT_PROTECT_ENABLED: True, OPT_PROTECT_PASSWORD: "pw", OPT_PROTECT_PORT: 8899, "rtsp_port": 8557, **kw}


@pytest.mark.asyncio
async def test_saving_records_the_primary_and_every_cameras_port():
    """The options flow replaces ALL options, so the bookkeeping must be in
    what it stores. Kill: stored ports/primary not computed."""
    with patch("custom_components.cuboai.go2rtc._port_bindable", return_value=True):
        errors, stored = await cf._protect_plan(_hass_flow(), {}, _on(**{OPT_PROTECT_CAMERAS: [A, B]}), CAMS, "entryA")
    assert errors == {}
    assert stored == {OPT_PROTECT_CAMERA: A, OPT_PROTECT_PORTS: {A: 8899, B: 8900}}


@pytest.mark.asyncio
async def test_an_upgraded_install_keeps_the_camera_protect_adopted_as_primary():
    """v2.6.40 exposed the first camera with the host MAC on 8899. Ticking B
    first must not hand B that identity. Kill: the legacy primary rule."""
    previous = {OPT_PROTECT_ENABLED: True, OPT_PROTECT_PASSWORD: "pw"}
    with patch("custom_components.cuboai.go2rtc._port_bindable", return_value=True):
        _, stored = await cf._protect_plan(_hass_flow(), previous, _on(**{OPT_PROTECT_CAMERAS: [B, A]}), CAMS, "entryA")
    assert stored[OPT_PROTECT_CAMERA] == A
    assert stored[OPT_PROTECT_PORTS] == {A: 8899, B: 8900}


@pytest.mark.asyncio
async def test_a_new_camera_skips_a_port_that_is_taken():
    """Kill: the retry that avoids unbindable ports for a new camera."""

    def bindable(port):
        return port != 8900

    with patch("custom_components.cuboai.go2rtc._port_bindable", side_effect=bindable):
        errors, stored = await cf._protect_plan(_hass_flow(), {}, _on(**{OPT_PROTECT_CAMERAS: [A, B]}), CAMS, "entryA")
    assert errors == {}
    assert stored[OPT_PROTECT_PORTS][B] == 8901


@pytest.mark.asyncio
async def test_a_cameras_own_port_taken_is_an_error_not_a_move():
    """Moving it would orphan the camera in Protect. Kill: the saved-port
    branch treated like a new camera."""
    previous = {OPT_PROTECT_CAMERA: A, OPT_PROTECT_PORTS: {A: 8899, B: 8900}}

    def bindable(port):
        return port != 8900

    with patch("custom_components.cuboai.go2rtc._port_bindable", side_effect=bindable):
        errors, stored = await cf._protect_plan(
            _hass_flow(), previous, _on(**{OPT_PROTECT_CAMERAS: [A, B]}), CAMS, "entryA"
        )
    assert errors == {OPT_PROTECT_CAMERAS: "onvif_camera_port_in_use"}
    assert stored[OPT_PROTECT_PORTS][B] == 8900


@pytest.mark.asyncio
async def test_our_own_running_ports_are_not_treated_as_taken():
    """Re-saving Configure while two services hold 8899/8900. Kill: the held
    exemption limited to one port."""
    hass = _hass_flow()
    hass.data[DOMAIN]["_ports_by_entry"] = {"entryA": {"onvif": {A: 8899, B: 8900}}}
    previous = {OPT_PROTECT_CAMERA: A, OPT_PROTECT_PORTS: {A: 8899, B: 8900}}
    with patch("custom_components.cuboai.go2rtc._port_bindable", return_value=False):
        errors, _ = await cf._protect_plan(hass, previous, _on(**{OPT_PROTECT_CAMERAS: [A, B]}), CAMS, "entryA")
    assert errors == {}


@pytest.mark.asyncio
async def test_choosing_no_camera_is_refused():
    """Kill: the empty-selection check removed."""
    with patch("custom_components.cuboai.go2rtc._port_bindable", return_value=True):
        errors, _ = await cf._protect_plan(_hass_flow(), {}, _on(**{OPT_PROTECT_CAMERAS: []}), CAMS, "entryA")
    assert errors[OPT_PROTECT_CAMERAS] == "unifi_protect_no_camera"


@pytest.mark.asyncio
async def test_switching_protect_off_keeps_the_ports_for_later():
    """Kill: bookkeeping dropped while the feature is off (turning it back on
    would renumber the cameras)."""
    previous = {OPT_PROTECT_CAMERA: A, OPT_PROTECT_PORTS: {A: 8899, B: 8900}}
    errors, stored = await cf._protect_plan(_hass_flow(), previous, {OPT_PROTECT_ENABLED: False}, CAMS, "entryA")
    assert errors == {} and stored == previous


@pytest.mark.asyncio
async def test_configure_saves_the_bookkeeping_with_the_options():
    """Through the real options step. Kill: `user_input.update(protect_stored)`
    removed — the map would vanish on every save."""
    hass = _hass_flow()
    entry = MagicMock(entry_id="entryA", options={}, data={"cameras": CAMS, "all_cameras": CAMS})
    flow = cf.CuboAIOptionsFlowHandler()
    flow.hass, flow.config_entry = hass, entry
    flow.async_create_entry = lambda **kw: kw
    with (
        patch.object(cf, "setup_file_logger", MagicMock()),
        patch("custom_components.cuboai.go2rtc._port_bindable", return_value=True),
    ):
        result = await flow.async_step_init(_on(**{OPT_PROTECT_CAMERAS: [A, C]}))
    assert result["data"][OPT_PROTECT_PORTS] == {A: 8899, C: 8900}
    assert result["data"][OPT_PROTECT_CAMERA] == A


# =============================================================================
# Streams, sensor, diagnostics
# =============================================================================


def test_every_exposed_camera_gets_its_fixed_protect_stream():
    """Kill: the go2rtc alias declared for one camera only."""
    import asyncio

    from custom_components.cuboai.go2rtc import Go2RTCManager

    mgr = Go2RTCManager(MagicMock())
    mgr._cameras = [{"device_id": d, "uid": d} for d in (A, B, C)]
    mgr._options = {OPT_PROTECT_ENABLED: True, OPT_PROTECT_CAMERAS: [A, C]}
    mgr._streams = {}
    mgr._rtsp_port = 8557
    asyncio.run(mgr._resolve_codecs())
    assert sorted(n for n in mgr._streams if n.startswith("cuboai_protect_")) == [
        f"cuboai_protect_{A}",
        f"cuboai_protect_{C}",
    ]


def test_each_cameras_sensor_shows_its_own_protect_address():
    """Kill: the sensor asking the group for the wrong camera."""
    # test_issue_85 installs the HA platform stubs sensor.py imports.
    from .test_issue_85 import CuboWebRTCStreamSensor

    def stats(port):
        return {"running": True, "start_error": None, "address": f"10.0.0.5:{port}"}

    group = SimpleNamespace(
        for_camera=lambda d: {
            A: SimpleNamespace(stats=lambda: stats(8899)),
            C: SimpleNamespace(stats=lambda: stats(8900)),
        }.get(d)
    )
    attrs = {}
    for dev in (A, B, C):
        sensor = object.__new__(CuboWebRTCStreamSensor)
        sensor._device_id = dev
        sensor.hass = MagicMock()
        sensor.hass.data = {DOMAIN: {"entryA": {"onvif": group}}}
        sensor.coordinator = MagicMock()
        sensor.coordinator.config_entry.entry_id = "entryA"
        sensor.coordinator.config_entry.options = {}
        sensor.coordinator.config_entry.data = {}
        attrs[dev] = sensor.extra_state_attributes.get("unifi_protect_address")
    assert attrs == {A: "10.0.0.5:8899", B: None, C: "10.0.0.5:8900"}


@pytest.mark.asyncio
async def test_the_recorded_primary_keeps_the_base_port_whatever_the_list_order():
    """B was recorded as the primary (on 8899 with the host MAC); ticking A,
    which comes first in the camera list, must not take that over. Kill: the
    recorded primary not kept."""
    previous = {OPT_PROTECT_CAMERA: B, OPT_PROTECT_PORTS: {B: 8899}}
    with patch("custom_components.cuboai.go2rtc._port_bindable", return_value=True):
        _, stored = await cf._protect_plan(_hass_flow(), previous, _on(**{OPT_PROTECT_CAMERAS: [A, B]}), CAMS, "entryA")
    assert stored[OPT_PROTECT_CAMERA] == B
    assert stored[OPT_PROTECT_PORTS] == {B: 8899, A: 8900}


@pytest.mark.asyncio
async def test_an_upgraded_install_that_unticks_its_camera_hands_no_one_its_identity():
    """v2.6.40 exposed A on 8899 with the host MAC. Ticking only B and C must
    leave 8899 and that MAC to A — handing them to B would make Protect take B
    for A. Kill: the legacy primary rule removed."""
    previous = {OPT_PROTECT_ENABLED: True, OPT_PROTECT_PASSWORD: "pw"}
    with patch("custom_components.cuboai.go2rtc._port_bindable", return_value=True):
        _, stored = await cf._protect_plan(_hass_flow(), previous, _on(**{OPT_PROTECT_CAMERAS: [B, C]}), CAMS, "entryA")
    assert stored[OPT_PROTECT_CAMERA] == A
    assert stored[OPT_PROTECT_PORTS] == {B: 8900, C: 8901}
