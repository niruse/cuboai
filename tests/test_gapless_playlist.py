"""Speaker songs play back to back in ONE talk session (`--playlist`), with no silence between them.

Each song used to be its own talk session: yt-dlp, a new child, a transcode of the WHOLE song
before the first frame, connect + 2.5 s warmup + the talk grant, then 3.5 s of silence padding
before SPEAKERSTOP. Every two songs had seconds of silence between them (YouTube and Spotify alike).

Now:
- `_TrackAacEncoder` streams a track's frames while it decodes, carries its encoder across tracks,
  and pads nothing between them (only `finish()`, at the end of the playlist, flushes);
- `_PlaylistFeed` reads one URL per stdin line, decodes ahead, marks where each song starts
  (TRACK i), skips a song that cannot be played (SKIP i), says IDLE when it has nothing left and
  waits for another line, and is `done` only when stdin ended AND every frame was taken.

Every test names the mutation it kills.
"""

import importlib.util
import math
import os
import queue
import struct
import subprocess
import sys
import time
import wave

import pytest

av = pytest.importorskip("av")

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")
_SCRIPT = os.path.join(_TUTK, "cuboai_stream_backchannel.py")
_spec = importlib.util.spec_from_file_location("gapless_backchannel", _SCRIPT)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)
_pspec = importlib.util.spec_from_file_location("gapless_pure", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_pspec)
_pspec.loader.exec_module(cp)

FRAME_S = 1024 / 16000  # one AAC frame at 16 kHz


def _tone(path, secs, hz=440, rate=8000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(
            b"".join(
                struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate))) for i in range(int(secs * rate))
            )
        )
    return str(path)


def _is_adts(u):
    return len(u) >= 8 and u[0] == 0xFF and (u[1] & 0xF6) == 0xF0


# ── the encoder ──────────────────────────────────────────────────────────────


def test_tracks_carry_no_silence_padding(tmp_path):
    """2 s + 1.5 s of audio come out as ~3.5 s of frames: no padding between or after tracks.

    Kill: pad each track with silence like `_aac_units` does (55 frames per track)."""
    enc = cp._TrackAacEncoder()
    a = list(enc.track(_tone(tmp_path / "a.wav", 2.0)))
    b = list(enc.track(_tone(tmp_path / "b.wav", 1.5, 880)))
    end = list(enc.finish())
    total = len(a) + len(b) + len(end)
    assert all(_is_adts(u) for u in a + b + end)
    assert abs(total * FRAME_S - 3.5) < 4 * FRAME_S, total
    assert abs(len(a) * FRAME_S - 2.0) < 3 * FRAME_S, len(a)


def test_a_track_streams_before_it_is_fully_decoded(tmp_path):
    """The first frame is out long before the track is decoded (no whole-file transcode first).

    Kill: build the whole list before yielding (`yield from list(...)`)."""
    path = _tone(tmp_path / "long.wav", 30.0)
    seen = []

    class _Container:
        def __init__(self, inner):
            self.inner = inner

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.inner.close()

        def decode(self, *a, **k):
            for f in self.inner.decode(*a, **k):
                seen.append(1)
                yield f

    class _Av:
        def __init__(self, real):
            self.real = real

        def __getattr__(self, name):
            return getattr(self.real, name)

        def open(self, *a, **k):
            return _Container(self.real.open(*a, **k))

    enc = cp._TrackAacEncoder()
    enc._av = _Av(enc._av)
    gen = enc.track(path)
    first = next(gen)
    assert _is_adts(first)
    decoded_before_first = len(seen)
    rest = sum(1 for _ in gen)
    assert rest > 400
    assert decoded_before_first < len(seen) / 10, (decoded_before_first, len(seen))


def test_finish_flushes_the_tail_of_the_playlist(tmp_path):
    """The last partial frame and the encoder's delayed frames come out at the very end: every
    input sample is covered (2 s = 31.25 frames at 16 kHz, plus the AAC encoder's priming frame).

    Kill: drop finish()'s partial-frame padding or its encoder flush (the song's end is lost)."""
    enc = cp._TrackAacEncoder()
    n = sum(1 for _ in enc.track(_tone(tmp_path / "a.wav", 2.0)))
    total = n + sum(1 for _ in enc.finish())
    assert total >= math.ceil(2.0 * 16000 / 1024) + 1, (n, total)


# ── the feed ─────────────────────────────────────────────────────────────────


class _Lines:
    """Blocking stdin stand-in: readline() returns the next pushed line; None pushes EOF."""

    def __init__(self):
        self._q = queue.Queue()

    def push(self, line):
        self._q.put(b"" if line is None else line.encode() + b"\n")

    def readline(self):
        return self._q.get(timeout=5)


class _Enc:
    """A track named 'nX' yields X frames named <name>/<k>; 'bad' raises before any frame."""

    silent_unit = b"silence"

    def track(self, path, options=None):
        if path == "bad":
            raise ValueError("cannot open")
        n = int(path.rsplit("n", 1)[1])
        for k in range(n):
            yield f"{path}/{k}".encode()

    def finish(self):
        yield b"tail"


def _feed(**kw):
    events = []
    lines = _Lines()
    feed = bc._PlaylistFeed(lines, encoder=_Enc(), emit=events.append, open_track=lambda u: (u, None, None), **kw)
    return feed, lines, events


