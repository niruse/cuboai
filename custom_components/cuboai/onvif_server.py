"""Present one CuboAI camera to UniFi Protect as a third-party ONVIF camera.

UniFi Protect (5.0.33+) adopts third-party cameras over ONVIF: it finds them by
WS-Discovery or is given `ip:port` by hand ("Advanced Adoption"), then talks
SOAP to the device and pulls RTSP from the URI the device hands it.

go2rtc — already our media engine — ships its own ONVIF server, but it is not
fit to put in front of Protect:
  * it offers EVERY go2rtc stream as a profile of one device (the speaker-only
    backchannel, the DVR stream, even streams other integrations registered at
    runtime, one of them named after the baby);
  * it advertises the same hard-coded 1080p/30 fps for all of them;
  * it has no authentication, and answers every `Set*` with HTTP 400 — which
    Protect 7.2, now pushing encoding and time settings to third-party cameras,
    reports as "invalid credentials";
  * it has no WS-Discovery, so Protect can never find it on its own.

So this module is a small ONVIF device of our own, and go2rtc stays the media
engine: GetStreamUri points Protect at go2rtc's RTSP port, exactly as an NVR is
pointed today. It serves ONE camera, because Protect identifies a third-party
camera by the MAC address of the host it talks to — two cameras behind one
Home Assistant host would be merged into one.

Three parts:
  * OnvifDevice — the SOAP logic. Pure functions of bytes in / (status, xml) out,
    so the whole protocol is unit-tested without a network.
  * WsDiscovery — answers ONVIF Probes on 239.255.255.250:3702. The port is
    usually already held by the Samba add-on's `wsdd` (Windows network
    discovery), so the socket is shared, and only ONVIF probes are answered:
    matched by NAMESPACE, because Windows asks for `wsdp:Device` and ONVIF for
    `tds:Device` — same local name, different namespace.
  * OnvifService — owns the aiohttp server and the discovery socket for one
    config entry; started and stopped with it.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import hashlib
import hmac
import io
import logging
import re
import socket
import struct
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from .const import (
    DESIRED_ONVIF_PORT,
    DOMAIN,
    OPT_PROTECT_PASSWORD,
    OPT_PROTECT_PORT,
    OPT_PROTECT_USERNAME,
    PROTECT_USERNAME_DEFAULT,
    effective_ports,
    protect_camera_id,
    protect_stream_name,
)

_LOGGER = logging.getLogger(__name__)

NS_SOAP = "http://www.w3.org/2003/05/soap-envelope"
NS_DEVICE = "http://www.onvif.org/ver10/device/wsdl"
NS_MEDIA = "http://www.onvif.org/ver10/media/wsdl"
#: ONVIF Media2 - the only Media service that can say "H265". Media v1's
#: encoding enum is JPEG | MPEG4 | H264, so over v1 an HEVC camera (Cubo 3)
#: can only be described as H.264, and Protect labels it that way while
#: passing the real H.265 straight through to viewers (seen live).
NS_MEDIA2 = "http://www.onvif.org/ver20/media/wsdl"
NS_EVENTS = "http://www.onvif.org/ver10/events/wsdl"
NS_SCHEMA = "http://www.onvif.org/ver10/schema"
NS_NETWORK = "http://www.onvif.org/ver10/network/wsdl"
NS_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
NS_WSA = "http://schemas.xmlsoap.org/ws/2004/08/addressing"
NS_WSD = "http://schemas.xmlsoap.org/ws/2005/04/discovery"

WSD_GROUP = "239.255.255.250"
WSD_PORT = 3702
WSD_PROBE = f"{NS_WSD}/Probe"

#: Requests larger than this are refused before parsing. A real ONVIF request is
#: a few hundred bytes; the cap keeps a hostile client from feeding the parser.
MAX_REQUEST_BYTES = 64 * 1024

#: The ONVIF spec's pre-authentication set: a client must be able to read the
#: device's clock (to build a WS-Security digest) and its service map before it
#: has credentials. Everything else requires a valid UsernameToken.
PRE_AUTH_OPS = frozenset(
    {"GetSystemDateAndTime", "GetCapabilities", "GetServices", "GetServiceCapabilities", "GetWsdlUrl"}
)

#: Verbs that ask the device to CHANGE something. They are acknowledged and
#: ignored: Protect 7.2 pushes time, encoding and user settings to third-party
#: cameras and treats any fault as a failed adoption ("invalid credentials"),
#: yet nothing Protect sends may reconfigure the integration, the engine or the
#: camera — and above all, `SystemReboot` must not restart anything.
_MUTATING_PREFIXES = ("Set", "Create", "Delete", "Add", "Remove", "Start", "Stop", "Upgrade", "Restore")
_MUTATING_EXACT = frozenset({"SystemReboot", "SetSystemFactoryDefault"})

_ENV_OPEN = (
    '<?xml version="1.0" encoding="UTF-8"?>'
    f'<s:Envelope xmlns:s="{NS_SOAP}" xmlns:tds="{NS_DEVICE}" xmlns:trt="{NS_MEDIA}" xmlns:tr2="{NS_MEDIA2}" '
    f'xmlns:tev="{NS_EVENTS}" xmlns:tt="{NS_SCHEMA}" xmlns:ter="http://www.onvif.org/ver10/error">'
    "<s:Body>"
)
_ENV_CLOSE = "</s:Body></s:Envelope>"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _ns(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _envelope(body: str) -> str:
    return _ENV_OPEN + body + _ENV_CLOSE


def soap_fault(sender: bool, subcode: str, reason: str) -> tuple[int, str]:
    """A SOAP 1.2 fault. Sender faults are HTTP 400, Receiver faults 500."""
    code = "s:Sender" if sender else "s:Receiver"
    body = (
        f"<s:Fault><s:Code><s:Value>{code}</s:Value><s:Subcode><s:Value>ter:{subcode}</s:Value>"
        f'</s:Subcode></s:Code><s:Reason><s:Text xml:lang="en">{escape(reason)}</s:Text></s:Reason></s:Fault>'
    )
    return (400 if sender else 500), _envelope(body)


# ── request parsing and WS-Security ──────────────────────────────────────────


@dataclass
class UsernameToken:
    username: str
    password: str
    digest: bool
    nonce: str
    created: str


@dataclass
class SoapRequest:
    operation: str
    namespace: str
    element: ET.Element
    token: UsernameToken | None


def parse_request(body: bytes) -> SoapRequest:
    """The operation and WS-Security token of one SOAP request. Raises ValueError."""
    if len(body) > MAX_REQUEST_BYTES:
        raise ValueError("request too large")
    try:
        root = ET.fromstring(body)
    except ET.ParseError as err:
        raise ValueError(f"not XML: {err}") from err
    soap_body = next((el for el in root if _local(el.tag) == "Body"), None)
    if soap_body is None or not len(soap_body):
        raise ValueError("no SOAP body")
    op_el = soap_body[0]
    return SoapRequest(_local(op_el.tag), _ns(op_el.tag), op_el, _parse_token(root))


def _parse_token(root: ET.Element) -> UsernameToken | None:
    token = next((el for el in root.iter() if _local(el.tag) == "UsernameToken"), None)
    if token is None:
        return None
    fields = {_local(el.tag): el for el in token}
    password_el = fields.get("Password")
    return UsernameToken(
        username=(fields["Username"].text or "") if "Username" in fields else "",
        password=(password_el.text or "") if password_el is not None else "",
        digest=password_el is not None and (password_el.get("Type") or "").endswith("#PasswordDigest"),
        nonce=(fields["Nonce"].text or "") if "Nonce" in fields else "",
        created=(fields["Created"].text or "") if "Created" in fields else "",
    )


def password_digest(nonce_b64: str, created: str, password: str) -> str:
    """WS-Security PasswordDigest: Base64(SHA-1(nonce + created + password))."""
    nonce = base64.b64decode(nonce_b64, validate=True)
    return base64.b64encode(hashlib.sha1(nonce + created.encode() + password.encode()).digest()).decode()


def token_is_valid(token: UsernameToken | None, username: str, password: str) -> bool:
    """Constant-time check of a UsernameToken.

    Deliberately NO `Created` freshness window: a device whose clock disagrees
    with the client's rejects every digest ("Wsse authorized time check
    failed" is the classic third-party-camera failure), and Protect 7.2 manages
    third-party time itself. Replay on a LAN is not the threat worth that.
    """
    if token is None or not password:
        return False
    if not hmac.compare_digest(token.username.encode(), username.encode()):
        return False
    if token.digest:
        try:
            expected = password_digest(token.nonce, token.created, password)
        except (ValueError, TypeError):
            return False
        return hmac.compare_digest(expected.encode(), token.password.encode())
    return hmac.compare_digest(token.password.encode(), password.encode())


# ── the device ───────────────────────────────────────────────────────────────


@dataclass
class DeviceSpec:
    """Everything the SOAP answers are built from, captured per request so that
    ports changed by a go2rtc restart are picked up immediately."""

    device_id: str
    host_ip: str
    onvif_port: int
    rtsp_port: int
    api_port: int
    stream: str
    username: str
    password: str
    firmware: str = "unknown"
    mac: str | None = None
    rtsp_userinfo: str = ""  # "user:pass@" when the RTSP listener requires auth
    h264_profile: str = "Main"  # native Cubo 2 SPS is Main; the transcode is High
    #: What the stream really carries: "H264" or "H265". Media2 advertises it
    #: honestly; Media v1 cannot express H265 and always says H264.
    encoding: str = "H264"
    width: int = 1920
    height: int = 1080
    fps: int = 10
    bitrate_kbps: int = 2048
    gov_length: int = 20

    @property
    def serial(self) -> str:
        """Stable per camera, but never the camera's own id."""
        return hashlib.sha1(f"cuboai:{self.device_id}".encode()).hexdigest()[:12].upper()

    @property
    def endpoint_uuid(self) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"cuboai:onvif:{self.device_id}"))

    @property
    def device_xaddr(self) -> str:
        return f"http://{self.host_ip}:{self.onvif_port}/onvif/device_service"

    @property
    def media_xaddr(self) -> str:
        return f"http://{self.host_ip}:{self.onvif_port}/onvif/media_service"

    @property
    def media2_xaddr(self) -> str:
        return f"http://{self.host_ip}:{self.onvif_port}/onvif/media2_service"

    @property
    def codec_profile(self) -> str:
        """The encoder profile for the advertised codec (HEVC from a Cubo 3 is Main)."""
        return self.h264_profile if self.encoding == "H264" else "Main"

    @property
    def stream_uri(self) -> str:
        # `?video`: Protect cannot play AAC (it breaks the stream), so v1 hands
        # it video only. go2rtc serves the same stream without the audio track.
        return f"rtsp://{self.rtsp_userinfo}{self.host_ip}:{self.rtsp_port}/{self.stream}?video"

    @property
    def snapshot_uri(self) -> str:
        # Served from OUR pinned port, not go2rtc's: Protect caches this URL at
        # adoption, and go2rtc's API port self-heals (1985 -> 1986 whenever a
        # restart finds it still releasing). Pointing Protect at go2rtc directly
        # left it with a snapshot link that broke on the next hop — seen live.
        return f"http://{self.host_ip}:{self.onvif_port}/onvif/snapshot"

    @property
    def go2rtc_frame_url(self) -> str:
        """Where the snapshot actually comes from, resolved per request."""
        return f"http://127.0.0.1:{self.api_port}/api/frame.jpeg?src={self.stream}"

    @property
    def scopes(self) -> list[str]:
        return [
            "onvif://www.onvif.org/type/video_encoder",
            "onvif://www.onvif.org/Profile/Streaming",
            "onvif://www.onvif.org/name/CuboAI",
            "onvif://www.onvif.org/hardware/Baby%20Monitor",
            "onvif://www.onvif.org/location/home",
        ]


