"""The TransCodePartial tail `Swap` must be applied on the data channel.

`custom_components/cuboai/tutk/cuboai_pure.py` (the engine every LIVE surface
uses — streaming, talkback, and every IOCTL GET/SET behind the switches,
selects, lights, numbers and the coordinator poll) carried an older revision of
`transcode`/`inv_transcode` that treated the partial-block tail as a plain XOR
and explicitly warned against re-adding the `Swap` permutation.

That is wrong for the data channel. Native TUTK applies `Swap` to the tail for
tail lengths 2/4/8 (identity everywhere else), so every data-channel frame whose
length & 0xF is 2, 4 or 8 was built and parsed with a mangled tail — e.g. the
216-byte lullaby-schedule SET (tail 8), whose duration field lives in the tail.

`custom_components/cuboai/tutk/playback_engine/cuboai_pure.py` — the copy the
DVR playback process already ships — has the corrected implementation, verified
byte-for-byte against the native library via ctypes. It is used here as the
oracle: the two copies must now agree exactly.
"""

import importlib.util
import os
import random

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


live = _load("live_pure", os.path.join(_TUTK, "cuboai_pure.py"))
oracle = _load("oracle_pure", os.path.join(_TUTK, "playback_engine", "cuboai_pure.py"))


def _samples():
    rnd = random.Random(20260903)
    for n in range(0, 97):
        yield bytes(rnd.randrange(256) for _ in range(n))
    for n in (216, 598, 88, 76, 52, 24):  # real frame sizes on this protocol
        yield bytes(rnd.randrange(256) for _ in range(n))


def test_tail_swap_helper_matches_oracle():
    assert live._TAIL_SWAP == oracle._TAIL_SWAP
    for length in range(0, 17):
        buf = bytes(range(length))
        assert live._tail_swap(buf, length) == oracle._tail_swap(buf, length)


def test_tail_swap_is_an_involution():
    for length in (2, 4, 8):
        buf = bytes(range(length))
        assert live._tail_swap(live._tail_swap(buf, length), length) == buf


def test_transcode_matches_oracle_byte_for_byte():
    for plain in _samples():
        assert live.transcode(plain) == oracle.transcode(plain), f"len={len(plain)}"
        assert live.transcode(plain, swap_tail=False) == oracle.transcode(plain, swap_tail=False)


def test_inv_transcode_matches_oracle_byte_for_byte():
    for wire in _samples():
        assert live.inv_transcode(wire) == oracle.inv_transcode(wire), f"len={len(wire)}"


def test_round_trip_is_symmetric():
    for plain in _samples():
        assert live.inv_transcode(live.transcode(plain)) == plain, f"len={len(plain)}"


def test_tail_2_4_8_actually_changed():
    """Guard against a silent revert: for tails 2/4/8 the new wire MUST differ
    from the old plain-XOR encoding, otherwise the fix is not in effect."""
    changed = 0
    for plain in _samples():
        n = len(plain)
        tl = n & 0xF
        full = n - tl
        legacy = bytearray(live.transcode(plain))
        for i in range(full, n):
            legacy[i] = live._K16[i - full] ^ plain[i]  # rebuild the OLD tail
        if tl in (2, 4, 8) and bytes(legacy[full:]) != live.transcode(plain)[full:]:
            changed += 1
    assert changed > 0, "tail Swap is not being applied — fix reverted?"


def test_search_frames_are_unchanged_by_the_fix():
    """The pre-session SEARCH/broadcast frames must keep the NO-Swap tail.

    This is the exact regression the old docstring feared ("it corrupts the
    probe tail and breaks connect"). Pinning them against the oracle proves the
    data-channel fix does not touch the frames that establish the session.
    """
    uid = b"BRR9ZN8XT17UZBWT111A"
    for R in (0x0000, 0x1234, 0xFFFF):
        assert live.build_probe(uid, R) == oracle.build_probe(uid, R)
        assert live.build_ack(uid, R) == oracle.build_ack(uid, R)
        assert live.build_lan_query(uid, R) == oracle.build_lan_query(uid, R)
        # 88-byte probe/ack have tail 8 — but as SEARCH frames they stay un-Swapped.
        assert len(live.build_probe(uid, R)) & 0xF == 8


if __name__ == "__main__":  # runnable without pytest
    for fn in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[fn]()
        print("ok:", fn)
