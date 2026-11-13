"""A lost fragment 0 (the first after the frag-seq wrap) must be resend-requested like any other.

_compute_holes treated a contiguous edge of 0xFFFF as the "no edge yet" sentinel. But 65535 IS
the real edge when fragment 0 is the one missing, so that hole was never requested and the edge
sat there until the gap-depth cap jumped it. CUBOAI_EDGE_WRAP=0 restores the old behaviour.

A responsive fake camera resends whatever the engine's 0x09 resend request lists.
"""

import importlib.util
import os
import struct
import time

from tests._stream_rig import Reader, au_parts, load_pure, plain_frag

cp = load_pure("live_pure_edge_wrap")
_spec = importlib.util.spec_from_file_location(
    "live_sv_edge_wrap",
    os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk", "cuboai_stream_video.py"),
)
_sv = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_sv)


def _run(monkeypatch, start, drop, edge_wrap="1"):
    env = {k: v for k, v in _sv.PRODUCTION_ENV.items() if "STALL" not in k and "FIRST_AU" not in k}
    env["CUBOAI_EDGE_WRAP"] = edge_wrap
    r = Reader(cp, env, monkeypatch)
    r.cam.setblocking(False)
    sent, resent = {}, set()

    def service():
        while True:
            try:
                raw, _ = r.cam.recvfrom(4096)
            except OSError:
                return
            d = cp.inv_transcode(raw)
            if len(d) >= 52 and d[28] == 0x09:
                c, cnt = struct.unpack_from("<H", d, 36)[0], struct.unpack_from("<H", d, 42)[0]
                if cnt >= 2 and len(d) >= 50 + 2 * cnt:  # the camera ignores count < 2
                    for k in range(cnt):
                        fr = (c + struct.unpack_from("<H", d, 50 + 2 * k)[0]) & 0xFFFF
                        if fr in sent and fr not in resent:
                            resent.add(fr)
                            r.send(sent[fr])

    f, i = start, 0
    for _ in range(120):
        for p in au_parts(i):
            d = plain_frag(i, f & 0xFFFF, p)
            sent[f & 0xFFFF] = d
            if (f & 0xFFFF) != drop:
                r.send(d)
            f += 1
        i += 1
        service()
    for _ in range(20):
        time.sleep(0.05)
        service()
    _aus, st = r.finish(settle=0.6)
    return st, resent


def test_lost_fragment_zero_is_requested(monkeypatch):
    st, resent = _run(monkeypatch, 65536 - 300, 0)
    assert 0 in resent
    assert st["gap_cap_jumps"] == 0


def test_edge_wrap_off_never_requests_it(monkeypatch):
    st, resent = _run(monkeypatch, 65536 - 300, 0, edge_wrap="0")
    assert 0 not in resent


def test_other_losses_at_the_wrap_still_recovered(monkeypatch):
    for drop in (65535, 1):
        _st, resent = _run(monkeypatch, 65536 - 300, drop)
        assert drop in resent
