"""The ONVIF device UniFi Protect adopts (onvif_server.OnvifDevice).

Pure request -> response tests: every answer is parsed as XML, authentication is
checked with real WS-Security digests, and the guards that keep Protect from
changing — or rebooting — anything are pinned. Every test names the mutation
it kills.
"""

import base64
import datetime as dt
import hashlib
import os
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from xml.etree import ElementTree as ET

import pytest

from custom_components.cuboai import onvif_server as onvif
from custom_components.cuboai.const import (
    DOMAIN,
    OPT_PROTECT_CAMERA,
    OPT_PROTECT_ENABLED,
    OPT_PROTECT_PASSWORD,
    OPT_PROTECT_PORT,
    OPT_PROTECT_USERNAME,
    protect_camera_id,
    protect_stream_name,
    protect_stream_target,
)

DEV = "CB02AABBCCDD0011"
USER, PASSWORD = "cuboai", "s3cret-pw"
NS = {"s": onvif.NS_SOAP, "tt": onvif.NS_SCHEMA, "trt": onvif.NS_MEDIA, "tds": onvif.NS_DEVICE}


def _spec(**kw):
    base = {
        "device_id": DEV,
        "host_ip": "10.9.8.7",
        "onvif_port": 8899,
        "rtsp_port": 8557,
        "api_port": 1985,
        "stream": f"cuboai_combined_{DEV}",
        "username": USER,
        "password": PASSWORD,
        "firmware": "2.6.37",
        "mac": "aa:bb:cc:dd:ee:ff",
    }
    base.update(kw)
    return onvif.DeviceSpec(**base)


def _device(**kw):
    spec = _spec(**kw)
    return onvif.OnvifDevice(lambda: spec), spec


def _digest_token(user=USER, password=PASSWORD, nonce=None, created=None):
    nonce = nonce or os.urandom(16)
    created = created or dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    digest = base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()
    return (
        f'<wsse:Security xmlns:wsse="{onvif.NS_WSSE}" '
        'xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd">'
        f"<wsse:UsernameToken><wsse:Username>{user}</wsse:Username>"
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{digest}</wsse:Password>'
        f"<wsse:Nonce>{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created>"
        "</wsse:UsernameToken></wsse:Security>"
    )


def _text_token(user=USER, password=PASSWORD):
    return (
        f'<wsse:Security xmlns:wsse="{onvif.NS_WSSE}"><wsse:UsernameToken><wsse:Username>{user}</wsse:Username>'
        '<wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/'
        f'oasis-200401-wss-username-token-profile-1.0#PasswordText">{password}</wsse:Password>'
        "</wsse:UsernameToken></wsse:Security>"
    )


def _req(op, ns=onvif.NS_MEDIA, header="", inner=""):
    return (
        f'<?xml version="1.0"?><s:Envelope xmlns:s="{onvif.NS_SOAP}"><s:Header>{header}</s:Header>'
        f'<s:Body><{op} xmlns="{ns}">{inner}</{op}></s:Body></s:Envelope>'
    ).encode()


def _call(device, op, ns=onvif.NS_MEDIA, auth=True, inner=""):
    status, xml = device.handle(_req(op, ns, _digest_token() if auth else "", inner), "10.0.0.1")
    return status, ET.fromstring(xml)  # every answer must be well-formed XML


def _fault_subcode(root):
    el = root.find(".//s:Fault/s:Code/s:Subcode/s:Value", NS)
    return el.text if el is not None else None


# =============================================================================
# Authentication
# =============================================================================


@pytest.mark.parametrize(
    "op, ns",
    [
        ("GetSystemDateAndTime", onvif.NS_DEVICE),
        ("GetCapabilities", onvif.NS_DEVICE),
        ("GetServices", onvif.NS_DEVICE),
    ],
)
def test_the_spec_pre_auth_operations_answer_without_credentials(op, ns):
    """A client needs the clock and the service map BEFORE it can build a
    digest. Kill: an op removed from PRE_AUTH_OPS."""
    device, _ = _device()
    status, _root = _call(device, op, ns, auth=False)
    assert status == 200


def test_everything_else_requires_credentials():
    """Kill: the auth check removed, or everything made pre-auth."""
    device, _ = _device()
    status, root = _call(device, "GetProfiles", auth=False)
    assert status == 400 and _fault_subcode(root) == "ter:NotAuthorized"
    assert device.auth_failures == 1


def test_a_correct_password_digest_is_accepted():
    device, _ = _device()
    assert _call(device, "GetProfiles")[0] == 200


