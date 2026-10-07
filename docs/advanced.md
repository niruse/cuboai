# Advanced

Details most installs never need. The main README links here where they matter.

## Streaming engine and ports

The integration runs its own [go2rtc](https://github.com/AlexxIT/go2rtc) process. It picks free
ports on start, so nothing needs configuring:

| Port | Purpose | If already in use |
|---|---|---|
| RTSP, `rtsp_port` option (8555 → usually `8557`) | Camera streams for NVRs, HomeKit, UniFi Protect | Moves to the next free port. Home Assistant's own go2rtc normally holds 8555, so 8557 is the expected result, not an error. |
| API `1985` | Card, snapshots, WebRTC signalling | Moves to the next free port (`1986`…) |
| WebRTC `8556` | Media for the card | Moves to the next free port |
| ONVIF `8899`, `8900`, … | UniFi Protect, one per camera shown there (only when turned on) | **Never moves** — Protect remembers the address; a notification says the port is taken |

The camera entity (`rtsp_port` attribute) and the WebRTC Stream sensor (`nvr_rtsp_url`,
`go2rtc_server`) always publish the ports actually in use. The card never deals in ports: it finds
its camera by the `device_id` attribute and lets that entity supply the stream.

Other safeguards:

- **Stopped with Home Assistant.** The engine is stopped when Home Assistant stops or restarts, so
  it never outlives it and the next start gets the same ports.
- **Leftover engine cleanup.** If a previous go2rtc survived a hard crash and still holds the ports,
  it is recognised by its `cuboai_*` streams and stopped on startup.
- **No retry storms.** If the engine could not start at all, camera entities stop offering streams
  instead of hammering a port that may belong to another process.
- **Crash supervision.** The engine is checked every 15 seconds and restarted if it exited, with a
  notification (on by default, switchable in Configure). If it dies within a minute of starting five
  times in a row, the integration stops restarting it, logs an error and tells you to check
  `go2rtc.log`. Reloading the integration arms the supervisor again.

## Streams per camera

| Stream | What it is |
|---|---|
| `cuboai_combined_<id>` | The live stream (video + audio). What the card, HomeKit and NVRs use. |
| `cuboai_h264_<id>` | The H.264 transcode, when *Transcode these cameras to H.264* is on for the camera; then it is the live stream. H.264 High, level 4.0, at most 1080p or 720p (*Size of the H.264 transcode*), each camera frame encoded once, a keyframe every 15 frames. |
| `cuboai_stamped_<id>` | The live stream with the time burned in, when the RTSP timestamp option is on; then `nvr_rtsp_url` points here. |
| `cuboai_protect_<id>` | A fixed name UniFi Protect is given; it carries the live stream or the transcode, whichever the H.264 option selects. |
| `cuboai_dvr_<id>` | Recorded playback; idle until `cuboai.play_recording` asks for a moment. |
| `cuboai_speaker_<id>` | The speaker / text-to-speech backchannel, not a video source. |

All of them share one camera session. The card's microphone does not use go2rtc: it goes over Home
Assistant's websocket to a talk process of its own — see [two-way-audio.md](two-way-audio.md).

## Stream stall detection

The engine reconnects in place when the camera goes silent or stops delivering decodable
keyframes; with debug logs on, `go2rtc.log` shows a `[stall]` line followed by `[reconnect] ok`.
Thresholds can be changed through the producer environment:

| Variable | Default | Meaning |
|---|---|---|
| `CUBOAI_STALL_S` | 4 | Seconds with no picture **and** no packet |
| `CUBOAI_OUTPUT_STALL_S` | 6 | Seconds with data arriving but no picture out |
| `CUBOAI_DESYNC_S` | 8 | Seconds with pictures out but none decodable |
| `CUBOAI_FIRST_AU_S` | 15 | Seconds to wait for the first picture after connecting |
| `CUBOAI_RECONNECT_MAX` | 3 | Consecutive failed reconnects before the stream gives up (`0` turns reconnecting off) |

## Files the integration writes

| Path | What |
|---|---|
| `www/cuboai_images/` | Downloaded alert photos (served as `/local/cuboai_images/…`) |
| `www/cuboai_cache/` | Cached YouTube/Spotify songs |
| `www/cuboai-card.js` | The cards, copied and registered automatically on start |
| `.storage/cuboai_media.json` | The card's song library, playlists and shared settings |
| `cuboai_debug.log`, `cuboai_last_alert_debug.log` | Debug logs (only with debug logs on; 2 MB rotation) |
| `custom_components/cuboai/bin/go2rtc.log` | Streaming-engine log (2 MB rotation) |

## Hidden option

`frontend_webrtc: true` in the entry's options switches the camera entity to Home Assistant's
native WebRTC class. It is not in Configure and not needed with the CuboAI card.
