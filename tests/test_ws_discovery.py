"""WS-Discovery: UniFi Protect finds the camera; Windows does not get confused.

UDP 3702 on a Home Assistant box is usually already held by the Samba add-on's
`wsdd` (Windows network discovery). We share the socket, so the one thing this
responder must never do is answer Windows' probes: Windows asks for
`wsdp:Device` (devprof namespace), ONVIF for `tds:Device` — same local name,
different namespace. Every test names the mutation it kills.
"""

import socket
from unittest.mock import patch
from xml.etree import ElementTree as ET

import pytest

from custom_components.cuboai import onvif_server as onvif

NS_DEVPROF = "http://schemas.xmlsoap.org/ws/2006/02/devprof"
NS_PUB = "http://schemas.microsoft.com/windows/pub/2005/07"
MSG_ID = "urn:uuid:11111111-2222-3333-4444-555555555555"


def _spec():
    return onvif.DeviceSpec(
        device_id="CB02AABBCCDD0011",
        host_ip="10.9.8.7",
        onvif_port=8899,
        rtsp_port=8557,
        api_port=1985,
        stream="cuboai_combined_CB02AABBCCDD0011",
        username="cuboai",
        password="pw",
    )


def _probe(types: str | None, extra_ns: str = "", action: str = onvif.WSD_PROBE) -> bytes:
    types_el = f"<d:Types{extra_ns if types is not None else ''}>{types}</d:Types>" if types is not None else ""
    return (
        f'<?xml version="1.0"?><s:Envelope xmlns:s="{onvif.NS_SOAP}" xmlns:a="{onvif.NS_WSA}" '
        f'xmlns:d="{onvif.NS_WSD}" xmlns:dn="{onvif.NS_NETWORK}" xmlns:tds="{onvif.NS_DEVICE}" '
        f'xmlns:wsdp="{NS_DEVPROF}" xmlns:pub="{NS_PUB}"><s:Header>'
        f"<a:MessageID>{MSG_ID}</a:MessageID><a:Action>{action}</a:Action></s:Header>"
        f"<s:Body><d:Probe>{types_el}</d:Probe></s:Body></s:Envelope>"
    ).encode()


# =============================================================================
# Whom we answer
# =============================================================================


def test_an_onvif_camera_probe_is_answered():
    """What Protect sends. Kill: NVT dropped from ONVIF_PROBE_TYPES."""
    assert onvif.probe_matches_onvif(_probe("dn:NetworkVideoTransmitter")) == MSG_ID


def test_an_onvif_device_probe_is_answered():
    assert onvif.probe_matches_onvif(_probe("tds:Device")) == MSG_ID


def test_a_windows_device_probe_is_ignored():
    """THE safety case: Windows' `wsdp:Device` shares the local name 'Device'
    with ONVIF's `tds:Device`. Answering it would put a phantom device into
    every Windows machine's Network view. Kill: match by local name only."""
    assert onvif.probe_matches_onvif(_probe("wsdp:Device")) is None


def test_a_windows_computer_probe_is_ignored():
    assert onvif.probe_matches_onvif(_probe("wsdp:Device pub:Computer")) is None


def test_a_probe_without_types_is_answered():
    """An untyped Probe asks every device (spec). Kill: empty Types rejected."""
    assert onvif.probe_matches_onvif(_probe(None)) == MSG_ID


def test_a_prefix_declared_on_the_types_element_resolves():
    """Namespaces can be declared anywhere. Kill: only Envelope-level
    declarations considered."""
    probe = _probe("x:NetworkVideoTransmitter", extra_ns=f' xmlns:x="{onvif.NS_NETWORK}"')
    assert onvif.probe_matches_onvif(probe) == MSG_ID


@pytest.mark.parametrize("action", [f"{onvif.NS_WSD}/Resolve", f"{onvif.NS_WSD}/Hello", "urn:other"])
def test_messages_that_are_not_probes_are_ignored(action):
    """Kill: the Action check removed (we would answer our own Hellos)."""
    assert onvif.probe_matches_onvif(_probe("dn:NetworkVideoTransmitter", action=action)) is None


def test_garbage_and_oversized_datagrams_are_ignored():
    assert onvif.probe_matches_onvif(b"\x00\xffnot xml") is None
    assert onvif.probe_matches_onvif(_probe("dn:NetworkVideoTransmitter") + b" " * 17000) is None


# =============================================================================
# What we say
# =============================================================================


def _parse(payload: bytes):
    return ET.fromstring(payload)


def test_the_probe_match_points_protect_at_our_device_service():
    """Kill: XAddrs or RelatesTo wrong/missing."""
    spec = _spec()
    root = _parse(onvif.probe_match(spec, MSG_ID))
    ns = {"a": onvif.NS_WSA, "d": onvif.NS_WSD}
    assert root.find(".//a:RelatesTo", ns).text == MSG_ID
    assert root.find(".//d:XAddrs", ns).text == spec.device_xaddr == "http://10.9.8.7:8899/onvif/device_service"
    assert "dn:NetworkVideoTransmitter" in root.find(".//d:Types", ns).text
    assert root.find(".//a:Address", ns).text == f"urn:uuid:{spec.endpoint_uuid}"


def test_the_endpoint_id_is_stable_and_not_the_camera_id():
    """Protect remembers the endpoint. Kill: a random uuid per start."""
    assert _spec().endpoint_uuid == _spec().endpoint_uuid
    assert "CB02AABBCCDD0011" not in _spec().endpoint_uuid


def test_hello_and_bye_are_well_formed_and_bye_carries_no_address():
    spec = _spec()
    ns = {"d": onvif.NS_WSD}
    assert _parse(onvif.hello(spec)).find(".//d:Hello/d:XAddrs", ns) is not None
    assert _parse(onvif.bye(spec)).find(".//d:Bye/d:XAddrs", ns) is None


def test_the_responder_answers_and_counts():
    responder = onvif.WsDiscovery(_spec)
    assert responder.reply_to(_probe("dn:NetworkVideoTransmitter")) is not None
    assert responder.reply_to(_probe("wsdp:Device")) is None
    assert responder.probes_answered == 1


# =============================================================================
# When the socket cannot be shared
# =============================================================================


@pytest.mark.asyncio
async def test_a_socket_that_cannot_be_shared_is_a_warning_not_a_failure():
    """Advanced Adoption by ip:port still works without discovery.
    Kill: the OSError swallowed-and-reported branch removed (start raises)."""
    responder = onvif.WsDiscovery(_spec)
    with patch.object(onvif.WsDiscovery, "_make_socket", side_effect=OSError(98, "Address in use")):
        await responder.start()
    assert responder.bound is False
    assert "Address in use" in responder.bind_error
    await responder.stop()  # must be safe after a failed start


def test_the_socket_is_opened_shareable():
    """Sharing 3702 with wsdd needs SO_REUSEADDR set BEFORE bind. Kill: the
    setsockopt removed."""
    calls = []
    real = socket.socket

    class Spy(real):
        def setsockopt(self, level, opt, value):
            calls.append((level, opt))
            return super().setsockopt(level, opt, value)

        def bind(self, addr):
            calls.append(("bind", addr))
            raise OSError("stop here")

    with patch.object(onvif.socket, "socket", Spy), pytest.raises(OSError):
        onvif.WsDiscovery._make_socket("127.0.0.1")
    assert calls.index((socket.SOL_SOCKET, socket.SO_REUSEADDR)) < calls.index(("bind", ("", onvif.WSD_PORT)))
