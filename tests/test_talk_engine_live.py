"""`send_audio_file(live_source=...)` must end a live talk on the camera's terms too.

The websocket microphone runs one talk session per tap. Before the new
guards, the engine could hang or cut speech in four ways:
- a camera that never logs into the talk channel looped forever;
- a mic released during the handshake was ignored until the grant;
- a camera that went quiet mid-talk was never noticed;
- SPEAKERSTOP followed the last word so closely that the word was cut off.

The guards are `grant_timeout`, the done-before-the-pump exit,
`liveness_timeout` and `tail_frames`. All of them default to OFF, so a
file / TTS / song plays exactly as before.

The fake camera is a localhost UDP socket, and nothing leaves the machine. The
frame builders are patched to readable tags and `inv_transcode` to identity,
so the camera reads what the engine sent and answers with crafted plaintext:
- a talk-login after SPEAKERSTART (sub 0x00 on channel 1, >= 300 B);
- then a 0x0A every 50 ms, meaning it is listening.

Every test names the mutation it kills.
"""

import collections
import importlib.util
import os
import socket
import struct
import threading
import time

import pytest

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")
_spec = importlib.util.spec_from_file_location("live_pure_talk_engine", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

SPEAKERSTART, SPEAKERSTOP = 0x0350, 0x0351
SILENT = b"--silent--"
FRAMEINFO = 24  # bytes the engine appends to every audio unit (_talk_frameinfo)


class _Camera:
    """The camera's side of the talk channel, driven from its own thread."""

    def __init__(self, login=True, react=True, quiet_after=None):
        self.login, self.react, self.quiet_after = login, react, quiet_after
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.01)
        self.addr = self.sock.getsockname()
        self.ioctls, self.audio = [], []  # (code, t) / (unit, t)
        self.granted_at = self.first_react_at = None
        self._peer = None
        self._stop = False
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _send(self, sub, size):
        pkt = bytearray(size)
        pkt[14], pkt[28] = 1, sub  # talk channel 1, sub-type
        self.sock.sendto(bytes(pkt), self._peer)

    def _run(self):
        last_react = 0.0
        while not self._stop:
            try:
                data, self._peer = self.sock.recvfrom(65536)
            except TimeoutError:
                data = None
            except OSError:
                return
            now = time.time()
            if data and data[:2] == b"IO":
                code = struct.unpack_from("<H", data, 2)[0]
                self.ioctls.append((code, now))
                if code == SPEAKERSTART and self.login:
                    self._send(0x00, 320)  # the camera's talk-login
            elif data and data[:2] == b"GR":
                self.granted_at = now
            elif data and data[:2] == b"AU":
                self.audio.append((data[2:-FRAMEINFO], now))
            if self.granted_at and self.react and now - last_react >= 0.05:
                if self.quiet_after is None or now - self.granted_at < self.quiet_after:
                    self._send(0x0A, 64)
                    last_react = now
                    self.first_react_at = self.first_react_at or now

    def codes(self):
        return [c for c, _ in self.ioctls]

    def close(self):
        self._stop = True
        self._thread.join(1)
        self.sock.close()


class _Feed:
    """A live_source: queued frames, `done`, and a record of when the camera first pulled."""

    silent_unit = SILENT

    def __init__(self, frames=(), done=False, done_on_first_pull=False, on_pull=None):
        self.q = collections.deque(frames)
        self.done = done
        self.done_on_first_pull = done_on_first_pull
        self.on_pull = on_pull
        self.first_pull_at = None

    def next_unit(self):
        if self.first_pull_at is None:
            self.first_pull_at = time.time()
            if self.done_on_first_pull:
                self.done = True
        unit = self.q.popleft() if self.q else None
        if self.on_pull:
            self.on_pull(self, unit)  # after the answer is decided, as another thread would
        return unit


