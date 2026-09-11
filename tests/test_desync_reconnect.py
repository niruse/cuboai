"""Decode stall: data still flows, but nothing decodable goes out (issue #105, second flavour).

Observed live on a CB02 twenty minutes into the v2.6.32 soak: a loss burst opened a
32-fragment hole, a torn access unit put clean_gop into "awaiting IDR", and for 21 s every
keyframe was damaged — the muxer dropped 150 AUs, the NVR starved and reset at ~10 s, and a
single-consumer viewer (a Nest Hub) would simply have died. The engine's silence gate stayed
quiet, correctly: fragments were arriving. Only the muxer knows that nothing decodable has
gone out, so the gate lives there: after `desync_reconnect_s` of awaiting an IDR it raises
`control.force`, and `_reconnecting_frames` tears the session down and brings it back — a
fresh session opens on an IDR and resets the wedged receive window.
"""

import importlib.util
import os
import time

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_TUTK, fname))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


sv = _load("live_stream_video_desync", "cuboai_stream_video.py")

BASE = 1_780_000_000_000
IDR = b"\x00\x00\x00\x01\x26\x01\xaf" + b"\xaa" * 40
PF = b"\x00\x00\x00\x01\x02\x01\xaf" + b"\xbb" * 40


def _vfi(i, kf, ts):
    return {"codec": "hevc", "ts_valid": True, "timestamp_ms": ts, "is_keyframe": kf, "frame_no": i}


def _feed(monkeypatch, script, desync_s=8.0, control=None):
    """script: list of (wall_seconds_to_advance_before, kind, data, fi). Returns (control, logs)."""
    now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    control = control if control is not None else sv._ReconnectControl()
    logs = []
    forced_at = []

    def gen():
        for adv, kind, data, fi in script:
            now[0] += adv
            yield (kind, data, fi)
            if control.force:
                forced_at.append(len(forced_at) + 1)
                control.force = False  # the real wrapper consumes it; keep counting here

    sv.mux_timed_stream(
        gen(),
        lambda _b: None,
        clean_gop=True,
        mux_audio=False,
        log=logs.append,
        control=control,
        desync_reconnect_s=desync_s,
    )
    return forced_at, logs


def _storm(seconds, start_index=2, start_ts=BASE + 154):
    """One second per P-frame with a valid FRAMEINFO — decodable-looking data that clean_gop must
    drop while awaiting an IDR (this is what a damaged-keyframe window looks like from the muxer)."""
    return [(1.0, "video", PF, _vfi(start_index + i, False, start_ts + 1000 * i)) for i in range(seconds)]


def test_muxer_requests_a_reconnect_after_the_desync_window(monkeypatch):
    script = [
        (0.0, "video", IDR, _vfi(0, True, BASE)),
        (0.077, "video", PF, _vfi(1, False, BASE + 77)),
        (0.077, "video", PF, None),  # torn AU -> desync, awaiting IDR
    ] + _storm(12)
    forced_at, logs = _feed(monkeypatch, script, desync_s=8.0)
    assert forced_at, "no reconnect was requested although nothing decodable went out for 12 s"
    assert len(forced_at) == 1, f"requested more than once inside one window: {forced_at}"
    assert any("no decodable video for" in m and "requesting reconnect" in m for m in logs), logs


def test_an_idr_inside_the_window_clears_the_request(monkeypatch):
    script = (
        [
            (0.0, "video", IDR, _vfi(0, True, BASE)),
            (0.077, "video", PF, None),  # desync
        ]
        + _storm(5)
        + [
            (1.0, "video", IDR, _vfi(20, True, BASE + 20_000)),  # clean IDR at +6 s
        ]
        + [(1.0, "video", PF, _vfi(21 + i, False, BASE + 21_000 + 1000 * i)) for i in range(10)]
    )
    forced_at, logs = _feed(monkeypatch, script, desync_s=8.0)
    assert not forced_at, "a reconnect was requested although an IDR resynced the stream in time"
    assert any("resync at IDR" in m for m in logs)


