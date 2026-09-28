"""`_aac_units` (the file/TTS path to the camera speaker) must encode a mono file.

On the HA box (PyAV 17.0.1 / libavcodec 62.11) a 20 s mono 8 kHz WAV raised
`avcodec_send_frame() returned 22 (Invalid argument)` in the trailing silence
padding. libav's own log named the cause: "Input contains (near) NaN/+-Inf".
The padding came from `av.AudioFrame(samples=1024)`, which is UNINITIALISED
memory, not silence: about half such frames held non-finite floats. The AAC
encoder rejects those; when they happened to be finite they played as a burst
of noise at the end of every clip instead.

The format the camera is proven to accept is pinned here too: AAC-LC, 16 kHz,
ADTS channel config 2 (stereo, PyAV's `add_stream('aac')` default).
"""

import importlib.util
import io
import math
import os
import struct
import wave

import pytest

av = pytest.importorskip("av")

_TUTK = os.path.join(os.path.dirname(__file__), "..", "custom_components", "cuboai", "tutk")
_spec = importlib.util.spec_from_file_location("live_pure_aac", os.path.join(_TUTK, "cuboai_pure.py"))
cp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cp)


def _mono_wav(path, secs=2.0, rate=8000, tone_hz=None):
    n = int(secs * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        if tone_hz is None:
            w.writeframes(bytes(2 * n))
        else:
            w.writeframes(
                b"".join(struct.pack("<h", int(8000 * math.sin(2 * math.pi * tone_hz * i / rate))) for i in range(n))
            )
    return str(path)


def _decode(units):
    """ADTS units -> (left, right) float samples."""
    left, right = [], []
    with av.open(io.BytesIO(b"".join(units)), format="adts") as c:
        for f in c.decode(audio=0):
            chans = [struct.unpack(f"<{f.samples}f", bytes(p)[: 4 * f.samples]) for p in f.planes]
            left += chans[0]
            right += chans[-1]
    return left, right


def _check_adts(units):
    assert units, "no AAC frames produced"
    for u in units:
        assert u[0] == 0xFF and (u[1] & 0xF6) == 0xF0, "not an ADTS sync word"
        assert u[1] & 0x01, "protection_absent must be set (7-byte header, no CRC)"
        assert (u[2] >> 6) == 1, "profile must be AAC-LC"
        assert (u[2] >> 2) & 0x0F == 8, "sampling index must be 8 (16 kHz)"
        assert ((u[2] & 0x01) << 2) | (u[3] >> 6) == 2, "channel config must be 2 (what the camera accepts)"
        assert ((u[3] & 0x03) << 11) | (u[4] << 3) | (u[5] >> 5) == len(u), "ADTS frame length mismatch"


@pytest.mark.parametrize("gain", [1.0, 0.5])
def test_mono_8k_input_yields_camera_format_adts(tmp_path, gain):
    units = cp._aac_units(_mono_wav(tmp_path / "silence.wav"), gain=gain)
    _check_adts(units)
    # 2 s of input + ~3.5 s of padding at 1024 samples / 16 kHz per frame
    assert 80 <= len(units) <= 95, len(units)
    assert units[0][:4] == bytes.fromhex("fff16080"), units[0][:7].hex()


@pytest.mark.parametrize("gain", [1.0, 0.5])
def test_uninitialised_frame_memory_never_reaches_the_encoder(tmp_path, monkeypatch, gain):
    """Simulate the box: every freshly allocated AudioFrame is full of NaN (0xFF bytes).
    If any padding frame is used without being zeroed, libav raises EINVAL here."""
    real = av.AudioFrame

    def nan_frame(*a, **kw):
        fr = real(*a, **kw)
        for plane in fr.planes:
            plane.update(b"\xff" * plane.buffer_size)
        return fr

    monkeypatch.setattr(av, "AudioFrame", nan_frame)
    _check_adts(cp._aac_units(_mono_wav(tmp_path / "silence.wav"), gain=gain))


def test_padding_is_digital_silence_and_the_tone_survives(tmp_path):
    left, right = _decode(cp._aac_units(_mono_wav(tmp_path / "tone.wav", tone_hz=440)))
    tail = left[-16000:] + right[-16000:]
    assert max(abs(v) for v in tail) == 0.0, "trailing padding must be silence, not leftover memory"
    seg = slice(2000, 30000)
    rms = [math.sqrt(sum(v * v for v in ch[seg]) / len(ch[seg])) for ch in (left, right)]
    # a 0.244-peak mono tone, upmixed at -3 dB into both channels
    assert all(0.10 < r < 0.14 for r in rms), rms