PROFILE_TOKENS = ("main", "sub")


class OnvifDevice:
    """Answers ONVIF SOAP requests for one camera. No I/O."""

    def __init__(self, spec_provider):
        self._spec = spec_provider  # zero-arg callable -> DeviceSpec
        self.requests: Counter = Counter()
        self.unhandled: Counter = Counter()
        self.auth_failures = 0
        self.clients: set[str] = set()

    def handle(self, body: bytes, client: str | None = None) -> tuple[int, str]:
        try:
            req = parse_request(body)
        except ValueError as err:
            return soap_fault(True, "InvalidArgs", f"Malformed request: {err}")
        spec = self._spec()
        if client:
            self.clients.add(client)
        self.requests[req.operation] += 1
        if req.operation not in PRE_AUTH_OPS and not token_is_valid(req.token, spec.username, spec.password):
            self.auth_failures += 1
            _LOGGER.info(
                "ONVIF %s from %s rejected: %s",
                req.operation,
                client,
                "no credentials" if req.token is None else "wrong username or password",
            )
            return soap_fault(True, "NotAuthorized", "Sender not authorized")
        handler = self._handler(req)
        if handler is None:
            self.unhandled[req.operation] += 1
            _LOGGER.warning(
                "ONVIF operation not supported yet: %s (%s) from %s — please report it",
                req.operation,
                req.namespace,
                client,
            )
            return soap_fault(False, "ActionNotSupported", f"{req.operation} is not supported")
        return 200, _envelope(handler(spec, req))

    # — dispatch —

    def _handler(self, req: SoapRequest):
        op = req.operation
        if req.namespace == NS_DEVICE:
            table = _DEVICE_OPS
            prefix = "tds"
        elif req.namespace == NS_MEDIA:
            table = _MEDIA_OPS
            prefix = "trt"
        elif req.namespace == NS_MEDIA2:
            table = _MEDIA2_OPS
            prefix = "tr2"
        else:
            return None
        if op in table:
            return table[op]
        if op in _MUTATING_EXACT or op.startswith(_MUTATING_PREFIXES):
            return lambda spec, _req, _op=op, _p=prefix: _acknowledged(_p, _op)
        return None