def test_the_window_restarts_after_a_reconnect_boundary(monkeypatch):
    """A reconnect can be triggered by the ENGINE's silence gate a few seconds into a desync.
    The fresh session must then get a full window of its own — otherwise the old desync clock
    (already 10 s old by the first new frame) would force a second reconnect immediately."""
    script = (
        [
            (0.0, "video", IDR, _vfi(0, True, BASE)),
            (0.077, "video", PF, None),  # desync at ~T
        ]
        + _storm(5)
        + [  # 5 s awaiting an IDR: under the 8 s threshold, no request yet
            (0.0, "reconnect", None, None),  # ...the silence gate reconnected us meanwhile
        ]
        + _storm(5, start_index=30, start_ts=BASE + 40_000)
    )  # 5 s into the fresh session
    forced_at, _ = _feed(monkeypatch, script, desync_s=8.0)
    assert not forced_at, "the fresh session must get its own 8 s window; 10 s since the OLD desync is irrelevant"


def test_a_fresh_session_that_never_syncs_is_still_reconnected(monkeypatch):
    """...but the restarted window does still fire on its own once 8 s pass without an IDR."""
    script = (
        [
            (0.0, "video", IDR, _vfi(0, True, BASE)),
            (0.077, "video", PF, None),
        ]
        + _storm(5)
        + [
            (0.0, "reconnect", None, None),
        ]
        + _storm(9, start_index=30, start_ts=BASE + 40_000)
    )
    forced_at, _ = _feed(monkeypatch, script, desync_s=8.0)
    assert len(forced_at) == 1


def test_desync_gate_is_off_when_disabled(monkeypatch):
    script = [(0.0, "video", IDR, _vfi(0, True, BASE)), (0.077, "video", PF, None)] + _storm(30)
    forced_at, logs = _feed(monkeypatch, script, desync_s=0.0)
    assert not forced_at
    assert not any("requesting reconnect" in m for m in logs)


def test_no_control_means_no_request_even_if_enabled(monkeypatch):
    """The validator replays through mux_timed_stream with no wrapper; it must never try."""
    now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    logs = []

    def gen():
        for adv, kind, data, fi in [(0.0, "video", IDR, _vfi(0, True, BASE)), (0.1, "video", PF, None)] + _storm(20):
            now[0] += adv
            yield (kind, data, fi)

    sv.mux_timed_stream(gen(), lambda _b: None, clean_gop=True, log=logs.append, control=None, desync_reconnect_s=8.0)
    assert not any("requesting reconnect" in m for m in logs)


# =============================================================================
# The wrapper honours a forced reconnect
# =============================================================================

V = ("video", PF, {"is_keyframe": False})


class _FakeSess:
    def __init__(self, sessions):
        self._sessions = [list(s) for s in sessions]
        self.reconnect_calls = 0
        self.connected = True
        self.last_stall_info = {"kind": "stall", "since_s": 0.0}
        self.closed_inner = 0

    def av_frames_timed(self, stop_when=None):
        items = self._sessions.pop(0) if self._sessions else []
        try:
            yield from items
        finally:
            self.closed_inner += 1

    def reconnect(self, timeout, settle):
        self.reconnect_calls += 1
        return True

    def get_stats(self):
        return {"keepalive_err": 0, "stalls": 0}


def test_wrapper_closes_the_session_and_reconnects_when_forced():
    sess = _FakeSess([[V] * 5, [V] * 3])
    control = sv._ReconnectControl()
    logs = []
    gen = sv._reconnecting_frames(sess, logs.append, max_fail=3, settle=0, backoff=(0, 0, 0), control=control)
    kinds = []
    for kind, _, _ in gen:
        kinds.append(kind)
        if len(kinds) == 2:
            control.force = True  # the muxer asks after the 2nd item
        if len(kinds) == 6:  # 2 + boundary + 3
            break
    gen.close()
    assert kinds == ["video", "video", "reconnect", "video", "video", "video"]
    assert sess.reconnect_calls == 1
    assert control.n_forced == 1 and control.force is False
    assert sess.closed_inner >= 1, "the inner engine generator was not closed before reconnecting"
    assert any(m.startswith("[stall] desync") for m in logs), logs


def test_production_profile_enables_the_desync_gate_and_raw_disables_it():
    keys = ("CUBOAI_DESYNC_S", "CUBOAI_STALL_S", "CUBOAI_FIRST_AU_S")
    saved = {k: os.environ.get(k) for k in set(sv.PRODUCTION_ENV) | set(sv.RAW_ENV)}
    try:
        for k in saved:
            os.environ.pop(k, None)
        sv.apply_env_profile(False)
        assert os.environ["CUBOAI_DESYNC_S"] == "8"
        sv.apply_env_profile(True)
        assert os.environ["CUBOAI_DESYNC_S"] == "0"
        assert all(k in sv.PRODUCTION_ENV and k in sv.RAW_ENV for k in keys)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# =============================================================================
