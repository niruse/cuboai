"""`_LiveAacEncoder` turns a microphone's PCM into camera AAC frames as it arrives.

The Home Assistant websocket microphone sends 16 kHz s16le mono. go2rtc's
backchannel sends 8 kHz A-law. Both must come out in the one format the camera
is proven to accept: AAC-LC, 16 kHz, ADTS channel config 2 (what the
text-to-speech path sends). The frames carry the hand-built `_adts_header`, because a
live encoder cannot wait on libav's muxer to buffer. Pipe reads are not
sample-aligned: an s16 sample split across two reads must be joined, or every
later sample decodes from the wrong byte pair and plays as loud noise.

Every test names the mutation it kills.
"""

import importlib.util
import io
import math
import os
import struct

import pytest

av = pytest.importorskip("av")

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")
_spec = importlib.util.spec_from_file_location("live_pure_encoder", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)

TONE_HZ = 440


def _check_adts(units):
    """Every unit is one ADTS frame in the camera's format: AAC-LC, 16 kHz, channel config 2."""
    assert units, "no AAC frames produced"
    for u in units:
        assert u[0] == 0xFF and (u[1] & 0xF6) == 0xF0, "not an ADTS sync word"
        assert u[1] & 0x01, "protection_absent must be set (7-byte header, no CRC)"
        assert (u[2] >> 6) == 1, "profile must be AAC-LC"
        assert (u[2] >> 2) & 0x0F == 8, "sampling index must be 8 (16 kHz)"
        assert ((u[2] & 0x01) << 2) | (u[3] >> 6) == 2, "channel config must be 2 (what the camera accepts)"
        assert ((u[3] & 0x03) << 11) | (u[4] << 3) | (u[5] >> 5) == len(u), "ADTS frame length mismatch"


def _decode(units):
    """ADTS units -> (left, right) float samples."""
    left, right = [], []
    with av.open(io.BytesIO(b"".join(units)), format="adts") as c:
        for f in c.decode(audio=0):
            chans = [struct.unpack(f"<{f.samples}f", bytes(p)[: 4 * f.samples]) for p in f.planes]
            left += chans[0]
            right += chans[-1]
    return left, right


def _libav_adts_units(secs=2.0, rate=16000, hz=TONE_HZ):
    """A tone through libav's own AAC encoder and ADTS muxer (the framing the text-to-speech path
    gets), split back into frames by their header lengths."""
    buf = io.BytesIO()
    with av.open(buf, "w", format="adts") as out:
        st = out.add_stream("aac", rate=rate)
        st.layout = "stereo"
        n = int(secs * rate)
        for i in range(0, n, 1024):
            fr = av.AudioFrame(format="fltp", layout="stereo", samples=1024)
            vals = struct.pack("<1024f", *(0.25 * math.sin(2 * math.pi * hz * (i + k) / rate) for k in range(1024)))
            for plane in fr.planes:
                plane.update(vals)
            fr.sample_rate = rate
            fr.pts = i
            for pkt in st.encode(fr):
                out.mux(pkt)
        for pkt in st.encode(None):
            out.mux(pkt)
    data, units, i = buf.getvalue(), [], 0
    while i + 7 <= len(data):
        n = ((data[i + 3] & 0x03) << 11) | (data[i + 4] << 3) | (data[i + 5] >> 5)
        units.append(data[i : i + n])
        i += n
    return units


def _s16(secs, rate, hz=TONE_HZ):
    return b"".join(
        struct.pack("<h", int(8000 * math.sin(2 * math.pi * hz * i / rate))) for i in range(int(secs * rate))
    )


