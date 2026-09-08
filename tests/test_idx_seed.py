"""A stream read that starts mid-session must not silently emit zero frames.

`_av_reader`'s `done_upto` is a per-reader local that starts at -1, but the
camera's AV message-index is SESSION-scoped and keeps advancing. So the FIRST
read of a session works, and any read that begins once the index is past the
`done_upto + 256` accept window rejects EVERY fragment — forever, and silently:
fragments keep arriving at full rate while zero access units come out, and no
error, gap counter or incomplete-AU counter moves.

Reachable through the legacy shim: `cuboai_transport_py.start_video()` opens a
fresh `av_frames()` iterator on each call, so calling it twice on one session
lands exactly here. `tutk/playback_engine/cuboai_pure.py` already carries the
fix (its DVR reader restores the live stream after playback and hits this on
every restore); this pins it in the live copy.

Fixture replays cannot catch this — every fixture starts at index ~0, inside
the window — so this drives the real `_av_reader` over a real UDP socketpair
with a synthetic camera whose message-index starts above the window, and
asserts the gate FLIPS the outcome.
"""

import importlib.util
import os
import queue
import socket
import struct
import threading
import time

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")

_spec = importlib.util.spec_from_file_location("live_pure_seed", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _build_av_frag(idx, frag, payload, channel=0):
    """One camera AV DATA fragment, wire-encoded. Layout per _av_reader: [28]=0x0C sub,
    [46:48]=fragment-seq, [52:54]=avlen, [56:58]=AV message-index, [58:64]!=0 (AV, not a
    reliable-IO frame), payload at [64:]."""
    dec = bytearray(64 + len(payload))
    dec[8:12] = b"\x08\x04\x12\x00"
    dec[14] = channel
    dec[28] = 0x0C
    struct.pack_into("<H", dec, 46, frag & 0xFFFF)
    struct.pack_into("<H", dec, 52, len(payload))
    struct.pack_into("<H", dec, 56, idx & 0xFFFF)
    dec[58:64] = b"\x01\x00\x00\x00\x00\x00"          # non-zero AV-unit id => not an IO frame
    dec[64:] = payload
    return cp.transcode(bytes(dec))


def _run_reader(start_idx, n_aus, seed_gate):
    """Drive the REAL _av_reader with a synthetic camera on loopback."""
    os.environ["CUBOAI_IDX_SEED"] = "1" if seed_gate else "0"
    for k in ("CUBOAI_NODROP", "CUBOAI_KF_GRACE", "CUBOAI_GRACE_SCALE", "CUBOAI_EMIT_COMPLETE"):
        os.environ.pop(k, None)
    try:
        sess = cp.TUTKDirectSession()
        sess._R = 0x1234
        sess.session_hdr = bytes(16)

        cam = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cam.bind(("127.0.0.1", 0))
        cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        cli.bind(("127.0.0.1", 0))
        cli.setblocking(False)
        sess._sock = cli
        sess._cam = cam.getsockname()

        out_q = queue.Queue(maxsize=600)
        stop_evt = threading.Event()
        th = threading.Thread(target=sess._av_reader, args=(cli, out_q, stop_evt), daemon=True)
        th.start()

        # One single-fragment video AU per message-index; +16 trailing AUs so the grace
        # window releases everything under test regardless of the configured _MSG_GRACE.
        au = b"\x00\x00\x00\x01\x26\x01\xAF" + b"\xAA" * 40   # start code + HEVC IDR_W_RADL
        for i in range(n_aus + 16):
            cam.sendto(_build_av_frag(start_idx + i, start_idx + i, au), cli.getsockname())
            time.sleep(0.002)
        time.sleep(1.2)
        stop_evt.set()
        th.join(timeout=2.0)

        got = []
        while True:
            try:
                item = out_q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                got.append(item)
        stats = sess.get_stats()
        cam.close()
        cli.close()
        return [u for kind, u, _f in got if kind == "video"], stats
    finally:
        os.environ.pop("CUBOAI_IDX_SEED", None)


def test_gate_defaults_on():
    os.environ.pop("CUBOAI_IDX_SEED", None)
    assert cp.TUTKDirectSession()._idx_seed is True


def test_fresh_session_path_is_unchanged():
    """Index starts inside the window — the normal first-read case. The seed must
    not fire, so ON and OFF have to produce byte-identical output."""
    on, _ = _run_reader(0, 24, True)
    off, _ = _run_reader(0, 24, False)
    assert len(on) > 0, "gate ON emitted no AUs on the normal path"
    assert len(off) > 0, "gate OFF emitted no AUs on the normal path"
    assert on == off, "the seed changed the fresh-session path — it must be a no-op there"


def test_mid_session_read_is_dead_without_the_fix_and_alive_with_it():
    """Index starts beyond the window — a second read of a live session."""
    off, s_off = _run_reader(5000, 24, False)
    on, _ = _run_reader(5000, 24, True)
    assert len(off) == 0, f"gate OFF should emit ZERO AUs (the bug); got {len(off)}"
    assert s_off["frags_recv"] > 0, "the failure must be silent — fragments still arrive"
    assert len(on) > 0, "gate ON should emit AUs"


def test_mid_session_output_matches_the_in_window_output():
    on_mid, _ = _run_reader(5000, 24, True)
    on_fresh, _ = _run_reader(0, 24, True)
    assert on_mid == on_fresh, "a mid-session read must yield the same AUs as a fresh one"


def test_survives_a_start_just_below_the_u16_wrap():
    on, _ = _run_reader(65530, 24, True)
    assert len(on) > 0, "gate ON emitted no AUs across the 65535->0 wrap"


if __name__ == "__main__":
    for fn in ("test_gate_defaults_on",
               "test_fresh_session_path_is_unchanged",
               "test_mid_session_read_is_dead_without_the_fix_and_alive_with_it",
               "test_mid_session_output_matches_the_in_window_output",
               "test_survives_a_start_just_below_the_u16_wrap"):
        globals()[fn]()
        print("ok:", fn)
