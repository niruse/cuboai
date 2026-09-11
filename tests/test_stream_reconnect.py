"""In-place reconnect on the same timeline and muxer (issue #105).

When the engine's AV generator ends on a silent camera, the producer must NOT
exit: a fresh process restarts PTS at 0 into an MPEG-TS muxer that never sets
the discontinuity indicator, which is fatal for an fMP4/MSE consumer (the Nest
Hub in #105). Instead `_reconnecting_frames` re-establishes the session on the
same engine object and yields one ('reconnect', None, None) boundary, and
`mux_timed_stream` keeps its AVTimeline and TSMuxer across it — consumers see a
real forward gap, continuous continuity counters, PAT/PMT re-emitted, and an IDR
with the random-access indicator as the first post-gap picture.

Also pinned: the wrapper's failure accounting and exhaustion, the timeline's
guard against a camera clock that stepped while we were away (otherwise PTS
crawls at +1 ms/frame forever), the SIGTERM handler that lets `finally:
sess.disconnect()` run on a go2rtc reap, and the health-line recovery suffix.
"""

import importlib.util
import inspect
import os
import signal
import threading
import time

import pytest

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_TUTK, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# The streamer puts tutk/ on sys.path at import and imports cuboai_session; the
# muxer/timeline it uses are the bare-name modules, so tests read those too.
sv = _load("live_stream_video_reconnect", "cuboai_stream_video.py")
import cuboai_mpegts as M  # noqa: E402
import cuboai_pts as P  # noqa: E402

_PROFILE_KEYS = tuple(sorted(set(sv.PRODUCTION_ENV) | set(sv.RAW_ENV)))


class _EnvSandbox:
    """Snapshot/restore every profile key — apply_env_profile() writes them all."""

    def __enter__(self):
        self._saved = {k: os.environ.get(k) for k in _PROFILE_KEYS}
        for k in _PROFILE_KEYS:
            os.environ.pop(k, None)
        return self

    def __exit__(self, *exc):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return False


# =============================================================================
# Profiles
# =============================================================================


def test_production_profile_enables_and_raw_disables():
    with _EnvSandbox():
        sv.apply_env_profile(False)
        assert os.environ["CUBOAI_STALL_S"] == "4"
        assert os.environ["CUBOAI_OUTPUT_STALL_S"] == "6"
        assert os.environ["CUBOAI_FIRST_AU_S"] == "15"
        sv.apply_env_profile(True)
        assert os.environ["CUBOAI_STALL_S"] == "0"
        assert os.environ["CUBOAI_OUTPUT_STALL_S"] == "0"
        assert os.environ["CUBOAI_FIRST_AU_S"] == "0"


def test_an_explicit_env_override_survives_the_production_profile():
    with _EnvSandbox():
        os.environ["CUBOAI_STALL_S"] = "3"
        sv.apply_env_profile(False)
        assert os.environ["CUBOAI_STALL_S"] == "3"


# =============================================================================
# The reconnecting wrapper
# =============================================================================

V = ("video", b"\x00\x00\x00\x01\x26\x01", {"is_keyframe": True})


class _FakeSess:
    def __init__(self, sessions, reconnect_results=None):
        self._sessions = [list(s) for s in sessions]
        self._rr = list(reconnect_results or [])
        self.reconnect_calls = []
        self.connected = True
        self.last_stall_info = {
            "kind": "stall",
            "since_s": 4.1,
            "emitted": 3,
            "last_frag_age": 4.1,
            "last_pkt_age": 4.1,
            "cam_clock_age": 4.0,
        }

    def av_frames_timed(self, stop_when=None):
        items = self._sessions.pop(0) if self._sessions else []
        yield from items

    def reconnect(self, timeout, settle):
        self.reconnect_calls.append((timeout, settle))
        return self._rr.pop(0) if self._rr else True

    def get_stats(self):
        return {"keepalive_err": 0, "stalls": 1}


def _drain(gen):
    out = []
    with pytest.raises(sv.StreamExhausted):
        for it in gen:
            out.append(it)
    return [k for k, _, _ in out]


def test_wrapper_reconnects_yields_a_boundary_and_exhausts_after_three_empties():
    sess = _FakeSess([[V] * 3, [V] * 3, [], [], []])
    logs = []
    kinds = _drain(
        sv._reconnecting_frames(sess, logs.append, max_fail=3, connect_timeout=1.0, settle=0, backoff=(0, 0, 0))
    )
    assert kinds == ["video"] * 3 + ["reconnect"] + ["video"] * 3 + ["reconnect"] * 3
    assert len(sess.reconnect_calls) == 4
    assert sess.reconnect_calls[0] == (1.0, 0)