def _alaw(pcm16, rate):
    """s16 mono -> G.711 A-law bytes, through libav's own encoder."""
    enc = av.CodecContext.create("pcm_alaw", "w")
    enc.sample_rate = rate
    enc.layout = "mono"
    enc.format = "s16"
    enc.open()
    fr = av.AudioFrame(format="s16", layout="mono", samples=len(pcm16) // 2)
    fr.planes[0].update(pcm16)
    fr.sample_rate = rate
    fr.pts = 0
    return b"".join(bytes(p) for p in enc.encode(fr)) + b"".join(bytes(p) for p in enc.encode(None))


def _feed(enc, data, sizes):
    """Feed `data` in chunks cycling through `sizes`, as a pipe hands it over."""
    out, i, k = [], 0, 0
    while i < len(data):
        n = sizes[k % len(sizes)]
        out += enc.feed(data[i : i + n])
        i += n
        k += 1
    return out


def _tone_stats(units):
    """(zero crossings per second, RMS) of the decoded left channel, past the encoder's warm-up."""
    left, _ = _decode(units)
    seg = left[2000:14000]
    crossings = sum(1 for a, b in zip(seg, seg[1:]) if (a < 0) != (b < 0))
    return crossings / (len(seg) / 16000), math.sqrt(sum(v * v for v in seg) / len(seg))


def test_adts_header_is_byte_identical_to_libav_muxer():
    """Every frame libav's ADTS muxer writes (the framing of the proven TTS path) must carry
    exactly the header `_adts_header` builds for it. Kill: any header bit changed (profile,
    sampling index, channel config, the length split across bytes 3-5, buffer fullness)."""
    units = _libav_adts_units()
    assert len(units) >= 30
    for u in units:
        assert cp._adts_header(len(u) - 7, 16000, 2) == u[:7], u[:7].hex()


@pytest.mark.parametrize("payload", [0, 255, 2040, 5000, 8184])
def test_adts_header_length_survives_every_byte_boundary(payload):
    """The 13-bit frame length spans three bytes. Kill: a shift or mask off by one bit there."""
    h = cp._adts_header(payload, 16000, 2)
    assert ((h[3] & 0x03) << 11) | (h[4] << 3) | (h[5] >> 5) == payload + 7


@pytest.mark.parametrize("in_rate", [16000, 48000, 44100])
def test_s16_microphone_becomes_camera_frames_that_decode_back(in_rate):
    """One second of s16 mono at the browser's rate -> ~15.6 frames of 1024 samples, less the
    encoder's one-frame delay. Kill: `in_codec` or `in_rate` ignored (A-law or 8 kHz decoding of
    s16 bytes is noise at the wrong pitch), or the output leaving the camera's format."""
    enc = cp._LiveAacEncoder(in_rate=in_rate, in_codec="pcm_s16le")
    units = _feed(enc, _s16(1.0, in_rate), [int(in_rate * 0.04) * 2])
    _check_adts(units)
    assert 13 <= len(units) <= 15, len(units)
    crossings, rms = _tone_stats(units)
    assert abs(crossings - 2 * TONE_HZ) < 20, crossings
    assert 0.10 < rms < 0.14, rms  # a 0.244-peak tone, upmixed at -3 dB


@pytest.mark.parametrize("sizes", [[1], [333, 1279, 7, 1]], ids=["one-byte-reads", "odd-reads"])
def test_unaligned_reads_give_the_same_frames(sizes):
    """A pipe read can end mid-sample. The odd byte must wait for its partner. Kill: dropping
    the carry (the byte is lost, every later sample is misaligned: noise, different frames)."""
    pcm = _s16(1.0, 16000)
    aligned = _feed(cp._LiveAacEncoder(in_rate=16000, in_codec="pcm_s16le"), pcm, [1280])
    unaligned = _feed(cp._LiveAacEncoder(in_rate=16000, in_codec="pcm_s16le"), pcm, sizes)
    assert unaligned == aligned


def test_alaw_8k_stays_the_default():
    """go2rtc's backchannel mode builds the encoder with no arguments and feeds A-law.
    Kill: the default switched to s16 or 16 kHz."""
    enc = cp._LiveAacEncoder()
    assert (enc.in_codec, enc.in_rate, enc.bytes_per_sec) == ("pcm_alaw", 8000, 8000)
    units = _feed(enc, _alaw(_s16(1.0, 8000), 8000), [320])
    _check_adts(units)
    crossings, _ = _tone_stats(units)
    assert abs(crossings - 2 * TONE_HZ) < 20, crossings


@pytest.mark.parametrize("codec", ["pcm_mulaw", "aac", "pcm_f32le"])
def test_other_input_codecs_are_refused(codec):
    """Kill: the codec check removed (any libav decoder name would be accepted and the pipe
    alignment would be wrong for it)."""
    with pytest.raises(ValueError):
        cp._LiveAacEncoder(in_codec=codec)


def test_bytes_per_sec_sizes_the_reads():
    """The stdin reader takes 40 ms per read from this. Kill: s16 counted as one byte a sample."""
    assert cp._LiveAacEncoder(in_rate=16000, in_codec="pcm_s16le").bytes_per_sec == 32000
    assert cp._LiveAacEncoder(in_rate=48000, in_codec="pcm_s16le").bytes_per_sec == 96000


def test_silent_unit_is_digital_silence_even_from_dirty_frame_memory(monkeypatch):
    """The gap filler plays between words, many times a second. A new AudioFrame is
    UNINITIALISED memory: simulate the box, where it can hold NaN.
    Kill: the priming frames no longer zeroed (EINVAL from the encoder, or noise between words)."""
    real = av.AudioFrame

    def nan_frame(*a, **kw):
        fr = real(*a, **kw)
        for plane in fr.planes:
            plane.update(b"\xff" * plane.buffer_size)
        return fr

    monkeypatch.setattr(av, "AudioFrame", nan_frame)
    silent = cp._LiveAacEncoder(in_rate=16000, in_codec="pcm_s16le").silent_unit
    monkeypatch.undo()
    _check_adts([silent])
    left, right = _decode([silent] * 4)
    assert left and max(abs(v) for v in left + right) < 1e-4
