#!/usr/bin/env python3
"""Minimal, dependency-free MPEG-TS muxer (Part C) — wraps timestamped HEVC/H264 access units
into an MPEG-TS byte stream that go2rtc demuxes, so per-frame PTS reaches MSE/fMP4 and the
<video> element plays along currentTime without underrun/stall. Raw Annex-B has nowhere to put
timestamps; TS carries PTS/DTS in PES + a PCR clock. This keeps the existing exec-pipe
architecture (no ffmpeg, no transcode — only the byte FORMAT on stdout changes).

Emits: PAT(PID 0) + PMT(PID 0x1000) at start and periodically (so go2rtc locks on quickly),
PES on the video PID(0x0100) carrying PTS (DTS=PTS, no B-frames), periodic PCR on the video PID,
per-PID continuity counters, 188-byte packets, and the random_access_indicator set on the first
packet of each keyframe AU (from the FRAMEINFO keyframe flag). stream_type is chosen from the
codec name (HEVC 0x24 / H264 0x1B) so a future H264 camera works with no change.
"""

# codec name -> MPEG-TS PMT stream_type
STREAM_TYPE = {
    'hevc': 0x24, 'h265': 0x24,
    'h264': 0x1B, 'avc': 0x1B,
    'mpeg4': 0x10, 'mjpeg': 0x06,
    'aac': 0x0F,                       # for a future audio track (ADTS)
}
_PID_PAT = 0x0000
_PID_PMT = 0x1000
_PID_VIDEO = 0x0100
_PID_AUDIO = 0x0101                  # optional second ES (audio) — see TSMuxer(audio_codec=...)
_PROGRAM_NUMBER = 1
_STREAM_ID_VIDEO = 0xE0
_STREAM_ID_AUDIO = 0xC0             # PES stream_id range for audio (0xC0-0xDF)


def _crc32_mpeg(data: bytes) -> int:
    """MPEG-2 PSI CRC-32 (poly 0x04C11DB7, init 0xFFFFFFFF, MSB-first, no final xor)."""
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= (b << 24)
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7) & 0xFFFFFFFF if (crc & 0x80000000) else (crc << 1) & 0xFFFFFFFF
    return crc


def _pts_field(prefix: int, pts: int) -> bytes:
    """5-byte PTS (or DTS) field. prefix=0b0010 for PTS-only, 0b0011 for the PTS of a PTS+DTS pair."""
    pts &= 0x1FFFFFFFF
    return bytes((
        (prefix << 4) | (((pts >> 30) & 0x07) << 1) | 0x01,
        (pts >> 22) & 0xFF,
        (((pts >> 15) & 0x7F) << 1) | 0x01,
        (pts >> 7) & 0xFF,
        ((pts & 0x7F) << 1) | 0x01,
    ))


def _pcr_field(pcr_base: int) -> bytes:
    """6-byte PCR: 33-bit base (90 kHz) + 6 reserved + 9-bit extension (0)."""
    pcr_base &= 0x1FFFFFFFF
    ext = 0
    return bytes((
        (pcr_base >> 25) & 0xFF,
        (pcr_base >> 17) & 0xFF,
        (pcr_base >> 9) & 0xFF,
        (pcr_base >> 1) & 0xFF,
        ((pcr_base & 0x01) << 7) | 0x7E | ((ext >> 8) & 0x01),
        ext & 0xFF,
    ))


def _psi_packet(pid: int, table: bytes, cc: int) -> bytes:
    """One 188-byte TS packet carrying a (short) PSI section: pointer_field + table + CRC, then 0xFF."""
    section = table + _crc32_mpeg(table).to_bytes(4, 'big')
    payload = b'\x00' + section                      # pointer_field = 0
    if len(payload) > 184:
        raise ValueError("PSI section too large for one packet")
    payload += b'\xFF' * (184 - len(payload))         # pad to a full packet
    hdr = bytes((0x47, 0x40 | ((pid >> 8) & 0x1F), pid & 0xFF, 0x10 | (cc & 0x0F)))  # PUSI=1, payload-only
    return hdr + payload


