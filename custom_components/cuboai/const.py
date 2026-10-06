import hashlib

DOMAIN = "cuboai"
DEFAULT_UPDATE_INTERVAL = 60

#: The go2rtc API port the integration aims for. Not user-configurable (unlike
#: rtsp_port), but NOT a promise either: _resolve_ports self-heals to a nearby
#: free port when it's taken and publishes the result as api_port_effective in
#: hass.data — which every consumer (camera entities, sensors, the card) reads
#: instead of assuming this value. Code touching the API port must use this
#: constant or effective_ports(), never a bare 1985.
DESIRED_API_PORT = 1985

#: Option key for the notification raised when the streaming engine had to be
#: restarted (or gave up). Lives here rather than in go2rtc.py so the options
#: flow can offer the toggle without importing the streaming module — that
#: import is deliberately deferred there (see config_flow's _port_bindable).
#:
#: ON by default, unlike every other toggle: this reports a failure that used
#: to be entirely silent — a crashed engine left the entry "loaded" and every
#: entity holding its last state, so a dead camera could go unnoticed all
#: night. The notification says how to switch it off, so it surprises once.
OPT_NOTIFY_ON_RESTART = "notify_on_engine_restart"
NOTIFY_ON_RESTART_DEFAULT = True

# ── UniFi Protect (ONVIF) ────────────────────────────────────────────────────
#: Expose cameras to UniFi Protect as ONVIF devices (onvif_server.py).
#: Off by default: it opens ports and answers discovery on the LAN.
OPT_PROTECT_ENABLED = "unifi_protect_enabled"
#: Which cameras (a list of device ids). Protect tells third-party cameras apart
#: by the MAC they REPORT over ONVIF, not the one on the wire (tested live: a
#: second device on the same IP with its own reported MAC was adopted as a
#: second camera), so each camera gets its own port and its own MAC.
OPT_PROTECT_CAMERAS = "unifi_protect_cameras"
#: v2.6.37–2.6.40: the single camera exposed. Still read, so a camera adopted
#: then keeps its port and MAC; no longer written.
OPT_PROTECT_CAMERA = "unifi_protect_camera"
#: {device_id: port}, written by the options flow. A camera keeps its port for
#: good once it has one — Protect stores ip:port at adoption.
OPT_PROTECT_PORTS = "unifi_protect_ports"
OPT_PROTECT_USERNAME = "unifi_protect_username"
OPT_PROTECT_PASSWORD = "unifi_protect_password"
OPT_PROTECT_PORT = "unifi_protect_port"
PROTECT_USERNAME_DEFAULT = "cuboai"
#: 8899 is the ONVIF port many IP cameras use, and it is not one HA or its
#: common add-ons take. PINNED, never self-healed: Protect stores ip:port when
#: the camera is adopted, so a silent hop to 8900 would orphan the camera in
#: Protect with no explanation (the same lesson as the NVR's RTSP port).
DESIRED_ONVIF_PORT = 8899

# ── H.264 transcode (the `cuboai_h264_<id>` stream for HomeKit / HLS) ────────
#: Largest picture the transcode sends. 1080p is HomeKit's ceiling; 720p is for
#: machines that cannot convert 1080p in real time (#85: a Raspberry Pi 4 ran
#: the 1080p conversion at 0.53-0.66x, so HomeKit gave up). A smaller source is
#: never enlarged.
OPT_H264_RESOLUTION = "h264_resolution"
H264_RESOLUTIONS = {"1080p": (1920, 1080), "720p": (1280, 720)}
H264_RESOLUTION_DEFAULT = "1080p"
#: Frames between the transcode's keyframes. A new viewer (HomeKit's ffmpeg,
#: HLS) cannot show anything before the next keyframe; go2rtc's template uses
#: 50 frames, which was 5 s on a Cubo 2 measured sending 10 fps. 15 frames is
#: 1-1.5 s at the 10-15 fps the cameras send.
H264_KEYINT = 15


def h264_resolution(options) -> str:
    """The configured transcode size, or the default for a missing or unknown value."""
    value = (options or {}).get(OPT_H264_RESOLUTION)
    return value if value in H264_RESOLUTIONS else H264_RESOLUTION_DEFAULT


