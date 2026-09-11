"""The engine notices a silent camera and ends its AV generator (issue #105).

Observed on a live Cubo Plus (CB02) about once an hour: the camera session goes
silent for good — sometimes after a loss burst, sometimes with no warning — and
the exec producer keeps running with stdout open and zero bytes forever. Nothing
in the reader noticed, and the producer was only replaced once the last consumer
gave up and go2rtc reaped it.

These tests drive the REAL `_av_reader` + `_read_av_units` with a synthetic camera
on loopback UDP (the harness from test_idx_seed.py) and pin the two gates:

* CUBOAI_STALL_S — after the first AU, end the generator once no AU has been
  dequeued AND no AV fragment has arrived for that long (the AND keeps a
  survivable head-of-line hold — fragments flowing, nothing sealing — from
  tripping it);
* CUBOAI_FIRST_AU_S — abandon a session that never streams.

Both are OFF by default (the engine stays vanilla; the production profile in
cuboai_stream_video.py turns them on). Also covered: the `reconnect()` helper's
ordering and counters, the reader-ref hazard in the generator's `finally`, and
the blocking close burst in `disconnect()`.
"""

import importlib.util
import os
import queue
import socket
import struct
import threading
import time

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")

_spec = importlib.util.spec_from_file_location("live_pure_stall", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

# Every gate the engine reads at construction that a test here may set. Cleared
# before AND after each rig so nothing leaks between tests (or into test_idx_seed).
_ENV_KEYS = (
    "CUBOAI_STALL_S",
    "CUBOAI_OUTPUT_STALL_S",
    "CUBOAI_FIRST_AU_S",
    "CUBOAI_IDX_SEED",
    "CUBOAI_NODROP",
    "CUBOAI_NODROP_GRACE",
    "CUBOAI_RECOVERY_HOLD",
    "CUBOAI_KF_GRACE",
    "CUBOAI_GRACE_SCALE",
    "CUBOAI_EMIT_COMPLETE",
)

# One single-fragment HEVC IDR access unit (start code + IDR_W_RADL), as in test_idx_seed.
AU = b"\x00\x00\x00\x01\x26\x01\xaf" + b"\xaa" * 40


def _clear_env():
    for k in _ENV_KEYS:
        os.environ.pop(k, None)


def _build_av_frag(idx, frag, payload, channel=0):
    """One camera AV DATA fragment, wire-encoded (layout per _av_reader)."""
    dec = bytearray(64 + len(payload))
    dec[8:12] = b"\x08\x04\x12\x00"
    dec[14] = channel
    dec[28] = 0x0C
    struct.pack_into("<H", dec, 46, frag & 0xFFFF)
    struct.pack_into("<H", dec, 52, len(payload))
    struct.pack_into("<H", dec, 56, idx & 0xFFFF)
    dec[58:64] = b"\x01\x00\x00\x00\x00\x00"
    dec[64:] = payload
    return cp.transcode(bytes(dec))


class _Rig:
    """A real engine session wired to a fake camera on loopback, plus a consumer
    thread that iterates the real `_read_av_units()` and records when it ends."""

    def __init__(self, **env):
        _clear_env()
        for k, v in env.items():
            os.environ[k] = str(v)
        self.sess = cp.TUTKDirectSession()
        self.sess._R = 0x1234
        self.sess.session_hdr = bytes(16)
        self.cam = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cam.bind(("127.0.0.1", 0))
        self.cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.cli.bind(("127.0.0.1", 0))
        self.cli.setblocking(False)
        self.sess._sock = self.cli
        self.sess._cam = self.cam.getsockname()
        self.items = []
        self.t_end = None
        self.err = None
        self._th = None

    def start_consumer(self, **kw):
        def run():
            try:
                for it in self.sess._read_av_units(**kw):
                    self.items.append((time.time(), it))
            except Exception as e:  # noqa: BLE001 - recorded for the assertion
                self.err = e
            finally:
                self.t_end = time.time()

        self._th = threading.Thread(target=run, daemon=True)
        self._th.start()

    def send(self, idx, payload=AU):
        self.cam.sendto(_build_av_frag(idx, idx, payload), self.cli.getsockname())

    def send_burst(self, start, n, gap=0.02):
        for i in range(n):
            self.send(start + i)
            time.sleep(gap)
        return time.time()

    def alive(self):
        return self._th is not None and self._th.is_alive()

    def join(self, t=5.0):
        if self._th is not None:
            self._th.join(t)

    def close(self):
        try:
            self.sess._stop_reader()
        except Exception:  # noqa: BLE001
            pass
        self.join(3.0)
        for s in (self.cam, self.cli):
            try:
                s.close()
            except OSError:
                pass
        _clear_env()


# =============================================================================
# Gates
# =============================================================================


def test_gates_default_off():
    _clear_env()
    s = cp.TUTKDirectSession()
    assert s._stall_s == 0.0 and s._first_au_s == 0.0


def test_garbage_gate_values_read_as_off():
    _clear_env()
    os.environ["CUBOAI_STALL_S"] = "abc"
    os.environ["CUBOAI_FIRST_AU_S"] = "-3"
    try:
        s = cp.TUTKDirectSession()
        assert s._stall_s == 0.0 and s._first_au_s == 0.0
    finally:
        _clear_env()


# =============================================================================
# Stall detection on the real reader
# =============================================================================


def test_generator_ends_after_silence():
    """Frames, then nothing: the generator must end ~STALL_S after the last one."""
    rig = _Rig(CUBOAI_STALL_S="0.5")
    try:
        rig.start_consumer()
        time.sleep(0.2)
        t_last = rig.send_burst(0, 40)
        rig.join(4.0)
        assert rig.t_end is not None, "generator never ended on silence"
        assert rig.err is None, rig.err
        assert len(rig.items) > 0, "no AU was delivered before the silence"
        dt = rig.t_end - t_last
        assert 0.4 <= dt <= 3.0, f"ended {dt:.2f}s after the last frame (expected ~0.5s; upper bound is CI slack)"
        assert rig.sess.get_stats()["stalls"] == 1
        assert rig.sess._av_reader_thread is None, "reader ref not cleared after a clean join"
        info = rig.sess._last_stall_info
        assert info and info["kind"] == "stall" and info["emitted"] > 0
    finally:
        rig.close()


def test_never_fires_before_the_first_au():
    """A cold session that has not delivered anything yet is not a stall."""
    rig = _Rig(CUBOAI_STALL_S="0.5", CUBOAI_FIRST_AU_S="0")
    try:
        rig.start_consumer()
        time.sleep(1.5)
        assert rig.alive(), "stall gate fired before any AU had arrived"
        rig.send_burst(0, 30)
        rig.join(4.0)
        assert rig.t_end is not None and len(rig.items) > 0
    finally:
        rig.close()


def test_first_au_timeout_ends_an_empty_session():
    rig = _Rig(CUBOAI_FIRST_AU_S="0.5")
    try:
        t0 = time.time()
        rig.start_consumer()
        rig.join(3.0)
        assert rig.t_end is not None, "an empty session never ended"
        assert 0.4 <= rig.t_end - t0 <= 2.5
        assert rig.items == []
        assert rig.sess.get_stats()["first_au_timeouts"] == 1
        assert rig.sess._last_stall_info["kind"] == "first_au"
    finally:
        rig.close()


def test_fragments_without_a_sealable_au_do_not_trip_the_stall():
    """Fragments keep arriving but a hole blocks the in-order seal for >STALL_S:
    that is a survivable recovery window, not a silent camera."""
    rig = _Rig(
        CUBOAI_STALL_S="0.5",
        CUBOAI_NODROP="1",
        CUBOAI_NODROP_GRACE="2000",
        CUBOAI_RECOVERY_HOLD="2000",
    )
    try:
        rig.start_consumer()
        time.sleep(0.2)
        for i in range(10):
            rig.send(i)
            time.sleep(0.02)
        time.sleep(0.3)
        # Hole at idx 10; keep sending 11.. for 1.2 s. Fragments flow, nothing can seal in order.
        t_burst0 = time.time()
        i = 11
        while time.time() - t_burst0 < 1.2:
            rig.send(i)
            i += 1
            time.sleep(0.02)
        assert rig.alive(), "stall gate fired while fragments were still arriving"
        rig.send(10)  # fill the hole
        t_last = time.time()
        rig.join(4.0)
        assert rig.t_end is not None, "generator never ended after the real silence"
        assert 0.4 <= rig.t_end - t_last <= 3.0
    finally:
        rig.close()


def test_gate_off_never_ends_on_silence():
    """With the gate off (the engine default) silence changes nothing; only a
    teardown ends the generator — today's behaviour, byte for byte."""
    rig = _Rig()
    try:
        rig.start_consumer()
        time.sleep(0.2)
        rig.send_burst(0, 20)
        time.sleep(1.5)
        assert rig.alive()
        assert rig.sess.get_stats()["stalls"] == 0
        rig.sess.disconnect()
        rig.join(3.0)
        assert rig.t_end is not None
    finally:
        rig.close()


def test_finally_keeps_reader_refs_while_the_reader_is_alive():
    """If the reader outlives the 1.5 s join, the refs must stay so a later
    _stop_reader()/disconnect() can still join it before closing the socket."""
    rig = _Rig()
    release = threading.Event()

    def stub_reader(s, out_q, stop_evt):
        out_q.put(("video", AU, None))
        release.wait(10)

    try:
        rig.sess._av_reader = stub_reader
        got = list(rig.sess._read_av_units(max_items=1))
        assert len(got) == 1
        th = rig.sess._av_reader_thread
        assert th is not None and th.is_alive(), "refs were cleared while the reader was still alive"
        release.set()
        rig.sess._stop_reader()
        assert rig.sess._av_reader_thread is None
    finally:
        release.set()
        rig.close()


# =============================================================================
# reconnect() and the close burst
# =============================================================================


def test_reconnect_helper_order_and_counters():
    _clear_env()
    sess = cp.TUTKDirectSession()
    calls = []
    results = iter([False, True])

    def fake_disconnect():
        calls.append("disconnect")

    def fake_connect(timeout=8.0, attempts=8):
        calls.append(("connect", timeout))
        return next(results)

    sess.disconnect = fake_disconnect
    sess.connect = fake_connect
    ok1 = sess.reconnect(timeout=1.0, settle=0)
    ok2 = sess.reconnect(timeout=1.0, settle=0)
    assert (ok1, ok2) == (False, True)
    assert calls == ["disconnect", ("connect", 1.0), "disconnect", ("connect", 1.0)]
    st = sess.get_stats()
    assert st["reconnects"] == 2 and st["reconnect_fail"] == 1
    assert st["reconnect_s"] >= 0


class _SockProxy:
    def __init__(self, real):
        self._real = real
        self.events = []

    def setblocking(self, flag):
        self.events.append(("setblocking", flag))
        return self._real.setblocking(flag)

    def sendto(self, data, addr):
        self.events.append(("sendto", len(data)))
        return self._real.sendto(data, addr)

    def close(self):
        self.events.append(("close",))
        return self._real.close()

    def __getattr__(self, name):
        return getattr(self._real, name)


def test_disconnect_sends_the_close_burst_blocking():
    """The close burst on a non-blocking socket could be EWOULDBLOCK-dropped at
    teardown, leaving the camera holding the slot — which an immediate reconnect
    must not race. It is now sent blocking, three times, and actually arrives."""
    _clear_env()
    sess = cp.TUTKDirectSession()
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.bind(("127.0.0.1", 0))
    rx.settimeout(1.0)
    real = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    real.bind(("127.0.0.1", 0))
    real.setblocking(False)
    proxy = _SockProxy(real)
    sess._sock = proxy
    sess._R = 0x1234
    sess._cam = rx.getsockname()
    sess._session_fp = None
    try:
        sess.disconnect()
        kinds = [e[0] for e in proxy.events]
        assert "setblocking" in kinds and "sendto" in kinds
        assert kinds.index("setblocking") < kinds.index("sendto"), "close burst sent before setblocking(True)"
        assert proxy.events[kinds.index("setblocking")] == ("setblocking", True)
        assert kinds.count("sendto") == 3
        got = [rx.recvfrom(64)[0] for _ in range(3)]
        assert all(g == cp.build_close(0x1234) for g in got)
        assert sess._sock is None
    finally:
        rx.close()


def test_idx_seed_harness_still_drives_the_reader():
    """The new stamps are additive: the plain reader path from test_idx_seed still emits."""
    rig = _Rig()
    try:
        out_q = queue.Queue(maxsize=600)
        stop_evt = threading.Event()
        th = threading.Thread(target=rig.sess._av_reader, args=(rig.cli, out_q, stop_evt), daemon=True)
        th.start()
        for i in range(40):
            rig.send(i)
            time.sleep(0.002)
        time.sleep(1.0)
        stop_evt.set()
        th.join(2.0)
        assert rig.sess._last_av_rx is not None and rig.sess._last_rx is not None
        n = 0
        while True:
            try:
                if out_q.get_nowait() is not None:
                    n += 1
            except queue.Empty:
                break
        assert n > 0
    finally:
        rig.close()


def test_closing_the_generator_from_the_consumer_joins_the_reader():
    """The desync path closes the engine generator from the muxer's thread; GeneratorExit must
    run its finally (stop + join) so the reader never outlives the session it belonged to."""
    rig = _Rig()
    try:
        gen = rig.sess._read_av_units()
        th = None
        rig.send_burst(0, 10)
        try:
            next(gen)
            th = rig.sess._av_reader_thread
            assert th is not None and th.is_alive()
        finally:
            gen.close()
        assert rig.sess._av_reader_thread is None, "reader still registered after close()"
        assert th is not None and not th.is_alive(), "reader thread outlived the closed generator"
    finally:
        rig.close()


def test_stop_when_hook_ends_the_generator_while_idle():
    """The streamer's decode deadline is polled in the idle loop, so a stall is caught on
    time even when no AU arrives to trigger the muxer's own per-item check."""
    rig = _Rig()  # silence gate OFF: only the hook can end this
    fire_at = [None]

    def hook():
        return fire_at[0] is not None and time.time() >= fire_at[0]

    try:
        rig.start_consumer(stop_when=hook)
        time.sleep(0.2)
        rig.send_burst(0, 20)
        time.sleep(0.5)
        assert rig.alive(), "ended before the hook was due"
        fire_at[0] = time.time() + 0.3
        rig.join(3.0)
        assert rig.t_end is not None, "the hook never ended the generator"
        assert rig.t_end - fire_at[0] <= 2.0
        assert rig.sess._last_stall_info["kind"] == "external"
        assert rig.sess.get_stats()["stalls"] == 0, "an external end is not an engine stall"
    finally:
        rig.close()


def test_output_stall_gate_default_off():
    _clear_env()
    assert cp.TUTKDirectSession()._output_stall_s == 0.0


def test_wedged_output_fires_even_while_fragments_arrive():
    """The 10:56 failure: no COMPLETE AU is delivered for a long stretch, but late resends keep
    arriving so `_last_av_rx` stays fresh — the silence gate (AND on no-fragment) must hold off,
    and the wedged-output gate must catch it at _output_stall_s instead."""
    rig = _Rig(CUBOAI_STALL_S="0.5", CUBOAI_OUTPUT_STALL_S="1.5")
    pump_stop = threading.Event()

    def reader(s, out_q, stop_evt):
        for _ in range(3):
            out_q.put(("video", AU, {"is_keyframe": True}))
        # then deliver NO further AU, but keep the AV-fragment clock fresh (late resends)
        while not stop_evt.is_set() and not pump_stop.is_set():
            rig.sess._last_av_rx = time.time()
            time.sleep(0.05)

    try:
        rig.sess._av_reader = reader
        rig.start_consumer()
        rig.join(5.0)
        assert rig.t_end is not None, "wedged-output stall never ended the generator"
        assert len(rig.items) == 3
        info = rig.sess._last_stall_info
        assert info["kind"] == "output_stall", info
        # must NOT have fired at the 0.5 s silence threshold — fragments were fresh throughout
        assert info["since_s"] >= 1.4, (
            f"fired at {info['since_s']}s — the silence gate misfired despite fresh fragments"
        )
        st = rig.sess.get_stats()
        assert st["output_stalls"] == 1
        assert st["stalls"] == 1, "a wedged stall must also count as a stall (it triggers one reconnect)"
    finally:
        pump_stop.set()
        rig.close()


def test_true_silence_still_beats_the_wedged_gate_to_the_punch():
    """When the camera is genuinely silent, the fast 4 s silence gate fires — not the slower
    wedged gate — so a dead camera is still caught quickly."""
    rig = _Rig(CUBOAI_STALL_S="0.5", CUBOAI_OUTPUT_STALL_S="3.0")
    try:
        rig.start_consumer()
        time.sleep(0.2)
        t_last = rig.send_burst(0, 20)
        rig.join(4.0)
        assert rig.t_end is not None
        assert rig.t_end - t_last <= 2.0, "silence took the slow wedged path instead of the fast one"
        info = rig.sess._last_stall_info
        assert info["kind"] == "stall", info
        assert rig.sess.get_stats()["output_stalls"] == 0
    finally:
        rig.close()
