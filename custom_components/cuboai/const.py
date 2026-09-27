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
#: Expose ONE camera to UniFi Protect as an ONVIF device (onvif_server.py).
#: Off by default: it opens a port and answers discovery on the LAN.
OPT_PROTECT_ENABLED = "unifi_protect_enabled"
#: Which camera. Protect identifies a third-party camera by the MAC address of
#: the host it talks to, so one Home Assistant host can present exactly one
#: camera to Protect — offering several would make Protect merge them.
OPT_PROTECT_CAMERA = "unifi_protect_camera"
OPT_PROTECT_USERNAME = "unifi_protect_username"
OPT_PROTECT_PASSWORD = "unifi_protect_password"
OPT_PROTECT_PORT = "unifi_protect_port"
PROTECT_USERNAME_DEFAULT = "cuboai"
#: 8899 is the ONVIF port many IP cameras use, and it is not one HA or its
#: common add-ons take. PINNED, never self-healed: Protect stores ip:port when
#: the camera is adopted, so a silent hop to 8900 would orphan the camera in
#: Protect with no explanation (the same lesson as the NVR's RTSP port).
DESIRED_ONVIF_PORT = 8899


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


def effective_onvif_port(hass, entry_id) -> int | None:
    """The port this entry's ONVIF server actually listens on, or None when it
    is not running. Published by onvif_server.OnvifService.start()."""
    domain_data = (getattr(hass, "data", None) or {}).get(DOMAIN) or {}
    return ((domain_data.get("_ports_by_entry") or {}).get(entry_id) or {}).get("onvif")