def _acknowledged(prefix: str, op: str) -> str:
    """The empty success response for a mutating call we ignore on purpose."""
    if op == "SystemReboot":
        return f"<tds:SystemRebootResponse><tds:Message>{escape('Reboot is not supported')}</tds:Message></tds:SystemRebootResponse>"
    return f"<{prefix}:{op}Response/>"


# — device service —


def _get_system_date_and_time(spec: DeviceSpec, _req) -> str:
    now = dt.datetime.now(dt.UTC)
    return (
        "<tds:GetSystemDateAndTimeResponse><tds:SystemDateAndTime>"
        "<tt:DateTimeType>NTP</tt:DateTimeType><tt:DaylightSavings>false</tt:DaylightSavings>"
        "<tt:TimeZone><tt:TZ>UTC0</tt:TZ></tt:TimeZone><tt:UTCDateTime>"
        f"<tt:Time><tt:Hour>{now.hour}</tt:Hour><tt:Minute>{now.minute}</tt:Minute><tt:Second>{now.second}</tt:Second></tt:Time>"
        f"<tt:Date><tt:Year>{now.year}</tt:Year><tt:Month>{now.month}</tt:Month><tt:Day>{now.day}</tt:Day></tt:Date>"
        "</tt:UTCDateTime></tds:SystemDateAndTime></tds:GetSystemDateAndTimeResponse>"
    )


def _get_device_information(spec: DeviceSpec, _req) -> str:
    return (
        "<tds:GetDeviceInformationResponse><tds:Manufacturer>CuboAI</tds:Manufacturer>"
        "<tds:Model>Baby Monitor</tds:Model>"
        f"<tds:FirmwareVersion>{escape(spec.firmware)}</tds:FirmwareVersion>"
        f"<tds:SerialNumber>{spec.serial}</tds:SerialNumber><tds:HardwareId>cuboai-onvif</tds:HardwareId>"
        "</tds:GetDeviceInformationResponse>"
    )


def _get_capabilities(spec: DeviceSpec, _req) -> str:
    return (
        "<tds:GetCapabilitiesResponse><tds:Capabilities>"
        f"<tt:Device><tt:XAddr>{spec.device_xaddr}</tt:XAddr>"
        "<tt:Network><tt:IPFilter>false</tt:IPFilter><tt:ZeroConfiguration>false</tt:ZeroConfiguration>"
        "<tt:IPVersion6>false</tt:IPVersion6><tt:DynDNS>false</tt:DynDNS></tt:Network>"
        "<tt:System><tt:DiscoveryResolve>false</tt:DiscoveryResolve><tt:DiscoveryBye>true</tt:DiscoveryBye>"
        "<tt:RemoteDiscovery>false</tt:RemoteDiscovery><tt:SystemBackup>false</tt:SystemBackup>"
        "<tt:SystemLogging>false</tt:SystemLogging><tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>"
        "<tt:SupportedVersions><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tt:SupportedVersions></tt:System>"
        "<tt:Security><tt:TLS1.1>false</tt:TLS1.1><tt:TLS1.2>false</tt:TLS1.2>"
        "<tt:OnboardKeyGeneration>false</tt:OnboardKeyGeneration><tt:AccessPolicyConfig>false</tt:AccessPolicyConfig>"
        "<tt:X.509Token>false</tt:X.509Token><tt:SAMLToken>false</tt:SAMLToken>"
        "<tt:KerberosToken>false</tt:KerberosToken><tt:RELToken>false</tt:RELToken></tt:Security></tt:Device>"
        f"<tt:Media><tt:XAddr>{spec.media_xaddr}</tt:XAddr><tt:StreamingCapabilities>"
        "<tt:RTPMulticast>false</tt:RTPMulticast><tt:RTP_TCP>true</tt:RTP_TCP>"
        "<tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP></tt:StreamingCapabilities></tt:Media>"
        "</tds:Capabilities></tds:GetCapabilitiesResponse>"
    )


def _get_services(spec: DeviceSpec, _req) -> str:
    def service(ns: str, xaddr: str) -> str:
        return (
            f"<tds:Service><tds:Namespace>{ns}</tds:Namespace><tds:XAddr>{xaddr}</tds:XAddr>"
            "<tds:Version><tt:Major>2</tt:Major><tt:Minor>5</tt:Minor></tds:Version></tds:Service>"
        )

    return (
        "<tds:GetServicesResponse>"
        + service(NS_DEVICE, spec.device_xaddr)
        + service(NS_MEDIA, spec.media_xaddr)
        + service(NS_MEDIA2, spec.media2_xaddr)
        + "</tds:GetServicesResponse>"
    )


def _get_device_service_capabilities(spec: DeviceSpec, _req) -> str:
    return (
        "<tds:GetServiceCapabilitiesResponse><tds:Capabilities>"
        '<tds:Network IPFilter="false" ZeroConfiguration="false" IPVersion6="false" DynDNS="false"/>'
        '<tds:Security UsernameToken="true" HttpDigest="false"/>'
        '<tds:System DiscoveryResolve="false" DiscoveryBye="true" RemoteDiscovery="false" '
        'SystemBackup="false" SystemLogging="false" FirmwareUpgrade="false"/>'
        "</tds:Capabilities></tds:GetServiceCapabilitiesResponse>"
    )


def _get_scopes(spec: DeviceSpec, _req) -> str:
    items = "".join(
        f"<tds:Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>{s}</tt:ScopeItem></tds:Scopes>"
        for s in spec.scopes
    )
    return f"<tds:GetScopesResponse>{items}</tds:GetScopesResponse>"


def _get_hostname(spec: DeviceSpec, _req) -> str:
    return (
        "<tds:GetHostnameResponse><tds:HostnameInformation><tt:FromDHCP>false</tt:FromDHCP>"
        "<tt:Name>cuboai-camera</tt:Name></tds:HostnameInformation></tds:GetHostnameResponse>"
    )


def _get_network_interfaces(spec: DeviceSpec, _req) -> str:
    if not spec.mac:
        return "<tds:GetNetworkInterfacesResponse/>"
    return (
        '<tds:GetNetworkInterfacesResponse><tds:NetworkInterfaces token="eth0"><tt:Enabled>true</tt:Enabled>'
        f"<tt:Info><tt:Name>eth0</tt:Name><tt:HwAddress>{spec.mac}</tt:HwAddress><tt:MTU>1500</tt:MTU></tt:Info>"
        "<tt:IPv4><tt:Enabled>true</tt:Enabled><tt:Config><tt:Manual>"
        f"<tt:Address>{spec.host_ip}</tt:Address><tt:PrefixLength>24</tt:PrefixLength></tt:Manual>"
        "<tt:DHCP>false</tt:DHCP></tt:Config></tt:IPv4></tds:NetworkInterfaces></tds:GetNetworkInterfacesResponse>"
    )


