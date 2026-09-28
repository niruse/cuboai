"""`_LiveFeed` sits between the microphone on stdin and the talk loop's 64 ms grid.

A reader thread encodes stdin as it arrives. The talk loop pulls one frame per
tick, and sends silence when nothing is ready. The feed is built for latency,
not completeness:
- the queue is capped at 8 frames (~0.5 s), and the OLDEST frame goes on
  overflow;
- the first pull comes 3-4 s after the tap, once the camera is listening, so
  only the newest 2 frames are kept and the handshake-time speech is dropped;
- that first pull is the READY signal, and it fires exactly once;
- after running dry, it waits until 2 frames are queued again (a 128 ms jitter
  floor for websocket bursts over TCP/5G);
- `done` is set however the reader stops, or the talk would never end.

A fake encoder turns each input byte into one named frame (f0, f1, ...), so the
tests can see which frames survive. Every test names the mutation it kills.
"""

import importlib.util
import os
import queue
import time

import pytest

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")
_spec = importlib.util.spec_from_file_location(
    "live_backchannel_feed", os.path.join(_TUTK, "cuboai_stream_backchannel.py")
)
bc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bc)


class _Enc:
    """One frame per input byte, numbered in arrival order. 16 kHz s16 sizing (32000 B/s)."""

    silent_unit = b"silence"
    bytes_per_sec = 32000

    def __init__(self, fail=False):
        self.n = 0
        self.fail = fail

    def feed(self, data):
        if self.fail:
            raise ValueError("bad input")
        out = [b"f%d" % (self.n + i) for i in range(len(data))]
        self.n += len(data)
        return out


class _Pipe:
    """A blocking stdin stand-in: each read() returns the next chunk pushed; b'' is EOF."""

    def __init__(self):
        self._q = queue.Queue()
        self.sizes = []

    def push(self, n):
        self._q.put(b"\x00" * n)

    def close(self):
        self._q.put(b"")

    def read(self, n):
        self.sizes.append(n)
        return self._q.get()


def _until(cond, secs=2.0):
    end = time.time() + secs
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.005)
    return cond()


@pytest.fixture
def pipe():
    p = _Pipe()
    yield p
    p.close()  # lets the reader thread finish


def _feed(pipe, **kw):
    return bc._LiveFeed(pipe, kw.pop("encoder", None) or _Enc(), **kw)


def _pulls(feed, n):
    return [feed.next_unit() for _ in range(n)]


def test_reads_40_ms_of_input_at_a_time(pipe):
    """Kill: the branch's fixed 320-byte read (10 ms of 16 kHz s16: 4x the syscalls and
    wakeups) or a size not derived from the encoder."""
    feed = _feed(pipe)
    assert _until(lambda: pipe.sizes)
    assert pipe.sizes[0] == feed.read_bytes == 1280


def test_overflow_drops_the_oldest(pipe):
    """12 frames into a cap of 8: f0-f3 go, f4-f11 stay (prebuffer 8, so no start trim).
    Kill: drop the newest instead (the camera would play stale speech, the delay would grow)."""
    feed = _feed(pipe, prebuffer=8)
    pipe.push(12)
    assert _until(lambda: feed.frames == 12)
    assert feed.dropped == 4
    assert _pulls(feed, 9) == [b"f%d" % i for i in range(4, 12)] + [None]


def test_first_pull_keeps_only_the_newest_two(pipe):
    """Frames queued during the handshake are stale when the camera starts listening.
    Kill: no trim (f0 first: the start of a sentence said seconds ago)."""
    feed = _feed(pipe)
    pipe.push(6)
    assert _until(lambda: feed.frames == 6)
    assert _pulls(feed, 3) == [b"f4", b"f5", None]
    assert feed.dropped == 4


def test_ready_fires_once_on_the_first_pull(pipe):
    """Kill: firing on every pull, or before the camera pulls (frames queued is not ready)."""
    calls = []
    feed = _feed(pipe, on_first_pull=lambda: calls.append(1))
    pipe.push(3)
    assert _until(lambda: feed.frames == 3)
    time.sleep(0.05)
    assert calls == []
    _pulls(feed, 10)
    assert calls == [1]


def test_after_running_dry_it_waits_for_two_frames(pipe):
    """Kill: no prebuffer (one frame, then silence, then one frame: choppy speech on a bursty
    link). The very first frame also waits for two."""
    feed = _feed(pipe)
    pipe.push(1)
    assert _until(lambda: feed.frames == 1)
    assert feed.next_unit() is None  # the first pull: one frame is not enough yet
    pipe.push(1)
    assert _until(lambda: feed.frames == 2)
    assert _pulls(feed, 3) == [b"f0", b"f1", None]  # ran dry
    pipe.push(1)
    assert _until(lambda: feed.frames == 3)
    assert feed.next_unit() is None  # f2 alone waits
    pipe.push(1)
    assert _until(lambda: feed.frames == 4)
    assert _pulls(feed, 3) == [b"f2", b"f3", None]


def test_done_releases_the_last_frame(pipe):
    """After EOF there is nothing to wait for. Kill: the prebuffer hold ignoring `done` (the
    last word would never be sent)."""
    feed = _feed(pipe)
    pipe.push(2)
    assert _until(lambda: feed.frames == 2)
    assert _pulls(feed, 3) == [b"f0", b"f1", None]
    pipe.push(1)
    pipe.close()
    assert _until(lambda: feed.done)
    assert _pulls(feed, 2) == [b"f2", None]


def test_eof_sets_done(pipe):
    """Kill: `done` never set on EOF (the talk would run until max_secs)."""
    feed = _feed(pipe)
    pipe.push(1)
    pipe.close()
    assert _until(lambda: feed.done)
    assert feed.error is None
    assert feed.received == 1


def test_an_encoder_error_ends_the_feed_and_is_reported(pipe, capsys):
    """Kill: `done` not set in `finally` (a feed that died would leave the talk sending
    silence until max_secs), or the error swallowed (the child reports it as ERROR internal)."""
    feed = _feed(pipe, encoder=_Enc(fail=True))
    pipe.push(1)
    assert _until(lambda: feed.done)
    assert isinstance(feed.error, ValueError)
    assert "talk feed error" in capsys.readouterr().err
