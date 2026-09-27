"""ONVIF Media2 and the codec Protect is told about.

Media v1's encoding enum is JPEG | MPEG4 | H264, so over v1 an HEVC camera
(Cubo 3) can only be described as H.264. Seen live on a UDM: over v1 Protect
labelled an H.265 stream "h264" while passing the H.265 through; with Media2
offered it read Media2 only and labelled the same stream "h265". The label must
be the truth, so the codec comes from what go2rtc actually receives. Every test
names the mutation it kills.
"""

import asyncio
import socket
from unittest.mock import AsyncMock, patch
from xml.etree import ElementTree as ET

import pytest

from custom_components.cuboai import onvif_server as onvif
from custom_components.cuboai.const import DOMAIN, OPT_PROTECT_PORT

from .test_onvif_server import DEV, _call, _device, _digest_token, _entry, _hass, _req, _svc

NS = {
    "s": onvif.NS_SOAP,
    "tt": onvif.NS_SCHEMA,
    "trt": onvif.NS_MEDIA,
    "tr2": onvif.NS_MEDIA2,
    "tds": onvif.NS_DEVICE,
}


def _call2(device, op, auth=True, inner=""):
    return _call(device, op, onvif.NS_MEDIA2, auth=auth, inner=inner)


# =============================================================================
# The Media2 service
# =============================================================================


def test_media2_is_listed_in_the_service_map():
    """Protect only asks Media2 if GetServices names it. Kill: the entry removed."""
    device, spec = _device()
    _, root = _call(device, "GetServices", onvif.NS_DEVICE)
    xaddrs = {
        s.find("tds:Namespace", NS).text: s.find("tds:XAddr", NS).text
        for s in root.iter(f"{{{onvif.NS_DEVICE}}}Service")
    }
    assert xaddrs[onvif.NS_MEDIA2] == spec.media2_xaddr == "http://10.9.8.7:8899/onvif/media2_service"
    assert onvif.NS_MEDIA in xaddrs, "Media v1 must stay for clients that only speak v1"


@pytest.mark.parametrize("encoding", ["H264", "H265"])
def test_media2_profiles_say_the_real_codec(encoding):
    """THE point of Media2. Kill: the encoding hard-coded."""
    device, _ = _device(encoding=encoding)
    _, root = _call2(device, "GetProfiles")
    profiles = root.findall(".//tr2:Profiles", NS)
    assert [p.get("token") for p in profiles] == ["main", "sub"]
    for p in profiles:
        enc = p.find("tr2:Configurations/tr2:VideoEncoder", NS)
        assert enc.find("tt:Encoding", NS).text == encoding
        assert enc.find("tt:RateControl/tt:FrameRateLimit", NS) is not None, "RateControl missing"


def test_media2_encoder_configurations_and_options_agree_with_the_profiles():
    """Protect reads the options too (seen live: 2 calls). Kill: either one
    left on H264."""
    device, _ = _device(encoding="H265")
    _, conf = _call2(device, "GetVideoEncoderConfigurations")
    assert {e.text for e in conf.iter(f"{{{onvif.NS_SCHEMA}}}Encoding")} == {"H265"}
    _, opts = _call2(device, "GetVideoEncoderConfigurationOptions")
    assert opts.find(".//tr2:Options/tt:Encoding", NS).text == "H265"


def test_hevc_is_advertised_as_main_profile():
    """A Cubo 3's HEVC is Main; the H.264 profile name must not leak into it.
    Kill: codec_profile returning h264_profile for H265."""
    device, _ = _device(encoding="H265", h264_profile="High")
    _, root = _call2(device, "GetProfiles")
    assert {p.get("Profile") for p in root.iter(f"{{{onvif.NS_MEDIA2}}}VideoEncoder")} == {"Main"}
    device, _ = _device(encoding="H264", h264_profile="High")
    _, root = _call2(device, "GetProfiles")
    assert {p.get("Profile") for p in root.iter(f"{{{onvif.NS_MEDIA2}}}VideoEncoder")} == {"High"}


def test_media2_stream_uri_is_the_same_video_only_stream():
    """AAC breaks Protect's stream. Kill: `?video` dropped or another stream."""
    device, spec = _device()
    _, root = _call2(device, "GetStreamUri", inner="<Protocol>RTSP</Protocol><ProfileToken>main</ProfileToken>")
    assert root.find(".//tr2:Uri", NS).text == f"rtsp://10.9.8.7:8557/{spec.stream}?video"


def test_media2_snapshot_uri_is_on_the_pinned_port():
    """Protect caches it at adoption. Kill: go2rtc's self-healing API port used."""
    device, _ = _device()
    _, root = _call2(device, "GetSnapshotUri")
    assert root.find(".//tr2:Uri", NS).text == "http://10.9.8.7:8899/onvif/snapshot"


def test_media2_needs_credentials():
    """Kill: Media2 dispatched before the auth check."""
    device, _ = _device()
    status, _root = _call2(device, "GetProfiles", auth=False)
    assert status == 400 and device.auth_failures == 1


def test_media2_configuration_pushes_are_acknowledged_and_ignored():
    """Protect 7.2 pushes encoder settings; a fault fails the adoption. Kill:
    the no-op not reaching Media2."""
    device, _ = _device(encoding="H265")
    status, root = _call2(device, "SetVideoEncoderConfiguration")
    assert status == 200
    assert root.find(".//tr2:SetVideoEncoderConfigurationResponse", NS) is not None


def test_media_v1_still_says_h264_for_an_hevc_camera():
    """H265 is not a v1 enum value; saying it there is a schema violation that
    can fail a strict v1 client. Kill: v1 using spec.encoding."""
    device, _ = _device(encoding="H265")
    _, root = _call(device, "GetProfiles")
    assert {e.text for e in root.iter(f"{{{onvif.NS_SCHEMA}}}Encoding")} == {"H264"}


