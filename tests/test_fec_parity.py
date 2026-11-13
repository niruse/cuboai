"""Rebuild a lost fragment from the camera's parity frame instead of dropping that frame.

The camera sends its data fragments in groups (header [36:38] group number, [38] position, [39]
group size k; 20 or 25 seen) followed by ONE parity frame: position k, payload 1044 bytes, [58:64]
zero. These are the 1108-byte frames the reader dropped as "0500... status frames" (avlen > 1024).
The parity is a systematic Reed-Solomon symbol over GF(2^8), polynomial 0x11d: with every member's
bytes [44:] zero-padded to 1044, the group's data interpolated on x = 0..k-1 and evaluated at x = k.
(Verified on a recorded capture in cuboai-tutk: 274/274 parity frames match, 6350/6350 leave-one-out
rebuilds byte-exact.) One lost fragment per group is now rebuilt locally and processed as if it
had arrived. CUBOAI_FEC=0 restores the old drop.

The parity here is computed by this test's OWN GF(256) code, independent of the engine's.
"""

import struct

from tests._stream_rig import Reader, au_parts, load_pure, plain_frag

cp = load_pure("live_pure_fec")
K = 20


def _gf():
    exp, log, x = [0] * 512, [0] * 256, 1
    for i in range(255):
        exp[i], log[x] = x, i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


EXP, LOG = _gf()


def _mul(a, b):
    return 0 if a == 0 or b == 0 else EXP[LOG[a] + LOG[b]]


def _div(a, b):
    return 0 if a == 0 else EXP[(LOG[a] - LOG[b]) % 255]


def _parity(members):
    """Evaluate the interpolating polynomial through (x=i, member_i) at x=k, bytewise."""
    k = len(members)
    blocks = [m[44:].ljust(1044, b"\0") for m in members]
    out = bytearray(1044)
    for i in range(k):
        num = den = 1
        for j in range(k):
            if j != i:
                num, den = _mul(num, k ^ j), _mul(den, i ^ j)
        c = _div(num, den)
        for b in range(1044):
            if blocks[i][b]:
                out[b] ^= _mul(c, blocks[i][b])
    return bytes(out)


def _parity_frame(group, members):
    d = bytearray(64 + 1044)
    d[8:12] = b"\x08\x04\x12\x00"
    d[28] = 0x0C
    struct.pack_into("<H", d, 36, group)
    d[38] = d[39] = len(members)
    struct.pack_into("<H", d, 52, 1044)
    d[64:] = _parity(members)
    return bytes(d)


def _stream(n_aus=60):
    frames, f = [], 0
    for i in range(n_aus):
        for p in au_parts(i):
            frames.append(plain_frag(i, f, p, group_size=K))
            f += 1
    return frames


def _run(monkeypatch, fec_on, drop_every_group=3):
    frames = _stream()
    r = Reader(cp, {"CUBOAI_FEC": "1" if fec_on else "0"}, monkeypatch)
    dropped = 0
    for g in range(len(frames) // K):
        members = frames[g * K : (g + 1) * K]
        for pos, d in enumerate(members):
            if g % drop_every_group == 1 and pos == 7:
                dropped += 1
                continue
            r.send(d)
        r.send(_parity_frame(g, members))
    for d in frames[(len(frames) // K) * K :]:
        r.send(d)
    aus, st = r.finish()
    return aus, st, dropped


def test_engine_coefficients_match_an_independent_construction():
    members = [plain_frag(0, f, bytes([f]) * (50 + f)) for f in range(K)]
    co, _ = cp._fec_coefficients(K)
    block = cp._fec_recover(K, {i: m[44:] for i, m in enumerate(members) if i != 4}, _parity(members))
    assert block is not None and block[0] == 4
    assert block[1][: len(members[4]) - 44] == members[4][44:]
    assert co[:4] == [143, 174, 91, 112]  # the k=20 row observed on the real camera


def test_lost_fragments_are_rebuilt(monkeypatch):
    aus_on, st_on, dropped = _run(monkeypatch, True)
    aus_off, st_off, _ = _run(monkeypatch, False)
    assert dropped >= 5
    assert st_on["fec_recovered"] == dropped and st_on["fec_bad"] == 0
    assert st_off["fec_recovered"] == 0 and st_off["fec_parity_frames"] == st_on["fec_parity_frames"] > 0
    assert len(aus_on) > len(aus_off)


def test_damaged_parity_is_rejected_not_injected():
    s = cp.TUTKDirectSession()
    members = _stream(4)[:K]
    pf = bytearray(_parity_frame(0, members))
    pf[64 + 2] ^= 0x5A  # corrupts the rebuilt fragment's frag-seq
    for i, m in enumerate(members):
        if i != 3:
            assert s._fec_note(m) is None
    assert s._fec_note(bytes(pf)) is None
    assert s._stat_fec_bad == 1 and s._stat_fec_recovered == 0


def test_two_missing_in_a_group_rebuild_nothing():
    s = cp.TUTKDirectSession()
    members = _stream(4)[:K]
    for i, m in enumerate(members):
        if i not in (3, 9):
            s._fec_note(m)
    assert s._fec_note(_parity_frame(0, members)) is None
    assert s._stat_fec_bad == 0 and s._stat_fec_recovered == 0


def _av(frag):
    d = bytearray(68)
    struct.pack_into("<H", d, 46, frag)
    d[58:64] = b"\x01\x00\x00\x00\x00\x00"
    return bytes(d)


def test_a_rebuilt_fragment_is_not_counted_or_timed_as_a_resend():
    import time

    for rebuilt in (True, False):
        s = cp.TUTKDirectSession()
        s._grace_scale = True  # the timing path under test (other tests may leave CUBOAI_GRACE_SCALE set)
        for f in range(100):
            s._note_cam_data(_av(f))
        now = time.time()
        s._hole_req_ts[100], s._hole_first_req[100] = now, now - 0.02  # frag 100 was requested
        s._note_cam_data(_av(100), rebuilt=rebuilt)
        assert 100 in s._frag_received and 100 not in s._hole_req_ts and 100 not in s._hole_first_req
        if rebuilt:  # filled, but no resend sample (it would pull the grace estimate down) and no resend count
            assert s._stat_resend_recovered == 0 and s._rtt_n == 0
        else:
            assert s._stat_resend_recovered == 1 and s._rtt_n == 1


def test_reader_marks_rebuilt_fragments():
    import inspect

    src = inspect.getsource(cp.TUTKDirectSession._av_reader)
    assert "self._note_cam_data(dec, rebuilt=raw is None)" in src
