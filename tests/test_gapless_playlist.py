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


def test_a_track_can_start_part_way_in(tmp_path):
    """`start` plays the song from that point: 4 s from 3 s in is ~1 s of frames.

    Kill: ignore `start` (the whole song again), or skip the trim after the container seek."""
    enc = cp._TrackAacEncoder()
    n = sum(1 for _ in enc.track(_tone(tmp_path / "a.wav", 4.0), start=3.0))
    assert abs(n * FRAME_S - 1.0) < 4 * FRAME_S, n


class _SeekSpyAv:
    """PyAV with a spy container: counts decoded frames; `land_at_zero` makes seek() a no-op
    (as a container whose index only reaches the start)."""

    def __init__(self, real, land_at_zero=False):
        self.real = real
        self.land_at_zero = land_at_zero
        self.decoded = 0

    def __getattr__(self, name):
        return getattr(self.real, name)

    def open(self, *a, **k):
        spy, inner = self, self.real.open(*a, **k)

        class _C:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                inner.close()

            def seek(self, *sa, **sk):
                if not spy.land_at_zero:
                    inner.seek(*sa, **sk)

            def decode(self, *da, **dk):
                for f in inner.decode(*da, **dk):
                    spy.decoded += 1
                    yield f

        return _C()


def test_a_seek_does_not_decode_the_song_from_its_start(tmp_path):
    """The container seeks: frames before the point are not decoded (a seek on a long song
    is quick). Kill: drop the container seek (decode everything, then trim)."""
    path = _tone(tmp_path / "a.wav", 20.0)
    full = _SeekSpyAv(cp._TrackAacEncoder()._av)
    enc = cp._TrackAacEncoder()
    enc._av = full
    list(enc.track(path))
    sought = _SeekSpyAv(full.real)
    enc2 = cp._TrackAacEncoder()
    enc2._av = sought
    list(enc2.track(path, start=18.0))
    assert sought.decoded < full.decoded / 4, (sought.decoded, full.decoded)


def test_frames_before_the_seek_point_are_dropped_when_the_seek_lands_early(tmp_path):
    """A container seek lands at or BEFORE the point (a keyframe); what comes before it must
    not play. Kill: drop the trim after the seek."""
    enc = cp._TrackAacEncoder()
    enc._av = _SeekSpyAv(enc._av, land_at_zero=True)
    n = sum(1 for _ in enc.track(_tone(tmp_path / "a.wav", 4.0), start=3.0))
    assert abs(n * FRAME_S - 1.0) < 4 * FRAME_S, n


def test_after_the_final_flush_a_reset_encoder_encodes_again(tmp_path):
    """A SEEK on the last song arrives after finish() flushed the encoder: reset() must give a
    working encoder back.

    Kill: reset() that only clears the FIFO (the flushed encoder then yields nothing)."""
    enc = cp._TrackAacEncoder()
    path = _tone(tmp_path / "a.wav", 2.0)
    list(enc.track(path))
    list(enc.finish())
    enc.reset()
    again = list(enc.track(path, start=1.0))
    assert len(again) > 10 and all(_is_adts(u) for u in again)


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

    def __init__(self):
        self.resets = 0

    def reset(self):
        self.resets += 1

    def track(self, path, options=None, start=0.0):
        if path == "bad":
            raise ValueError("cannot open")
        n = int(path.rsplit("n", 1)[1])
        for k in range(int(start), n):  # `start` counts frames here
            yield f"{path}/{k}".encode()

    def finish(self):
        yield b"tail"


def _feed(**kw):
    events = []
    lines = _Lines()
    feed = bc._PlaylistFeed(lines, encoder=_Enc(), emit=events.append, open_track=lambda u: (u, None, None), **kw)
    return feed, lines, events


def _eventually(cond, timeout=2.0):
    """`done` turns true once the decoder has seen the last frame go (the engine polls it)."""
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.01)
    return False


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
        def track(self, path, options=None, start=0.0):
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
    assert _eventually(lambda: feed.done)


def test_a_decoder_failure_still_plays_what_was_queued():
    """The decoder dies (here: the final flush raises) with song frames still queued: they are
    played before the feed reports done. Kill: `done` = the decoder stopped (ignoring the queue)."""

    class _Broken(_Enc):
        def finish(self):
            raise RuntimeError("encoder gone")
            yield  # pragma: no cover

    events = []
    lines = _Lines()
    feed = bc._PlaylistFeed(lines, encoder=_Broken(), emit=events.append, open_track=lambda u: (u, None, None))
    lines.push("a-n20")
    lines.push("END")
    time.sleep(0.3)  # the song is queued and the flush has failed
    assert feed.error is not None
    assert not feed.done
    assert len(_pull(feed, 20)) == 20
    assert _eventually(lambda: feed.done)


def test_the_decoder_stays_a_bounded_distance_ahead():
    """At most `ahead` frames wait in memory: a long song never decodes into RAM all at once.

    Kill: drop the bound in _put."""
    feed, lines, events = _feed(ahead=5)
    lines.push("a-n100")
    time.sleep(0.3)
    queued = sum(1 for x in feed._queue if not isinstance(x, tuple))
    assert queued <= 5
    assert len(_pull(feed, 100)) == 100