def effective_ports(hass, entry_id, rtsp_default: int = 8555) -> tuple[int, int]:
    """The (rtsp, api) ports go2rtc ACTUALLY bound, for ONE config entry.

    Go2RTCManager is constructed per config entry, so a second entry (a second
    CuboAI account) runs a SECOND go2rtc that must self-heal onto different
    ports. The resolved ports were previously published to domain-global keys,
    where the last entry to start overwrote the first — entry A's camera
    stream_source, snapshots and NVR URLs then pointed at entry B's go2rtc.
    Keying them by entry_id is what makes two entries independent.

    Falls back to the legacy domain-global keys (and finally to the defaults) so
    a single-entry install — every install today — is unaffected.
    """
    domain_data = (getattr(hass, "data", None) or {}).get(DOMAIN) or {}
    # `_ports_by_entry` is domain-level (not inside the entry's own store, which
    # unload pops) so the record survives an entry reload — see
    # Go2RTCManager._resolve_ports.
    own = (domain_data.get("_ports_by_entry") or {}).get(entry_id) or {}
    rtsp = own.get("rtsp") or domain_data.get("rtsp_port_effective") or rtsp_default
    api = own.get("api") or domain_data.get("api_port_effective") or DESIRED_API_PORT
    return int(rtsp), int(api)


def live_stream_name(device_id: str, options) -> str:
    """The go2rtc stream every live-view consumer must name for this camera.

    ONE rule, in ONE place, because #85 was twice caused by consumers
    disagreeing about which stream to use:

    * `cuboai_combined_<id>` normally — one stream, one camera session.
    * `cuboai_h264_<id>` when the per-camera H.264 transcode is on. That
      stream's ONLY video is H.264; the combined stream also carries the
      camera's native HEVC, and a plain RTSP consumer (HA's stream worker,
      hence HomeKit; an NVR; the diagnostics) takes whatever is offered
      first and gets the HEVC it cannot decode.

    `options` is the config entry's options mapping.
    """
    if device_id in ((options or {}).get("h264_cameras") or []):
        return f"cuboai_h264_{device_id}"
    return f"cuboai_combined_{device_id}"


def nvr_stream_name(device_id: str, options) -> str:
    """The stream an NVR should record for this camera.

    When the per-camera RTSP-timestamp option is on, this is the dedicated
    `cuboai_stamped_<id>` stream — a transcode of the combined stream with the
    time burned into the image, so NVR recordings show when each frame was
    captured. Otherwise it falls back to the normal live stream name.

    Deliberately SEPARATE from live_stream_name(): only the NVR path pays the
    burn-in transcode. The card (WebRTC) and HomeKit keep the passthrough live
    stream, so the timestamp is not baked into the live view (the card has its
    own on-video badge for that) and non-NVR consumers are unaffected.
    """
    if device_id in ((options or {}).get("rtsp_timestamp_cameras") or []):
        return f"cuboai_stamped_{device_id}"
    return live_stream_name(device_id, options)


def protect_stream_name(device_id: str, options) -> str:
    """The FIXED go2rtc stream name UniFi Protect is pointed at for this camera.

    A stable alias (go2rtc.py re-reads protect_stream_target() into it), not
    the real stream: Protect's media server locks in the address it is given
    at adoption, so if this name followed the H.264 option, ticking it after
    adopting would leave Protect on the old stream until the camera was
    removed and adopted again. The name never changes; what is behind it does.
    """
    return f"cuboai_protect_{device_id}"


def protect_stream_target(device_id: str, options) -> str:
    """What the Protect alias carries: the same rule as every other H.264-only
    consumer — the `cuboai_h264_` transcode when that camera's H.264 option is
    on, else the combined stream (native H.264 on a Cubo 2 / CB02). An HEVC
    camera (Cubo 3) without the option sends HEVC, which Protect passes to
    viewers untranscoded; the integration never turns the option on itself.
    """
    return live_stream_name(device_id, options)


def protect_camera_id(options, cameras) -> str | None:
    """The device id of the camera exposed to UniFi Protect, or None.

    The chosen camera if it is still configured, else the first configured one
    (a camera removed after being chosen must not leave Protect pointing at
    nothing), else None.
    """
    ids = [c.get("device_id") for c in (cameras or []) if c.get("device_id")]
    chosen = (options or {}).get(OPT_PROTECT_CAMERA)
    if chosen in ids:
        return chosen
    return ids[0] if ids else None