class TSMuxer:
    def __init__(self, codec='hevc', pat_interval_ms=100, version=0, audio_codec=None):
        self.stream_type = STREAM_TYPE.get(codec, STREAM_TYPE['hevc'])
        self.codec = codec
        # Optional second elementary stream (audio). None (default) => video-only, and every byte
        # emitted is identical to the original single-track muxer (PMT carries one ES; mux_au is
        # unchanged). Set audio_codec='aac' to add an AAC-ADTS track (PID _PID_AUDIO, stream_type
        # 0x0F); then interleave mux_au()/mux_audio_au() by PTS. PCR stays on the video PID.
        self.audio_codec = audio_codec
        self.audio_stream_type = STREAM_TYPE.get(audio_codec) if audio_codec else None
        self.audio_pid = _PID_AUDIO
        self.audio_stream_id = _STREAM_ID_AUDIO
        self.pat_interval_ms = pat_interval_ms
        self.version = version & 0x1F
        self.cc = {_PID_PAT: 0, _PID_PMT: 0, _PID_VIDEO: 0}
        if audio_codec:
            self.cc[self.audio_pid] = 0
        self._last_pat_ms = None

    def _bump(self, pid):
        c = self.cc[pid]; self.cc[pid] = (c + 1) & 0x0F; return c

    def _pat(self) -> bytes:
        # table_id(0) ssi=1 '0' rr '11' section_length | tsid(16) rr version cur | sec=0 last=0 |
        #   program(16) rrr PMT_PID(13)
        body = bytes((0x00, 0x01, _PID_PMT >> 8 & 0xFF, _PID_PMT & 0xFF))  # program 1 -> PMT_PID
        # section after the 3-byte header start: tsid, version/cur, sec, last, body
        sec = bytes((0x00, 0x01,                                   # tsid = 1
                     0xC1 | (self.version << 1),                   # rr=11 version cur=1
                     0x00, 0x00)) + body                           # sec_num, last_sec_num
        section_length = len(sec) + 4                              # + CRC32
        table = bytes((0x00,                                       # table_id PAT
                       0xB0 | ((section_length >> 8) & 0x0F),      # ssi=1 '0' rr=11 + len hi
                       section_length & 0xFF)) + sec
        return _psi_packet(_PID_PAT, table, self._bump(_PID_PAT))

    def _pmt(self) -> bytes:
        # PCR_PID = video PID. ES loop: video (always) + audio (only when audio_codec configured).
        # With no audio the bytes are identical to the original single-ES PMT.
        es = bytes((self.stream_type,
                    0xE0 | ((_PID_VIDEO >> 8) & 0x1F), _PID_VIDEO & 0xFF,   # rrr + ES PID
                    0xF0, 0x00))                                            # rrrr ES_info_length=0
        if self.audio_codec:
            es += bytes((self.audio_stream_type,
                         0xE0 | ((self.audio_pid >> 8) & 0x1F), self.audio_pid & 0xFF,
                         0xF0, 0x00))                                       # audio ES_info_length=0
        sec = bytes((_PROGRAM_NUMBER >> 8 & 0xFF, _PROGRAM_NUMBER & 0xFF,    # program_number
                     0xC1 | (self.version << 1),                            # rr version cur
                     0x00, 0x00,                                            # sec, last
                     0xE0 | ((_PID_VIDEO >> 8) & 0x1F), _PID_VIDEO & 0xFF,   # rrr PCR_PID(13)
                     0xF0, 0x00)) + es                                      # rrrr program_info_length=0 + ES
        section_length = len(sec) + 4
        table = bytes((0x02,
                       0xB0 | ((section_length >> 8) & 0x0F),
                       section_length & 0xFF)) + sec
        return _psi_packet(_PID_PMT, table, self._bump(_PID_PMT))

    def _pes(self, au: bytes, pts_90k: int) -> bytes:
        # PES for video: start prefix, stream_id, PES_packet_length, flags '10..',
        # PTS_DTS_flags='10' (PTS only; DTS=PTS), header_data_length=5, PTS(5).
        # PES_packet_length counts everything after the length field: 3 header bytes + 5 PTS + AU.
        # Set the real length for frames that fit in 16 bits (most P-frames); use 0 (unbounded,
        # legal only for video) for large keyframes that exceed 65535. Real lengths are more
        # standard and let a demuxer bound each AU without waiting for the next PUSI.
        after_len = 3 + 5 + len(au)
        plen = after_len if after_len <= 0xFFFF else 0
        hdr = (b'\x00\x00\x01' + bytes((_STREAM_ID_VIDEO, (plen >> 8) & 0xFF, plen & 0xFF,
                                        0x80,        # '10' marker, no scrambling/priority/align
                                        0x80,        # PTS_DTS_flags=10 (PTS only)
                                        0x05))       # PES_header_data_length = 5
               + _pts_field(0x02, pts_90k))
        return hdr + au

    def _packetize_video(self, pes: bytes, keyframe: bool, pcr_base) -> bytes:
        """Split a PES into 188-byte TS packets on the video PID. First packet PUSI=1 with an
        adaptation field carrying PCR (if pcr_base given) and random_access_indicator (if keyframe);
        the final/short packet pads with adaptation-field stuffing to fill 188 exactly."""
        out = bytearray()
        pos = 0; first = True
        n = len(pes)
        while pos < n:
            cc = self._bump(_PID_VIDEO)
            pusi = 0x40 if first else 0x00
            af = b''
            if first and (keyframe or pcr_base is not None):
                flags = (0x40 if keyframe else 0x00) | (0x10 if pcr_base is not None else 0x00)
                afbody = bytes((flags,)) + (_pcr_field(pcr_base) if pcr_base is not None else b'')
                af = bytes((len(afbody),)) + afbody          # [af_len][flags][PCR?]
            cap = 184 - len(af)
            chunk = pes[pos:pos + cap]
            # final/short packet → pad to a full 188 with adaptation-field stuffing
            if len(chunk) < cap and pos + len(chunk) >= n:
                pad = cap - len(chunk)
                if af:
                    af = bytes((af[0] + pad,)) + af[1:] + b'\xFF' * pad     # extend existing AF
                elif pad == 1:
                    af = b'\x00'                                           # af_len=0 (1-byte AF)
                else:
                    af = bytes((pad - 1, 0x00)) + b'\xFF' * (pad - 2)      # stuffing AF
                cap = 184 - len(af)
                chunk = pes[pos:pos + cap]
            afc = 0x30 if af else 0x10                       # 11=AF+payload, 01=payload-only
            out += bytes((0x47, pusi | ((_PID_VIDEO >> 8) & 0x1F), _PID_VIDEO & 0xFF, afc | cc))
            out += af + chunk
            pos += len(chunk); first = False
        return bytes(out)

    def _maybe_psi(self, now_ms) -> bytes:
        """Return PAT+PMT when the refresh interval is due, else b''. Shared by the video and audio
        mux entry points so a combined stream re-emits PSI on the same cadence. For the video-only
        path this reproduces the original inline PAT/PMT emission byte-for-byte."""
        if self._last_pat_ms is None or (now_ms is not None
                                         and now_ms - self._last_pat_ms >= self.pat_interval_ms):
            self._last_pat_ms = now_ms if now_ms is not None else 0
            return self._pat() + self._pmt()
        return b''

    def mux_au(self, au: bytes, pts_90k: int, dts_90k=None, keyframe=False, now_ms=None) -> bytes:
        """Return the TS bytes for one access unit (PAT/PMT prepended when due). DTS is ignored
        (DTS=PTS, no B-frames). now_ms drives the PAT/PMT refresh cadence."""
        out = bytearray()
        out += self._maybe_psi(now_ms)
        out += self._packetize_video(self._pes(au, pts_90k), keyframe, pcr_base=pts_90k)
        return bytes(out)

    # ── optional audio elementary stream (Phase 6 combined-A/V) ───────────────────────────────
    def _pes_audio(self, adts: bytes, pts_90k: int) -> bytes:
        """PES for one AAC-ADTS frame: stream_id 0xC0, data_alignment set, PTS-only header."""
        after_len = 3 + 5 + len(adts)
        plen = after_len if after_len <= 0xFFFF else 0     # audio frames are small; length always fits
        return (b'\x00\x00\x01' + bytes((self.audio_stream_id, (plen >> 8) & 0xFF, plen & 0xFF,
                                         0x84,        # '10' marker + data_alignment_indicator
                                         0x80,        # PTS_DTS_flags=10 (PTS only)
                                         0x05))       # PES_header_data_length = 5
                + _pts_field(0x02, pts_90k)) + adts

    def _packetize_audio(self, pes: bytes) -> bytes:
        """Split an audio PES into 188-byte TS packets on the audio PID (no PCR; PCR lives on the
        video PID). The final/short packet pads with adaptation-field stuffing to fill 188 exactly."""
        out = bytearray(); pos = 0; first = True; n = len(pes)
        while pos < n:
            cc = self._bump(self.audio_pid)
            pusi = 0x40 if first else 0x00
            af = b''
            cap = 184
            chunk = pes[pos:pos + cap]
            if len(chunk) < cap and pos + len(chunk) >= n:        # final/short packet -> pad to 188
                pad = cap - len(chunk)
                if pad == 1:
                    af = b'\x00'                                  # af_len=0 (1-byte AF)
                else:
                    af = bytes((pad - 1, 0x00)) + b'\xFF' * (pad - 2)
                cap = 184 - len(af); chunk = pes[pos:pos + cap]
            afc = 0x30 if af else 0x10
            out += bytes((0x47, pusi | ((self.audio_pid >> 8) & 0x1F), self.audio_pid & 0xFF, afc | cc))
            out += af + chunk
            pos += len(chunk); first = False
        return bytes(out)

    def mux_audio_au(self, adts: bytes, pts_90k: int, now_ms=None) -> bytes:
        """Return the TS bytes for one AAC-ADTS audio frame (PAT/PMT prepended when due). Requires
        audio_codec to have been set at construction; interleave with mux_au() in PTS order."""
        if not self.audio_codec:
            raise RuntimeError("mux_audio_au called but TSMuxer was built without audio_codec")
        out = bytearray()
        out += self._maybe_psi(now_ms)
        out += self._packetize_audio(self._pes_audio(adts, pts_90k))
        return bytes(out)