def test_a_productive_session_resets_the_failure_count():
    sess = _FakeSess([[], [V], [], []])
    kinds = _drain(sv._reconnecting_frames(sess, lambda _m: None, max_fail=2, settle=0, backoff=(0, 0)))
    # empty(1) -> R -> productive(reset) -> R -> empty(1) -> R -> empty(2) -> exhausted
    assert kinds == ["reconnect", "video", "reconnect", "reconnect"]


def test_handshake_failures_back_off_and_exhaust():
    sess = _FakeSess([[V]], reconnect_results=[False, False, False])
    logs = []
    t0 = time.time()
    kinds = _drain(
        sv._reconnecting_frames(sess, logs.append, max_fail=3, connect_timeout=1.0, settle=0, backoff=(0.05, 0.1, 0.2))
    )
    elapsed = time.time() - t0
    assert kinds == ["video"]
    assert len(sess.reconnect_calls) == 3
    assert elapsed >= 0.13, f"backoff not honoured ({elapsed:.2f}s)"
    assert sum("FAILED" in m for m in logs) == 3


def test_the_stall_line_carries_the_diagnostics():
    sess = _FakeSess([[V], []])
    logs = []
    _drain(sv._reconnecting_frames(sess, logs.append, max_fail=1, settle=0, backoff=(0,)))
    stall = [m for m in logs if m.startswith("[stall]")]
    assert stall, logs
    line = stall[0]
    for needle in (
        "no AU for 4.1s",
        "last frag 4.1s",
        "last pkt 4.1s",
        "cam_clock 4.0s",
        "kaerr 0",
        "stalls 1",
        "-> reconnect #1",
    ):
        assert needle in line, (needle, line)


def test_main_uses_the_wrapper_and_exits_nonzero_only_when_exhausted():
    src = inspect.getsource(sv.main)
    assert "_reconnecting_frames(" in src
    assert "except StreamExhausted" in src
    assert "sys.exit(rc)" in src
    assert src.index("_install_sigterm_handler()") < src.index("mux_timed_stream(")


# =============================================================================
# Muxer + timeline across the boundary — parse the real TS bytes
# =============================================================================

BASE = 1_780_000_000_000
IDR = b"\x00\x00\x00\x01\x26\x01\xaf" + b"\xaa" * 40  # HEVC IDR_W_RADL (nal type 19)
PF = b"\x00\x00\x00\x01\x02\x01\xaf" + b"\xbb" * 40  # HEVC TRAIL_R (nal type 1)
AAC = b"\xff\xf1" + b"\x00" * 30


def _vfi(i, kf, ts):
    return {"codec": "hevc", "ts_valid": True, "timestamp_ms": ts, "is_keyframe": kf, "frame_no": i}