def test_a_wrong_password_digest_is_refused():
    """Kill: digest comparison inverted or skipped."""
    device, _ = _device()
    status, _ = device.handle(_req("GetProfiles", header=_digest_token(password="wrong")), None)
    assert status == 400


def test_a_wrong_username_is_refused():
    """Kill: username comparison skipped."""
    device, _ = _device()
    status, _ = device.handle(_req("GetProfiles", header=_digest_token(user="admin")), None)
    assert status == 400


def test_a_plain_text_password_is_accepted_and_checked():
    """Some clients send PasswordText. Kill: the text branch removed or unchecked."""
    device, _ = _device()
    assert device.handle(_req("GetProfiles", header=_text_token()), None)[0] == 200
    assert device.handle(_req("GetProfiles", header=_text_token(password="nope")), None)[0] == 400


def test_an_empty_configured_password_rejects_everyone():
    """Never an open door: with no password set, no credentials can match —
    not even an empty one. Kill: the `not password` guard removed."""
    device, _ = _device(password="")
    assert device.handle(_req("GetProfiles", header=_text_token(password="")), None)[0] == 400


def test_a_stale_created_timestamp_is_still_accepted():
    """No freshness window, on purpose: clock skew is the classic third-party
    camera auth failure. Kill: a Created-window check added."""
    device, _ = _device()
    token = _digest_token(created="2001-01-01T00:00:00Z")
    assert device.handle(_req("GetProfiles", header=token), None)[0] == 200


def test_the_digest_vector_is_the_ws_security_formula():
    """Kill: the digest formula changed (field order, hash)."""
    nonce, created = b"0123456789abcdef", "2026-09-27T10:00:00Z"
    expected = base64.b64encode(hashlib.sha1(nonce + created.encode() + b"pw").digest()).decode()
    assert onvif.password_digest(base64.b64encode(nonce).decode(), created, "pw") == expected


# =============================================================================
# What Protect needs to adopt and stream
# =============================================================================


def test_exactly_two_profiles_main_and_sub():
    """Protect wants a high and a low stream. go2rtc's server offered every
    stream it had (speaker, DVR, others' streams). Kill: profile count changed."""
    device, _ = _device()
    _, root = _call(device, "GetProfiles")
    profiles = root.findall(".//trt:Profiles", NS)
    assert [p.get("token") for p in profiles] == ["main", "sub"]


def test_every_profile_carries_h264_and_rate_control():
    """RateControl is mandatory for Protect — without it adoption fails
    (go2rtc #1520/#1994). Kill: RateControl dropped."""
    device, spec = _device()
    _, root = _call(device, "GetProfiles")
    for enc in root.findall(".//tt:VideoEncoderConfiguration", NS):
        assert enc.find("tt:Encoding", NS).text == "H264"
        rc = enc.find("tt:RateControl", NS)
        assert rc is not None, "RateControl missing"
        assert rc.find("tt:FrameRateLimit", NS).text == str(spec.fps)


def test_the_stream_uri_is_video_only_on_the_handed_out_stream():
    """AAC breaks Protect's stream, so v1 hands it video only. Kill: `?video`
    dropped, or the URI built from another stream."""
    device, spec = _device()
    _, root = _call(device, "GetStreamUri", inner="<ProfileToken>main</ProfileToken>")
    uri = root.find(".//tt:Uri", NS).text
    assert uri == f"rtsp://10.9.8.7:8557/{spec.stream}?video"


def test_the_stream_uri_carries_rtsp_credentials_when_the_listener_needs_them():
    """With NVR RTSP auth on, Protect must be able to pull the stream.
    Kill: rtsp_userinfo dropped from the URI."""
    device, _ = _device(rtsp_userinfo="nvr:pw@")
    _, root = _call(device, "GetStreamUri")
    assert root.find(".//tt:Uri", NS).text.startswith("rtsp://nvr:pw@10.9.8.7:8557/")


def test_the_snapshot_uri_is_on_our_pinned_port_not_go2rtcs():
    """Protect caches this URL at adoption. Pointing it at go2rtc's API port
    (which self-heals 1985 -> 1986) broke thumbnails on the next hop — seen
    live in the adoption loop. Kill: snapshot_uri back on the API port."""
    device, spec = _device()
    _, root = _call(device, "GetSnapshotUri")
    assert root.find(".//tt:Uri", NS).text == "http://10.9.8.7:8899/onvif/snapshot"
    assert spec.go2rtc_frame_url == f"http://127.0.0.1:1985/api/frame.jpeg?src={spec.stream}"


