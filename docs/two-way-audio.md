# Two-way audio (the card's microphone)

How the microphone button works, end to end, and why it is built this way. For maintainers: the
user-facing description is in the README's [Two-way audio](../README.md#two-way-audio) section.

## The path

```
phone / browser                     Home Assistant                    child process (per talk)            camera
───────────────                     ──────────────                    ────────────────────────            ──────
getUserMedia (echo cancel, AGC)
AudioWorklet tap (ScriptProcessor
  fallback) → resample to 16 kHz
  s16le mono → 40 ms chunks
                  ── cuboai/talk ──▶ ws_talk (talk.py)
                  ◀─ result+started─ checks, arbiter claim,
                     {handler_id}    spawn child ───────────────────▶ cuboai_stream_backchannel.py --live
[handler_id byte]+PCM ─ binary ────▶ TalkSession.on_binary ─ stdin ─▶ _LiveAacEncoder: PCM → AAC-LC ADTS
                                                                       feed queue (jitter floor 2 frames)
                                                                       send_audio_file(live_source=…)
                                                                         SPEAKERSTART ────────────────▶ talk login
                  ◀── event: live ── @@TALK READY ◀── stdout ──────── first pull after the grant
                  ◀─ event: status ─ @@TALK STATUS (≤ every 5 s)       one frame per 64 ms, silence when
                                                                         no speech is queued ───────────▶ speaker
1-byte frame (stop) ───────────────▶ close stdin ──────────────────▶ EOF → tail silence → SPEAKERSTOP ─▶
                  ◀── event: ended ─ @@TALK END (always last)
```

The talk never touches the video. The picture keeps playing on its own WebRTC/MSE connection, and
a mic tap never reconnects it.

## The card (`www/cuboai-card.js`)

