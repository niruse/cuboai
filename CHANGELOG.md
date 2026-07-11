# Changelog

All notable changes to this project will be documented in this file.

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
