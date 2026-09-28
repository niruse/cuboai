#!/usr/bin/env python3
"""
cuboai_stream_backchannel.py — Two-way audio backchannel for CuboAI cameras.

Three modes:
  1. File/URL mode (TTS): called with a URL or file path as sys.argv[1].
     Downloads the file and sends it to the camera speaker in one shot.

  2. Live stdin mode (go2rtc backchannel): called with no arguments by go2rtc.
     go2rtc writes PCMA (G.711 A-law, 8kHz, mono) to our stdin continuously.
     From its first byte (not before: the camera is not touched until audio
     arrives) it is encoded to AAC and streamed over ONE talk session until
     stdin closes (go2rtc sends SIGTERM) or the engine's guards end it.

  3. Websocket microphone mode (Home Assistant's `cuboai/talk` command):
         --live --in-codec pcm_s16le --in-rate 16000 --max-secs N [--dry-run]
     stdin is raw mono PCM at --in-rate, and EOF is the end of the talk.
     Progress goes back to Home Assistant as `@@TALK ...` lines on the
     ORIGINAL stdout (see _Proto for the protocol); every other print lands on
     stderr. --dry-run runs the whole path except the camera: the camera
     stack is never imported, no socket is opened, nothing plays.

Credentials come from the environment only (CUBOAI_UID, CUBOAI_ACCOUNT,
CUBOAI_PASSWORD, CUBOAI_CAMERA_IP), never from the command line.
"""
import os
import sys
import time
import signal
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# The camera stack (cuboai_session -> cuboai_transport_py) is imported inside the handlers that
# talk to a camera, never at module level: --dry-run must not load it (END reports camera=0).

GRANT_TIMEOUT = 10       # s after SPEAKERSTART with no talk-login from the camera -> camera_refused
LIVENESS_TIMEOUT = 5     # s with no packet at all from the camera after the grant -> camera_lost
TAIL_FRAMES = 8          # ~0.5 s of silence after the last word, so SPEAKERSTOP does not cut it off
STATUS_EVERY = 5.0       # s between STATUS lines (Home Assistant forwards them to the browser)
DRY_HANDSHAKE_SECS = 1.0  # --dry-run: stands in for connect + warmup + talk-login
GO2RTC_MAX_SECS = 125    # go2rtc's live mode: the same backstop as the websocket mic (HA's 120 s + 5)


def _camera_env():
    """(uid, account, password, camera_ip) from the environment."""
    return tuple(os.environ.get(k, '').strip('"') for k in
                 ('CUBOAI_UID', 'CUBOAI_ACCOUNT', 'CUBOAI_PASSWORD', 'CUBOAI_CAMERA_IP'))


def _stdin_raw():
    """stdin without Python's buffer: the feed thread can still be blocked in a read when SIGTERM
    ends the process, and interpreter shutdown must never wait on (or abort over) the buffer lock
    that thread holds. The raw file has no lock; its read(n) returns what one read() gives."""
    stdin = sys.stdin.buffer
    return getattr(stdin, "raw", stdin)


def _send_file(sess, audio_path, audio_format=None, audio_options=None):
    """Send a single audio file/chunk to the camera speaker."""
    sess.send_audio_file(audio_path, format=audio_format, options=audio_options)