def test_device_information_never_exposes_the_camera_id():
    """Kill: the serial built from the raw device id."""
    device, spec = _device()
    _, root = _call(device, "GetDeviceInformation", onvif.NS_DEVICE)
    serial = root.find(".//tds:SerialNumber", NS).text
    assert serial == spec.serial and DEV not in serial and DEV.lower() not in serial.lower()
    assert root.find(".//tds:Manufacturer", NS).text == "CuboAI"
    assert _device()[1].serial == serial, "serial must be stable"


def test_network_interfaces_report_the_real_mac_or_nothing():
    """Protect identifies a camera by MAC. Kill: MAC omitted when known."""
    device, _ = _device()
    _, root = _call(device, "GetNetworkInterfaces", onvif.NS_DEVICE)
    assert root.find(".//tt:HwAddress", NS).text == "aa:bb:cc:dd:ee:ff"
    device, _ = _device(mac=None)
    _, root = _call(device, "GetNetworkInterfaces", onvif.NS_DEVICE)
    assert root.find(".//tt:HwAddress", NS) is None


def test_the_clock_is_current_utc():
    device, _ = _device()
    _, root = _call(device, "GetSystemDateAndTime", onvif.NS_DEVICE, auth=False)
    utc = root.find(".//tt:UTCDateTime", NS)
    got = dt.datetime(
        int(utc.find("tt:Date/tt:Year", NS).text),
        int(utc.find("tt:Date/tt:Month", NS).text),
        int(utc.find("tt:Date/tt:Day", NS).text),
        int(utc.find("tt:Time/tt:Hour", NS).text),
        int(utc.find("tt:Time/tt:Minute", NS).text),
        tzinfo=dt.UTC,
    )
    assert abs((dt.datetime.now(dt.UTC) - got).total_seconds()) < 120


@pytest.mark.parametrize("op", sorted(onvif._DEVICE_OPS))
def test_every_device_operation_answers_well_formed_xml(op):
    device, _ = _device()
    status, _root = _call(device, op, onvif.NS_DEVICE)
    assert status == 200


@pytest.mark.parametrize("op", sorted(onvif._MEDIA_OPS))
def test_every_media_operation_answers_well_formed_xml(op):
    device, _ = _device()
    status, _root = _call(device, op)
    assert status == 200


# =============================================================================
# Protect may change nothing
# =============================================================================


@pytest.mark.parametrize(
    "op, ns",
    [
        ("SetSystemDateAndTime", onvif.NS_DEVICE),
        ("SetNTP", onvif.NS_DEVICE),
        ("CreateUsers", onvif.NS_DEVICE),
        ("SetVideoEncoderConfiguration", onvif.NS_MEDIA),
        ("SetSystemFactoryDefault", onvif.NS_DEVICE),
    ],
)
def test_configuration_pushes_are_acknowledged_and_ignored(op, ns):
    """Protect 7.2 pushes settings and treats any fault as a failed adoption.
    Kill: the mutating-verb no-op removed (they would fault)."""
    device, _ = _device()
    status, root = _call(device, op, ns)
    assert status == 200
    assert root.find(".//s:Fault", NS) is None


def test_reboot_is_acknowledged_and_does_nothing():
    """SystemReboot must never restart anything. Kill: SystemReboot not
    treated as a no-op."""
    device, _ = _device()
    status, root = _call(device, "SystemReboot", onvif.NS_DEVICE)
    assert status == 200
    assert "not supported" in root.find(".//tds:Message", NS).text


def test_mutating_calls_still_need_credentials():
    """No-op or not, an anonymous caller gets nothing. Kill: no-ops placed
    before the auth check."""
    device, _ = _device()
    assert _call(device, "SetSystemDateAndTime", onvif.NS_DEVICE, auth=False)[0] == 400


# =============================================================================
# The loop's signal: what we do not answer yet
# =============================================================================


def test_an_unknown_operation_is_refused_and_counted():
    """Kill: unknown ops answered with success, or not counted."""
    device, _ = _device()
    status, root = _call(device, "GetFancyThing")
    assert status == 500 and _fault_subcode(root) == "ter:ActionNotSupported"
    assert device.unhandled == {"GetFancyThing": 1}


def test_event_subscriptions_are_not_mistaken_for_harmless_no_ops():
    """CreatePullPointSubscription starts with 'Create' but lives in the events
    service; an empty 'success' would be a broken subscription. Kill: the
    namespace check in dispatch removed."""
    device, _ = _device()
    status, root = _call(device, "CreatePullPointSubscription", onvif.NS_EVENTS)
    assert status == 500 and _fault_subcode(root) == "ter:ActionNotSupported"


