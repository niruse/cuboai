"""DVR replay (tutk/playback_engine): parse extension fragments, and ACK channel N from the real edge.

1. PlaybackReader.feed() dropped every byte[29] & 0x08 fragment as a "secondary low-rate
   sub-stream, band ~5120+". That band is the misread: with the 8-byte header extension the
   standard offsets read (group size << 8 | position), 0x14xx/0x19xx, as the frag-seq. On a live
   DVR replay all flagged fragments fit the extended layout; dropping them cost 54 resend requests
   per replayed minute, parsing them 0. CUBOAI_AV_EXT=0 restores the old drop.
2. _reader_loop's non-SACK channel-N ACK seeded C=0xFFFF on its first round (C far above D) and
   then echoed the previous round's D. C is now the contiguous edge (frag_una - 1), with the same
   C=D fallback compute_channel_sack uses when nothing contiguous is known yet.
"""

import importlib.util
import os
import queue
import socket
import sys
import threading
import time
import types

import pytest

from tests._stream_rig import au_parts, extended, plain_frag

_ENGINE = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk", "playback_engine")


def _load(name, fname):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_ENGINE, fname))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_cp = _load("dvr_engine_pure_under_test", "cuboai_pure.py")
_pb = _load("dvr_engine_playback_under_test", "cuboai_playback.py")


@pytest.fixture
def pb(monkeypatch):
    # cuboai_playback imports its engine flat (`import cuboai_pure`); pin it to the DVR copy.
    monkeypatch.setitem(sys.modules, "cuboai_pure", _cp)
    return _pb


def _feed(pb, monkeypatch, ext_on, n_aus=60, every=9):
    monkeypatch.setenv("CUBOAI_AV_EXT", "1" if ext_on else "0")
    r, out, f = pb.PlaybackReader(0), [], 0
    for i in range(n_aus):
        for p in au_parts(i):
            d = plain_frag(i, f, p)
            r.feed(extended(d) if f % every == 5 else d, out)
            f += 1
    return r, out


def test_dvr_reader_parses_extension_fragments(pb, monkeypatch):
    r_on, out_on = _feed(pb, monkeypatch, True)
    r_off, out_off = _feed(pb, monkeypatch, False)
    assert r_off.sub2 > 20 and r_off.ext_parsed == 0
    assert r_on.ext_parsed == r_off.sub2 and r_on.sub2 == 0
    assert len(out_on) > len(out_off)


class _StubReader:
    def __init__(self, frag_hi, frag_una):
        self.frag_hi, self.frag_una = frag_hi, frag_una
        self.frames = self.frames_video = self.frames_audio = 0
        self.dropped = self.honored = self.sub2 = self.ext_parsed = 0
        self.req_frags = set()


def _first_ack(pb, frag_hi, frag_una):
    calls, orig = [], pb.build_channel_ack

    def spy(inner, channel, C, D, **kw):
        calls.append((C, D))
        return orig(inner, channel, C, D, **kw)

    sess = pb.PlaybackSession.__new__(pb.PlaybackSession)
    cam = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cam.bind(("127.0.0.1", 0))
    cli = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    cli.bind(("127.0.0.1", 0))
    cli.setblocking(False)
    sess.cp, sess.channel, sess.reader = None, 1, _StubReader(frag_hi, frag_una)
    sess.inner = types.SimpleNamespace(
        _sock=cli, _cam=cam.getsockname(), _send_ack=lambda: None, _seq=0, _relseq=0, _R=0x1234
    )
    sess._hole_req, sess.use_sack, sess.arm = {}, False, False
    sess._cam_clock_N = sess._cam_clock_ts_N = None
    sess._nak0a_seen = sess._feed_errors = 0
    sess._seq_N, sess._relseq_N, sess._ackord_N = 4, 0, 0
    sess.target, sess.engage_tol_s = None, 5
    sess.reopen_interval = sess.ack0_interval = sess.nak_interval = 1e12
    sess.ackN_interval = 0
    sess._out, sess.stats = queue.Queue(maxsize=600), {}
    sess._stop_evt = threading.Event()
    sess._log = lambda m: None
    pb.build_channel_ack = spy
    try:
        th = threading.Thread(target=sess._reader_loop, daemon=True)
        th.start()
        deadline = time.time() + 2.0
        while not calls and time.time() < deadline:
            time.sleep(0.01)
        sess._stop_evt.set()
        th.join(2.0)
    finally:
        pb.build_channel_ack = orig
        cam.close()
        cli.close()
    return calls[0] if calls else None


def test_first_channel_n_ack_uses_the_contiguous_edge(pb):
    assert _first_ack(pb, frag_hi=120, frag_una=101) == (100, 120)


def test_first_channel_n_ack_falls_back_to_c_equals_d(pb):
    assert _first_ack(pb, frag_hi=7, frag_una=None) == (7, 7)
