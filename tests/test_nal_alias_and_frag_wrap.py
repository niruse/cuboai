"""Two reader bugs reported by Fredrik (cuboai-tutk v1.0.0 / v1.1.0-rc1).

1. H.264/HEVC aliasing: judged by its first byte alone, x264's ordinary P-slice header
   0x41 is HEVC type 32 (VPS). So the codec sniffers called an H.264 P-frame "hevc"
   (poisoning the PMT on the #85 mux fallback) and the keyframe tests called it a
   keyframe (DVR replay, which has no FRAMEINFO keyframe bit, opened its start gate
   on a P-frame). A valid HEVC header is two bytes; `hevc_nal_type` checks both.

2. Fragment-seq wrap: the live reader keyed a message's fragments by the raw u16
   fragment-seq and assembled them with sorted(). An AU straddling 65535 -> 0 sorted
   its 0.. fragments first, started on a middle chunk and was dropped as non-AV.
   `_lift_frag` keys them on an unbounded counter instead.

Both tutk copies (live + playback_engine) carry the same code, so every check runs
against both.
"""

import importlib.util
import os
import sys
import types

import pytest

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")
_COPIES = {"live": _TUTK, "engine": os.path.join(_TUTK, "playback_engine")}


def _load(copy, name):
    spec = importlib.util.spec_from_file_location(f"{copy}_{name}_nalwrap", os.path.join(_COPIES[copy], f"{name}.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # @dataclass looks its module up while the class is built
    sys.path.insert(0, _COPIES[copy])
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.path.remove(_COPIES[copy])
    return mod


PURE = {c: _load(c, "cuboai_pure") for c in _COPIES}

SC = b"\x00\x00\x00\x01"
H264_P = SC + b"\x41\x9a\x02\x0c"  # x264 P-slice: nal_ref_idc 2, type 1
H264_IDR = SC + b"\x67\x64\x00\x1f" + SC + b"\x68\xee\x3c\x80" + SC + b"\x65\x88\x84\x00"
H264_IDR_ONLY = SC + b"\x65\x88\x84\x00"
HEVC_KF = SC + b"\x40\x01\x0c\x01" + SC + b"\x42\x01\x01\x01" + SC + b"\x44\x01\xc1\x72" + SC + b"\x26\x01\xaf\x00"
HEVC_IDR_ONLY = SC + b"\x26\x01\xaf\x00"
HEVC_P = SC + b"\x02\x01\xd0\x00"  # TRAIL_R


@pytest.fixture(params=sorted(_COPIES))
def cp(request):
    return PURE[request.param]


def test_hevc_header_needs_both_bytes(cp):
    assert cp.hevc_nal_type(H264_P) is None, "0x41 is an H.264 P-slice, not an HEVC VPS"
    assert cp.hevc_nal_type(H264_IDR_ONLY) is None
    assert cp.hevc_nal_type(SC + b"\x67\x64") is None  # H.264 SPS
    assert cp.hevc_nal_type(HEVC_KF) == 32
    assert cp.hevc_nal_type(HEVC_IDR_ONLY) == 19
    assert cp.hevc_nal_type(HEVC_P) == 1
    assert cp.hevc_nal_type(SC + b"\x40") is None  # truncated header
    assert cp.hevc_nal_type(SC + b"\x40\x00") is None  # TemporalId+1 == 0 is forbidden
    assert cp.hevc_nal_type(SC + b"\x40\x09") is None  # nuh_layer_id 1
    # A later slice of a multi-slice H.264 frame: first_mb_in_slice != 0 can leave byte 1
    # small, so only the layer bit of byte 0 (set on every odd H.264 type) rejects it.
    assert cp.hevc_nal_type(SC + b"\x41\x01") is None


def test_detect_video_codec_does_not_call_an_h264_p_frame_hevc(cp):
    # default=None is the live #85 mux fallback: an undecidable AU must WAIT, not guess.
    assert cp.detect_video_codec(H264_P, default=None) is None
    assert cp.detect_video_codec(H264_IDR, default=None) == "h264"
    assert cp.detect_video_codec(HEVC_KF, default=None) == "hevc"
    assert cp.detect_video_codec(HEVC_IDR_ONLY, default=None) == "hevc"
    assert cp.detect_video_codec(HEVC_P, default=None) is None


def test_hevc_keyframe_test_rejects_an_h264_p_frame(cp):
    assert not cp._is_video_keyframe(H264_P, "hevc")
    assert cp._is_video_keyframe(HEVC_KF, "hevc")
    assert cp._is_video_keyframe(HEVC_IDR_ONLY, "hevc")
    assert not cp._is_video_keyframe(HEVC_P, "hevc")


def _nested(module, outer, inner, **free):
    """Build the nested function `inner` of `module.outer` from its real code object,
    binding its free variables (closure cells) from `free`."""
    code = next(
        c for c in getattr(module, outer).__code__.co_consts if isinstance(c, types.CodeType) and c.co_name == inner
    )
    cells = tuple(types.CellType(free[name]) for name in code.co_freevars)
    return types.FunctionType(code, vars(module), inner, None, cells)


def _playback_outer(mod):
    return next(
        name
        for name, fn in vars(mod).items()
        if isinstance(fn, types.FunctionType)
        and any(isinstance(c, types.CodeType) and c.co_name == "_nal_kf" for c in fn.__code__.co_consts)
    )


@pytest.mark.parametrize("copy", sorted(_COPIES))
def test_dvr_keyframe_test_rejects_an_h264_p_frame(copy):
    pb = _load(copy, "cuboai_playback")
    nal_kf = _nested(pb, _playback_outer(pb), "_nal_kf", cp=PURE[copy])
    assert not nal_kf(H264_P), "every H.264 P-frame was a 'keyframe' on DVR replay"
    assert nal_kf(H264_IDR) and nal_kf(H264_IDR_ONLY)
    assert nal_kf(HEVC_KF) and nal_kf(HEVC_IDR_ONLY)
    assert not nal_kf(HEVC_P)


def test_dvr_codec_sniff_calls_an_h264_p_frame_h264():
    pb = _load("engine", "cuboai_playback")
    sniff = _nested(pb, _playback_outer(pb), "_sniff_codec", cp=PURE["engine"])
    assert sniff(H264_P, {}) == "h264"
    assert sniff(HEVC_KF, None) == "hevc"
    assert sniff(H264_P, {"codec": "hevc"}) == "hevc"  # FRAMEINFO still wins


@pytest.mark.parametrize("copy", sorted(_COPIES))
def test_live_ts_keyframe_test_rejects_an_h264_p_frame(copy):
    sv = _load(copy, "cuboai_stream_video")
    outer = next(
        name
        for name, fn in vars(sv).items()
        if isinstance(fn, types.FunctionType)
        and any(isinstance(c, types.CodeType) and c.co_name == "_nal_kf" for c in fn.__code__.co_consts)
    )
    nal_kf = _nested(sv, outer, "_nal_kf", hevc_nal_type=PURE[copy].hevc_nal_type)
    assert not nal_kf(H264_P)
    assert nal_kf(HEVC_KF) and nal_kf(HEVC_IDR_ONLY)


def test_no_one_byte_hevc_test_is_left():
    """Guard: the one-byte form `((b >> 1) & 0x3f) in (32, ...` must not come back."""
    import re

    pat = re.compile(r"\(\(\w+(?:\[\d\])? >> 1\) & 0x3[fF]\) in \(")
    hits = []
    for root in _COPIES.values():
        for name in ("cuboai_pure.py", "cuboai_playback.py", "cuboai_stream_video.py"):
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                hits += [f"{root}/{name}:{n}" for n, line in enumerate(fh, 1) if pat.search(line)]
    assert not hits, hits


def _feed(cp, frags, gmax=None):
    """Window `frags` like the reader: seed on the first, then _lift_frag. Returns the keys."""
    keys = []
    for f in frags:
        if gmax is None:
            gmax = key = f
        else:
            key, gmax = cp._lift_frag(f, gmax)
        keys.append(key)
    return keys, gmax


def test_au_straddling_the_wrap_assembles_start_code_first(cp):
    chunks = {65534: SC + b"\x41\x9a", 65535: b"BB", 0: b"CC", 1: b"DD"}
    keys, _ = _feed(cp, [65533, 65534, 65535, 0, 1])
    fm = dict(zip(keys[1:], [chunks[f] for f in (65534, 65535, 0, 1)]))
    blob = b"".join(fm[k] for k in sorted(fm))
    assert blob == SC + b"\x41\x9aBBCCDD", "the AU must start with its start code"
    assert keys == [65533, 65534, 65535, 65536, 65537]


def test_retransmit_across_the_wrap_lands_in_place(cp):
    keys, gmax = _feed(cp, [65534, 0, 1, 65535])  # 65535 arrives late
    assert keys == [65534, 65536, 65537, 65535]
    assert gmax == 65537
    keys, gmax = _feed(cp, [65535, 1, 2, 0])  # 0 arrives late, after the wrap
    assert keys == [65535, 65537, 65538, 65536], "a late post-wrap fragment must not sort first"


def test_below_the_wrap_keys_equal_the_wire_value(cp):
    frags = [100, 101, 103, 102, 104, 300]
    keys, _ = _feed(cp, frags)
    assert keys == frags


def test_out_of_band_fragment_is_dropped(cp):
    assert cp._lift_frag(5000, 100) == (None, 100)  # far forward
    assert cp._lift_frag((100 - 129) & 0xFFFF, 100) == (None, 100)  # too far behind
    assert cp._lift_frag(100 + 256, 100) == (356, 356)  # window edges stay as before
    assert cp._lift_frag(100 + 257, 100) == (None, 100)
    assert cp._lift_frag(100 - 128, 100) == (-28, 100)


@pytest.mark.parametrize("copy", sorted(_COPIES))
def test_live_reader_stores_fragments_under_the_lifted_key(copy):
    """The reader loop itself is a large closure; pin that it windows through _lift_frag and
    stores under the lifted key, not the raw u16 wire value."""
    with open(os.path.join(_COPIES[copy], "cuboai_pure.py"), encoding="utf-8") as fh:
        src = fh.read()
    assert "fkey, gmax = _lift_frag(frag, gmax)" in src
    assert "msgs.setdefault(idx, {})[fkey] = chunk" in src
    assert "msgs.setdefault(idx, {})[frag] = chunk" not in src
