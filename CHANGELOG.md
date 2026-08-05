# Changelog

All notable changes to this project will be documented in this file.

## [2.4.11]

### Fixed
- **Custom card showed "connection reset by peer" while the generic WebRTC card worked (issue #89)**: the card located its camera by pattern-matching the entity id — it had to start with `camera.cuboai_`, end with `_local_camera`, and contain a "baby name" token sliced out of the speaker's entity id. None of that is guaranteed. The camera entity is named "<baby> Local Camera", so its id is whatever Home Assistant composes from the device and entity names, which differs between installs, HA versions and any rename; HA's `_2` duplicate suffix breaks it outright; and the baby-name token need not appear in the camera id at all. Whenever the match failed, the card silently fell back to a hardcoded `rtsp://127.0.0.1:8555/...` URL — and on Home Assistant OS that port belongs to HA's *own* built-in go2rtc WebRTC listener, which accepts the connection then immediately tears it down. That is the reported error, and it is why the generic card (pointed straight at the entity) kept working. The card now finds its camera by the `device_id` attribute the entity publishes, which is immune to all of the above, and streams through the entity — so the port go2rtc actually bound is always the port used, along with the NVR credentials and stream pre-warm that the hand-built URL silently skipped. With no camera entity at all, the card now says so instead of connecting to a port that will reset.
- **"Transcode this camera to H.264" was permanently greyed out**: the card editor located the camera with the same impossible entity-id filter, so the checkbox was disabled for everyone, silently defeating the H.265/HomeKit fix from 2.4.5.
- **Auto-detect branch of the card was dead code**: it looked for `media_player.cuboai_speaker_<id>`, but the speaker entity is `media_player.<baby>_speaker`, so the device id was always empty and the branch never ran.

### Changed
- **The RTSP port setting finally has a label**: the field appeared in both setup and options with no translation, so Home Assistant rendered the raw key `rtsp_port`. It now has a name and an explanation that the port self-heals and that `8557` is the expected default because Home Assistant's built-in go2rtc normally holds 8555.

## [2.4.10]

### Fixed
- **Sound stopped a few seconds after minimizing the window**: the embedded WebRTC player intentionally disconnects the stream ~5s after the page is hidden (minimized window, background tab, app switch) to save resources — wrong for a baby monitor, and it also cut the stream feeding the Picture-in-Picture mini-window. The card now runs the player with `background: true`, keeping video and audio alive while hidden.

### Added
- **H.265/HomeKit stream diagnostics under "Enable debug logs" (issue #85)**: three new signals to pinpoint why a transcoded stream won't play. (1) On startup, a `go2rtc stream plan` line per camera showing whether the H.264 transcode is actually applied (`video=h264` vs `video=copy`) and the raw `h264_cameras` option. (2) go2rtc's `ffmpeg` module now logs at debug, capturing the full ffmpeg command line of the transcode producer in `go2rtc.log`. (3) When HA/HomeKit requests the stream, a `[stream diag]` line (and a second one 20s later) dumps go2rtc's live view: each producer/consumer's codec per track (`hevc` vs `h264`), packet counts, and byte counters — showing at a glance whether the transcode ran, delivered data, and whether the consumer received any. Credentials are redacted from the logged output.

## [2.4.9]

### Fixed
- **No sound in the camera card (audio regression)**: the card listened over MSE on non-Apple browsers, but the streams offered Opus-only audio — and MSE cannot decode Opus, so the card was silent everywhere even when unmuted. Two-part fix: (1) every stream now offers **both** Opus and AAC audio (`#audio=opus#audio=aac`), and (2) the card listens over **WebRTC** (which negotiates Opus) with MSE as fallback. HLS/HomeKit consumers keep working via the AAC track.
- **"Unmuting failed and the element was paused" console warning**: the card attempted to unmute before the user had interacted with the page, which Chrome's autoplay policy answers by pausing the video and logging the warning. The card now checks `navigator.userActivation` — with no interaction yet it starts muted (clean autoplay) and brings the sound up on the first tap.
- **go2rtc hopped to port 1986 on reload/restart**: after terminating a previous go2rtc instance (orphan kill, or the integration's own stop on reload), the OS needs a moment to release port 1985 — probing immediately saw it still bound and hopped to 1986, stranding the frontend and HomeKit on the old port ("Cannot connect to …:1985"). Startup now waits briefly (up to 5s) for 1985 to free before resolving ports.

## [2.4.8]

### Fixed
- **Old card kept running from a stale manual dashboard resource (follow-up to #86)**: setups that added the card as a Lovelace resource with a *fixed* cache-buster (e.g. `/local/cuboai-card.js?v=111`) had that URL cached by the browser forever, so card updates never reached it — the removed `customElements.define` patch kept executing from the cached copy (`[CuboAI Patch] Prevented duplicate registration…` in the console). On start-up the integration now re-points any such `cuboai-card.js` resource at the live file mtime, forcing a fresh fetch on every update. Best-effort (skipped in YAML-resource mode). If you still see it, delete the manual `cuboai-card.js` resource under Settings → Dashboards → Resources — the integration loads the card automatically.

## [2.4.7]

### Changed
- **Clear guidance when no camera IP is set (issue #83)**: the pure-Python transport reaches the camera with a unicast probe to its LAN IP (no broadcast auto-discovery), so with no IP set the handshake fails with a cryptic "no 0x2041" and local sensors + the stream stay unavailable. The coordinator now logs one actionable warning per camera — telling you to set the camera's LAN IP under Settings → Devices & Services → CuboAI → Configure — instead of only the low-level error. The warning re-arms if the connection later recovers, and covers both failure modes (timeout and empty result).

## [2.4.6]

### Fixed
- **Intermittent blank white screen across all of Home Assistant (issue #86)**: the card globally overrode `customElements.define` to swallow "duplicate registration" errors, which intercepted **every** custom element on the page — including HA's own core UI shell (`home-assistant-main`, `ha-panel-config`, `ha-init-page`, …). It raced HA's frontend bootstrap and blocked those core registrations as "duplicates", producing a blank page that needed many refreshes to load. The global patch is removed; the card's own two elements remain individually guarded with `customElements.get()`, which is all that was ever needed.
- **Android: minimizing the video (Picture-in-Picture) did nothing (issue #87)**: the card renders PiP through a `<canvas>` overlay technique (to keep the BPM/temperature overlays in the floating window). Android Chrome hardware-decodes the video and can't read those frames back into a canvas, so PiP showed black / rejected. Android now uses the browser's **native** PiP (like Apple already did) — it works, without the drawn overlays. Desktop Chrome keeps the overlay technique.

### Added
- **Per-camera H.264 transcoding for HomeKit / HLS (issue #85)**: H.265/HEVC cameras (e.g. Cubo 3 / SW05) can't be consumed by HomeKit or HA's stream pipeline, which are H.264-only — the passthrough stream failed with "demuxing … timed out" / HomeKit "No Response". A new per-camera toggle (in Settings → CuboAI → Configure, in the card's config editor, and via the `cuboai.set_h264_transcode` service) makes go2rtc transcode that camera's video to H.264 (plus AAC audio for HLS, keeping Opus for WebRTC). Default off, so native-H.264 cameras (Cubo 2 / CB02) keep the efficient passthrough with no extra CPU. The camera entity exposes an `h264_transcode` attribute.

## [2.4.5]

### Fixed
- **Combined-stream codec misdetection (issue #85)**: the TS muxer chose its PMT codec from the first video access unit with a blind `hevc` fallback when the FRAMEINFO trailer was missing (mid-GOP join). A wrong PMT poisoned the go2rtc producer — H.264 declared as H.265 yields no parameter sets, go2rtc registers no video track, and every video consumer fails (frame.jpeg "codecs not matched", RTSP resets, HA stream "finding first packet" timeout / HomeKit "No Response"). Now: FRAMEINFO codec when present, else a decisive NAL-header sniff, else wait for the next access unit instead of guessing.
- **One-toggle debug logging**: `enable_debug_logs` now also switches go2rtc into `log: {exec/rtsp/streams: debug}` and runs the stream producers with a verbose FRAMEINFO census (first 300 frames), capturing full streaming diagnostics into `go2rtc.log` for issue reports.

## [2.4.4]

### Fixed
- **go2rtc API port conflict caused an infinite retry loop and resource exhaustion (issue #84)**: when TCP 1985 was held by another process, the integration logged an error but started anyway — camera snapshot/stream/WebRTC requests then landed on the stranger's socket. If that stranger was an *orphaned CuboAI go2rtc* surviving a hard HA crash (it still serves the `cuboai_*` streams from its old config), every request respawned a TUTK producer inside the orphan — an endless `Using native library: libIOTCAPIs_ALL.so` loop that piled up processes until the host locked up. Three layers of fix: (1) the API port now self-heals to a free port exactly like the RTSP port already did (8555→8557), and every consumer — camera snapshots, stream pre-warm, WebRTC offers, sensor attributes — follows the effective port; (2) an orphaned CuboAI go2rtc holding the port is detected (by its `cuboai_*` streams) and terminated on startup, reclaiming the standard ports; (3) if go2rtc could not start at all, camera entities return no stream source and skip live snapshots instead of hammering a port they don't own.
- **Duplicate entity registrations — "Platform cuboai does not generate unique IDs" (issue #84)**: the cloud API returns one entry per *baby profile*, so a renamed or re-created baby profile produced the same camera device twice and every platform collided on its unique IDs. Camera profiles are now deduplicated by device id (newest profile wins), and config entries that already persisted duplicates are healed on startup.

### Added
- README: "Streaming ports & conflicts" troubleshooting section documenting the default ports, the automatic fallback behaviour, and the orphaned-go2rtc cleanup.

## [2.4.3]

### Added
- **Brand icons**: the integration now ships its own CuboAI icon (`custom_components/cuboai/brand/`), served natively by Home Assistant 2026.3+ in Settings → Devices & Services, the config flow, and HACS — no home-assistant/brands entry needed.

## [2.4.2]

### Fixed
- **iOS (iPhone) sound broken by 2.4.0**: the MSE-only listening transport doesn't fit Apple WebKit — iPhones have no classic MSE (`ManagedMediaSource` only from iOS 17.1), and the 2.4.0 audio logic interfered with the native player. iOS now keeps the exact v2.3.x behaviour on all three fronts: legacy `webrtc,mse` dual transport (the configuration proven to deliver sound on iPhones), stock speaker-icon behaviour (appears when audio is detected, tap to unmute), and zero scripted mute interference — the desktop unmute logic, mute watchdog, and pinned speaker icon apply to desktop/Android only.

## [2.4.1]

### Fixed
- **NVR password could never be cleared**: the options form declared the password with `default=<old value>`, so when the field was cleared the frontend omitted the key and voluptuous silently re-inserted the old password — the "no authentication" mode was unreachable and the NVR URL sensor kept showing stale credentials. The field now uses `suggested_value` (still pre-filled, but clearing it really clears it) and an emptied password is stored explicitly as `""`, which disables RTSP auth and updates the sensor URL on reload.

## [2.4.0]

### Fixed
- **Reliable live audio in the card (desktop "no sound" fix)**: the card listed `mode: webrtc,mse` — video-rtc runs BOTH transports simultaneously and they race, with the winner ripping the other's source out of the `<video>` (endless `SourceBuffer` errors, spontaneous mutes, and audio-less WebRTC takeovers — the camera's AAC audio cannot ride WebRTC without a fragile Opus transcode). The card now uses exactly one transport per state: **MSE for listening** (plays the camera's AAC natively, like the mobile app) and **WebRTC only while the two-way mic is active**.
- **Native HA camera view (more-info / device page) lag & reconnect loop**: the entity forced WebRTC on the frontend, which requires an AAC→Opus ffmpeg transcode inside go2rtc that dies on every stream stall — each death killed the WebRTC session and the frontend resubscribed in a loop ("Received event for unknown subscription"). The frontend now defaults to **HLS** (carries H264+AAC natively, no transcode). HA detects WebRTC support by class introspection, so the WebRTC handlers moved to an opt-in subclass: set the `frontend_webrtc` entry option for HEVC models where frontend HLS can't play the video.
- **"Always Start Unmuted" broke the player**: browsers refuse unmuted autoplay, so the video never started and the volume button never rendered. The card now always starts playback muted (video + controls always alive), then tries for sound: if the browser allows unmuted play it's immediate; otherwise the card unmutes on the user's first interaction. An explicit user mute always wins.
- **Spontaneous re-mutes**: video-rtc force-mutes on ANY `play()` rejection, including harmless `AbortError`s from MSE source reloads ("plays with sound, flips to mute seconds later"). A watchdog now reverts mutes nobody asked for, detects Chrome's "unmuting failed, element paused instead" punishment, and degrades gracefully to unmute-on-first-click.
- **Speaker button missing/unclickable**: webrtc-camera creates the volume control `display:none` and only reveals it after audio detection on each (re)connect. The card pins it always-visible and keeps its icon in sync with the real mute state.
- **Card editor showed stale values**: the editor rendered once and ignored the saved config delivered afterwards, so dropdowns (e.g. "Initial Audio State") always showed defaults. The form now re-syncs whenever the config arrives.
- **Repeat mode (and speaker volume) reset on every restart**: the speaker media player is now a `RestoreEntity` — Repeat and volume survive HA restarts, and since the entity is the live cross-device authority for repeat, all devices stay in sync after a reboot.
- **Lullaby Timer / Play Time reset on every integration reload**: both number entities now restore their last value (`RestoreNumber`).
- **Media library wipe guard**: a stale browser could save an empty song/playlist list over a populated library. Saves that would replace >1 items with an empty list are now refused.
- **Video opened slightly zoomed/cropped**: the inner video defaulted to crop-to-fill; it now shows the whole camera frame (`object-fit: contain`).

### Changed
- Repeat chip in the card updates optimistically (instant feedback, entity round-trip confirms).

## [2.3.6]

### Fixed
- **Wrong-architecture go2rtc binary persisted forever (#80 follow-up)**: the repo shipped an ARM64 go2rtc binary, and the downloader skipped downloading whenever the file existed — so x86_64 hosts kept a binary that could never start, nothing listened on the API/RTSP ports, and every stream got "Connection refused". The downloader now validates the ELF architecture (`e_machine`) of the existing binary against the host on every startup and replaces it automatically when it doesn't match. The prebuilt ARM64 binary and the aarch64 TUTK library are no longer shipped in the repository — each host downloads the correct build on first start.

## [2.3.5]

### Fixed
- **RTSP port conflict with Home Assistant's built-in go2rtc (#80)**: on HAOS, HA's own go2rtc holds TCP 8555 (its WebRTC listener), so CuboAI's RTSP listener silently failed to bind while its API kept answering — every RTSP consumer got "connection reset by peer" / "Invalid data found when processing input". go2rtc now probes its ports before starting and self-heals to the nearest free port, publishing the effective port to the camera/sensor `rtsp_port` attributes that the card already reads — no manual reconfiguration or cache clearing needed.

## [2.3.4]

### Fixed
- **Sporadic "Error demuxing stream (Operation timed out)" from the HLS pipeline**: on a cold start the pure-python stream engine needs several seconds (camera handshake + first HEVC keyframe) — longer than HA's HLS demux timeout, so HLS consumers (e.g. the companion app) hit timeout/retry cycles. `stream_source()` now pre-warms the go2rtc producer (blocks until frames are flowing) before handing HA the RTSP URL, so the HLS worker gets packets immediately. WebRTC playback is unaffected.

## [2.3.3]

### Added
- **Clear Song Cache everywhere**: a new "Clear Song Cache" button entity on the CuboAI Media Library device, a trash button in the card's music toolbar (with confirmation), and a "Clear Song Cache" action in the card's configuration dialog — all equivalent to the `cuboai.clear_youtube_cache` service.
- **Cache controls in the card editor**: the card configuration dialog gets a "Song Cache" section with the cache checkbox and the clear action (global settings, shared by all cards/cameras).

### Fixed
- **Cached Spotify songs no longer contact spotify.com on every replay**: the cache key was computed after the Spotify→YouTube conversion, so even cached songs needed a network round-trip for the title (and a Spotify title-format change silently invalidated the cache). The cache is now keyed by the original link and checked before any network access.

### Changed
- The cache switch is renamed to **"Cache YouTube/Spotify Songs"** to reflect that Spotify links are cached too (resolved via YouTube). The internal entity id is unchanged, so state, history and dashboards are preserved.

## [2.3.2]

### Added
- **Cache toggle everywhere**: the "Save YouTube/Spotify songs to local cache" setting is now available as a checkbox in the integration Options and as a Cache ON/OFF button in the card's music panel (next to Shuffle/Repeat). All three controls (including the switch entity) drive the same setting.
- **CuboAI Media Library device**: the global entities (Cache YouTube Songs switch, Media Library sensor) are grouped under a visible device instead of hiding in the raw entity list.
- **Cross-device card settings**: Shuffle now syncs across all devices/browsers via the shared media library (new `cuboai.save_settings` service); Repeat was already synced through the Speaker entity.

### Fixed
- **Speaker Play Time is now a total session budget**: the timer used to restart for every queued song, so playlists of short tracks (or Repeat ALL) never stopped. Playback now stops at the deadline regardless of track count.
- **Card Play Time dropdown was dead**: it targeted a guessed `number.cuboai_speaker_timer_<device>` entity id that never exists; both the write and read paths now derive the real entity id.
- **Lullaby timers**: two coexisting mechanisms, cleanly separated. Playing from the entity controls / automations sends the Lullaby Timer to the camera (native enforcement, camera-supported durations 0/30/60). Playing from the card follows the card's Play Time with an HA-sent stop — any duration works. Lullabies started from the CuboAI app or camera schedule are never touched: the HA stop only exists for card-initiated playback and verifies the playing song before firing.
- Volume changes preserve the running lullaby session's timer mode; Lullaby Timer changes during native playback update the camera.

## [2.3.1]

### Fixed
- **"Unknown error" on `media_player.volume_set`**: the Speaker entity advertised volume support but never implemented `async_set_volume_level` (always crashed); and lullaby/switch/brightness commands could fail transiently because the camera rate-limits rapid session attempts. Camera commands now retry once and raise a descriptive error message instead of the generic toast.
- **Deselected cameras still visible**: unchecking a camera in Options now removes its device and entities from the registry instead of leaving them as "unavailable".
- **RTSP port silently moved on options save**: the options form probed for a free port while the integration's own go2rtc held the current one, suggesting a new port (8555 → 8557) on every save with no stored value and breaking open streams. The options flow now keeps the effective port.

## [2.3.0]

### Added
- **Camera selection**: setup now shows a "Select Cameras" checklist after login instead of automatically adding every camera on the CuboAI account. The same picker is available in the integration Options, so cameras can be added or removed later without reinstalling. New cameras appearing on the account are never set up automatically — they are offered in the Options picker instead. Existing installs keep their current cameras as the initial selection.

### Fixed
- **WebRTC offers rejected**: go2rtc answers `POST /api/webrtc` with `201 Created` on current versions; the handler only accepted `200`, so the frontend fell back to HLS/RTSP (which times out on HEVC).
- **Startup crash on Python 3.13/3.14 (HA 2025.x)**: `asyncio.create_task()` was given an executor `Future` instead of a coroutine in the media-library setup, aborting the whole component setup with `TypeError: a coroutine was expected`. The library load is now properly awaited.
- **Whole-HA segfault from concurrent native TUTK sessions**: the coordinator poll and lullaby/switch commands could each run their own native `TUTKSession` in separate threads; one calling `IOTC_DeInitialize()` while the other was inside `connect()`/`ioctl()` crashed the entire Home Assistant process (seen in `home-assistant.log.fault`). Native session lifetimes are now serialized by a process-wide lock.
- **HA process could be killed by the broadcast-redirect shim**: `os.execve()` in the shim loader replaced the running process; on hosts with `gcc` this could re-exec Home Assistant itself. Re-exec is now restricted to the standalone stream/CLI scripts.
- **Expired tokens were never refreshed during runtime**: all coordinator API errors were gathered with `return_exceptions=True` and only logged, so the 401 → refresh path was dead code and sensors silently went stale until restart. 401s now trigger the central token refresh.
- **Transiently offline cameras were deleted from the config entry** (entities, credentials and streams lost) if the cloud state query failed during startup. Offline cameras are now kept configured.
- **WebRTC on HA 2025.x**: implemented the async WebRTC offer API (`async_handle_async_webrtc_offer`); the legacy handler was removed from HA core, which silently broke the WebRTC/mic button.
- **Sleep/lullaby timers never applied**: timer number-entities were looked up by a guessed entity_id that never matched; now resolved through the entity registry.
- **Switch commands failing silently**: sleep mode / status LED / flip / baby-presence failures now surface as errors instead of optimistically showing the new state while the camera never received the command.
- **Native two-way-talk crash**: the native backend's `send_audio_file` imported a function that doesn't exist; it now clearly reports that talk is pure-Python-only (per upstream cuboai-tutk research).
- **Event-loop blocking I/O** in setup, downloader, config flow, media player, and the debug logger (now a QueueListener writing off-loop); assorted fd leaks (go2rtc log, backchannel stderr) closed.
- **Reload storm on camera-IP discovery**: the coordinator saving an auto-discovered camera IP no longer reloads the whole integration mid-refresh.
- **Interrupted go2rtc download** no longer leaves a permanently broken truncated binary (atomic rename).
- **Duplicate unique_id errors** with multiple config entries (global YouTube-cache switch, media-library sensor) and stale singleton flags after unload.
- Camera-IP autodetection no longer mistakes version strings like `2.1.0.5` for the camera's LAN IP (validates octets and requires a private-range address).

### Changed
- **Device/architecture support**: go2rtc is now downloaded for armv7/armv6/i386 hosts as well (previously anything but x86_64/aarch64 disabled all local features). The native TUTK library is optional everywhere — if it is missing or fails to load, the integration falls back to the pure-Python transport instead of failing.
- go2rtc's HTTP API now binds to `127.0.0.1` only (it exposes stream config including camera credentials).
- Subprocesses (go2rtc exec lines, backchannel) launch with HA's own Python interpreter instead of whatever `python3` is on PATH.
- `yt-dlp` is a declared requirement (no more runtime `pip install`).
- Tokens and camera passwords are no longer written to debug logs.

## [Unreleased]

### Added
- **Debug Logging Configuration**: Added an "Enable Debug Logging to File" checkbox to the Configuration and Options flows. When enabled, this cleanly pipes all integration logs, streaming engine logs (go2rtc/ffmpeg), and background tasks into strict 2MB-capped rotating `.log` files in the Home Assistant configuration directory. This makes troubleshooting networking issues significantly easier without filling up disk space.

### Fixed
- **Configuration Flow Hangs**: Added strict `asyncio.wait_for` timeouts to all background data fetching operations during setup. This prevents the configuration flow from hanging indefinitely when Home Assistant fails to connect to the camera on restrictive networks.
- **Exception Handling Crash**: Fixed an issue where newer Python versions returned `CancelledError` as a `BaseException` instead of an `Exception`, which bypassed the previous exception handlers and crashed the setup process. 
- **Direct IP Connection (Docker/VM Fix)**: Fixed a major issue where providing a static `Camera IP` in the configuration flow was silently ignored due to missing `gcc` compilers on Home Assistant OS/Docker environments. The integration will now automatically bypass the native C networking library and strictly use a pure-Python fallback connection method whenever a static IP is provided. This completely resolves the "Unknown" sensors issue for users running Home Assistant in Docker or Virtual Machines where UDP broadcast discovery is blocked.
