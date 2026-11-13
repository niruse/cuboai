"""Fragments with byte[29] & 0x08 carry an 8-byte header extension; parse them, don't drop them.

On a 0x0C data frame with byte[29] bit 0x08 set there are 8 extra header bytes at [36:44] (the
camera's periodic 0x0a timing record — ms-clock, value, count — riding on a data fragment), and
every later field moves by 8: frag-seq [54:56], length [60:62], payload [72:]. Read with the
standard offsets the frag-seq comes out as (group size << 8 | position), 0x14xx or 0x19xx, so
~1.2 % of fragments were discarded as out-of-band and had to be re-requested. Measured on an HEVC
camera (firmware 3.0.1426): reported loss 1.3-1.6 % -> 0.05-0.17 %, resend requests 154-176/min ->
4-15/min. Stripping happens only when the length arithmetic proves the extended layout, so a camera
that never sends one is unaffected. CUBOAI_AV_EXT=0 restores the old drop.
"""

import struct

from tests._stream_rig import Reader, au_parts, extended, load_pure, plain_frag

cp = load_pure("live_pure_av_ext")


def test_strip_restores_the_standard_layout():
    plain = plain_frag(5, 41, b"\x00\x00\x00\x01" + b"\x55" * 300)
    ext = extended(plain, clock=0xBEEF)
    assert struct.unpack_from("<H", ext, 46)[0] != 41  # the misread
    out = cp._strip_av_ext(ext)
    assert out == plain


def test_strip_leaves_other_frames_alone():
    plain = plain_frag(5, 41, b"\x55" * 300)
    assert cp._strip_av_ext(plain) == plain  # no flag
    bad = bytearray(extended(plain))
    struct.pack_into("<H", bad, 60, 999)  # flag set but the length does not prove the layout
    assert cp._strip_av_ext(bytes(bad)) == bytes(bad)


def _run(monkeypatch, ext_on, n_aus=80, every=9):
    r = Reader(cp, {"CUBOAI_AV_EXT": "1" if ext_on else "0", "CUBOAI_FEC": "0"}, monkeypatch)
    f = 0
    for i in range(n_aus):
        for p in au_parts(i):
            d = plain_frag(i, f, p)
            r.send(extended(d, clock=0x4000 + f) if f % every == 5 else d)
            f += 1
    return r.finish()


def test_reader_parses_extension_fragments(monkeypatch):
    aus_on, st_on = _run(monkeypatch, True)
    aus_off, st_off = _run(monkeypatch, False)
    assert st_on["av_ext_frames"] > 50 and st_off["av_ext_frames"] == st_on["av_ext_frames"]
    assert st_on["av_ext_parsed"] is True and st_off["av_ext_parsed"] is False
    assert len(aus_on) > len(aus_off)
    assert len(aus_on) >= 60


def test_clock_is_taken_from_the_extension(monkeypatch):
    r = Reader(cp, {"CUBOAI_AV_EXT": "1"}, monkeypatch)
    f = 0
    for i in range(10):
        for p in au_parts(i):
            d = plain_frag(i, f, p)
            r.send(extended(d, clock=0x7000 + f) if f == 33 else d)
            f += 1
    r.finish(settle=0.5)
    assert r.sess._cam_clock == 0x7000 + 33


def _io_frame(n, flag=0x08):
    """A reliable IO/control response ([58:64] all zero) of n bytes."""
    d = bytearray(n)
    d[8:12] = b"\x08\x04\x12\x00"
    d[28], d[29] = 0x0C, flag
    struct.pack_into("<H", d, 52, max(0, n - 64))
    struct.pack_into("<H", d, 56, 7)
    if n >= 66:
        struct.pack_into("<H", d, 64, 0x0965)
    d[66:] = b"\xab" * (n - 66)
    return bytes(d)


def test_io_frames_are_never_stripped():
    # The layout test reads [60:62], which lies inside the IO marker [58:64] (all zero on an IO
    # frame): it can only hold at exactly 72 bytes, and len >= 76 excludes that. So no IO frame,
    # of any length, is ever cut down — including the 72-byte case.
    for n in range(64, 1200):
        f = _io_frame(n)
        assert cp.TUTKDirectSession._is_io_frame(f)
        assert cp._strip_av_ext(f) == f, n


def test_an_extended_io_frame_is_restored_not_left_misparsed():
    io = _io_frame(100, flag=0)
    ext = bytearray(io[:36] + struct.pack("<HHHH", 1, 30, 1, 0) + io[36:])
    ext[29] |= 0x08
    assert cp._strip_av_ext(bytes(ext)) == io