def _get_network_protocols(spec: DeviceSpec, _req) -> str:
    return (
        "<tds:GetNetworkProtocolsResponse>"
        f"<tds:NetworkProtocols><tt:Name>HTTP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{spec.onvif_port}</tt:Port></tds:NetworkProtocols>"
        f"<tds:NetworkProtocols><tt:Name>RTSP</tt:Name><tt:Enabled>true</tt:Enabled><tt:Port>{spec.rtsp_port}</tt:Port></tds:NetworkProtocols>"
        "</tds:GetNetworkProtocolsResponse>"
    )


def _get_users(spec: DeviceSpec, _req) -> str:
    return (
        f"<tds:GetUsersResponse><tds:User><tt:Username>{escape(spec.username)}</tt:Username>"
        "<tt:UserLevel>Administrator</tt:UserLevel></tds:User></tds:GetUsersResponse>"
    )


_DEVICE_OPS = {
    "GetSystemDateAndTime": _get_system_date_and_time,
    "GetDeviceInformation": _get_device_information,
    "GetCapabilities": _get_capabilities,
    "GetServices": _get_services,
    "GetServiceCapabilities": _get_device_service_capabilities,
    "GetScopes": _get_scopes,
    "GetHostname": _get_hostname,
    "GetNetworkInterfaces": _get_network_interfaces,
    "GetNetworkProtocols": _get_network_protocols,
    "GetUsers": _get_users,
    "GetDNS": lambda s, r: (
        "<tds:GetDNSResponse><tds:DNSInformation><tt:FromDHCP>true</tt:FromDHCP></tds:DNSInformation></tds:GetDNSResponse>"
    ),
    "GetNTP": lambda s, r: (
        "<tds:GetNTPResponse><tds:NTPInformation><tt:FromDHCP>true</tt:FromDHCP></tds:NTPInformation></tds:GetNTPResponse>"
    ),
    "GetNetworkDefaultGateway": lambda s, r: (
        "<tds:GetNetworkDefaultGatewayResponse><tds:NetworkGateway/></tds:GetNetworkDefaultGatewayResponse>"
    ),
    "GetDiscoveryMode": lambda s, r: (
        "<tds:GetDiscoveryModeResponse><tds:DiscoveryMode>Discoverable</tds:DiscoveryMode></tds:GetDiscoveryModeResponse>"
    ),
    "GetWsdlUrl": lambda s, r: (
        "<tds:GetWsdlUrlResponse><tds:WsdlUrl>http://www.onvif.org/</tds:WsdlUrl></tds:GetWsdlUrlResponse>"
    ),
}


# — media service —


def _video_source_configuration(spec: DeviceSpec) -> str:
    return (
        "<tt:Name>vsc</tt:Name><tt:UseCount>2</tt:UseCount><tt:SourceToken>vs</tt:SourceToken>"
        f'<tt:Bounds x="0" y="0" width="{spec.width}" height="{spec.height}"/>'
    )


def _video_encoder_configuration(spec: DeviceSpec, token: str) -> str:
    # RateControl is not optional for Protect: without it adoption fails (and
    # an AI Port logs "Channel fps is not found") — go2rtc #1520 / #1994.
    return (
        f"<tt:Name>{token}</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>H264</tt:Encoding>"
        f"<tt:Resolution><tt:Width>{spec.width}</tt:Width><tt:Height>{spec.height}</tt:Height></tt:Resolution>"
        "<tt:Quality>5</tt:Quality>"
        f"<tt:RateControl><tt:FrameRateLimit>{spec.fps}</tt:FrameRateLimit><tt:EncodingInterval>1</tt:EncodingInterval>"
        f"<tt:BitrateLimit>{spec.bitrate_kbps}</tt:BitrateLimit></tt:RateControl>"
        f"<tt:H264><tt:GovLength>{spec.gov_length}</tt:GovLength><tt:H264Profile>{spec.h264_profile}</tt:H264Profile></tt:H264>"
        "<tt:Multicast><tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>"
        "<tt:Port>0</tt:Port><tt:TTL>0</tt:TTL><tt:AutoStart>false</tt:AutoStart></tt:Multicast>"
        "<tt:SessionTimeout>PT60S</tt:SessionTimeout>"
    )


def _profile(spec: DeviceSpec, token: str, tag: str = "trt:Profiles") -> str:
    return (
        f'<{tag} token="{token}" fixed="true"><tt:Name>{token}</tt:Name>'
        f'<tt:VideoSourceConfiguration token="vsc">{_video_source_configuration(spec)}</tt:VideoSourceConfiguration>'
        f'<tt:VideoEncoderConfiguration token="vec_{token}">{_video_encoder_configuration(spec, "vec_" + token)}'
        f"</tt:VideoEncoderConfiguration></{tag}>"
    )


def _requested_token(req: SoapRequest, name: str) -> str | None:
    el = next((c for c in req.element.iter() if _local(c.tag) == name), None)
    return (el.text or "").strip() if el is not None else None


def _get_profiles(spec: DeviceSpec, _req) -> str:
    # Two profiles, both on the same stream: Protect wants a high and a low
    # stream and picks them itself. v1 serves one stream for both rather than
    # paying for a second transcode on the HA box.
    return (
        "<trt:GetProfilesResponse>" + "".join(_profile(spec, t) for t in PROFILE_TOKENS) + "</trt:GetProfilesResponse>"
    )


def _get_profile(spec: DeviceSpec, req) -> str:
    token = _requested_token(req, "ProfileToken")
    token = token if token in PROFILE_TOKENS else PROFILE_TOKENS[0]
    return f"<trt:GetProfileResponse>{_profile(spec, token, 'trt:Profile')}</trt:GetProfileResponse>"


def _get_video_sources(spec: DeviceSpec, _req) -> str:
    return (
        '<trt:GetVideoSourcesResponse><trt:VideoSources token="vs">'
        f"<tt:Framerate>{spec.fps}</tt:Framerate>"
        f"<tt:Resolution><tt:Width>{spec.width}</tt:Width><tt:Height>{spec.height}</tt:Height></tt:Resolution>"
        "</trt:VideoSources></trt:GetVideoSourcesResponse>"
    )


def _vsc_list(op: str, element: str):
    def handler(spec: DeviceSpec, _req) -> str:
        return (
            f'<trt:{op}Response><trt:{element} token="vsc">{_video_source_configuration(spec)}'
            f"</trt:{element}></trt:{op}Response>"
        )

    return handler