- `_cuboStartMic` / `_cuboStopMic` / `_cuboMicTeardown`. The button's states are `idle` → `connecting`
  (amber: waiting for Home Assistant, then for the camera's grant) → `live` (red). Tap in
  `connecting` or `live` stops.
- **Capture:** `getUserMedia({audio: {channelCount: 1, echoCancellation, noiseSuppression,
  autoGainControl}})`, falling back to `{audio: true}` on `OverconstrainedError`/`TypeError`. The tap
  is an AudioWorklet loaded from a Blob URL (`CUBOAI_MIC_WORKLET_SRC`), or a ScriptProcessor where
  worklets are missing.
- **Format:** `cuboaiMicResampler` resamples from the context's *real* sample rate to 16 kHz, with a
  low-pass first above 17.6 kHz. `cuboaiMicChunker` cuts 640-sample messages (40 ms, 1281 bytes
  with the handler byte, 25 a second). They are sent straight on `hass.connection.socket` as
  binary frames.
- **Back-pressure:** if the socket already holds more than 32 KB (about 1 s), the chunk is dropped,
  not queued. A talk that falls behind catches up and never lags.
- **Timers, on the card as a backup to the server's:** 10 s for Home Assistant to accept, 40 s for
  `live`, a hard stop at 125 s, and 4 s after the end frame to wait for `ended`.
- **Other stops:** the page is hidden (`visibilitychange`), the card is removed, the track ends (a
  phone call takes the microphone), or Home Assistant's connection drops. A talk is never resumed
  after a reconnect: handler ids die with the socket, and a talk nobody asked for again must not
  restart.
- **Secure context only:** `cuboaiMicCanCapture` requires `isSecureContext`. On plain `http://`,
  `navigator.mediaDevices` does not exist, and the button explains why.
- **Test mode:** `talk_dry_run` fails safe. Any value except a missing key or the literal `false`
  turns it on (`cuboaiMicDryRunAsked`), so a YAML typo can never become a real talk in the nursery.
- **Speaker:** by default the talk leaves the room's sound alone. The phone's echo cancellation keeps
  its speaker out of the microphone.
  - With `talk_mutes_speaker`, `_cuboTalkMuteOn` mutes after the microphone is captured, and
    `_cuboTalkMuteOff` restores inside the stop tap. An iPhone only unmutes inside a user gesture,
    and a restore outside it came out "the opposite way".
  - A speaker tap during the talk is marked `touched` and stands.
  - The mute is never saved or synced, and the icon is never set by hand: it follows the video's
    real `muted`.

## Home Assistant (`talk.py`)

- **`cuboai/talk`** is registered once per Home Assistant run (`async_setup_talk`); 2026.7 cannot
  unregister a websocket command. Because of that it looks the camera up on every call
  (`_find_camera`) and captures nothing at setup.
  - Schema: `device_id`, `sample_rate` ∈ 8000…48000, `max_secs` 5…120 (default 120), `dry_run`.
- **Permission:** the talk plays through the camera's speaker, so it needs `POLICY_CONTROL` on the
  Speaker media player, the same right as playing a song. Admin is not required.
- **One talker per speaker:** `SpeakerArbiter` is shared with the Speaker media player.
  - The talk refuses (`speaker_busy`) while a song, TTS or lullaby plays, rather than stopping it:
    no hidden side effects.
  - While a talk runs, the media player refuses new audio and drops its queue.
  - Tapping stop and then talk again waits up to 5 s for the previous child to finish sending
    SPEAKERSTOP, instead of refusing.
- **Relay only:** `TalkSession.on_binary` writes the PCM to the child's stdin.
  - More than 32 KB pending means frames are dropped. A frame over 16 KB is refused.
  - A 1-byte frame is the end.
- **Events to the card:** `started {handler_id, dry_run, max_secs}`, `live`, `status` (rate-limited
  to 5 s: Home Assistant disconnects a client whose outbound queue piles up), `error {code,
  message}`, `ended {reason, sent, speech, camera}`.
  - The client only ever sees fixed text. The child's raw lines go to the log.
- **Credentials** go to the child in its environment only (`build_backchannel_env`), never on the
  command line (`talk_argv`).

### Every way a talk ends

This is a baby monitor, so a talk must never outlive the person talking.

| `ended.reason` / error | When |
|---|---|
| `client_end` | The card's 1-byte end frame (stop tap) |
| `unsubscribed` | The client unsubscribed, or the websocket closed (app backgrounded, network change) |
| `idle` | No audio frame for 6 s (the card streams its silence too, so this means the talker is gone) |
| `max_duration` | 120 s. The child's own cap is 125 s, only as a backstop |
| `camera_timeout` | The camera did not start pulling audio within 35 s |
| `camera_unreachable` / `camera_refused` / `camera_lost` | The child's errors: no session, no talk login within 10 s of SPEAKERSTART, or nothing from the camera for 5 s |
| `child_exit` / `internal` | The child exited by itself, or something broke |
| `unloaded` / `shutdown` | The config entry unloads (`async_stop_for_entry` waits for the child), or Home Assistant stops |

The child is stopped by closing its stdin (4 s grace), then SIGTERM, which runs the same `finally`
so SPEAKERSTOP still goes out (3 s grace), then SIGKILL. If Home Assistant itself dies, the child
sees EOF too.

## The child (`tutk/cuboai_stream_backchannel.py --live`)

```
--live --in-codec pcm_s16le --in-rate 16000 --max-secs 125 [--dry-run]
```

- **A separate process** because the camera session also receives the full live stream and decodes
  every packet in pure Python: 5–7 % of a core with the GIL held, which must not run inside Home
  Assistant. The process boundary also gives a clean, forced stop.
- **Protocol:** `_Proto` keeps the *original* stdout for `@@TALK READY | STATUS | ERROR | END` lines
  and points fd 1 at stderr, so a stray print from anywhere can't corrupt it. `END` is always sent,
  and always last.
- **Feed:** `_LiveFeed` reads 40 ms at a time and runs `_LiveAacEncoder` (in `cuboai_pure.py`).
  - s16 samples split across two reads are joined; a split sample decoded wrong is loud noise.
  - Up to 8 frames are queued, and the oldest is dropped beyond that, so the delay never grows.
  - The queue keeps a jitter floor of 2 frames.
  - Speech said during the handshake is trimmed at the first pull; it is stale by then.
- **Engine:** `send_audio_file(live_source=…)` runs **one** talk session for the whole feed.
  - Frames go out on the camera's 64 ms grid, with a precomputed silent frame whenever no speech is
    queued, so the camera never underruns.
  - `grant_timeout`, `liveness_timeout` and `tail_frames` (0.5 s of silence before SPEAKERSTOP, so the
    last word isn't cut) default to off, so songs and TTS play exactly as before.
- **AAC format:** stereo AAC-LC at 16 kHz in ADTS, byte-compatible with what `_aac_units` produces
  for TTS. That is the format the camera is proven to accept.
- **`--dry-run`:** never imports the camera stack and never opens a socket. A 1 s simulated handshake,
  then the same pacing against an ADTS checker. `END` reports `camera=0`.

The same script still has its other two modes: a file or URL (songs, TTS), and go2rtc's live
backchannel on stdin (A-law 8 kHz). The go2rtc mode is inert on a stock install: the stream offers
`#audio=pcma` with no clock rate, which go2rtc 1.9.14 never matches to a browser's `PCMA/8000`.

## Why it is built this way (what was tried first)

1. **The mic inside the video's WebRTC connection** (WebRTC Camera's `media: video,audio,microphone`).
   - Every mic tap reconfigured the player, which meant a reconnect, a black picture and the speaker
     state reset.
   - On an iPhone the player's dual mode (`webrtc,mse`) closes the peer connection when MSE scores
     higher, and the mic went with it.
2. **A separate audio-only WebRTC connection** to go2rtc's `cuboai_speaker_<id>` stream (through
   `auth/sign_path` and `/api/webrtc/ws`).
   - It worked on the LAN.
   - Away from home over an HTTP tunnel (Cloudflare, Home Assistant Cloud), WebRTC media has no route
     to go2rtc's media port without a VPN, a port forward or TURN.
   - Two more problems surfaced along the way: the `pcma` clock-rate mismatch above, and a live mode
     that opened one talk session per second of audio (about 6 s of warmup and handshake each, so
     speech arrived in fragments).