def _pull_until(feed, want, timeout=3):
    """Pull frames (skipping silence) until `want` is seen; returns everything pulled."""
    out, t0 = [], time.time()
    while time.time() - t0 < timeout:
        u = feed.next_unit()
        if u is None:
            time.sleep(0.002)
            continue
        out.append(u)
        if u == want:
            return out
    raise AssertionError(f"{want!r} never came; got {out[-5:]!r}")


def test_seek_plays_the_current_song_from_the_new_point():
    """SEEK drops what was queued and the song goes on from the new point, in the same session;
    the next song still follows it, announced once.

    Kill: keep the queued frames on SEEK, or decode the song from its start again."""
    feed, lines, events = _feed()
    lines.push("a-n100")
    lines.push("b-n2")
    assert _pull(feed, 3) == [b"a-n100/0", b"a-n100/1", b"a-n100/2"]
    lines.push("SEEK 50")
    time.sleep(0.1)
    got = _pull(feed, 1)
    assert got == [b"a-n100/50"]
    assert "SEEKED 0 50.0" in events
    rest = _pull_until(feed, b"b-n2/1")
    assert rest[:2] == [b"a-n100/51", b"a-n100/52"] and rest[-3:] == [b"a-n100/99", b"b-n2/0", b"b-n2/1"]
    assert events.count("TRACK 0") == 1 and events.count("TRACK 1") == 1
    assert feed._enc.resets == 1


def test_seek_while_the_next_song_is_already_decoding_rewinds_the_decoder():
    """The decoder may be on the NEXT song when SEEK comes: it goes back to the song heard, and
    the next song is decoded again after it (none of it is lost or doubled).

    Kill: leave `_next` where it was (the next song would be skipped)."""
    feed, lines, events = _feed()
    lines.push("a-n3")
    lines.push("b-n40")
    time.sleep(0.2)  # a is fully queued, the decoder is into b
    assert _pull(feed, 1) == [b"a-n3/0"]
    lines.push("SEEK 1")
    time.sleep(0.1)
    got = _pull_until(feed, b"b-n40/39")
    assert got[:3] == [b"a-n3/1", b"a-n3/2", b"b-n40/0"]
    assert len(got) == 2 + 40
    assert events.count("TRACK 1") == 1


def test_a_seek_before_anything_plays_is_ignored():
    """Nothing heard yet: there is no song to move in.

    Kill: accept SEEK with no song playing (it would replay song -1)."""
    feed, lines, events = _feed()
    lines.push("SEEK 1")
    lines.push("a-n2")
    lines.push(None)
    assert _pull(feed, 3) == [b"a-n2/0", b"a-n2/1", b"tail"]
    assert not any(e.startswith("SEEKED") for e in events)


def test_end_ends_the_playlist_but_a_seek_on_the_last_song_still_works():
    """END (not EOF) ends the playlist, so stdin stays open for a SEEK on the last song; a URL
    after END is not played.

    Kill: treat END as a song, or ignore lines after END."""
    feed, lines, events = _feed()
    lines.push("a-n100")
    lines.push("END")
    lines.push("z-n5")
    assert _pull(feed, 2) == [b"a-n100/0", b"a-n100/1"]
    lines.push("SEEK 97")
    time.sleep(0.1)
    got = _pull_until(feed, b"tail")
    assert got == [b"a-n100/97", b"a-n100/98", b"a-n100/99", b"tail"]
    assert _eventually(lambda: feed.done)
    assert "IDLE" not in events


def test_a_downloaded_song_is_kept_for_seek_and_removed_once_the_next_one_plays(tmp_path):
    """The downloaded file stays while its song may be sought, then goes (no disk leak).

    Kill: delete the download as soon as it is decoded, or never."""
    files = {}

    def _open(url):
        f = tmp_path / (url + ".bin")
        f.write_bytes(b"x")
        files[url] = f
        return url, None, str(f)

    events = []
    lines = _Lines()
    feed = bc._PlaylistFeed(lines, encoder=_Enc(), emit=events.append, open_track=_open)
    lines.push("a-n3")
    lines.push("b-n200")
    _pull(feed, 1)
    time.sleep(0.2)  # a is fully decoded by now
    assert files["a-n3"].exists()
    _pull_until(feed, b"b-n200/0")
    assert not files["a-n3"].exists()
    lines.push(None)
    _pull_until(feed, b"tail")
    time.sleep(0.1)
    assert not files["b-n200"].exists()


def test_duration_is_reported_for_the_seek_bar(tmp_path):
    """DUR i <secs> as soon as a song is opened (the entity's seek bar needs its length).

    Kill: never emit DUR."""
    path = _tone(tmp_path / "a.wav", 2.0)
    events = []
    lines = _Lines()
    feed = bc._PlaylistFeed(lines, emit=events.append, open_track=lambda u: (u, None, None))
    lines.push(path)
    lines.push(None)
    _pull_until(feed, feed.silent_unit) if False else _pull(feed, 5)
    assert "DUR 0 2.0" in events


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