def _afi(ts):
    return {"ts_sec": ts // 1000, "ts_valid": True, "sample_rate": 16000}


def _ts_packets(buf):
    assert len(buf) % 188 == 0, len(buf)
    for i in range(0, len(buf), 188):
        p = buf[i : i + 188]
        assert p[0] == 0x47
        pid = ((p[1] & 0x1F) << 8) | p[2]
        afc = (p[3] >> 4) & 3
        pos = 4
        af_flags = None
        if afc & 2:
            af_len = p[4]
            pos = 5 + af_len
            if af_len > 0:
                af_flags = p[5]
        yield {
            "pid": pid,
            "pusi": bool(p[1] & 0x40),
            "cc": p[3] & 0xF,
            "af_flags": af_flags,
            "payload": p[pos:] if afc & 1 else b"",
        }


def _pes_pts(payload):
    assert payload[:3] == b"\x00\x00\x01"
    b = payload[9:14]
    return (((b[0] >> 1) & 7) << 30) | (b[1] << 22) | (((b[2] >> 1) & 0x7F) << 15) | (b[3] << 7) | (b[4] >> 1)


def _run_mux_with_boundary(monkeypatch, gap_ms=5000, k=12):
    # The timeline compares the camera clock against WALL time across a boundary (a camera
    # timestamp that outruns wall time can only be a clock step). A real outage advances both,
    # so the wall clock must advance by the gap here too — otherwise the guard correctly reads
    # the simulated jump as a clock step and collapses it (that is what it is for).
    now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    items = []
    for i in range(k):
        ts = BASE + i * 77
        items.append(("video", IDR if i == 0 else PF, _vfi(i, i == 0, ts)))
        items.append(("audio", AAC, _afi(ts)))
    items.append(("reconnect", None, None))
    t_last = BASE + (k - 1) * 77
    # first post-gap AU is a P-frame (must be dropped), then an IDR, then more
    items.append(("video", PF, _vfi(k, False, t_last + gap_ms)))
    items.append(("video", IDR, _vfi(k + 1, True, t_last + gap_ms + 77)))
    for j in range(2, k):
        ts = t_last + gap_ms + 77 * j
        items.append(("video", PF, _vfi(k + j, False, ts)))
        items.append(("audio", AAC, _afi(ts)))

    chunks, logs, marks = [], [], []

    def gen():
        for it in items:
            if it[0] == "reconnect":
                marks.append(len(chunks))
                now[0] += gap_ms / 1000.0  # the outage really took this long
            yield it

    sv.mux_timed_stream(gen(), chunks.append, clean_gop=True, mux_audio=True, log=logs.append)
    pre = b"".join(chunks[: marks[0]])
    post = b"".join(chunks[marks[0] :])
    return pre, post, logs, (k - 1) * 77, gap_ms + 77


def test_mux_keeps_timeline_and_muxer_across_the_boundary(monkeypatch):
    pre, post, logs, last_pre_ms, expected_gap_ms = _run_mux_with_boundary(monkeypatch)
    whole = list(_ts_packets(pre + post))
    post_pkts = list(_ts_packets(post))

    # (a) continuity counters continue per PID across the boundary — same TSMuxer
    last_cc = {}
    for p in whole:
        if p["pid"] in last_cc:
            assert p["cc"] == (last_cc[p["pid"]] + 1) & 0xF, f"CC break on pid {p['pid']:#x}"
        last_cc[p["pid"]] = p["cc"]

    # (b) video PTS strictly increasing, and the boundary is a real forward gap — same AVTimeline
    vpts = [_pes_pts(p["payload"]) for p in whole if p["pid"] == M._PID_VIDEO and p["pusi"]]
    assert all(a < b for a, b in zip(vpts, vpts[1:])), "video PTS not strictly increasing"
    pre_v = [_pes_pts(p["payload"]) for p in _ts_packets(pre) if p["pid"] == M._PID_VIDEO and p["pusi"]]
    post_v = [_pes_pts(p["payload"]) for p in post_pkts if p["pid"] == M._PID_VIDEO and p["pusi"]]
    assert pre_v[-1] == last_pre_ms * 90
    gap = (post_v[0] - pre_v[-1]) / 90.0
    assert 4900 <= gap <= 5300, f"boundary gap {gap:.0f} ms"

    # (e) the first post-gap picture is the IDR (the leading P-frame was dropped)
    assert post_v[0] == (last_pre_ms + expected_gap_ms) * 90

    # (c) PAT then PMT are the first packets after the boundary
    assert post_pkts[0]["pid"] == M._PID_PAT and post_pkts[1]["pid"] == M._PID_PMT

    # (d) that IDR carries the random-access indicator
    first_video = next(p for p in post_pkts if p["pid"] == M._PID_VIDEO)
    assert first_video["af_flags"] is not None and first_video["af_flags"] & 0x40

    # (f) exactly one muxer was ever built
    assert sum("[mpegts] muxing" in m for m in logs) == 1, logs
    assert any("[reconnect] session re-established" in m for m in logs)
    assert any("resync at IDR (dropped 1 AUs)" in m for m in logs), logs


def test_a_plain_stream_without_a_boundary_is_unaffected():
    """No sentinel → no reconnect log, one muxer, monotonic PTS (the everyday path)."""
    items = [("video", IDR, _vfi(0, True, BASE))] + [("video", PF, _vfi(i, False, BASE + i * 77)) for i in range(1, 6)]
    chunks, logs = [], []
    sv.mux_timed_stream(iter(items), chunks.append, clean_gop=True, mux_audio=False, log=logs.append)
    assert not any("[reconnect]" in m for m in logs)
    vpts = [_pes_pts(p["payload"]) for p in _ts_packets(b"".join(chunks)) if p["pid"] == M._PID_VIDEO and p["pusi"]]
    assert vpts == [i * 77 * 90 for i in range(6)]


# =============================================================================
# Timeline guard against a camera clock that stepped while we were away
# =============================================================================


def _ten_frames(av, now):
    last = None
    for i in range(10):
        ts = BASE + i * 77
        last = av.video({"timestamp_ms": ts, "ts_valid": True, "is_keyframe": i == 0, "frame_no": i})
        av.audio({"ts_sec": ts // 1000, "ts_valid": True, "sample_rate": 16000})
        now[0] += 0.077
    return last


def test_timeline_rebases_when_the_camera_clock_steps_back():
    now = [1000.0]
    av = P.AVTimeline(clock=lambda: now[0])
    last = _ten_frames(av, now)
    av.mark_discontinuity()
    now[0] += 5.0
    back = BASE + 9 * 77 - 60_000  # camera clock 60 s behind after the outage
    r = av.video({"timestamp_ms": back, "ts_valid": True, "is_keyframe": True, "frame_no": 10})
    assert abs(r["pts_ms"] - (last["pts_ms"] + 5000)) < 200, r
    assert av._v.n_clamp == 0, "the clock step was clamped (the +1 ms crawl) instead of rebased"
    assert av.n_rebase == 1 and av.stats()["rebase"] == 1
    a = av.audio({"ts_sec": back // 1000, "ts_valid": True, "sample_rate": 16000})
    assert abs(a["pts_ms"] - r["pts_ms"]) < 1100, ("A/V offset lost — was only the video clock shifted?", a, r)
    # and the timeline stays monotonic afterwards at the normal cadence
    r2 = av.video({"timestamp_ms": back + 77, "ts_valid": True, "is_keyframe": False, "frame_no": 11})
    assert 60 < r2["pts_ms"] - r["pts_ms"] < 90


def test_timeline_keeps_the_honest_gap_when_the_clock_is_sane():
    now = [1000.0]
    av = P.AVTimeline(clock=lambda: now[0])
    last = _ten_frames(av, now)
    av.mark_discontinuity()
    now[0] += 5.0
    r = av.video({"timestamp_ms": BASE + 9 * 77 + 5000, "ts_valid": True, "is_keyframe": True, "frame_no": 10})
    assert r["pts_ms"] == last["pts_ms"] + 5000
    assert av.n_rebase == 0


def test_without_a_discontinuity_mark_a_backward_step_still_clamps():
    """The pre-existing behaviour (a stray backward ts mid-session) is unchanged."""
    now = [1000.0]
    av = P.AVTimeline(clock=lambda: now[0])
    _ten_frames(av, now)
    r = av.video({"timestamp_ms": BASE - 5000, "ts_valid": True, "is_keyframe": False, "frame_no": 10})
    assert av._v.n_clamp == 1 and av.n_rebase == 0
    assert r["pts_ms"] == 9 * 77 + 1


# =============================================================================
# SIGTERM → clean teardown
# =============================================================================


def test_sigterm_handler_is_installed_one_shot_and_raises_keyboardinterrupt():
    orig = signal.getsignal(signal.SIGTERM)
    try:
        sv._install_sigterm_handler()
        h = signal.getsignal(signal.SIGTERM)
        assert callable(h) and h is not signal.SIG_DFL
        with pytest.raises(KeyboardInterrupt):
            h(signal.SIGTERM, None)
        assert signal.getsignal(signal.SIGTERM) == signal.SIG_DFL, "handler must be one-shot"
    finally:
        signal.signal(signal.SIGTERM, orig)


# =============================================================================
# Health line: recovery counters are visible, and only when something happened
# =============================================================================


def test_health_line_shows_recovery_counters_only_when_nonzero(capsys):
    import cuboai_pure as cp

    base = cp.TUTKDirectSession().get_stats()

    class _S:
        def __init__(self, over):
            self.over = over
            self.connected = True

        def get_stats(self):
            d = dict(base)
            d.update(self.over)
            d["t"] = time.time()
            return d

    def run(over):
        stop = threading.Event()
        th = threading.Thread(target=sv._verbose_loop, args=(_S(over), 0.02, False, stop), daemon=True)
        th.start()
        time.sleep(0.15)
        stop.set()
        th.join(2.0)
        return capsys.readouterr().err

    err = run({"keepalive_err": 3, "reconnects": 2, "reconnect_fail": 1, "reconnect_s": 1.3, "stalls": 1})
    assert "RECOVERED reconn 2 (fail 1, 1.3s) stalls 1 first_au 0 kaerr 3" in err, err
    err = run({})
    assert "[health t=" in err and "RECOVERED" not in err, err
