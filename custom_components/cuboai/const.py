DOMAIN = "cuboai"
DEFAULT_UPDATE_INTERVAL = 60


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