@pytest.fixture
def engine(monkeypatch):
    """TUTKDirectSession with a bound, already-'connected' socket and readable frame builders."""
    monkeypatch.setattr(cp, "inv_transcode", lambda b: b)
    monkeypatch.setattr(cp, "build_ioctl_data", lambda R, seq, relseq, frmno, io, pl: b"IO" + struct.pack("<H", io))
    monkeypatch.setattr(cp, "build_talk_grant", lambda *a, **k: b"GR")
    monkeypatch.setattr(cp, "build_talk_audio", lambda R, ch, seq, relseq, frag, idx, au: b"AU" + au)
    made = []

    def make(cam):
        s = object.__new__(cp.TUTKDirectSession)
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.setblocking(False)
        s._sock, s.session_hdr, s._R, s._cam = sock, bytes(16), 0x1234, cam.addr
        s._cam_grant_cap = None
        s._seq = s._relseq = s._frmno = 0
        s._stop_reader = lambda: None
        s._send_ack = lambda: None
        s._note_cam_data = lambda dec: None
        s._talk_stop = False
        made.append((s, cam))
        return s

    yield make
    for s, cam in made:
        s._sock.close()
        cam.close()


def _talk(s, feed, **kw):
    kw.setdefault("warmup", 0.1)
    t = time.time()
    try:
        return s.send_audio_file("live", live_source=feed, **kw), time.time() - t
    finally:
        time.sleep(0.05)  # let the camera thread read the last packets


def test_grant_timeout_ends_a_talk_the_camera_never_takes(engine):
    """No talk-login after SPEAKERSTART. Kill: the grant check removed (the talk would loop until
    max_secs, 120 s at the microphone's default, with the camera in talk mode)."""
    cam = _Camera(login=False)
    s = engine(cam)
    t = time.time()
    with pytest.raises(cp.TalkTimeout) as exc:
        _talk(s, _Feed(), grant_timeout=0.3, max_secs=4)
    assert exc.value.reason == "grant"
    assert time.time() - t < 1.5
    time.sleep(0.05)
    assert cam.codes()[-2:] == [SPEAKERSTART, SPEAKERSTOP]


def test_a_granted_talk_outlives_the_grant_timeout(engine):
    """The child always passes grant_timeout=10. Kill: the grant not disarming the timer
    (`and not grant_sent` dropped): every real talk would end as camera_refused 10 s after
    SPEAKERSTART, cut off mid-sentence."""
    cam = _Camera()
    s = engine(cam)
    sent, secs = _talk(s, _Feed(), grant_timeout=0.3, max_secs=1.5)
    assert 1.4 < secs < 2.0
    assert sent > 10


def test_mic_released_during_the_handshake_ends_the_talk_at_once(engine):
    """SPEAKERSTART is out, the camera has not logged in, and the mic ends. Kill: `done`
    checked only once the pump runs (the talk would wait for a grant that may never come)."""
    cam = _Camera(login=False)
    s = engine(cam)
    feed = _Feed()
    threading.Timer(0.4, lambda: setattr(feed, "done", True)).start()
    sent, secs = _talk(s, feed, max_secs=4)
    assert sent == 0
    assert secs < 0.4 + 0.3
    assert cam.codes()[-2:] == [SPEAKERSTART, SPEAKERSTOP]


def test_mic_released_before_speakerstart_sends_neither(engine):
    """SPEAKERSTOP only follows a SPEAKERSTART, and a talk that ended during warmup never
    clicks the camera speaker. Kill: SPEAKERSTART sent regardless of `done`."""
    cam = _Camera()
    s = engine(cam)
    sent, secs = _talk(s, _Feed(done=True), warmup=0.5, max_secs=4)
    assert (sent, cam.audio) == (0, [])
    assert secs < 0.3
    assert SPEAKERSTART not in cam.codes() and SPEAKERSTOP not in cam.codes()


@pytest.mark.parametrize("tail", [8, None], ids=["tail-8", "default-no-tail"])
def test_tail_of_silence_follows_the_last_word(engine, tail):
    """Kill: tail_frames ignored (SPEAKERSTOP right after the last word cuts it off), or a tail
    by default (go2rtc's live mode passes none and must end as before). Also pins the READY
    contract: the first pull comes after the grant AND the camera's first reply."""
    cam = _Camera()
    s = engine(cam)
    words = [b"S%d" % i for i in range(5)]
    feed = _Feed(words, done_on_first_pull=True)
    kw = {} if tail is None else {"tail_frames": tail}
    sent, _ = _talk(s, feed, max_secs=4, **kw)
    assert [u for u, _ in cam.audio] == words + [SILENT] * (tail or 0)
    assert sent == 5 + (tail or 0)
    assert cam.codes()[-1] == SPEAKERSTOP
    assert cam.granted_at <= cam.first_react_at <= feed.first_pull_at