# The decode deadline is published to the wrapper and honoured while idle
# =============================================================================


def test_muxer_publishes_and_clears_the_decode_deadline(monkeypatch):
    now = [1_000_000.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    control = sv._ReconnectControl()
    seen = []

    def gen():
        for adv, kind, data, fi in [
            (0.0, "video", IDR, _vfi(0, True, BASE)),  # first IDR -> synced: cleared
            (0.077, "video", PF, _vfi(1, False, BASE + 77)),
            (0.077, "video", PF, None),  # hole -> desync: armed
            (1.0, "video", PF, _vfi(3, False, BASE + 3000)),
            (1.0, "video", IDR, _vfi(4, True, BASE + 4000)),  # resync: cleared again
        ]:
            now[0] += adv
            yield (kind, data, fi)
            seen.append(control.deadline)

    sv.mux_timed_stream(
        gen(), lambda _b: None, clean_gop=True, log=lambda _m: None, control=control, desync_reconnect_s=8.0
    )
    assert seen[0] is None and seen[1] is None
    assert seen[2] is not None and abs(seen[2] - (now[0] - 2.0 + 8.0)) < 0.01, "armed at the hole, +8 s"
    assert seen[3] == seen[2]
    assert seen[4] is None, "resync must clear the deadline, or the idle loop would fire it later"


def test_control_due_reflects_the_deadline(monkeypatch):
    now = [100.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    c = sv._ReconnectControl()
    assert c.due() is False
    c.deadline = 108.0
    assert c.due() is False
    now[0] = 108.0
    assert c.due() is True
    c.deadline = None
    assert c.due() is False


def test_wrapper_passes_the_deadline_hook_and_treats_an_idle_end_as_desync():
    class _S(_FakeSess):
        def __init__(self, sessions):
            super().__init__(sessions)
            self.hooks = []

        def av_frames_timed(self, stop_when=None):
            self.hooks.append(stop_when)
            items = self._sessions.pop(0) if self._sessions else []
            yield from items
            self.last_stall_info = {"kind": "external", "since_s": 0.3}  # the engine's idle loop fired

    sess = _S([[V] * 2, [V] * 2])
    control = sv._ReconnectControl()
    control.deadline = 1.0  # a deadline that has long passed
    logs = []
    gen = sv._reconnecting_frames(sess, logs.append, max_fail=3, settle=0, backoff=(0, 0, 0), control=control)
    kinds = []
    for kind, _, _ in gen:
        kinds.append(kind)
        if len(kinds) == 5:
            break
    gen.close()
    assert kinds == ["video", "video", "reconnect", "video", "video"]
    assert sess.hooks and all(h == control.due for h in sess.hooks), "the engine must be given control.due"
    assert control.deadline is None, "the wrapper must clear the deadline after acting on it"
    assert any(m.startswith("[stall] desync") and "idle deadline" in m for m in logs), logs


def test_resync_log_reports_a_per_event_drop_count(monkeypatch):
    """The 'dropped N AUs' figure must be per desync event. It used to accumulate for the
    producer's lifetime, which made a run of short self-healing bursts read as ever-longer ones."""
    script = (
        [(0.0, "video", IDR, _vfi(0, True, BASE)), (0.1, "video", PF, None)]  # hole #1
        + [(0.1, "video", PF, _vfi(2 + i, False, BASE + 200 + 100 * i)) for i in range(3)]  # 3 dropped
        + [(0.1, "video", IDR, _vfi(6, True, BASE + 700))]  # resync #1 -> 4 dropped (hole + 3)
        + [(0.1, "video", PF, _vfi(7, False, BASE + 800)), (0.1, "video", PF, None)]  # hole #2
        + [(0.1, "video", IDR, _vfi(9, True, BASE + 1000))]  # resync #2 -> 1 dropped (the hole only)
    )
    _, logs = _feed(monkeypatch, script, desync_s=0.0)
    resyncs = [m for m in logs if "resync at IDR (dropped" in m]
    assert resyncs == [
        "[clean_gop] resync at IDR (dropped 0 AUs)",  # the first IDR: the muxer starts unsynced
        "[clean_gop] resync at IDR (dropped 4 AUs)",
        "[clean_gop] resync at IDR (dropped 1 AUs)",  # would read 5 if the count still accumulated
    ], resyncs