def protect_camera_ids(options, cameras) -> list[str]:
    """The device ids exposed to UniFi Protect, in configured-camera order.

    The multi-select when it has been saved (cameras no longer configured are
    dropped); before that, the single camera of v2.6.37–2.6.40 — the chosen one,
    else the first — so an upgrade changes nothing in Protect.
    """
    ids = [c.get("device_id") for c in (cameras or []) if c.get("device_id")]
    chosen = (options or {}).get(OPT_PROTECT_CAMERAS)
    if isinstance(chosen, list):
        return [i for i in ids if i in chosen]
    single = protect_camera_id(options, cameras)
    return [single] if single else []


def protect_primary_id(options, cameras) -> str | None:
    """The camera on the base port that reports the host's real MAC.

    Recorded in OPT_PROTECT_CAMERA (the v2.6.37–2.6.40 single-camera key, which
    meant exactly this), so it never moves: if it is no longer exposed there is
    no primary and the base port stays reserved for it. Never recorded (an
    install that kept the default) means the first configured camera — the one
    those versions exposed.
    """
    exposed = protect_camera_ids(options, cameras)
    recorded = (options or {}).get(OPT_PROTECT_CAMERA)
    if recorded is not None:
        return recorded if recorded in exposed else None
    first = protect_camera_id(options, cameras)
    return first if first in exposed else None


def assign_protect_ports(options, cameras, avoid=()) -> dict[str, int]:
    """{device_id: port} for every exposed camera. Stable by construction:
    a saved assignment is never changed, the primary camera takes the base
    port, and a new camera takes the lowest free port above the base that no
    camera — exposed now or earlier — has, skipping `avoid`."""
    options = options or {}
    base = int(options.get(OPT_PROTECT_PORT) or DESIRED_ONVIF_PORT)
    saved = {k: int(v) for k, v in (options.get(OPT_PROTECT_PORTS) or {}).items()}
    exposed = protect_camera_ids(options, cameras)
    primary = protect_primary_id(options, cameras)
    ports: dict[str, int] = {}
    if primary:
        ports[primary] = base  # the base port field is the primary's port
    taken = {base, *(int(a) for a in avoid)} | {p for d, p in saved.items() if d != primary}
    for dev in exposed:
        if dev in ports:
            continue
        if dev in saved and saved[dev] != base:
            ports[dev] = saved[dev]
            continue
        port = base + 1
        while port in taken or port in ports.values():
            port += 1
        ports[dev] = port
        taken.add(port)
    return ports


def protect_mac(device_id: str, primary: bool, host_mac: str | None) -> str | None:
    """The MAC a camera reports to Protect, which is how Protect tells cameras
    apart. The primary keeps the host's real one (what a pre-2.6.41 camera was
    adopted with); every other camera gets a stable, locally administered,
    unicast address derived from its id, never the camera's own."""
    if primary:
        return host_mac
    digest = hashlib.sha1(f"cuboai:mac:{device_id}".encode()).digest()
    return ":".join(f"{b:02x}" for b in (0x02, *digest[:5]))


def protect_display_name(hass, device_id: str, cameras) -> str:
    """What Protect shows after "CuboAI ": the device's name as renamed in Home
    Assistant, else the camera's name from the CuboAI account."""
    name = None
    try:
        from homeassistant.helpers import device_registry as dr

        device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, device_id)})
        by_user = getattr(device, "name_by_user", None)
        if isinstance(by_user, str) and by_user.strip():
            name = by_user.strip()
    except Exception:  # noqa: BLE001 - a registry hiccup must never break ONVIF
        name = None
    if name is None:
        for cam in cameras or []:
            if cam.get("device_id") == device_id and isinstance(cam.get("baby_name"), str):
                name = cam["baby_name"].strip()
                break
    name = name or "Baby Monitor"
    # Protect prefixes the manufacturer itself: "CuboAI Nursery", not "CuboAI CuboAI Nursery".
    if name.lower().startswith("cuboai ") and len(name) > 7:
        name = name[7:].strip()
    return name[:48]


def effective_onvif_ports(hass, entry_id) -> dict[str, int]:
    """{device_id: port} this entry's ONVIF services actually listen on (empty
    when none runs). Published by onvif_server.OnvifService.start()."""
    domain_data = (getattr(hass, "data", None) or {}).get(DOMAIN) or {}
    return dict(((domain_data.get("_ports_by_entry") or {}).get(entry_id) or {}).get("onvif") or {})
