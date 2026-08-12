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