def _vec_list(op: str, element: str, single: bool = False):
    def handler(spec: DeviceSpec, req) -> str:
        tokens = PROFILE_TOKENS
        if single:
            wanted = (_requested_token(req, "ConfigurationToken") or "").removeprefix("vec_")
            tokens = (wanted if wanted in PROFILE_TOKENS else PROFILE_TOKENS[0],)
        items = "".join(
            f'<trt:{element} token="vec_{t}">{_video_encoder_configuration(spec, "vec_" + t)}</trt:{element}>'
            for t in tokens
        )
        return f"<trt:{op}Response>{items}</trt:{op}Response>"

    return handler


def _get_video_encoder_configuration_options(spec: DeviceSpec, _req) -> str:
    return (
        "<trt:GetVideoEncoderConfigurationOptionsResponse><trt:Options>"
        "<tt:QualityRange><tt:Min>1</tt:Min><tt:Max>10</tt:Max></tt:QualityRange><tt:H264>"
        f"<tt:ResolutionsAvailable><tt:Width>{spec.width}</tt:Width><tt:Height>{spec.height}</tt:Height></tt:ResolutionsAvailable>"
        "<tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>250</tt:Max></tt:GovLengthRange>"
        f"<tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>{spec.fps}</tt:Max></tt:FrameRateRange>"
        "<tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>"
        f"<tt:H264ProfilesSupported>{spec.h264_profile}</tt:H264ProfilesSupported>"
        "</tt:H264></trt:Options></trt:GetVideoEncoderConfigurationOptionsResponse>"
    )


def _media_uri(op: str, uri: str) -> str:
    return (
        f"<trt:{op}Response><trt:MediaUri><tt:Uri>{escape(uri)}</tt:Uri>"
        "<tt:InvalidAfterConnect>false</tt:InvalidAfterConnect><tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>"
        f"<tt:Timeout>PT0S</tt:Timeout></trt:MediaUri></trt:{op}Response>"
    )


def _get_media_service_capabilities(spec: DeviceSpec, _req) -> str:
    return (
        '<trt:GetServiceCapabilitiesResponse><trt:Capabilities SnapshotUri="true" Rotation="false" '
        'VideoSourceMode="false" OSD="false"><trt:ProfileCapabilities MaximumNumberOfProfiles="2"/>'
        '<trt:StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true"/>'
        "</trt:Capabilities></trt:GetServiceCapabilitiesResponse>"
    )


def _empty(op: str):
    return lambda spec, req: f"<trt:{op}Response/>"


_MEDIA_OPS = {
    "GetServiceCapabilities": _get_media_service_capabilities,
    "GetProfiles": _get_profiles,
    "GetProfile": _get_profile,
    "GetVideoSources": _get_video_sources,
    "GetVideoSourceConfigurations": _vsc_list("GetVideoSourceConfigurations", "Configurations"),
    "GetVideoSourceConfiguration": _vsc_list("GetVideoSourceConfiguration", "Configuration"),
    "GetCompatibleVideoSourceConfigurations": _vsc_list("GetCompatibleVideoSourceConfigurations", "Configurations"),
    "GetVideoEncoderConfigurations": _vec_list("GetVideoEncoderConfigurations", "Configurations"),
    "GetVideoEncoderConfiguration": _vec_list("GetVideoEncoderConfiguration", "Configuration", single=True),
    "GetCompatibleVideoEncoderConfigurations": _vec_list("GetCompatibleVideoEncoderConfigurations", "Configurations"),
    "GetVideoEncoderConfigurationOptions": _get_video_encoder_configuration_options,
    "GetGuaranteedNumberOfVideoEncoderInstances": lambda s, r: (
        "<trt:GetGuaranteedNumberOfVideoEncoderInstancesResponse><trt:TotalNumber>2</trt:TotalNumber>"
        "<trt:H264>2</trt:H264></trt:GetGuaranteedNumberOfVideoEncoderInstancesResponse>"
    ),
    "GetStreamUri": lambda s, r: _media_uri("GetStreamUri", s.stream_uri),
    "GetSnapshotUri": lambda s, r: _media_uri("GetSnapshotUri", s.snapshot_uri),
    # v1 is video-only: no audio, no analytics, no metadata, no OSD.
    "GetAudioSources": _empty("GetAudioSources"),
    "GetAudioSourceConfigurations": _empty("GetAudioSourceConfigurations"),
    "GetAudioEncoderConfigurations": _empty("GetAudioEncoderConfigurations"),
    "GetAudioOutputs": _empty("GetAudioOutputs"),
    "GetAudioOutputConfigurations": _empty("GetAudioOutputConfigurations"),
    "GetAudioDecoderConfigurations": _empty("GetAudioDecoderConfigurations"),
    "GetCompatibleAudioSourceConfigurations": _empty("GetCompatibleAudioSourceConfigurations"),
    "GetCompatibleAudioEncoderConfigurations": _empty("GetCompatibleAudioEncoderConfigurations"),
    "GetVideoAnalyticsConfigurations": _empty("GetVideoAnalyticsConfigurations"),
    "GetMetadataConfigurations": _empty("GetMetadataConfigurations"),
    "GetOSDs": _empty("GetOSDs"),
}


# - media2 service (ver20) -


def _v2_encoder(spec: DeviceSpec, token: str, tag: str) -> str:
    """A VideoEncoder2Configuration. Encoding is a free string here, so H265 can be said."""
    return (
        f'<{tag} token="vec_{token}" GovLength="{spec.gov_length}" Profile="{spec.codec_profile}">'
        f"<tt:Name>vec_{token}</tt:Name><tt:UseCount>1</tt:UseCount><tt:Encoding>{spec.encoding}</tt:Encoding>"
        f"<tt:Resolution><tt:Width>{spec.width}</tt:Width><tt:Height>{spec.height}</tt:Height></tt:Resolution>"
        f'<tt:RateControl ConstantBitRate="false"><tt:FrameRateLimit>{spec.fps}</tt:FrameRateLimit>'
        f"<tt:BitrateLimit>{spec.bitrate_kbps}</tt:BitrateLimit></tt:RateControl>"
        f"<tt:Quality>5</tt:Quality></{tag}>"
    )


def _v2_source(spec: DeviceSpec, tag: str) -> str:
    return f'<{tag} token="vsc">{_video_source_configuration(spec)}</{tag}>'


def _v2_profile(spec: DeviceSpec, token: str) -> str:
    return (
        f'<tr2:Profiles token="{token}" fixed="true"><tr2:Name>{token}</tr2:Name><tr2:Configurations>'
        + _v2_source(spec, "tr2:VideoSource")
        + _v2_encoder(spec, token, "tr2:VideoEncoder")
        + "</tr2:Configurations></tr2:Profiles>"
    )