3. **Now: Home Assistant's own websocket**, the pattern Assist uses for voice
   (`async_register_binary_handler`).
   - It works wherever the dashboard works: at home, over a tunnel or over Home Assistant Cloud.
   - It needs no port, no VPN and no WebRTC.
   - The cost is a little latency (TCP and a child process), which is fine for speech.
4. **Muting the room while talking** was first always on, restored when the talk ended. On an iPhone
   the restore ran outside a tap and could leave the sound in the opposite state. It is now the
   opt-in `talk_mutes_speaker`, restored inside the stop tap.

## Testing

**Automated (no camera, no sound):**

| Test | What it covers |
|---|---|
| `test_mic_capture.py` | The card's mic path run in Node: resampler, chunker, back-pressure, dry-run flag, error texts, the tap-to-stop flow and the talk-mute option against a fake browser and connection |
| `test_mic_button.py` | Source rules of the card: what must come before what, and what must never appear (the mic never rides the video connection) |
| `test_mic_contract.py` | The three parts against each other: the child's flags as Home Assistant writes them, every code one side sends, the card's limits against the server's, and one whole dry-run talk through all three |
| `test_talk_ws.py` | `talk.py`: permissions, arbiter, refusals, every stop reason, timers, unload/shutdown, back-pressure |
| `test_backchannel_cli.py` | The child's CLI and `@@TALK` protocol, dry run, stdout isolation, exit codes |
| `test_live_encoder.py`, `test_live_feed.py` | PCM → ADTS (split samples, silence), feed queue, jitter floor, stale trim |
| `test_talk_engine_live.py` | `send_audio_file(live_source=…)` against a fake camera on localhost UDP: grant/liveness timeouts, tail frames |

Every test names the mutation it kills. Mutate a guard and the named test must fail.

**By hand, without a sound in the room:** add `talk_dry_run: true` to a card's YAML and tap the mic.
Everything runs except the camera. The notice says *TEST MODE*, and `ended` reports `camera: 0`.

**A real talk:**
- Test with someone in the room, or with the camera away from anyone sleeping. SPEAKERSTART is
  audible.
- Check at home and away (mobile data through the tunnel).
- Check each stop path: tap, background the app, lock the phone, and wait out the 2 minutes.

## Known limits

- It needs https: a browser gives the microphone only to a secure page.
- One talker per camera, and no talking while the speaker plays.
- A talk is at most 2 minutes. Tap again to continue.
- Opening the camera's speaker takes a few seconds (the amber state). Speech before `live` is not
  heard.