def _handle_file_or_url(media_id, uid, account, password, camera_ip):
    """TTS / file mode: download if needed, send once, exit."""
    import urllib.request

    from cuboai_session import get_session

    audio_path = media_id
    audio_format = None
    audio_options = None

    if media_id.startswith(("http://", "https://")):
        if "googlevideo.com" in media_id:
            # YouTube streams: pass directly to PyAV for live streaming with a valid User-Agent
            print("DEBUG: YouTube stream detected. Streaming directly via PyAV...", file=sys.stderr)
            audio_path = media_id
            audio_options = {'user_agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36'}
        else:
            # TTS or small files: download to a temp file
            print("DEBUG: Downloading HTTP URL...", file=sys.stderr)
            temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
            temp_file.close()
            import urllib.request
            req = urllib.request.Request(media_id, headers={'User-Agent': 'Mozilla/5.0'})
            with urllib.request.urlopen(req) as response, open(temp_file.name, 'wb') as out_file:
                out_file.write(response.read())
            audio_path = temp_file.name
            print(f"DEBUG: Downloaded to {audio_path}", file=sys.stderr)

    with get_session(uid, account, password,
                     camera_ip=camera_ip if camera_ip else None,
                     defer_stream_start=True, defer_video_start_late=True,
                     auto_discover_lib=False) as sess:

        if hasattr(sess, '_inner') and hasattr(sess._inner, '_send_video_start_mid'):
            print("DEBUG: Sending AUDIOSTART IOCTL to wake camera...", file=sys.stderr)
            sess._inner._send_video_start_mid()
            time.sleep(0.5)

        _send_file(sess, audio_path, audio_format, audio_options)

    # Only remove files we downloaded ourselves (never a caller-supplied local path)
    if audio_path != media_id and os.path.exists(audio_path):
        os.remove(audio_path)


class _LiveFeed:
    """A microphone on stdin, encoded to AAC as it arrives, for ONE talk session.

    A reader thread turns the stream into ADTS frames; the talk loop takes one frame per 64 ms
    tick (`next_unit`), or the silent frame when nothing is ready. Built for latency, not for
    completeness:
      - the queue is short (`max_queued`, 8 frames ~ 0.5 s): when the camera falls behind the
        microphone the OLDEST frames are dropped, so the delay can never grow;
      - the FIRST pull comes when the camera has just started listening, 3-4 s after the tap:
        only the newest `prebuffer` frames are kept, what was said during the handshake is stale;
      - after running dry it waits until `prebuffer` frames (128 ms) are queued again before the
        next one goes out — a jitter floor for the bursts a websocket over TCP / 5G arrives in,
        instead of alternating word and silence frame by frame. Once `done`, the rest goes out.
    `on_first_pull` runs once, on that first pull: the "camera is listening" signal.
    Counters, read from other threads for reporting only: received (bytes in), frames (speech
    frames encoded), dropped (speech frames discarded), error (why reading stopped, if not EOF)."""

    READ_SECS = 0.04         # 40 ms of input per read: low latency, few syscalls

    def __init__(self, stream, encoder=None, max_queued=8, prebuffer=2, on_first_pull=None):
        import collections

        if encoder is None:
            from cuboai_pure import _LiveAacEncoder
            encoder = _LiveAacEncoder()                  # go2rtc's A-law, 8 kHz
        self._enc = encoder
        self.silent_unit = encoder.silent_unit
        self.read_bytes = max(1, int(encoder.bytes_per_sec * self.READ_SECS))
        self.max_queued = max_queued
        self.prebuffer = min(prebuffer, max_queued)
        self._on_first_pull = on_first_pull
        self._queue = collections.deque()
        self._lock = threading.Lock()
        self._pulled = False
        self._starved = True                             # the first frame also waits for `prebuffer`
        self.done = False
        self.error = None
        self.received = 0
        self.frames = 0
        self.dropped = 0
        self._stream = stream
        self._thread = threading.Thread(target=self._read, name="cuboai-talk-feed", daemon=True)
        self._thread.start()

    def _read(self):
        read = getattr(self._stream, "read1", None) or self._stream.read
        try:
            while True:
                data = read(self.read_bytes)
                if not data:
                    break
                self.received += len(data)
                for unit in self._enc.feed(data):
                    with self._lock:
                        if len(self._queue) >= self.max_queued:
                            self._queue.popleft()        # the oldest goes: the delay never grows
                            self.dropped += 1
                        self._queue.append(unit)
                        self.frames += 1
        except Exception as e:  # noqa: BLE001 - reported, then the talk session ends cleanly
            self.error = e
            print(f"DEBUG: talk feed error: {e!r}", file=sys.stderr, flush=True)
        finally:
            self.done = True                             # always AFTER the last frame is queued

    def next_unit(self):
        """The next speech frame, or None: send silence (and if `done` was already set before
        this call, the feed is over)."""
        first = False
        with self._lock:
            if not self._pulled:
                self._pulled = first = True
                while len(self._queue) > self.prebuffer:
                    self._queue.popleft()                # said during the handshake: stale by now
                    self.dropped += 1
            if self._starved and len(self._queue) < self.prebuffer and not self.done:
                unit = None                              # refilling the jitter floor
            elif self._queue:
                self._starved = False
                unit = self._queue.popleft()
            else:
                self._starved = True
                unit = None
        if first and self._on_first_pull is not None:
            self._on_first_pull()
        return unit