def _v2_get_profiles(spec: DeviceSpec, req) -> str:
    wanted = _requested_token(req, "Token")
    tokens = (wanted,) if wanted in PROFILE_TOKENS else PROFILE_TOKENS
    return "<tr2:GetProfilesResponse>" + "".join(_v2_profile(spec, t) for t in tokens) + "</tr2:GetProfilesResponse>"


def _v2_encoder_configurations(spec: DeviceSpec, req) -> str:
    wanted = (_requested_token(req, "ConfigurationToken") or "").removeprefix("vec_")
    tokens = (wanted,) if wanted in PROFILE_TOKENS else PROFILE_TOKENS
    items = "".join(_v2_encoder(spec, t, "tr2:Configurations") for t in tokens)
    return f"<tr2:GetVideoEncoderConfigurationsResponse>{items}</tr2:GetVideoEncoderConfigurationsResponse>"


def _v2_encoder_options(spec: DeviceSpec, _req) -> str:
    return (
        "<tr2:GetVideoEncoderConfigurationOptionsResponse>"
        f'<tr2:Options GovLengthRange="1 250" FrameRatesSupported="{spec.fps}" ProfilesSupported="{spec.codec_profile}">'
        f"<tt:Encoding>{spec.encoding}</tt:Encoding><tt:QualityRange><tt:Min>1</tt:Min><tt:Max>10</tt:Max></tt:QualityRange>"
        f"<tt:ResolutionsAvailable><tt:Width>{spec.width}</tt:Width><tt:Height>{spec.height}</tt:Height></tt:ResolutionsAvailable>"
        f"<tt:BitrateRange><tt:Min>256</tt:Min><tt:Max>{spec.bitrate_kbps * 4}</tt:Max></tt:BitrateRange>"
        "</tr2:Options></tr2:GetVideoEncoderConfigurationOptionsResponse>"
    )


def _v2_uri(op: str, uri: str) -> str:
    return f"<tr2:{op}Response><tr2:Uri>{escape(uri)}</tr2:Uri></tr2:{op}Response>"


def _v2_service_capabilities(spec: DeviceSpec, _req) -> str:
    return (
        '<tr2:GetServiceCapabilitiesResponse><tr2:Capabilities SnapshotUri="true" Rotation="false" '
        'VideoSourceMode="false" OSD="false" Mask="false" SourceMask="false">'
        '<tr2:ProfileCapabilities MaximumNumberOfProfiles="2" ConfigurationsSupported="VideoSource VideoEncoder"/>'
        '<tr2:StreamingCapabilities RTSPStreaming="true" RTPMulticast="false" RTP_RTSP_TCP="true" '
        'NonAggregateControl="false" AutoStartMulticast="false"/></tr2:Capabilities></tr2:GetServiceCapabilitiesResponse>'
    )


def _v2_empty(op: str):
    return lambda spec, req: f"<tr2:{op}Response/>"


_MEDIA2_OPS = {
    "GetServiceCapabilities": _v2_service_capabilities,
    "GetProfiles": _v2_get_profiles,
    "GetVideoSourceConfigurations": lambda s, r: (
        "<tr2:GetVideoSourceConfigurationsResponse>"
        + _v2_source(s, "tr2:Configurations")
        + "</tr2:GetVideoSourceConfigurationsResponse>"
    ),
    "GetVideoEncoderConfigurations": _v2_encoder_configurations,
    "GetVideoEncoderConfigurationOptions": _v2_encoder_options,
    "GetStreamUri": lambda s, r: _v2_uri("GetStreamUri", s.stream_uri),
    "GetSnapshotUri": lambda s, r: _v2_uri("GetSnapshotUri", s.snapshot_uri),
    # This feature is video-only for now.
    "GetAudioSourceConfigurations": _v2_empty("GetAudioSourceConfigurations"),
    "GetAudioEncoderConfigurations": _v2_empty("GetAudioEncoderConfigurations"),
    "GetAudioOutputConfigurations": _v2_empty("GetAudioOutputConfigurations"),
    "GetAudioDecoderConfigurations": _v2_empty("GetAudioDecoderConfigurations"),
    "GetMetadataConfigurations": _v2_empty("GetMetadataConfigurations"),
    "GetAnalyticsConfigurations": _v2_empty("GetAnalyticsConfigurations"),
    "GetOSDs": _v2_empty("GetOSDs"),
}


# ── WS-Discovery ─────────────────────────────────────────────────────────────

#: The types that mean "an ONVIF camera", as (namespace, local name). Matching is
#: by namespace on purpose: Windows' discovery (the Samba add-on's wsdd shares
#: this port) probes for `wsdp:Device` = (devprof namespace, "Device"), which a
#: local-name match would answer — putting a phantom device into every Windows
#: machine's Network view.
ONVIF_PROBE_TYPES = frozenset({(NS_NETWORK, "NetworkVideoTransmitter"), (NS_DEVICE, "Device")})


def probe_matches_onvif(data: bytes) -> str | None:
    """The Probe's MessageID if it asks for an ONVIF camera, else None.

    A Probe with no Types at all asks every device to answer, and is answered.
    """
    if len(data) > 16 * 1024:
        return None
    prefixes: dict[str, str] = {}
    try:
        events = ET.iterparse(io.BytesIO(data), events=("start-ns", "end"))
        root = None
        for kind, value in events:
            if kind == "start-ns":
                prefixes.setdefault(value[0], value[1])
            else:
                root = value
    except ET.ParseError:
        return None
    if root is None:
        return None
    action = next((el.text for el in root.iter() if _local(el.tag) == "Action"), None) or ""
    if action.strip() != WSD_PROBE:
        return None
    message_id = next((el.text for el in root.iter() if _local(el.tag) == "MessageID"), None)
    types_el = next((el for el in root.iter() if _local(el.tag) == "Types" and _ns(el.tag) == NS_WSD), None)
    wanted = (types_el.text or "").split() if types_el is not None else []
    if wanted:
        resolved = set()
        for qname in wanted:
            prefix, _, local = qname.rpartition(":")
            resolved.add((prefixes.get(prefix, ""), local))
        if not resolved & ONVIF_PROBE_TYPES:
            return None
    return (message_id or "").strip() or "urn:uuid:" + str(uuid.uuid4())


def _wsd_envelope(action: str, body: str, relates_to: str | None = None) -> bytes:
    relates = f"<a:RelatesTo>{escape(relates_to)}</a:RelatesTo>" if relates_to else ""
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<s:Envelope xmlns:s="{NS_SOAP}" xmlns:a="{NS_WSA}" xmlns:d="{NS_WSD}" '
        f'xmlns:dn="{NS_NETWORK}" xmlns:tds="{NS_DEVICE}"><s:Header>'
        f"<a:MessageID>urn:uuid:{uuid.uuid4()}</a:MessageID>{relates}"
        "<a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>"
        f"<a:Action>{NS_WSD}/{action}</a:Action></s:Header><s:Body>{body}</s:Body></s:Envelope>"
    ).encode()