def test_done_is_read_before_the_pull(engine):
    """The reader can queue the last frame and set `done` after next_unit() found the queue empty
    but before it returned. Kill: `done` read after next_unit() (that last frame is never sent)."""

    def reader_races_ahead(feed, unit):
        if unit is None and not feed.done:
            feed.q.append(b"LAST")  # queued, then done: the order the real reader keeps
            feed.done = True

    cam = _Camera()
    s = engine(cam)
    feed = _Feed([b"S0"], on_pull=reader_races_ahead)
    _talk(s, feed, max_secs=4)  # no tail: the last pull decides the end on its own
    assert [u for u, _ in cam.audio] == [b"S0", SILENT, b"LAST"]


def test_camera_gone_quiet_mid_talk_is_camera_lost(engine):
    """Granted, a few replies, then silence. Kill: the liveness check removed (a camera that
    dropped off Wi-Fi would keep the talk "live" until max_secs)."""
    cam = _Camera(quiet_after=0.3)
    s = engine(cam)
    t = time.time()
    with pytest.raises(cp.TalkTimeout) as exc:
        _talk(s, _Feed(), liveness_timeout=0.5, max_secs=5)
    assert exc.value.reason == "camera_lost"
    assert exc.value.sent > 0
    assert time.time() - t < 2.0
    time.sleep(0.05)
    assert cam.codes()[-1] == SPEAKERSTOP


def test_a_camera_that_keeps_answering_is_never_lost(engine):
    """Kill: `last_rx` not updated on each packet (every talk would die after the timeout)."""
    cam = _Camera()
    s = engine(cam)
    sent, secs = _talk(s, _Feed(), liveness_timeout=0.4, max_secs=1.5)
    assert 1.4 < secs < 2.0
    assert sent > 10


def test_max_secs_caps_a_live_talk(engine):
    """A mic that never ends (e.g. HA stopped writing but never closed stdin). Kill: max_secs
    not applied in live mode."""
    cam = _Camera()
    s = engine(cam)
    sent, secs = _talk(s, _Feed(), max_secs=1.0)
    assert 0.95 < secs < 1.5
    assert sent > 5
    assert {u for u, _ in cam.audio} == {SILENT}  # nothing queued: the grid is fed with silence
    assert cam.codes()[-1] == SPEAKERSTOP


def test_file_mode_defaults_never_time_out(engine, monkeypatch, tmp_path):
    """Songs and TTS pass none of the new arguments. Kill: grant_timeout or liveness_timeout
    defaulting on (a camera slow to log in, or quiet after the grant, would now abort TTS)."""
    monkeypatch.setattr(cp, "_aac_units", lambda *a, **k: [b"U0", b"U1", b"U2"])
    song = tmp_path / "song.mp3"
    song.write_bytes(b"x")

    cam = _Camera(login=False)  # never logs in: no grant timeout by default
    s = engine(cam)
    t = time.time()
    assert s.send_audio_file(str(song), warmup=0.1, max_secs=1.0) == 0
    assert time.time() - t >= 0.95

    cam = _Camera(quiet_after=0.0)  # granted, then never a reply: no liveness by default
    s = engine(cam)
    assert s.send_audio_file(str(song), warmup=0.1, max_secs=1.0) == 0

    cam = _Camera()
    s = engine(cam)
    assert s.send_audio_file(str(song), warmup=0.1, max_secs=4) == 3
    time.sleep(0.05)
    assert [u for u, _ in cam.audio] == [b"U0", b"U1", b"U2"]  # the file, exactly


def test_pure_session_passes_the_live_arguments_through():
    """The child talks through PureSession, not the engine. Kill: any new argument dropped on
    the way (the timeouts would silently be off for the microphone)."""
    spec = importlib.util.spec_from_file_location("live_transport_talk", os.path.join(_TUTK, "cuboai_transport_py.py"))
    tp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tp)
    seen = {}

    class _Inner:
        def send_audio_file(self, path, **kw):
            seen.update(kw, path=path)
            return 42

    ps = object.__new__(tp.PureSession)
    ps._inner = _Inner()
    feed = _Feed()
    got = ps.send_audio_file("live", live_source=feed, max_secs=7, grant_timeout=10, liveness_timeout=5, tail_frames=8)
    assert got == 42
    assert (seen["path"], seen["live_source"], seen["max_secs"]) == ("live", feed, 7)
    assert (seen["grant_timeout"], seen["liveness_timeout"], seen["tail_frames"]) == (10, 5, 8)