class _Prepended:
    """A raw stream that gives back `head` (bytes already read from it) before reading on."""

    def __init__(self, head, stream):
        self._head = head
        self._stream = stream

    def read(self, n):
        if self._head:
            out, self._head = self._head[:n], self._head[n:]
            return out
        return self._stream.read(n)


def _handle_live_stdin(uid, account, password, camera_ip):
    """go2rtc backchannel mode: one talk session, fed continuously from go2rtc's stdin.

    go2rtc writes G.711 A-law (8 kHz mono) to our stdin while a WebRTC client's microphone is on.
    It used to be cut into 1-second temp files, each sent with its own talk session — ~6 s of
    warmup and handshake per second of speech, so only fragments reached the speaker.
    (Inert on a stock install: the go2rtc stream offers `#audio=pcma`, which no browser matches.)

    Nothing reaches the camera before the first audio byte: go2rtc can start this producer with
    no microphone behind it, and SPEAKERSTART clicks in the nursery. The talk then carries the
    engine's guards (grant, liveness, a hard cap) — it runs in go2rtc's process, where Home
    Assistant's talk timers and speaker arbiter cannot see it."""
    from cuboai_session import get_session

    stdin = _stdin_raw()
    first = stdin.read(320)                              # blocks until audio (40 ms of A-law) or EOF
    if not first:
        print("DEBUG: Live stdin mode — stdin closed before any audio, nothing sent.",
              file=sys.stderr, flush=True)
        return

    print("DEBUG: Live stdin mode — one talk session, streaming PCMA from go2rtc...",
          file=sys.stderr, flush=True)

    with get_session(uid, account, password,
                     camera_ip=camera_ip if camera_ip else None,
                     defer_stream_start=True, defer_video_start_late=True,
                     auto_discover_lib=False) as sess:

        if hasattr(sess, '_inner') and hasattr(sess._inner, '_send_video_start_mid'):
            print("DEBUG: Sending AUDIOSTART IOCTL to wake camera...", file=sys.stderr, flush=True)
            sess._inner._send_video_start_mid()
            time.sleep(0.5)

        feed = _LiveFeed(_Prepended(first, stdin))
        last = [0.0]

        def status(st):
            now = time.time()
            if now - last[0] >= 5:
                last[0] = now
                print(f"DEBUG: talk: {st['sent']} frames sent, {st['delivered']} delivered, "
                      f"{feed.received} mic bytes in, {feed.frames} speech frames",
                      file=sys.stderr, flush=True)

        sent = sess.send_audio_file("live", live_source=feed, max_secs=GO2RTC_MAX_SECS,
                                    grant_timeout=GRANT_TIMEOUT, liveness_timeout=LIVENESS_TIMEOUT,
                                    on_status=status)

    print(f"DEBUG: Live talk finished: {sent} frames sent, {feed.received} mic bytes in.",
          file=sys.stderr, flush=True)


# ── websocket microphone (--live) ────────────────────────────────────────────────