def test_garbage_and_oversized_requests_fault_without_crashing():
    device, _ = _device()
    assert device.handle(b"not xml at all", None)[0] == 400
    assert device.handle(b"<a>" + b"x" * (onvif.MAX_REQUEST_BYTES + 1) + b"</a>", None)[0] == 400


def test_clients_and_requests_are_recorded_for_diagnostics():
    device, _ = _device()
    device.handle(_req("GetProfiles", header=_digest_token()), "192.0.2.10")
    assert device.clients == {"192.0.2.10"} and device.requests["GetProfiles"] == 1


# =============================================================================
# The service: which camera, which stream, which port
# =============================================================================


def _entry(options=None, cameras=None):
    return SimpleNamespace(
        entry_id="entryA",
        options={OPT_PROTECT_ENABLED: True, OPT_PROTECT_PASSWORD: PASSWORD, **(options or {})},
        data={"cameras": cameras if cameras is not None else [{"device_id": DEV}, {"device_id": "CB02FFEE00112233"}]},
    )


def _hass():
    hass = MagicMock()
    hass.data = {DOMAIN: {"_ports_by_entry": {"entryA": {"rtsp": 8557, "api": 1985}}}}

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = AsyncMock(side_effect=_run)
    return hass


def test_the_chosen_camera_is_served_and_a_removed_one_falls_back_to_the_first():
    """Kill: protect_camera_id ignoring the choice, or leaving a removed camera."""
    cams = [{"device_id": DEV}, {"device_id": "CB02FFEE00112233"}]
    assert protect_camera_id({OPT_PROTECT_CAMERA: "CB02FFEE00112233"}, cams) == "CB02FFEE00112233"
    assert protect_camera_id({OPT_PROTECT_CAMERA: "GONE0000"}, cams) == DEV
    assert protect_camera_id({}, []) is None


def test_protect_gets_one_fixed_stream_name_whatever_the_h264_option():
    """Protect locks in the stream address at adoption (seen live: a reconnect,
    its own re-adopt and a go2rtc restart all kept it on the old stream). So
    the name handed out never changes; the H.264 option only changes what is
    behind it. Kill: the name following the option again."""
    off = onvif.OnvifService(_hass(), _entry({})).spec()
    on = onvif.OnvifService(_hass(), _entry({"h264_cameras": [DEV]})).spec()
    assert off.stream == on.stream == protect_stream_name(DEV, {}) == f"cuboai_protect_{DEV}"
    assert protect_stream_target(DEV, {}) == f"cuboai_combined_{DEV}"
    assert protect_stream_target(DEV, {"h264_cameras": [DEV]}) == f"cuboai_h264_{DEV}"


def test_the_spec_follows_the_h264_option_and_nvr_auth():
    """The profile follows the option; NVR auth reaches the stream URI.
    Kill: profile ignoring the option; NVR creds dropped."""
    opts = {"h264_cameras": [DEV], "nvr_enabled": True, "nvr_username": "nvr", "nvr_password": "p w"}
    spec = onvif.OnvifService(_hass(), _entry(opts)).spec()
    assert spec.h264_profile == "High"
    assert onvif.OnvifService(_hass(), _entry({})).spec().h264_profile == "Main"
    assert spec.rtsp_userinfo == "nvr:p%20w@"
    assert spec.rtsp_port == 8557 and spec.username == "cuboai"


@pytest.mark.asyncio
async def test_no_password_means_the_service_never_starts():
    """Kill: the start-time password guard removed."""
    service = onvif.OnvifService(_hass(), _entry({OPT_PROTECT_PASSWORD: ""}))
    assert await service.start() is False
    assert service.running is False and "password" in service.start_error


@pytest.mark.asyncio
async def test_a_taken_port_is_reported_and_never_hopped():
    """Protect remembers ip:port at adoption; a silent hop would orphan the
    camera. Kill: fallback to another port added."""
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("0.0.0.0", 0))
    blocker.listen(1)
    taken = blocker.getsockname()[1]
    try:
        hass = _hass()
        service = onvif.OnvifService(hass, _entry({OPT_PROTECT_PORT: taken}))
        with patch.object(service, "_notify_port_conflict") as notified:
            assert await service.start() is False
        assert service.port == taken, "the port was changed"
        assert "onvif" not in hass.data[DOMAIN]["_ports_by_entry"]["entryA"]
        notified.assert_called_once()
    finally:
        blocker.close()


