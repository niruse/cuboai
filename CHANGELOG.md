# Changelog

All notable changes to this project will be documented in this file.

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
