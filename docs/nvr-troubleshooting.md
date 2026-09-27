# NVR troubleshooting

The main README covers turning the RTSP export on and copying the URL
([RTSP — recording to an NVR](../README.md#rtsp--recording-to-an-nvr)). This page is for when a
recorder still won't record.

## Hikvision / HiLook NVRs — verified settings

These recorders don't take a URL. They take a *protocol definition* with the address, port and
path in **separate fields**, so a mistyped or truncated path is the most common failure. Verified
end to end on a HiLook NVR:

Configuration → Camera → **Custom Protocol** (create one, e.g. named `CuboAI`):

| Field | Value |
|---|---|
| Type | `RTSP` |
| Transmission Protocol | `RTP Over RTSP` (TCP) |
| Port | Whatever `nvr_rtsp_url` shows. Commonly `8557` (Home Assistant's own go2rtc usually holds `8555`); never `554` unless you set that yourself. |
| Stream Path | `/cuboai_combined_<device id>` — the **whole** name, with the leading slash, no host, no port |

Set the **same path for both Main Stream and Sub Stream**; a blank sub-stream can keep the channel
offline. Then add the camera: Configuration → Camera → **IP Camera → Custom Add**, with protocol
`CuboAI`, the Home Assistant host's IP and the same port. Leave the password empty if the stream is
unauthenticated.

- The path field accepts a `?video` suffix (`/cuboai_combined_<device id>?video`) if the NVR
  complains about the audio track.
- These units send `rtsp://<ip>/<path>` with **no port in the URL**, even though they connect on the
  right port. That is normal and go2rtc handles it.

## The recorder shows "offline" and `go2rtc.log` shows nothing at all

**go2rtc does not log a request for a stream name it doesn't have.** No 404 line, no client
address, nothing — so a recorder with one wrong character in its path is invisible on the server
side, and it is easy to conclude the recorder never made contact. This is the most likely cause of
an NVR that won't come online. A real example: a path re-entered by hand as `/combined_<device id>`
(missing the `cuboai_` prefix) produced `ipcStreamFail` on the NVR and total silence in the log;
fixing the path brought it online at once.

Check, in this order:

1. **Compare the recorder's stored path with the sensor, character by character.** The truth is
   the `nvr_rtsp_url` attribute of the camera's *WebRTC Stream* sensor. The stream name changes when
   you turn the H.264 transcode or the RTSP timestamp on or off.
2. **Test the exact URL from a computer** (`ffprobe`, VLC). If it plays there and the recorder still
   fails, the recorder's stored value is not what you think it is.
3. **Only then read the log — and confirm the client address.** A `new consumer` line carries no IP;
   check `remote_addr` under `http://<HA-IP>:<api-port>/api/streams?src=<stream>` (the API port is in
   the WebRTC Stream sensor's `go2rtc_server` attribute). It is easy to mistake your own test pull
   for the recorder.

## Connects, streams for 30–60 seconds, then drops repeatedly

go2rtc answers RTSP `OPTIONS` but ignores `GET_PARAMETER` mid-session
([AlexxIT/go2rtc#289](https://github.com/AlexxIT/go2rtc/issues/289)). Some clients use
`GET_PARAMETER` as their keepalive, so this *can* matter — but the HiLook above held sessions open
indefinitely once its path was right. Confirm the path first, and look for drops at a regular
interval before blaming keepalives.

## Does recording load the camera?

No. Every consumer — the card, HomeKit, an NVR, UniFi Protect — shares **one** camera session.
Recording continuously does not open a second connection to the camera.

## Short gaps in a recording

The camera's session can go silent every hour or two, or keep sending damaged keyframes after a
burst of packet loss. The streaming engine detects both and **reconnects in place**: viewers and
recorders see a gap of a few seconds, then the stream continues on the same timeline. A recorder
that gives up in under ~8 seconds would drop once and reconnect; every recorder seen so far waits
longer. The tunables are in [advanced.md](advanced.md#stream-stall-detection).
