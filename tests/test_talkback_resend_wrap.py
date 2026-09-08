"""Talkback loss-recovery must keep resolving resend requests past the u16 wrap.

Two-way talk (`cuboai_stream_backchannel.py` -> `send_audio`) keeps every frame
it has sent in `sent_buf`, keyed by `talk_frag`. `talk_frag` is deliberately
MONOTONIC and unbounded — it must not restart when a looped file wraps its
content, or the camera rejects the replayed frames as already-seen.

The camera's 0x09 resend request, however, names frames with a u16:
`frag = (C + entry) & 0xFFFF`. Past 65536 frames — at 64 ms/frame that is about
70 minutes of continuous or looping talkback — every `sent_buf.get(frag)`
misses, and talkback loss-recovery silently stops. Nothing errors: the wire
stays well-formed, `resends_sent` simply stops rising.

The fix lifts the u16 BACKWARD into `talk_frag`'s space. A resend request always
names a frame we already sent, so the nearest congruent value at-or-below the
current frag is the right one. Below the wrap it is the identity.

`tutk/playback_engine/cuboai_pure.py` already carries this; this pins the live
copy. Same bug class as test_dataack_wrap.py.
"""

import importlib.util
import os
import re
import struct

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")
_SRC = os.path.join(_TUTK, "cuboai_pure.py")

_spec = importlib.util.spec_from_file_location("live_pure_talk", _SRC)
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def test_identity_below_the_wrap():
    """Below 65536 the lift must change nothing, so existing wire behaviour is untouched."""
    for cur in (0, 1, 999, 65535):
        for idx in range(0, min(cur, 300) + 1):
            assert cp._unwrap_index_back(idx, cur) == idx, (idx, cur)


def test_resolves_across_the_wrap():
    cur = 70000  # ~74 min of talkback
    # the camera asks for frame 69990, which on the wire is 69990 % 65536
    assert cp._unwrap_index_back(69990 % 65536, cur) == 69990
    # the frame right at the wrap boundary
    assert cp._unwrap_index_back(65536 % 65536, cur) == 65536
    # and the current frame itself
    assert cp._unwrap_index_back(cur % 65536, cur) == cur


def test_always_returns_a_value_at_or_below_cur():
    """A resend always names an already-sent frame, so the lift must never point ahead."""
    for cur in (0, 65535, 65536, 65537, 131071, 200000):
        for wire in (0, 1, 32767, 32768, 65534, 65535):
            got = cp._unwrap_index_back(wire, cur)
            assert got <= cur, (wire, cur, got)
            assert (got - wire) % 65536 == 0, (wire, cur, got)
            assert cur - got < 65536, (wire, cur, got)


def _sack(C, entries):
    """A camera 0x09 resend-request frame: count at [42:44], C at [36:38], entries from [50:]."""
    dec = bytearray(52 + 2 * len(entries))
    struct.pack_into("<H", dec, 36, C & 0xFFFF)
    struct.pack_into("<H", dec, 42, len(entries))
    for i, e in enumerate(entries):
        struct.pack_into("<H", dec, 50 + 2 * i, e & 0xFFFF)
    return bytes(dec)


def _lookup(dec, sent_buf, talk_frag, use_fix):
    """The exact decode + lookup the SACK branch of send_audio performs."""
    hits = []
    cnt = struct.unpack_from("<H", dec, 42)[0]
    C = struct.unpack_from("<H", dec, 36)[0]
    if 0 < cnt < 256 and C != 0xFFFF:
        for k in range(min(cnt, (len(dec) - 50) // 2)):
            frag = (C + struct.unpack_from("<H", dec, 50 + 2 * k)[0]) & 0xFFFF
            if use_fix:
                frag = cp._unwrap_index_back(frag, talk_frag)
            if sent_buf.get(frag) is not None:
                hits.append(frag)
    return hits


def test_lookup_misses_without_the_fix_and_hits_with_it():
    """The concrete failure: ~70 minutes in, the camera asks for three recent
    frames and the legacy u16 lookup resolves none of them."""
    talk_frag = 70000
    sent_buf = dict.fromkeys(range(talk_frag - 128, talk_frag), b"au")  # the real 128-frame buffer
    wanted = [69995, 69996, 69997]
    C = wanted[0] & 0xFFFF
    dec = _sack(C, [w - wanted[0] for w in wanted])

    assert _lookup(dec, sent_buf, talk_frag, use_fix=False) == [], "legacy lookup should miss (the bug)"
    assert _lookup(dec, sent_buf, talk_frag, use_fix=True) == wanted


def test_lookup_is_unchanged_below_the_wrap():
    talk_frag = 5000
    sent_buf = dict.fromkeys(range(talk_frag - 128, talk_frag), b"au")
    wanted = [4990, 4991]
    dec = _sack(wanted[0], [w - wanted[0] for w in wanted])
    assert _lookup(dec, sent_buf, talk_frag, use_fix=False) == wanted
    assert _lookup(dec, sent_buf, talk_frag, use_fix=True) == wanted


def test_source_still_applies_the_lift_at_the_lookup():
    """Pin the call site: the model above only proves the arithmetic, so make sure
    the shipped SACK branch actually performs the lift before sent_buf.get()."""
    src = open(_SRC, encoding="utf-8").read()
    m = re.search(r"frag = \(C \+ struct\.unpack_from.*?au = sent_buf\.get\(frag\)", src, re.S)
    assert m, "the talkback SACK-replay lookup could not be located"
    assert "_unwrap_index_back(frag, talk_frag)" in m.group(0), (
        "the SACK-replay lookup no longer lifts the u16 into talk_frag's space"
    )


if __name__ == "__main__":
    for fn in sorted(k for k in dict(globals()) if k.startswith("test_")):
        globals()[fn]()
        print("ok:", fn)