def _wsd_endpoint(spec: DeviceSpec, with_xaddrs: bool = True) -> str:
    xaddrs = f"<d:XAddrs>{spec.device_xaddr}</d:XAddrs>" if with_xaddrs else ""
    return (
        f"<a:EndpointReference><a:Address>urn:uuid:{spec.endpoint_uuid}</a:Address></a:EndpointReference>"
        "<d:Types>dn:NetworkVideoTransmitter tds:Device</d:Types>"
        f"<d:Scopes>{' '.join(spec.scopes)}</d:Scopes>{xaddrs}<d:MetadataVersion>1</d:MetadataVersion>"
    )


def probe_match(spec: DeviceSpec, relates_to: str) -> bytes:
    return _wsd_envelope(
        "ProbeMatches",
        f"<d:ProbeMatches><d:ProbeMatch>{_wsd_endpoint(spec)}</d:ProbeMatch></d:ProbeMatches>",
        relates_to,
    )


def hello(spec: DeviceSpec) -> bytes:
    return _wsd_envelope("Hello", f"<d:Hello>{_wsd_endpoint(spec)}</d:Hello>")


def bye(spec: DeviceSpec) -> bytes:
    return _wsd_envelope("Bye", f"<d:Bye>{_wsd_endpoint(spec, with_xaddrs=False)}</d:Bye>")


class _WsdProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: WsDiscovery):
        self._owner = owner
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        reply = self._owner.reply_to(data, addr)
        if reply is not None and self.transport is not None:
            self.transport.sendto(reply, addr)


class WsDiscovery:
    """Answers ONVIF WS-Discovery Probes; announces Hello/Bye."""

    def __init__(self, spec_provider):
        self._spec = spec_provider
        self._transport: asyncio.DatagramTransport | None = None
        self.bound = False
        self.bind_error: str | None = None
        self.probes_answered = 0

    def reply_to(self, data: bytes, addr=None) -> bytes | None:
        message_id = probe_matches_onvif(data)
        if message_id is None:
            return None
        self.probes_answered += 1
        _LOGGER.debug("WS-Discovery: answering ONVIF probe from %s", addr)
        return probe_match(self._spec(), message_id)

    @staticmethod
    def _make_socket(host_ip: str) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            # Shared with the Samba add-on's wsdd, which already holds 3702:
            # multicast is delivered to every socket bound with SO_REUSEADDR.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", WSD_PORT))
            mreq = struct.pack("4s4s", socket.inet_aton(WSD_GROUP), socket.inet_aton(host_ip))
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(host_ip))
            sock.setblocking(False)
        except OSError:
            sock.close()
            raise
        return sock

    async def start(self) -> None:
        spec = self._spec()
        try:
            sock = self._make_socket(spec.host_ip)
            loop = asyncio.get_running_loop()
            self._transport, _ = await loop.create_datagram_endpoint(lambda: _WsdProtocol(self), sock=sock)
        except OSError as err:
            # Discovery is a convenience: Advanced Adoption by ip:port still works.
            self.bind_error = str(err)
            _LOGGER.warning(
                "UniFi Protect auto-discovery is unavailable (could not share UDP %s: %s). "
                "Add the camera in Protect by address instead: %s:%s",
                WSD_PORT,
                err,
                spec.host_ip,
                spec.onvif_port,
            )
            return
        self.bound = True
        self._send(hello(spec))

    def _send(self, payload: bytes) -> None:
        if self._transport is not None:
            try:
                self._transport.sendto(payload, (WSD_GROUP, WSD_PORT))
            except OSError:
                pass

    async def stop(self) -> None:
        if self._transport is not None:
            self._send(bye(self._spec()))
            self._transport.close()
            self._transport = None
        self.bound = False


# ── service lifecycle ────────────────────────────────────────────────────────


#: How often the service re-reads which codec the camera really sends.
CODEC_REFRESH_S = 30


def video_encoding_of(stream_info: dict | None) -> str | None:
    """The video codec of one go2rtc /api/streams entry, "H264" or "H265", or
    None when no producer is serving video yet (idle, or still dialing)."""
    for producer in (stream_info or {}).get("producers") or []:
        for media in (producer or {}).get("medias") or []:
            kind, _, rest = str(media).partition(",")
            if kind.strip() != "video":
                continue
            codecs = {c.strip().upper() for c in rest.split(",")}
            if codecs & {"H265", "HEVC"}:
                return "H265"
            if "H264" in codecs:
                return "H264"
    return None