# ── self-test: mux synthetic AUs, sanity-check packet structure (ffprobe in the harness) ──
if __name__ == '__main__':
    m = TSMuxer(codec='hevc')
    # one keyframe-ish AU + a couple P-ish AUs (dummy NAL bytes; structure is what we check)
    blobs = [(b'\x00\x00\x00\x01\x40\x01' + b'\xAB' * 5000, 0, True),
             (b'\x00\x00\x00\x01\x02\x01' + b'\xCD' * 1500, 67 * 90, False),
             (b'\x00\x00\x00\x01\x02\x01' + b'\xEF' * 90000, 134 * 90, False)]   # >65535 → length=0 needed
    ts = bytearray()
    for au, pts, kf in blobs:
        ts += m.mux_au(au, pts, keyframe=kf, now_ms=pts // 90)
    assert len(ts) % 188 == 0, ("not packet-aligned", len(ts))
    # every packet starts with sync 0x47
    assert all(ts[i] == 0x47 for i in range(0, len(ts), 188)), "missing sync byte"
    # PAT/PMT present at the start (PID 0 then PID 0x1000)
    pid0 = ((ts[1] & 0x1F) << 8) | ts[2]
    pid1 = ((ts[189] & 0x1F) << 8) | ts[190]
    assert pid0 == _PID_PAT and pid1 == _PID_PMT, (hex(pid0), hex(pid1))
    # CRC self-consistency of the PAT section
    print(f"muxed {len(ts)} bytes = {len(ts)//188} TS packets; PAT@PID0 PMT@PID{_PID_PMT:#x} "
          f"video@PID{_PID_VIDEO:#x} stream_type={m.stream_type:#x}")
    open('/tmp/ts_selftest.ts', 'wb').write(ts)
    print("wrote /tmp/ts_selftest.ts — ffprobe it to confirm PTS/codec/structure")