class _Proto:
    """The `@@TALK` progress channel to Home Assistant: the ORIGINAL stdout, kept private.

    `claim_stdout()` duplicates fd 1 for it, then points fd 1 (and sys.stdout) at stderr, so a
    stray print anywhere — this script, the camera stack's traces, a C library — lands in the
    debug log instead of corrupting the protocol. Line-buffered. One lock: READY and STATUS come
    from the talk loop, END from the main thread's `finally`.

      @@TALK READY                   once, when the camera starts pulling audio (the first
                                     next_unit() after the grant; --dry-run: after the
                                     simulated 1 s handshake). Speech from now on is heard.
      @@TALK STATUS sent=N speech=N delivered=N rx=BYTES dropped=N     at most every 5 s
      @@TALK ERROR <code>            camera_unreachable | camera_refused | camera_lost | internal
      @@TALK END sent=N speech=N rx=BYTES dropped=N camera=0|1         always, and always last

    sent = frames sent to the camera (speech and silence); speech = speech frames encoded from
    the microphone; delivered = the camera's own decoder count; rx = microphone bytes read;
    dropped = speech frames discarded (queue overflow, stale audio trimmed at the start);
    camera = 1 when the camera stack was loaded (0 on --dry-run)."""

    def __init__(self, stream):
        self._stream = stream
        self._lock = threading.Lock()

    @classmethod
    def claim_stdout(cls):
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        stream = os.fdopen(os.dup(1), "w", buffering=1, encoding="ascii", newline="\n")
        os.dup2(2, 1)
        sys.stdout = sys.stderr
        return cls(stream)

    def emit(self, line):
        with self._lock:
            try:
                self._stream.write(f"@@TALK {line}\n")
                self._stream.flush()
            except (OSError, ValueError):
                pass                                     # Home Assistant is gone: nobody to tell


class _LiveTalk:
    """One --live run: the feed, its counters, and the protocol lines about them."""

    def __init__(self, proto):
        self.proto = proto
        self.feed = None
        self.sent = 0
        self.delivered = 0
        self.error = None
        self._last_status = None

    def start_feed(self, args):
        """Start reading stdin — BEFORE connecting, so the pipe drains (and is encoded) during the
        handshake instead of filling up behind Home Assistant's writes."""
        from cuboai_pure import _LiveAacEncoder
        enc = _LiveAacEncoder(in_rate=args.in_rate, in_codec=args.in_codec)
        self.feed = _LiveFeed(_stdin_raw(), enc, on_first_pull=lambda: self.proto.emit("READY"))

    def status(self, st):
        """send_audio_file's on_status (every 16 frames) -> a STATUS line at most every 5 s."""
        self.sent, self.delivered = st["sent"], st["delivered"]
        now = time.monotonic()
        if self._last_status is None or now - self._last_status >= STATUS_EVERY:
            self._last_status = now
            f = self.feed
            self.proto.emit(f"STATUS sent={self.sent} speech={f.frames} delivered={self.delivered} "
                            f"rx={f.received} dropped={f.dropped}")

    def fail(self, code, exc=None):
        if exc is not None:
            print(f"DEBUG: talk ended with {code}: {exc!r}", file=sys.stderr, flush=True)
        if self.error is None:                           # the first failure is the one that counts
            self.error = code
            self.proto.emit(f"ERROR {code}")

    def end(self):
        f = self.feed
        speech, rx, dropped = (f.frames, f.received, f.dropped) if f is not None else (0, 0, 0)
        camera = int("cuboai_session" in sys.modules)
        self.proto.emit(f"END sent={self.sent} speech={speech} rx={rx} dropped={dropped} camera={camera}")