@pytest.mark.parametrize("op", sorted(onvif._MEDIA2_OPS))
def test_every_media2_operation_answers_well_formed_xml(op):
    device, _ = _device(encoding="H265")
    status, _root = _call2(device, op)
    assert status == 200


# =============================================================================
# Which codec: go2rtc's view of the camera
# =============================================================================


@pytest.mark.parametrize(
    "info, expected",
    [
        ({"producers": [{"medias": ["video, recvonly, H265", "audio, recvonly, MPEG4-GENERIC/16000"]}]}, "H265"),
        ({"producers": [{"medias": ["video, recvonly, HEVC"]}]}, "H265"),
        ({"producers": [{"medias": ["audio, recvonly, H265", "video, recvonly, H264"]}]}, "H264"),
        ({"producers": [{"medias": ["audio, recvonly, PCMA/8000"]}]}, None),
        ({"producers": [{"url": "exec:...", "medias": None}]}, None),  # still dialing
        ({"producers": []}, None),
        (None, None),
    ],
)
def test_the_codec_is_read_from_the_video_media_only(info, expected):
    """Kill: audio media consulted; idle/dialing treated as H.264 knowledge."""
    assert onvif.video_encoding_of(info) == expected


def test_the_h264_option_always_means_h264():
    """With the option on, Protect gets the transcode. Kill: the option ignored
    once the native codec is known."""
    service = _svc(_hass(), _entry({"h264_cameras": [DEV]}))
    service.native_encoding[DEV] = "H265"
    assert service.spec().encoding == "H264"


def test_without_the_option_the_native_codec_is_told_and_unknown_is_h264():
    """Kill: the learned codec not reaching the spec; unknown defaulting to H265."""
    service = _svc(_hass(), _entry({}))
    assert service.spec().encoding == "H264"
    service.native_encoding[DEV] = "H265"
    assert service.spec().encoding == "H265"
    assert service.stats()["advertised_encoding"] == "H265"


async def _fake_go2rtc(answers):
    """A go2rtc whose /api/streams answers from `answers` (popped per call)."""
    from aiohttp import web

    seen = []

    async def streams(request):
        seen.append(request.query.get("src"))
        return web.json_response(answers.pop(0) if answers else {})

    app = web.Application()
    app.router.add_get("/api/streams", streams)
    runner = web.AppRunner(app)
    await runner.setup()
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port, seen


H265_UP = {"producers": [{"medias": ["video, recvonly, H265"]}]}
IDLE = {"producers": [{"url": "exec:..."}]}


@pytest.mark.asyncio
async def test_the_codec_is_learned_from_the_cameras_own_stream_and_kept():
    """Kill: another stream asked (the alias would report the transcode);
    an idle producer erasing what was learned."""
    runner, port, seen = await _fake_go2rtc([H265_UP, IDLE])
    try:
        hass = _hass()
        hass.data[DOMAIN]["_ports_by_entry"]["entryA"]["api"] = port
        service = _svc(hass, _entry({}))
        await service._refresh_encoding()
        assert service.spec().encoding == "H265"
        assert seen == [f"cuboai_combined_{DEV}"]
        service._codec_checked.clear()  # past the throttle
        await service._refresh_encoding()
        assert len(seen) == 2
        assert service.spec().encoding == "H265", "an idle stream erased the known codec"
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_the_codec_check_is_throttled():
    """Protect polls every few seconds. Kill: the CODEC_REFRESH_S throttle removed."""
    runner, port, seen = await _fake_go2rtc([H265_UP, H265_UP])
    try:
        hass = _hass()
        hass.data[DOMAIN]["_ports_by_entry"]["entryA"]["api"] = port
        service = _svc(hass, _entry({}))
        await service._refresh_encoding()
        await service._refresh_encoding()
        assert len(seen) == 1
    finally:
        await runner.cleanup()


@pytest.mark.asyncio
async def test_a_dead_engine_leaves_the_codec_unchanged():
    """Kill: a connection error escaping into Protect's SOAP answer."""
    hass = _hass()
    hass.data[DOMAIN]["_ports_by_entry"]["entryA"]["api"] = 1  # nothing listens
    service = _svc(hass, _entry({}))
    service.native_encoding[DEV] = "H265"
    await asyncio.wait_for(service._refresh_encoding(), 5)
    assert service.spec().encoding == "H265"


@pytest.mark.asyncio
async def test_protect_is_told_h265_end_to_end():
    """Over a real socket, the way Protect asks. Kill: _handle not refreshing
    the codec before answering."""
    import aiohttp

    runner, api_port, _seen = await _fake_go2rtc([H265_UP])
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    onvif_port = free.getsockname()[1]
    free.close()
    hass = _hass()
    hass.data[DOMAIN]["_ports_by_entry"]["entryA"]["api"] = api_port
    service = _svc(hass, _entry({OPT_PROTECT_PORT: onvif_port}))
    with patch.object(onvif.WsDiscovery, "start", AsyncMock()), patch.object(onvif.WsDiscovery, "stop", AsyncMock()):
        assert await service.start()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{onvif_port}/onvif/media2_service",
                    data=_req("GetProfiles", onvif.NS_MEDIA2, _digest_token()),
                ) as resp:
                    assert resp.status == 200
                    root = ET.fromstring(await resp.read())
            assert {e.text for e in root.iter(f"{{{onvif.NS_SCHEMA}}}Encoding")} == {"H265"}
        finally:
            await service.stop()
            await runner.cleanup()