def _udp_source_ip() -> str | None:
    """The IPv4 this host uses for its default route. No packet is sent."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("192.0.2.1", 9))  # TEST-NET-1, never routed anywhere real
            return s.getsockname()[0]
        except OSError:
            return None


def _mac_for_ip(ip: str) -> str | None:
    """The MAC of the interface holding `ip`, best effort. Blocking."""
    try:
        import ifaddr

        for adapter in ifaddr.get_adapters():
            if any(getattr(a, "ip", None) == ip for a in adapter.ips):
                with open(f"/sys/class/net/{adapter.name}/address", encoding="ascii") as fh:
                    mac = fh.read().strip().lower()
                return mac if re.fullmatch(r"([0-9a-f]{2}:){5}[0-9a-f]{2}", mac) else None
    except Exception:  # noqa: BLE001 - optional detail, never fatal
        return None
    return None


class OnvifService:
    """The ONVIF device + discovery for one config entry."""

    def __init__(self, hass, entry, firmware: str = "unknown"):
        self.hass = hass
        self.entry = entry
        self.firmware = firmware
        self.host_ip: str | None = None
        self.mac: str | None = None
        self.port = int(entry.options.get(OPT_PROTECT_PORT) or DESIRED_ONVIF_PORT)
        self.device = OnvifDevice(self.spec)
        self.discovery = WsDiscovery(self.spec)
        self.running = False
        self.start_error: str | None = None
        self._runner = None
        # What the camera's own stream carries, learned from go2rtc while it
        # runs and kept once known (a camera's codec does not change).
        self.native_encoding: dict[str, str] = {}
        self._codec_checked: dict[str, float] = {}

    def camera_id(self) -> str | None:
        return protect_camera_id(self.entry.options, self.entry.data.get("cameras"))

    def spec(self) -> DeviceSpec:
        """Built per request: go2rtc may have been restarted onto other ports."""
        opts = self.entry.options
        dev = self.camera_id() or ""
        rtsp_port, api_port = effective_ports(
            self.hass, self.entry.entry_id, rtsp_default=int(opts.get("rtsp_port", 8555))
        )
        userinfo = ""
        if opts.get("nvr_enabled") and opts.get("nvr_password"):
            from urllib.parse import quote

            userinfo = f"{quote(opts.get('nvr_username') or 'cuboai', safe='')}:{quote(opts['nvr_password'], safe='')}@"
        return DeviceSpec(
            device_id=dev,
            host_ip=self.host_ip or "127.0.0.1",
            onvif_port=self.port,
            rtsp_port=rtsp_port,
            api_port=api_port,
            stream=protect_stream_name(dev, opts),
            username=opts.get(OPT_PROTECT_USERNAME) or PROTECT_USERNAME_DEFAULT,
            password=opts.get(OPT_PROTECT_PASSWORD) or "",
            firmware=self.firmware,
            mac=self.mac,
            rtsp_userinfo=userinfo,
            h264_profile="High" if dev in (opts.get("h264_cameras") or []) else "Main",
            encoding=self.encoding_for(dev),
        )

    def encoding_for(self, dev: str) -> str:
        """The codec Protect is told about, which must be what it will receive.

        With the H.264 option on, the stream is the transcode: H.264, certain.
        Otherwise it is the camera's own video, as go2rtc last saw it. Until
        that is known the answer is H.264, which is what every version before
        Media2 said.
        """
        if dev in (self.entry.options.get("h264_cameras") or []):
            return "H264"
        return self.native_encoding.get(dev, "H264")

    async def _refresh_encoding(self) -> None:
        """Learn the camera's native codec from go2rtc, at most every
        CODEC_REFRESH_S. Read-only: /api/streams never attaches a consumer."""
        from aiohttp import ClientError, ClientSession, ClientTimeout

        dev = self.camera_id()
        now = time.monotonic()
        if not dev or now - self._codec_checked.get(dev, -CODEC_REFRESH_S) < CODEC_REFRESH_S:
            return
        self._codec_checked[dev] = now
        url = f"http://127.0.0.1:{self.spec().api_port}/api/streams?src=cuboai_combined_{dev}"
        try:
            async with ClientSession(timeout=ClientTimeout(total=2)) as session, session.get(url) as resp:
                if resp.status != 200:
                    return
                info = await resp.json(content_type=None)
        except (ClientError, TimeoutError, ValueError):
            return
        encoding = video_encoding_of(info if isinstance(info, dict) else None)
        if encoding:
            self.native_encoding[dev] = encoding

    async def _resolve_host(self) -> None:
        try:
            from homeassistant.components.network import async_get_source_ip

            self.host_ip = await async_get_source_ip(self.hass)
        except Exception:  # noqa: BLE001
            self.host_ip = None
        if not self.host_ip or self.host_ip.startswith("127."):
            self.host_ip = await self.hass.async_add_executor_job(_udp_source_ip)
        if self.host_ip:
            self.mac = await self.hass.async_add_executor_job(_mac_for_ip, self.host_ip)

    async def start(self) -> bool:
        if not self.camera_id():
            self.start_error = "no camera configured"
            return False
        if not (self.entry.options.get(OPT_PROTECT_PASSWORD) or ""):
            self.start_error = "no password set"
            _LOGGER.error("UniFi Protect support is on but has no password; Protect refuses empty passwords")
            return False
        await self._resolve_host()
        from aiohttp import web

        app = web.Application(client_max_size=MAX_REQUEST_BYTES)
        app.router.add_get("/onvif/snapshot", self._snapshot)
        app.router.add_post("/onvif/{tail:.*}", self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            # Pinned port, never a hop: Protect stores ip:port at adoption.
            await web.TCPSite(runner, "0.0.0.0", self.port, reuse_address=True).start()
        except OSError as err:
            await runner.cleanup()
            self.start_error = f"port {self.port} unavailable: {err}"
            _LOGGER.error(
                "UniFi Protect support could not listen on port %s (%s). It is NOT moved to another port, "
                "because Protect remembers the address it adopted. Free the port or choose another one in "
                "Configure.",
                self.port,
                err,
            )
            self._notify_port_conflict(err)
            return False
        self._runner = runner
        self.running = True
        domain_data = self.hass.data.setdefault(DOMAIN, {})
        domain_data.setdefault("_ports_by_entry", {}).setdefault(self.entry.entry_id, {})["onvif"] = self.port
        await self.discovery.start()
        _LOGGER.info("UniFi Protect (ONVIF) ready at %s:%s", self.host_ip, self.port)
        return True

    def _notify_port_conflict(self, err) -> None:
        try:
            from homeassistant.components import persistent_notification

            persistent_notification.async_create(
                self.hass,
                f"CuboAI could not open port {self.port} for UniFi Protect ({err}). The port is not changed "
                "automatically, because Protect remembers the address it adopted. Free the port, or choose "
                "another one in Settings → Devices & Services → CuboAI → Configure.",
                title="CuboAI: UniFi Protect port unavailable",
                notification_id=f"cuboai_protect_port_{self.entry.entry_id}",
            )
        except Exception:  # noqa: BLE001
            pass

    async def _handle(self, request):
        from aiohttp import web

        body = await request.read()
        await self._refresh_encoding()
        status, xml = self.device.handle(body, request.remote)
        return web.Response(status=status, text=xml, content_type="application/soap+xml", charset="utf-8")

    async def _snapshot(self, request):
        """A JPEG of the camera, fetched from go2rtc on whatever port it holds now.

        Unauthenticated, like go2rtc's own /api/frame.jpeg it forwards to (which
        is already open on the LAN); Protect fetches it without credentials.
        """
        from aiohttp import ClientError, ClientSession, ClientTimeout, web

        url = self.spec().go2rtc_frame_url
        try:
            async with ClientSession(timeout=ClientTimeout(total=20)) as session, session.get(url) as resp:
                body = await resp.read()
                if resp.status != 200:
                    return web.Response(status=502, text=f"engine answered {resp.status}")
                return web.Response(body=body, content_type=resp.content_type or "image/jpeg")
        except (ClientError, TimeoutError) as err:
            return web.Response(status=502, text=f"no frame from the streaming engine: {err}")

    async def stop(self) -> None:
        await self.discovery.stop()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None
        ports = (self.hass.data.get(DOMAIN) or {}).get("_ports_by_entry") or {}
        (ports.get(self.entry.entry_id) or {}).pop("onvif", None)
        self.running = False

    def stats(self) -> dict:
        return {
            "running": self.running,
            "start_error": self.start_error,
            "address": f"{self.host_ip}:{self.port}" if self.host_ip else None,
            "mac_known": bool(self.mac),
            "advertised_encoding": self.encoding_for(self.camera_id() or ""),
            "discovery_bound": self.discovery.bound,
            "discovery_error": self.discovery.bind_error,
            "probes_answered": self.discovery.probes_answered,
            "requests": dict(self.device.requests),
            "unhandled_operations": dict(self.device.unhandled),
            "auth_failures": self.device.auth_failures,
            "clients": sorted(self.device.clients),
        }
