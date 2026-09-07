# 🍼 CuboAI Home Assistant Integration

[![CI](https://github.com/niruse/cuboai/actions/workflows/ci.yml/badge.svg)](https://github.com/niruse/cuboai/actions/workflows/ci.yml)
[![codecov](https://codecov.io/gh/niruse/cuboai/branch/main/graph/badge.svg)](https://codecov.io/gh/niruse/cuboai)
[![HACS](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)

Bring your CuboAI baby monitor into Home Assistant!  
Monitor alerts, camera status, subscription, and more—directly in your smart home dashboard.

---

## ☕ Support

If you found this project helpful, you can [buy me a coffee](https://coff.ee/niruse)!

---

## 🚨 Disclaimer

> **Warning:**  
> This is an unofficial integration.  
> You are fully responsible for the use of your credentials and your data.  
> The author and contributors take no responsibility for any issues, account restrictions, or data loss that may occur.  
>  
> Use at your own risk.

---

## ✨ Features

This integration provides a massive suite of local control and real-time monitoring entities for your CuboAI cameras! 

### 🎛️ Controls
- **Night Light Brightness**: Native Home Assistant brightness slider (1% - 100%)
- **Night Light Switch**: Toggle the night light on/off
- **Lullaby Player**: Media player to start, stop, and select lullabies
- **Lullaby Timer**: Set the duration for lullabies to play
- **Speaker Play Time**: Adjust how long the speaker stays active
- **Flip Screen**: Toggle the camera's physical video orientation
- **Night Vision**: Switch between Auto, On, and Off
- **Sleep Mode**: Put the camera into privacy sleep mode
- **Status LED**: Toggle the physical status indicator LED
- **Baby Presence**: Toggle baby presence tracking

### 📊 Live Sensors
- **Baby Info**: Demographics (Name, Age, etc.)
- **Camera State**: Online/Offline/Streaming status
- **Detection Statuses**: Cry Detection, Cough Detection, Sleep Safety (Face Covered/Rollover)
- **Cry Sensitivity**: Current sensitivity level for cry detection
- **Temperature & Humidity**: Real-time environmental readings
- **Temperature & Humidity Alerts**: High/Low thresholds configured in the app
- **Sleep Sensor Pad (Mat)**: Live BPM (Heart Rate) and Mat Battery/State
- **Thermometer**: Live reading and battery state of the external thermometer
- **Last Alert**: Image thumbnail and time of the last captured event
- **Firmware Version**: The active firmware installed on the camera
- **WebRTC Stream**: Raw go2rtc stream ID (`cuboai_combined_<device id>`) for embedding ultra-low latency video

### 🛠️ Diagnostics
- **WiFi Diagnostics**: Signal strength (RSSI), Quality (%), Noise, Channel, and SSID
- **Network Info**: Local IP Address and MAC Address
- **Connection Details**: Connection Mode (LAN vs P2P) and Connected Users count
- **Hardware Info**: Camera Stand Type and Session History

### 🕘 DVR History Sensors (opt-in)
- **Baby Present, Caregiver Activity, Noise Level, Motion, Privacy Mode**: read from the camera's own on-SD DVR log (~1 minute lag). Enable with the **History sensors** option. Every reading carries `age_seconds` / `stale` attributes, and an entity goes unavailable rather than present a stale reading as live — this is a baby monitor, after all.

### 📼 Recorded Playback
- **Recording camera entity** (`camera.<baby>_recording`) + the `cuboai.play_recording` service replay any moment from roughly the last **72 hours** of on-camera DVR footage — see [Recorded Playback](#-recorded-playback-on-camera-dvr).

### 🌟 Plus:
- **Zero-Delay Local Streaming**: Video is fetched directly from the camera on your local network!
- **Two-Way Audio & Picture-in-Picture**: talk to the room through the bundled Lovelace card's mic button; pop the video out into a floating window (native PiP on Android/Apple, overlay PiP with the BPM/temperature badges on desktop Chrome).
- **NVR / RTSP Export**: publish a credential-protected RTSP URL that Frigate, Synology, HiLook or any recorder can consume — see [RTSP — recording to an NVR](#-rtsp--recording-to-an-nvr-frigate-synology-hilook-blue-iris-).
- **HomeKit-friendly H.264 Transcode**: a per-camera toggle (also the `cuboai.set_h264_transcode` service) for H.265 models (Cubo 3 / SW05) whose native video HomeKit and HA's HLS player cannot decode. With it on, the camera publishes a dedicated H.264-only stream (`cuboai_h264_<id>`) and points HomeKit/HLS at it — a stream that still carried the camera's native HEVC alongside the transcode would hand HomeKit the HEVC. It reuses the same camera session, so the camera is still opened only once.
- **Multi-Camera Support**: Add as many CuboAI cameras as you own!
- **Secure Authentication**: Uses native AWS Cognito SRP authentication.

---

## 🛠️ Installation

### ⚠️ Requirements
Before installing CuboAI, you **must** install the **WebRTC Camera** custom component by AlexxIT (available in HACS). This provides the underlying ultra-low latency WebRTC streaming engine that this integration hooks into!

---

### 📦 Installation via HACS

1. Go to **HACS** in Home Assistant.
2. Click the **three dots menu** (⋮) > **Custom repositories**.
3. Add this repository URL:  
   `https://github.com/niruse/cuboai`
4. In the **Category** dropdown, select **Integration**.
5. Click **Add**.<br>
   <img width="257" height="139" alt="HACS Custom Repository" src="https://github.com/user-attachments/assets/c5cb26a9-029e-45db-b05b-e75e5cd146f4" />
6. Search for **CuboAI** in HACS and click **Install**.
7. **Restart Home Assistant** to complete the installation.

---

### 📁 Manual Installation

1. Download the `cuboai` folder from this repository
2. Place it in `/config/custom_components/` on your Home Assistant instance
3. Restart Home Assistant

---

### ⚙️ Configuration & Settings

After adding the integration, you can click on **Configure** at any time to tweak its behavior. 
These settings can be changed seamlessly without needing to log out or remove the integration!

<img src="docs/images/config_options.png" width="400" />

You can adjust:
- **Cameras:** add or remove which of your account's cameras this entry manages.
- **Download Images:** Toggle whether to save event thumbnails locally.
- **Alerts Count:** How many recent alerts to track in the sensor.
- **Max Saved Photos:** The maximum number of alert images kept on disk. Keep
  it **at or above Alerts Count** — a lower cap prunes photos of alerts the
  sensor still lists, which shows up as broken thumbnails on older alerts.
- **Hours Back:** How far back in time to fetch alerts on startup.
- **Update Interval:** How often to poll the API for changes.
- **Camera IP (per camera):** the local transport reaches the camera with a **unicast probe to its LAN IP — there is no broadcast auto-discovery**. The IP is auto-learned and saved after the first successful connection, but if local sensors and the stream stay unavailable (log shows a "no 0x2041" handshake failure and a warning pointing here), set it manually from your router's client list.
- **RTSP Port:** the port the embedded go2rtc serves RTSP on (it self-heals to a free port if taken).
- **History sensors:** enable the DVR history sensors (Baby Present, Caregiver Activity, Noise, Motion, Privacy).
- **H.264 transcode (per camera):** transcode an H.265 camera's video to H.264 for HomeKit / HLS.
- **NVR export / username / password:** protect and publish the RTSP stream for external recorders — see [RTSP — recording to an NVR](#-rtsp--recording-to-an-nvr-frigate-synology-hilook-blue-iris-).
- **Cache YouTube/Spotify songs:** keep downloaded lullaby audio on disk for instant replay.
- **Enable debug logs:** one toggle for full diagnostics (integration + go2rtc + stream engine) — see Troubleshooting.

### ❌ Missing / Unsupported Features
While we provide a massive suite of entities, some native CuboAI app features cannot be implemented in Home Assistant currently:

- **Sleep reports (Total Sleep, Wake-ups, Longest Sleep, the routine chart):** CuboAI keeps these behind its paid tier — the app itself renders them as *"Report Preview — Activate Ultimate to see more"* — so the integration cannot fetch them either. The dashboard in this repo computes comparable figures locally from the camera's own DVR history instead.
- **Body temperature history:** only available while a compatible thermometer is paired and reporting.
- **Pan / Tilt:** The CuboAI camera is fixed and does not physically support PTZ (Pan-Tilt-Zoom).

Two-way audio **is** supported — through the bundled `cuboai-card.js` card's mic button (HA's stock camera card has no microphone backchannel). Past video playback is supported since **2.5.0** ([Recorded Playback](#-recorded-playback-on-camera-dvr)), with retention of roughly the last 72 hours depending on the SD card and motion.

---

## Sample Images of Sensors

Here are example screenshots from the CuboAI integration:

### Last 5 Alerts Sensor Card

![CuboAI Alerts Example](https://github.com/user-attachments/assets/ea368a6b-ca80-4f08-9160-898309fcd0f0)

### Camera State & Subscription Status

![CuboAI Camera and Status Example](https://github.com/user-attachments/assets/eb5eca1e-ccf1-4ed4-b6e0-f4defc56641d)

![CuboAI Camera and Subscription Example](https://github.com/user-attachments/assets/0ac518f7-e24e-471e-b550-dcf928ab6ddc)

## Baby Info
![CuboAI Camera and Subscription Example](https://github.com/user-attachments/assets/3f8d49bf-38b3-41e9-9f41-6c7f63563c8d)

## 🖥️ Example Lovelace Dashboard

Below is a sample of how you might present the alerts in a Markdown card, including event images:
![CuboAI Dashboard sample](https://github.com/user-attachments/assets/4acccaf6-451e-4b34-96bd-e97271ebb800)

> 💡 Replace `{{Your Baby Name}}` with the actual entity suffix (e.g., `john`).

```yaml
type: markdown
title: 🍼 CuboAI Last 5 Alerts
content: >
  {% set alerts = state_attr('sensor.cuboai_last_alert_{{Your Baby Name}}', 'alerts') %}

  {% if alerts %}

  | Type | Time | Image |

  |------|------|-------|

  {% for alert in alerts %}

  | **{{ alert['type'].replace('CUBO_ALERT_','').replace('_',' ').title() }}**
  | 
    {{ as_timestamp(alert['created']) | timestamp_custom('%Y-%m-%d %H:%M', true) }} | 
    {% if alert['image'] %}![img]({{ alert['image'] }}){% else %}-{% endif %} |
  {% endfor %}

  {% else %}

  _No recent alerts_

  {% endif %}

```
---

## 🎨 CuboAI Custom Lovelace Card (Recommended!)

For the absolute best experience, we provide a **Custom Lovelace Card** (`cuboai-card.js`) that automatically wraps the WebRTC Camera card and provides a fully native app-like experience directly in Home Assistant!

![CuboAI Custom Lovelace Card Preview](docs/images/custom_card_preview.png)

### ✨ Features:
- **Live Environmental Overlays**: Real-time Temperature & Humidity floating directly over the video feed!
- **Baby Vitals**: Live BPM (Heart Rate) overlay directly on the video if you have the Sleep Sensor Pad!
- **Two-Way Audio**: tap the mic button to talk to the room — the card negotiates the WebRTC microphone backchannel that stock HA cards can't.
- **Picture-in-Picture**: pop the video into a floating window — native PiP on Android and Apple devices, and on desktop Chrome an overlay PiP that keeps the BPM/temperature badges visible in the floating window.
- **Sound that just works**: listening runs over WebRTC (Opus) with an MSE fallback, so audio plays on Chrome, Safari and iOS alike; unmuting waits for your first tap so the browser never blocks it.
- **Smart Fallback**: Automatically leverages the camera entity to enable fallback to MSE/HLS when you are outside your home network (so video always plays flawlessly over Home Assistant Cloud / Nabu Casa)!
- **Advanced Lullaby Player**: A dynamic, sliding drawer menu to manage lullabies and speaker logic natively:
  - **Sources**: Play songs directly from **YouTube**, or use **Spotify** links (currently in testing mode).
  - **Library Management**: Create custom playlists, add your own songs, and use the built-in search logic to find tracks easily.
  - **Playback Control**: Manage play time filters, shuffle/repeat (synced across your devices), and the underlying speaker logic intuitively from the UI.
  - **Song Cache**: with the cache option on, downloaded songs replay instantly; the card's editor has a Clear Song Cache action (also a button entity + `cuboai.clear_youtube_cache`).
- **Built-in Config Editor**: the card has a visual editor (camera picker, default mute, song filters, per-camera H.264 transcode toggle) — no YAML required.
  
  <p float="left">
    <img src="docs/images/lullaby_step_1.png" width="300" />
    <img src="docs/images/lullaby_step_2.png" width="300" />
  </p>

### 🛠️ Installing the Custom Card

To use the custom card, you must first install the **WebRTC Camera** custom card (by AlexxIT) from HACS, as our card uses it under the hood for ultra-low latency video.

1. **Install WebRTC Camera:** Go to HACS -> Frontend -> Search for "WebRTC Camera" and install it.
2. **That's it.** Since **2.4.8** the integration registers `cuboai-card.js` automatically (and keeps its cache-buster current on every update) — there is nothing to add under Settings → Dashboards → Resources.

> ⚠️ **Upgrading from an old install?** If you once added `/local/cuboai-card.js?v=…` as a manual resource, **delete that resource** — a manually pinned URL is cached by the browser forever and keeps running old card code (the "`[CuboAI Patch] Prevented duplicate registration`" console message is the telltale). The integration re-points stale entries automatically on startup, but removing the manual resource is the clean fix.

The same file also provides `custom:cuboai-timeline-card` (the day/night report timeline used by the bundled dashboard) — no separate resource needed for it either.

### 💻 Using the Card in your Dashboard

Go to your dashboard, click "Edit Dashboard", add a "Manual" card, and paste the following YAML. Provide your camera's internal `device_id` to link all of its sensors automatically!

```yaml
type: custom:cuboai-camera-card
device_id: {cubo_id}
default_mute_state: unmuted
```

The card will automatically detect all the related sensors (temperature, humidity, lullaby, etc.) using your camera's device ID and seamlessly link them all together!

### 📱 Using the Custom Features
Once the card is on your dashboard, you have full control over the camera directly from the video feed:
- **Night Light:** Tap on the Night Light icon overlay to instantly toggle the camera's physical night light on or over.
- **Lullabies:** Click the music note icon to open the sliding Lullaby drawer. You can select a song, adjust the timer, and play/pause the music natively. 
- **Instant Syncing:** Because this hooks directly into the Home Assistant entities, any action you take (like turning on a lullaby) will **instantly synchronize across all devices**. If you play a lullaby on your iPad, your phone's dashboard will immediately reflect that the lullaby is playing!

---

## ⏪ Recorded Playback (on-camera DVR)

The camera records to its own storage. This integration can play that footage
back **inside the card you already have** — no cloud subscription, and no second
card on your dashboard.

Retention is **however much the SD card holds, oldest pruned first**:
measured on one camera it was ~2 days while recording lightly, and a sliding
~18-20 hours once baby-presence detection (which records heavily) was on —
the same window the official app's bar shows. A moment the card no longer
holds shows "Nothing recorded at that moment", and the scrub bar clamps
itself to the oldest moment that actually plays.

### Using it

A scrub bar sits under the picture, with a time field and fine-adjust buttons:

```
Live                                            ● LIVE
────────┬────────┬────────┬────────┬────────┬────────
      Wed 18   Thu 00   Thu 06   Thu 12   Thu 18
                    ▲ drag
[ 06/08/2026 02:00:00 ]                         [ Go ]
[  -1m  ][  -10s  ][  +10s  ][  +1m  ]
```

| Control | For |
|---|---|
| **The bar** | The rough moment. Two days across a phone is about 8 minutes per pixel. |
| **Time field + Go** | An exact date, hour and minute. |
| **−1m / −10s / +10s / +1m** | Seconds. iOS renders `datetime-local` as a wheel with no seconds whatever `step` says, so these exist on every platform. Taps are debounced, so four taps are one seek. |
| **● LIVE** | Back to the live stream. |

Releasing the playhead plays 15 minutes from that point, in the same card. The
picture switches back to live when you press LIVE.

While playback runs, the label under the bar is a **running timecode** — it
ticks with the footage moment (seek time + seconds actually played) and the
playhead walks along the bar with it, like the official app.

**Empty moments are normal.** The camera's SD card does not hold every minute:
retention varies with how much was recorded (with baby-presence detection on,
it can be as short as roughly the current camera-local day — the camera
rotates its per-day recordings, so "yesterday evening" can disappear at
midnight while the whole current day plays fine). Scrubbing to a moment the
card doesn't hold shows *"Nothing recorded at that moment — try another time"*
and returns to live by itself. Seek times are exact — a request for 02:50
plays the camera's own 02:50 footage, verified frame-for-frame against the
official app.

### Card options

```yaml
type: custom:cuboai-camera-card
# Everything below is optional.
show_timeline: true          # false hides the scrub bar entirely
timeline_hours: 18           # span of the bar; adjust to your card's retention
timeline_play_seconds: 900   # how much footage one request plays
show_timestamp: false        # on-video clock, bottom-right (see below)
show_mat_overlay: true       # sleep-mat BPM badge (auto-hides with no mat)
show_env_overlay: true       # temperature/humidity badge (auto-hides with no data)
show_music: true             # false hides the lullabies/music section
```

**`show_timestamp`** draws a clock onto the video, bottom-right. It is driven by
**frame progress, not a wall clock**, so a frozen picture is obvious rather than
hidden behind a ticking clock:

- **Live view:** shows the current time while frames flow; if the stream stalls
  for more than ~4 seconds it **freezes at the last-frame moment and turns red**,
  so a stale image looks stale.
- **Reverse / recorded playback:** shows the **footage time** you are watching —
  it follows the moment under the playhead as you scrub back, matching the scrub
  bar's own running label, and returns to the live clock when you press LIVE.

This is the card's own overlay (browser-side). To bake the time into the RTSP
stream an NVR records, use the separate **RTSP timestamp** option instead — see
[the RTSP section](#optional-burn-the-time-into-the-recording).

**The bar clamps itself to what actually plays.** The camera prunes its
oldest recordings as the SD card fills — measured live, the playable window
under heavy recording slides around **18-20 hours**, matching the official
app's own playback bar. The card's bar starts at `timeline_hours` (default
18 — an upper bound, not a promise) and, once a seek has come back "Nothing
recorded", clamps its left edge to the oldest playable moment it has learned
— from then on it shows the same window the official app does, per camera,
and keeps tracking it as the camera prunes. A later successful seek older
than the mark (a privacy-mode gap, not deletion) resets the clamp; a later successful seek older than the mark clears
it (the gap was privacy mode, not deletion).

### The service

Playback is also a service, so automations and scripts can use it:

```yaml
service: cuboai.play_recording
data:
  device_id: CB02XXXXXXXXXXXX
  start_time: "10m"          # "90s", "10m", "2h", "3d" — or an absolute time
  duration: 900
```

`start_time` accepts a relative amount ago or an absolute date/time. A naive
string like `2026-08-06 02:00:00` is read in **your** timezone. `duration`
defaults to **60** seconds and is capped at **900** (15 minutes) per request.

```yaml
# Show the last ten minutes on a wall tablet when the doorbell rings.
automation:
  - alias: Rewind the nursery on doorbell
    triggers:
      - trigger: state
        entity_id: binary_sensor.doorbell
        to: "on"
    actions:
      - action: cuboai.play_recording
        data:
          device_id: CB02XXXXXXXXXXXX
          start_time: "10m"
          duration: 600
```

A second camera entity, `camera.<baby>_recording`, carries the playback stream.
You do **not** need to put it on a dashboard — the card drives it for you. It
idles with no picture until something asks for a moment.

---

---

## 📡 RTSP — recording to an NVR (Frigate, Synology, HiLook, Blue Iris, …)

The integration runs its own go2rtc server, so the camera's live feed is
available as a plain RTSP stream that any recorder or player (VLC included)
can consume. **The URL is published as an entity attribute — copy it from
there rather than typing one out**, because both the port and the stream name
can legitimately differ from the examples below.

### 1. Turn the export on

Settings → Devices & Services → **CuboAI → Configure**:

- **Expose RTSP stream for NVR** — on.
- **NVR username / password** — strongly recommended. Leave the password empty
  and the stream is **open to everyone on your network**; the sensor will say
  so plainly (`nvr_auth: none (open stream)`).

### 2. Copy the URL from the sensor

Developer Tools → **States** → `sensor.cuboai_<baby>_cuboai_webrtc_stream_<baby>`:

| Attribute | What it is |
|---|---|
| `nvr_rtsp_url` | **The one to paste into your recorder** — reachable from your LAN, credentials included when set |
| `nvr_rtsp_url_video_only` | Same, with `?video` — video only, no audio track |
| `rtsp_url` | The same stream via `127.0.0.1`; only useful *inside* the HA host |
| `nvr_auth` | `basic` if protected, `none (open stream)` if not |
| `web_player_url` | A browser test page served by go2rtc |

They look like this:

```
rtsp://user:password@<HA-IP>:<rtsp-port>/cuboai_combined_<device id>
rtsp://user:password@<HA-IP>:<rtsp-port>/cuboai_combined_<device id>?video

# e.g. — but copy YOUR values from the sensor, never these:
rtsp://user:password@192.168.1.50:8557/cuboai_combined_CB02XXXXXXXXXXXX
```

**Use `?video` if your recorder refuses the stream.** The default URL carries
the two-way-audio (PCMA backchannel) track, and several NVRs — Hikvision and
HiLook in particular — reject a stream with a sendonly audio media instead of
just ignoring it.

### Optional: burn the time into the recording

**Options → RTSP timestamp** (a per-camera checklist, like the H.264 toggle):
check a camera to draw the current time into the RTSP video image, so your
NVR's recordings show when each frame was captured. When it's on, `nvr_rtsp_url`
automatically points at a dedicated `cuboai_stamped_<device id>` stream — **re-copy
the URL into your recorder** after enabling it.

It is opt-in because it forces a transcode of the NVR stream (extra CPU on the HA
host, only while the recorder is connected). The live card and HomeKit are
unaffected — they keep the un-stamped passthrough stream; if you want a clock in
the card's own view, use `show_timestamp` (above), which correctly shows the
footage time during recorded playback too.

This RTSP burn-in is **live-stream only**: it writes the real wall-clock time onto
the frames as they are captured, which is exactly what an NVR recording needs. It
is deliberately not applied to the DVR-playback stream — that replays *past*
footage, so a "now" clock burned into it would be wrong. (The card's
`show_timestamp` overlay handles playback correctly by showing the footage time,
because it is a browser overlay that knows which moment is on screen.)

### 3. Two things that are not what you would guess

- **The port is whatever go2rtc actually bound — read it, don't assume it.**
  It starts from the `rtsp_port` option (default `8555`), but Home Assistant's
  own built-in go2rtc normally holds `8555`, so the integration self-heals to
  the next free port; `8557` is the common outcome, not a constant. The
  resolved value is published in `nvr_rtsp_url` and in the camera's
  `rtsp_port` attribute — those are the source of truth, and they follow the
  port automatically if it ever changes. See
  [Streaming ports & conflicts](#-streaming-ports--conflicts).
- **The stream name is not fixed.** It is `cuboai_combined_<device id>`
  normally, and `cuboai_h264_<device id>` when the per-camera **H.264
  transcode** is on (issue #85). A URL copied before you flipped that toggle
  will 404 afterwards. This is exactly why the attribute is the source of
  truth: it always names the stream the integration is currently serving.

### Which stream is which

go2rtc serves three streams per camera. Only the first belongs in an NVR:

| Stream | For |
|---|---|
| `cuboai_combined_<id>` (or `cuboai_h264_<id>`) | **The live view** — this is the one to record |
| `cuboai_dvr_<id>` | Recorded playback; idle until `cuboai.play_recording` asks for a moment |
| `cuboai_speaker_<id>` | The speaker/TTS backchannel, not a video source |

### If the recorder cannot connect

| Symptom | Cause |
|---|---|
| **404 / "stream not found"** | Wrong stream name — re-copy `nvr_rtsp_url`. Renames happen on upgrade ([2.6.0](#-upgrading-to-260--nvr--rtsp-users-must-re-copy-their-url)) and when the H.264 toggle changes. On recorders that take a *path field* rather than a URL, re-read the stored value from the device: a lost prefix looks identical to a network fault. |
| **Connection refused / reset** | Wrong port (see above), or you used the `127.0.0.1` URL from another machine — that address only works on the HA host itself. |
| **401 / authentication failed** | The NVR password changed; re-copy the URL, which embeds the current credentials. |
| **Connects, but no picture on an H.265 camera** (Cubo 3 / SW05) | The recorder cannot decode HEVC. Turn on **H.264 transcode** for that camera, then re-copy the URL — it will now name the `cuboai_h264_` stream. |
| **Picture, but the recorder complains about audio** | Use `nvr_rtsp_url_video_only`. |
| **Recorder says offline and `go2rtc.log` shows nothing at all** | Almost always a wrong stream path — go2rtc logs nothing for a name it doesn't have. See below. |
| **Connects, streams for ~30-60 s, then drops repeatedly** | Possibly the `GET_PARAMETER` keepalive go2rtc ignores — but confirm the path first. See below. |

### Hikvision / HiLook NVRs — verified working settings

These recorders do **not** take a URL. They take a *protocol definition* with
the address, port and path in **separate fields**, which is exactly why a
mistyped or truncated path is the most common failure (see below). Verified end
to end on an NVR-216MH-C/16P running firmware V3.4.97:

Configuration → Camera → **Custom Protocol** (create one, e.g. named `CuboAI`):

| Field | Value |
|---|---|
| Type | `RTSP` |
| Transmission Protocol | `RTP Over RTSP` (TCP) |
| Port | **whatever `nvr_rtsp_url` shows** — copy it from the sensor. Commonly `8557` (HA's own go2rtc usually holds `8555`), and never `554` unless you set that yourself. |
| Stream Path | `/cuboai_combined_<device id>` — the **whole** name, leading slash, no host, no port |

Set the **same path for both Main Stream and Sub Stream** (the NVR asks for
both; a blank sub-stream can keep the channel offline).

Then add the camera: Configuration → Camera → **IP Camera → Custom Add**, with
protocol `CuboAI`, the HA host's IP, and the same port. Leave the password
empty if the stream is unauthenticated.

Two facts worth knowing about these units:

- The path field accepts a `?video` suffix, so
  `/cuboai_combined_<device id>?video` is valid if you want to drop the audio
  track — but plain H.264 + AAC records fine, so only reach for it if the NVR
  complains about audio.
- The request they emit puts **no port in the RTSP URL** (`rtsp://<ip>/<path>`)
  even though they connect on the right port. That is normal and go2rtc
  handles it.

### 🔎 The recorder shows "offline" and the log shows nothing at all

**go2rtc does not log a request for a stream name it does not have.** No 404
line, no client address, nothing — so a recorder with one wrong character in
its path is completely invisible on the server side, and it is very easy to
conclude the recorder never made contact.

This is the single most likely cause of an NVR that will not come online, and
it is worth ruling out before suspecting the network. Verified on a HiLook
NVR-216MH-C/16P (firmware V3.4.97): the channel had been re-entered by hand
after the 2.6.0 stream rename and had ended up as

```
/combined_<device id>         <- missing the "cuboai_" prefix
```

which produced `ipcStreamFail` on the NVR and total silence in `go2rtc.log`.
Correcting the path brought the channel online immediately and it then ran
continuously with no drops.

**How to check it properly, in order:**

1. **Compare the recorder's stored path against the sensor, character by
   character.** On Hikvision/HiLook, Configuration → Custom Protocol → *Stream
   Path*; the truth is the `nvr_rtsp_url` attribute on the WebRTC Stream
   sensor. Renames happen on upgrade ([2.6.0](#-upgrading-to-260--nvr--rtsp-users-must-re-copy-their-url))
   and whenever the H.264 toggle changes.
2. **Test the exact URL from a computer** (`ffprobe`, VLC). If it plays there
   and the recorder still fails, the recorder's stored value differs from what
   you think it is.
3. **Only then look at the log** — and confirm the *address*. A `new consumer`
   line carries no IP; check `remote_addr` under
   `http://<HA-IP>:<api-port>/api/streams?src=<stream>` (the API port is
   `1985` unless it self-healed too — the WebRTC Stream sensor's
   `go2rtc_server` attribute always has the live value). It is easy to mistake your
   own test pull (VLC, ffprobe) for the recorder and chase a phantom.

### A note on RTSP keepalives

go2rtc's RTSP server answers `OPTIONS` but ignores `GET_PARAMETER` entirely —
mid-session it sends no reply at all, not even an error
([AlexxIT/go2rtc#289](https://github.com/AlexxIT/go2rtc/issues/289)). Some RTSP
clients use `GET_PARAMETER` as their keepalive, so this *can* matter. It did
**not** affect the HiLook above, which kept a session open indefinitely once
its path was correct — so do not assume it is your problem: confirm the path
first, and look for a drop at a regular interval (~30–60 s) before blaming it.

### A note on load

Every consumer shares **one** camera session — recording does not open a second
connection to the camera, which is deliberate ([issue #85](https://github.com/niruse/cuboai/issues/85):
a second session broke HomeKit on the Cubo 3). Recording continuously is fine;
the camera is opened once no matter how many things are watching.

## 📈 Sleep windows and the timeline chart

CuboAI keeps Total Sleep, Wake-ups, Longest Sleep and the sleep-routine chart
behind its paid tier — the app renders them as *"Report Preview — Activate
Ultimate to see more"*. This integration cannot fetch them either.

What the camera **does** give away for free is its DVR history, including
`baby_present`. Recorded by Home Assistant, that supports a comparable set of
figures, computed locally.

### The timeline card

`history-graph` gives every entity its own strip and its own axis, so you
cannot see that a noise spike and the baby leaving the crib were the same
moment. This card puts every row on **one shared axis**:

```yaml
type: custom:cuboai-timeline-card
title: Last night
from: "19:00"        # a clock window; `to` at or before `from` spans midnight
to: "07:00"
rows:
  - entity: sensor.cuboai_mia_cuboai_baby_present_mia
    label: In crib
    icon: mdi:sleep
    match: in crib
    color: "#2a9d8f"
  - entity: sensor.cuboai_mia_cuboai_baby_present_mia
    label: Not in crib
    icon: mdi:baby-carriage
    match: ["not in crib", "0"]     # a list matches any of them
    color: "#e9c46a"
  - entity: sensor.cuboai_mia_cuboai_noise_level_mia
    label: Noise over 26
    icon: mdi:volume-high
    above: 26                       # numeric rows use a threshold
    color: "#5e5ce6"
```

| Option | Meaning |
|---|---|
| `from` / `to` | A clock window. Always the most recently **started** one, so a Night card keeps showing last night all through the next day. An unfinished window keeps its full width and fetches only up to now. |
| `days: 7` | A multi-day span ending now. Marks switch to one per day past 48 h. |
| `hours: 14` | The last N hours ending now. |
| `match` | A state string, or a list of them. |
| `above` | For numeric sensors: shade wherever the value is at or above this. |
| `events` | The attribute holding a list of point events — see below. |
| `match_type` | On an `events` row: draw only this alert `type` (or a list of them). |

#### Alert markers

Cry, Cough and Caregiver in the CuboAI app's own chart are **point events**:
they have a moment, not a duration, and Home Assistant never records them as
states. They arrive as a list on an attribute instead, so a row that names that
attribute is drawn as ticks rather than as shading:

```yaml
  - entity: sensor.cuboai_last_alert_mia
    label: Alerts
    icon: mdi:bell-ring
    events: alerts                  # the attribute holding the list
    color: "#ff453a"
  - entity: sensor.cuboai_last_alert_mia
    label: Crying
    icon: mdi:emoticon-cry
    events: alerts
    match_type: CUBO_ALERT_CRY      # one type per lane, once they appear
    color: "#ff9f0a"
```

Such a row makes **no recorder call at all** — the list is already in the
frontend as a state attribute, which is also why it keeps drawing when the
history behind the other lanes fails. Tap a marker for its type, its time and
its photo if one was saved.

No alert type is built in. `CUBO_ALERT_TEMPERATURE` is the only one seen on this
camera so far, the types are free-form strings from CuboAI's API, and a row with
no `match_type` draws whatever arrives — including types that do not exist yet.

The legend counts markers rather than showing a share of the window, and says
**recent** because that is what they are: the integration keeps only its last
`alerts_count` alerts (5 by default) from the last `hours_back` hours (12), so
the opening hours of a longer window cannot be covered however quiet they were.

Tap a bar for what it is and how long it lasted; tap a row's icon to open that
sensor. Each lane's share of the window is shown in the legend — that number is
what makes one period visibly different from another.

Two behaviours worth knowing: `unavailable` and `unknown` are **never** drawn as
a negative reading (the difference between "not in the crib" and "no idea"), and
a span runs from a matching reading to the next reading of any kind, so gaps are
not swallowed into one long block.

#### Two lanes people ask about

- **There is no `Caregiver?` lane, on purpose.** The firmware's *wellbeing*
  bit was the only candidate signal, and the experiment is settled: a real,
  known 2 a.m. caregiver visit produced **zero** of the `out of crib
  (caregiver?)` states upstream guessed would mark one — the bit just churned
  its two noise states straight through the visit. A visit **is** clearly
  visible as strong motion, so the example dashboard's Moving lane matches
  `moving`, `strong (2)` and `strong (3)` and covers it. (The Caregiver
  Activity sensor still exists and records, in case a firmware update ever
  gives the bit meaning.)
- **`In crib` (with the sleep lane and the Sleeps count) can stay at 0** —
  presence readings depend on the camera actually detecting someone; if the
  lane never fills, check the camera's baby-presence / sleep-safety settings
  in the CuboAI app. An empty room is a reading, not a fault.

#### Seeing *last night's* alerts the next day

The alert lanes can only draw alerts the integration still holds:
`alerts_count` of them, from the last `hours_back` hours (defaults 5 and 12).
With the defaults, last night's alerts age out by the following afternoon and
the lane goes empty. If you want the Nighttime tab to keep its alert markers
through the next day, raise both in **Settings → CuboAI → Configure** — e.g.
`alerts_count: 20` and `hours_back: 24`.

### The figures

`packages/cuboai_sleep.yaml` in this repo builds the same four metrics for
three windows using Home Assistant's built-in `history_stats`:

| Metric | Night 19:00–07:00 | Day 07:00–19:00 | Week |
|---|---|---|---|
| Time in crib | ✅ | ✅ | ✅ |
| Time not in crib | ✅ | ✅ | ✅ |
| Number of sleeps | ✅ | ✅ | ✅ |
| Time the sensor said nothing | ✅ | ✅ | ✅ |

Plus, for the week, an average day and a **coverage** percentage saying how much
of the total to believe.

Two things it is careful about, both learned the hard way:

- **It matches state strings, not raw numbers.** The sensor reports the
  library's phrase where it has one, so `1` surfaces as `in crib` and `2` as
  `not in crib`. Matching `"1"` silently measures nothing.
- **`0` is treated as "not in the crib".** What `0` means is not documented
  upstream — only 1 and 2 are — but it holds steady while a room is empty.
  It is *not* treated as "the camera said nothing": those readings arrive fresh
  and available. Genuine blindness is `unavailable`/`unknown` and is measured
  separately, so an empty room is never reported as camera downtime. Note that a
  Home Assistant restart also makes entities unavailable, so restarts count
  toward it.

---

### What it looks like

![CuboAI dashboard — Live and Nighttime tabs](docs/images/dashboard-sample.svg)

*An illustration, drawn from the card's own layout and colours — not a photograph
of anyone's nursery.* The left panel is the **Live** tab: the camera with the DVR
scrub bar, the time field and the seconds buttons under it. The right is
**Nighttime**: the window it covers, the four figures, and the swimlane timeline
with each lane's share of the window in the legend.

<!--
  Replace with real screenshots when convenient — drop PNGs into docs/images/
  and swap the link above. Taken at phone width, where the layout matters most:
    dashboard-live.png  dashboard-night.png  dashboard-summary.png  dashboard-alerts.png
  Check them for the baby's name and real entity ids before committing: this
  repository is public.
-->

## 🗂️ Installing the full dashboard

The repo ships a five-tab dashboard — **Live · Nighttime · Daytime · Summary ·
Alerts** — as a worked example.

> **The example uses the entity IDs of a camera named `mia`.** Yours will differ.
> Open Developer Tools → States, filter for `cuboai`, and replace `mia`
> throughout both files with your own. Nothing will render until you do.
> **[`dashboards/README.md`](dashboards/README.md) walks through exactly what to
> replace** — including the area-prefix trap (HA prepends the room name to
> entity ids) and which `mia_` sensors to leave alone. Read it before editing.

**1. Copy both files into your config.**

```bash
# from the repo
cp dashboards/cuboai.yaml            /config/dashboards/cuboai.yaml
cp dashboards/packages/cuboai_sleep.yaml /config/packages/cuboai_sleep.yaml
```

**2. Enable packages** (skip if you already have them) in `configuration.yaml`:

```yaml
homeassistant:
  packages: !include_dir_named packages
```

**3. Register the dashboard** in `configuration.yaml`:

```yaml
lovelace:
  dashboards:
    cuboai-dashboard:
      mode: yaml
      title: CuboAI
      icon: mdi:baby-face-outline
      show_in_sidebar: true
      filename: dashboards/cuboai.yaml
```

**4. Check the config before restarting** — Developer Tools → YAML → *Check
configuration*, or:

```bash
ha core check
```

**5. Restart Home Assistant.** A restart is required: packages and YAML
dashboards are only read at startup.

**6. Hard-refresh the browser** (Ctrl+Shift+R, or force-close the Companion
app). The card is cached, and a stale copy is the most common reason a change
appears not to have landed.

### What each tab holds

| Tab | Contents |
|---|---|
| **Live** | The camera card with the scrub bar, current temperature / humidity / noise, presence, and the controls. |
| **Nighttime** | 19:00–07:00: the four figures, and the timeline for that window. |
| **Daytime** | 07:00–19:00: the same four figures, same lanes. |
| **Summary** | The last 7 days, plus average day and coverage. |
| **Alerts** | The latest alert, recent alerts with thumbnails, and what the camera is watching for. |

The three report tabs deliberately measure **the same things** and differ only in
the period they cover — otherwise they cannot be compared with one another.

### If a tab looks empty

- `In crib` stays at zero until the camera has actually reported someone in the
  crib. An empty room is a reading, not a fault — and **it can only ever report
  one with baby-presence / sleep-safety detection turned on** (CuboAI app, or
  the integration's Baby Presence switch).
- `Caregiver?` at 0% is the expected baseline — see
  [Two lanes people ask about](#two-lanes-people-ask-about).
- Every window also publishes how long the sensor said *nothing*. If that is
  large, the figures beside it cover only part of the window — an offline camera
  and an empty room otherwise look identical.
- The `Camera online` lane on the chart exists for the same reason.


---

## 🛠️ Troubleshooting

### ⚠️ Upgrading to 2.6.0+ — NVR / RTSP users must re-copy their URL

In **2.6.0** the live stream name changed from `cuboai_<device id>` to
**`cuboai_combined_<device id>`** — one stream per camera is what fixed HomeKit
"No Response" on the Cubo 3 (issue #85), and **no alias is offered** for the old
name (an alias would silently re-create the double-session bug). If you ever
pasted an RTSP URL into an external recorder (Frigate, Synology, HiLook, …), it
now points at nothing and the recorder fails **silently**. Re-copy the address
from the WebRTC Stream sensor's `nvr_rtsp_url` attribute. Everything inside Home
Assistant (card, HLS, HomeKit, snapshots) migrated automatically.

### Debug logs

If you are experiencing issues (stream not playing, HomeKit "No Response", sensors showing as "Unknown", the configuration flow hanging), one toggle collects everything needed for a report:

1. Go to **Settings > Devices & Services > CuboAI > Configure**
2. Check **"Enable debug logs"** and submit, then restart Home Assistant.
3. Reproduce the problem once (open the stream, wait for a sensor update, ...).
4. Collect the files below and attach the relevant ones to your issue.

| Log file | Location | What it contains |
|---|---|---|
| `cuboai_debug.log` | HA `config` folder | Every debug/info/error message from the whole integration (sensors, camera, media player, coordinator) |
| `go2rtc.log` | `config/custom_components/cuboai/bin/` | Streaming diagnostics (since v2.4.5): detected video codec (`[mpegts] muxing ...`), per-frame codec census of the first 300 frames (`FICENSUS`), fps/bitrate/loss health line every 10 s, GOP sync events, RTSP session negotiation |
| `cuboai_last_alert_debug.log` | HA `config` folder | Alert polling / image download trace |
| Home Assistant log | **Settings > System > Logs** | The integration's debug messages also appear here automatically — no `logger:` changes in `configuration.yaml` needed |

> ⚠️ **Redact before sharing:** `go2rtc.log` (like `go2rtc.yaml`) contains your cameras' `CUBOAI_UID`, `CUBOAI_ACCOUNT` and `CUBOAI_PASSWORD` values on the exec command lines — replace them with `XXX` before attaching anything to a GitHub issue.

Log files are capped at 2 MB with rotation to protect disk space. Turning the toggle off restores the quiet defaults.

### 🔌 Streaming ports & conflicts

The integration runs its own internal go2rtc server for local streaming. It uses these TCP ports by default, and **self-heals automatically** when one is already taken (for example by Home Assistant's built-in go2rtc or the WebRTC Camera add-on):

| Port | Purpose | If already in use |
|------|---------|-------------------|
| `8555` | RTSP listener (camera stream) | Hops to the next free port (usually `8557`) |
| `1985` | go2rtc API (snapshots, card, WebRTC) | Hops to the next free port (usually `1986`) |
| `8556` | WebRTC listener | Hops to the next free port |

You never need to configure anything for this. The camera entity and sensors publish the effective port they resolved, and the custom card does not deal in ports at all — it finds its camera entity by the `device_id` attribute and lets that entity supply the stream source, so whatever port go2rtc actually bound is the port that gets used. A log line like `go2rtc API port 1985 is already in use by another process — using port 1986 instead` is informational, not an error.

`8555` is normally already taken by Home Assistant's own built-in go2rtc (it is *its* WebRTC listener), so a default of `8557` in the RTSP port field is the expected outcome rather than a problem.

Additional protections (since v2.4.4):

- **Orphaned go2rtc cleanup**: if a previous go2rtc process survived a hard Home Assistant crash and still holds the ports, the integration detects it (by its `cuboai_*` streams) and terminates it on startup, reclaiming the standard ports.
- **No retry storms**: if the internal go2rtc could not start at all, camera entities stop offering stream sources and live snapshots instead of hammering a port that may belong to another process (which previously caused an endless `Using native library` loop and resource exhaustion — see issue [#84](https://github.com/niruse/cuboai/issues/84)).

---

## 📝 Changelog

See the [CHANGELOG.md](CHANGELOG.md) file for a detailed history of updates, bug fixes, and improvements.

---

## 💖 Credits & Special Thanks

Massive thanks to [Fredrick (Fredde87)](https://github.com/Fredde87/cuboai-tutk) for his incredible reverse-engineering work and for providing the TUTK Kalay P2P protocol implementations that make the local streaming functionality of this integration possible!

---

## 🤝 Contributing

We welcome:
- 🔧 Bug fixes
- 🌟 Features
- 🧠 Suggestions

Submit a PR or [open an issue](https://github.com/niruse/cuboai/issues)

**Adding a sensor for something the camera knows but this integration doesn't show yet?**
See [docs/camera-probe.md](docs/camera-probe.md) — how to check whether an endpoint returns real
data before you write an entity for it, which endpoints answer on firmware 2.0.2273, and the
safety rules for probing a live baby monitor (short version: never sweep even IOCTL codes blindly —
SET codes are even too, and a blind scan will change your camera's settings).
