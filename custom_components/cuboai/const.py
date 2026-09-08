DOMAIN = "cuboai"
DEFAULT_UPDATE_INTERVAL = 60

#: The go2rtc API port the integration aims for. Not user-configurable (unlike
#: rtsp_port), but NOT a promise either: _resolve_ports self-heals to a nearby
#: free port when it's taken and publishes the result as api_port_effective in
#: hass.data — which every consumer (camera entities, sensors, the card) reads
#: instead of assuming this value. Code touching the API port must use this
#: constant or effective_ports(), never a bare 1985.
DESIRED_API_PORT = 1985


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