def _check_adts(unit):
    """Raise ValueError unless `unit` is one whole ADTS frame in the format the camera takes:
    AAC-LC, sampling index 8 (16 kHz), channel config 2. --dry-run's stand-in for the camera."""
    if len(unit) < 8 or unit[0] != 0xFF or (unit[1] & 0xF6) != 0xF0:
        raise ValueError("not an ADTS frame (sync word)")
    if unit[2] >> 6 != 1:
        raise ValueError("ADTS profile is not AAC-LC")
    if (unit[2] >> 2) & 0x0F != 8:
        raise ValueError("ADTS sampling index is not 8 (16 kHz)")
    if ((unit[2] & 0x01) << 2) | (unit[3] >> 6) != 2:
        raise ValueError("ADTS channel config is not 2")
    if ((unit[3] & 0x03) << 11) | (unit[4] << 3) | (unit[5] >> 5) != len(unit):
        raise ValueError("ADTS frame length does not match")


class _DryTalk:
    """--dry-run: send_audio_file's pacing and live_source handling, with no camera at all.

    Waits DRY_HANDSHAKE_SECS as if connecting (a feed that ends first ends the talk, as in the
    engine), then pulls on the same ABSOLUTE 64 ms grid, silence when nothing is ready, the same
    tail after the feed ends, and checks every speech frame's ADTS header on the camera's behalf.
    Never opens a socket and never imports the camera stack."""

    def __init__(self, feed, max_secs, rate=16000, tail_frames=TAIL_FRAMES, on_status=None):
        self.feed = feed
        self.max_secs = max_secs
        self.rate = rate
        self.tail_frames = tail_frames
        self.on_status = on_status
        self.sent = 0

    def run(self):
        t0 = time.time()
        _check_adts(self.feed.silent_unit)
        while time.time() - t0 < DRY_HANDSHAKE_SECS:
            if self.feed.done or time.time() - t0 >= self.max_secs:
                return self.sent
            time.sleep(0.02)
        period = 1024.0 / self.rate
        next_at = time.time()
        tail_left = self.tail_frames
        while True:
            now = time.time()
            if now - t0 >= self.max_secs:
                break
            if now < next_at:
                time.sleep(next_at - now)
                continue
            ended = self.feed.done                       # before next_unit(), as in the engine
            unit = self.feed.next_unit()
            if unit is None:
                if ended:
                    if tail_left <= 0:
                        break
                    tail_left -= 1
                unit = self.feed.silent_unit
            else:
                _check_adts(unit)
            self.sent += 1
            next_at += period
            if now - next_at > 8 * period:               # fell far behind -> resync, don't burst
                next_at = now + period
            if self.on_status and self.sent % 16 == 0:
                self.on_status(dict(sent=self.sent, delivered=0, decoding=False, resends=0))
        return self.sent


def _handle_dry_live(args, talk):
    """--live --dry-run: stdin -> encoder -> feed -> _DryTalk. Returns the exit code."""
    print("DEBUG: websocket talk DRY RUN - no camera, nothing plays", file=sys.stderr, flush=True)
    talk.start_feed(args)
    talk.sent = _DryTalk(talk.feed, args.max_secs, on_status=talk.status).run()
    if talk.feed.error is not None:
        talk.fail("internal")
        return 1
    return 0