def _pull(feed, n, timeout=3):
    out, t0 = [], time.time()
    while len(out) < n and time.time() - t0 < timeout:
        u = feed.next_unit()
        if u is None:
            time.sleep(0.005)
        else:
            out.append(u)
    return out


def test_songs_follow_each_other_frame_by_frame():
    """Song 1's first frame is the frame right after song 0's last: no silence in between.

    Kill: queue silence frames between tracks."""
    feed, lines, events = _feed()
    lines.push("a-n3")
    lines.push("b-n2")
    lines.push(None)
    got = _pull(feed, 6)
    assert got == [b"a-n3/0", b"a-n3/1", b"a-n3/2", b"b-n2/0", b"b-n2/1", b"tail"]
    assert events == ["TRACK 0", "TRACK 1"]


def test_track_is_announced_when_its_first_frame_goes_out():
    """TRACK i comes with song i's first frame (when it is heard), not when it was decoded.

    Kill: emit TRACK from the reader thread as the song is opened."""
    feed, lines, events = _feed()
    lines.push("a-n2")
    lines.push("b-n2")
    time.sleep(0.2)  # both songs are decoded and queued by now
    assert events == []
    _pull(feed, 2)
    assert events == ["TRACK 0"]
    _pull(feed, 1)
    assert events == ["TRACK 0", "TRACK 1"]


def test_a_song_that_cannot_play_is_skipped_and_the_next_one_plays():
    """A failing song is reported (SKIP i) and the playlist goes on with its track numbers.

    Kill: end the feed (or stop reading lines) when a song fails."""
    feed, lines, events = _feed()
    lines.push("a-n1")
    lines.push("bad")
    lines.push("c-n1")
    lines.push(None)
    got = _pull(feed, 3)
    assert got == [b"a-n1/0", b"c-n1/0", b"tail"]
    assert "SKIP 1" in events and events.index("TRACK 0") < events.index("TRACK 2")


def test_idle_once_when_everything_was_sent_and_no_next_line_is_there():
    """IDLE tells Home Assistant to send the next song or end the playlist; once per wait.

    Kill: never emit IDLE (the run would never end), or emit it on every empty pull."""
    feed, lines, events = _feed()
    lines.push("a-n1")
    _pull(feed, 1)
    for _ in range(20):
        assert feed.next_unit() is None
        time.sleep(0.005)
    assert events.count("IDLE") == 1
    lines.push("b-n1")  # a song added later still plays, and a new wait gets a new IDLE
    assert _pull(feed, 1) == [b"b-n1/0"]
    for _ in range(20):
        feed.next_unit()
        time.sleep(0.005)
    assert events.count("IDLE") == 2


def test_no_idle_while_a_song_is_still_decoding():
    """An empty queue while the reader is still decoding (slow network) is not IDLE.

    Kill: emit IDLE whenever the queue is empty."""

    class _Slow(_Enc):
        def track(self, path, options=None):
            time.sleep(0.3)
            yield b"late"

    events = []
    lines = _Lines()
    feed = bc._PlaylistFeed(lines, encoder=_Slow(), emit=events.append, open_track=lambda u: (u, None, None))
    lines.push("x-n1")
    time.sleep(0.05)
    for _ in range(10):
        feed.next_unit()
        time.sleep(0.01)
    assert "IDLE" not in events
    assert _pull(feed, 1) == [b"late"]


def test_done_only_after_stdin_ended_and_every_frame_was_taken():
    """The talk loop ends on `done`: true while songs are still queued would cut them off
    (a short playlist decoded before the camera's grant would never play at all).

    Kill: `done` = stdin ended (ignoring the queue)."""
    feed, lines, events = _feed()
    lines.push("a-n3")
    lines.push(None)
    time.sleep(0.2)  # everything is decoded and queued
    assert not feed.done
    _pull(feed, 4)
    assert feed.done


def test_the_decoder_stays_a_bounded_distance_ahead():
    """At most `ahead` frames wait in memory: a long song never decodes into RAM all at once.

    Kill: drop the bound in _put."""
    feed, lines, events = _feed(ahead=5)
    lines.push("a-n100")
    time.sleep(0.3)
    queued = sum(1 for x in feed._queue if not isinstance(x, tuple))
    assert queued <= 5
    assert len(_pull(feed, 100)) == 100


# ── the child, end to end (--playlist --dry-run: no camera) ─────────────────


def test_dry_run_plays_the_songs_back_to_back(tmp_path):
    """The real child: song 2 starts right after song 0 (2.0 s), the bad one is skipped.

    Kill: a talk session per song, padding between songs, or the feed's `done` too early."""
    a = _tone(tmp_path / "a.wav", 2.0)
    b = _tone(tmp_path / "b.wav", 1.0, 880)
    stdin = f"{a}\n{tmp_path / 'missing.wav'}\n{b}\n"
    t0 = time.time()
    proc = subprocess.Popen(
        [sys.executable, "-I", _SCRIPT, "--playlist", "--dry-run"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    proc.stdin.write(stdin.encode())
    proc.stdin.close()
    at = {}
    for raw in proc.stdout:
        line = raw.decode().strip()
        at.setdefault(line.split(" sent=")[0], time.time() - t0)
    proc.wait(timeout=30)
    assert "@@TALK SKIP 1" in at
    assert "@@TALK END" in at
    gap = at["@@TALK TRACK 2"] - at["@@TALK TRACK 0"]
    assert 1.8 < gap < 2.4, gap