@pytest.mark.asyncio
async def test_the_service_serves_soap_over_http_and_publishes_its_port():
    """End to end over a real socket. Kill: route or publication removed."""
    import aiohttp

    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    hass = _hass()
    service = onvif.OnvifService(hass, _entry({OPT_PROTECT_PORT: port, OPT_PROTECT_USERNAME: USER}))
    with patch.object(onvif.WsDiscovery, "start", AsyncMock()), patch.object(onvif.WsDiscovery, "stop", AsyncMock()):
        assert await service.start() is True
        try:
            assert hass.data[DOMAIN]["_ports_by_entry"]["entryA"]["onvif"] == port
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{port}/onvif/device_service",
                    data=_req("GetSystemDateAndTime", onvif.NS_DEVICE),
                ) as resp:
                    assert resp.status == 200
                    assert "application/soap+xml" in resp.headers["Content-Type"]
                    ET.fromstring(await resp.read())
        finally:
            await service.stop()
    assert "onvif" not in hass.data[DOMAIN]["_ports_by_entry"]["entryA"]


@pytest.mark.asyncio
async def test_the_snapshot_follows_go2rtc_when_its_port_moves():
    """The live bug, end to end: go2rtc restarts onto another API port and the
    snapshot URL Protect cached must keep working. Kill: the proxy resolving
    the port once at start (or not at all)."""
    import aiohttp
    from aiohttp import web

    jpeg = bytes([0xFF, 0xD8, 0xFF, 0xE0]) + b"fake-frame"
    got = {}

    async def frame(request):
        got["src"] = request.query.get("src")
        return web.Response(body=jpeg, content_type="image/jpeg")

    fake = web.Application()
    fake.router.add_get("/api/frame.jpeg", frame)
    runner = web.AppRunner(fake)
    await runner.setup()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    engine_port = free.getsockname()[1]
    free.close()
    await web.TCPSite(runner, "127.0.0.1", engine_port).start()

    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    onvif_port = free.getsockname()[1]
    free.close()
    hass = _hass()
    hass.data[DOMAIN]["_ports_by_entry"]["entryA"]["api"] = 1  # stale: nothing listens there
    service = onvif.OnvifService(hass, _entry({OPT_PROTECT_PORT: onvif_port}))
    with patch.object(onvif.WsDiscovery, "start", AsyncMock()), patch.object(onvif.WsDiscovery, "stop", AsyncMock()):
        assert await service.start()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{onvif_port}/onvif/snapshot") as resp:
                    assert resp.status == 502, "a dead engine port must be an error, not a hang"
                # go2rtc "restarts" onto a new port:
                hass.data[DOMAIN]["_ports_by_entry"]["entryA"]["api"] = engine_port
                async with session.get(f"http://127.0.0.1:{onvif_port}/onvif/snapshot") as resp:
                    assert resp.status == 200
                    assert await resp.read() == jpeg
            assert got["src"] == f"cuboai_protect_{DEV}"
        finally:
            await service.stop()
            await runner.cleanup()


@pytest.mark.asyncio
async def test_an_engine_error_is_not_passed_off_as_a_picture():
    """go2rtc answering 500/404 ('stream not found') must become a 502, not an
    error text served to Protect as a JPEG. Kill: the status check removed."""
    import aiohttp
    from aiohttp import web

    async def broken(request):
        return web.Response(status=500, text="stream not found")

    fake = web.Application()
    fake.router.add_get("/api/frame.jpeg", broken)
    runner = web.AppRunner(fake)
    await runner.setup()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    engine_port = free.getsockname()[1]
    free.close()
    await web.TCPSite(runner, "127.0.0.1", engine_port).start()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    onvif_port = free.getsockname()[1]
    free.close()
    hass = _hass()
    hass.data[DOMAIN]["_ports_by_entry"]["entryA"]["api"] = engine_port
    service = onvif.OnvifService(hass, _entry({OPT_PROTECT_PORT: onvif_port}))
    with patch.object(onvif.WsDiscovery, "start", AsyncMock()), patch.object(onvif.WsDiscovery, "stop", AsyncMock()):
        assert await service.start()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"http://127.0.0.1:{onvif_port}/onvif/snapshot") as resp:
                    assert resp.status == 502
                    assert "image" not in (resp.headers.get("Content-Type") or "")
        finally:
            await service.stop()
            await runner.cleanup()