def _handle_ws_live(args, talk):
    """--live: stdin -> ONE talk session on the camera. Returns the exit code."""
    uid, account, password, camera_ip = _camera_env()
    if not all([uid, account, password]):
        print("ERROR: Missing CUBOAI_UID/ACCOUNT/PASSWORD env vars.", file=sys.stderr, flush=True)
        talk.fail("internal")
        return 1
    talk.start_feed(args)
    from cuboai_pure import TalkTimeout

    phase = "connect"
    try:
        from cuboai_session import get_session
        with get_session(uid, account, password,
                         camera_ip=camera_ip if camera_ip else None,
                         defer_stream_start=True, defer_video_start_late=True,
                         auto_discover_lib=False) as sess:
            phase = "talk"
            if hasattr(sess, '_inner') and hasattr(sess._inner, '_send_video_start_mid'):
                sess._inner._send_video_start_mid()
                time.sleep(0.5)
            talk.sent = sess.send_audio_file("live", live_source=talk.feed, max_secs=args.max_secs,
                                             grant_timeout=GRANT_TIMEOUT,
                                             liveness_timeout=LIVENESS_TIMEOUT,
                                             tail_frames=TAIL_FRAMES, on_status=talk.status)
    except TalkTimeout as e:
        talk.sent = max(talk.sent, e.sent)
        talk.fail("camera_refused" if e.reason == "grant" else "camera_lost", e)
        return 1
    except (RuntimeError, OSError) as e:
        if phase == "connect" or "handshake failed" in str(e):
            talk.fail("camera_unreachable", e)
        elif isinstance(e, OSError):
            talk.fail("camera_lost", e)
        else:
            talk.fail("internal", e)
        return 1
    if talk.feed.error is not None:
        talk.fail("internal")
        return 1
    return 0


def _parse_live_args(argv):
    import argparse

    p = argparse.ArgumentParser(
        prog="cuboai_stream_backchannel.py",
        description="Talk through the camera speaker from a microphone on stdin (raw mono PCM). "
                    "Credentials come from CUBOAI_* environment variables only.")
    p.add_argument("--live", action="store_true", required=True,
                   help="websocket microphone mode (stdin EOF ends the talk)")
    p.add_argument("--in-codec", choices=("pcm_s16le", "pcm_alaw"), default="pcm_s16le")
    p.add_argument("--in-rate", type=int, default=16000, help="input sample rate, 8000-48000 Hz")
    p.add_argument("--max-secs", type=float, default=120.0, help="hard cap on the talk session")
    p.add_argument("--dry-run", action="store_true",
                   help="everything except the camera: no camera stack, no socket, no sound")
    args = p.parse_args(argv)
    if not 8000 <= args.in_rate <= 48000:
        p.error("--in-rate must be 8000-48000")
    if not args.max_secs > 0:
        p.error("--max-secs must be positive")
    return args


def _live_main(argv):
    """--live: the Home Assistant websocket microphone. Returns the exit code; END is always the
    last protocol line (SIGTERM -> sys.exit unwinds through the talk's SPEAKERSTOP, then here)."""
    proto = _Proto.claim_stdout()
    talk = _LiveTalk(proto)
    try:
        try:
            args = _parse_live_args(argv)
        except SystemExit:                               # argparse has said why, on stderr
            talk.fail("internal")
            return 2
        if args.dry_run:
            return _handle_dry_live(args, talk)
        return _handle_ws_live(args, talk)
    except Exception as e:  # noqa: BLE001 - reported to Home Assistant, then END
        talk.fail("internal", e)
        return 1
    finally:
        talk.end()


def main() -> None:
    # Graceful shutdown on SIGTERM (go2rtc / Home Assistant send this to stop us): sys.exit unwinds
    # through send_audio_file's `finally`, which sends SPEAKERSTOP.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if len(sys.argv) >= 2 and sys.argv[1].startswith("--"):
        sys.exit(_live_main(sys.argv[1:]))               # a media URL never starts with "--"

    media_id = "pipe:0"
    if len(sys.argv) >= 2:
        media_id = sys.argv[1].strip("'").strip('"')

    uid, account, password, camera_ip = _camera_env()

    if not all([uid, account, password]):
        print("ERROR: Missing CUBOAI_UID/ACCOUNT/PASSWORD env vars.", file=sys.stderr)
        sys.exit(1)

    try:
        print(f"DEBUG: media_id is {repr(media_id)}", file=sys.stderr, flush=True)

        if media_id == "pipe:0":
            _handle_live_stdin(uid, account, password, camera_ip)
        else:
            _handle_file_or_url(media_id, uid, account, password, camera_ip)

    except SystemExit:
        raise
    except Exception as e:
        print(f"Backchannel error: {e}", file=sys.stderr, flush=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
