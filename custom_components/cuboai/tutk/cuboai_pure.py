#!/usr/bin/env python3
"""
cuboai_pure.py — Pure Python TUTK/IOTC LAN transport for CuboAI camera.

Builds the LAN packets in pure Python; no native TUTK library is required.

============================================================================
THE AV-CONNECT OBFUSCATION
============================================================================
The 598-byte av-connect wire packet is not "plaintext XOR a 16-byte key". It
is the real plaintext av-connect (which contains the account + password in the
clear) run through TUTK's `TransCodePartial` block-scramble. The repeating
"XOR frame key" 6e2e8d8c... is simply `TransCodePartial(<16 zero bytes>)`, so
XOR-ing it back only "decodes" the all-zero regions; the structured regions
are real transformed data.

  * `TransCodePartial` (function `transcode` below) processes the buffer in
    independent 16-byte blocks: for each block of 4 LE u32 words w0..w3:
        A=ror32(w0,1)^K0 ; B=ror32(w1,5)^K4 ; C=ror32(w2,9)^K8 ; D=ror32(w3,13)^K12
    then a fixed byte-shuffle, then ror 3/7/11/15 on the four output words.
    The trailing (len % 16) bytes are just XOR'd with the key. The constant
    key K is the TUTK string:
        K = b"Charlie is the designer of P2P!!"   (only first 16 bytes used)

  * AV[56:62] sits in the block at offset 48, whose plaintext is
        [4-byte token] ++ first 12 bytes of the account
    The 4-byte token is `rand()` (LE; low byte += channel). The camera does
    not validate this token: when it inverse-transforms the block it only
    requires that bytes [4:16] still spell the account. There is no timestamp
    and no key/nonce in this region.

WHAT THE CAMERA VALIDATES in the av-connect: the 16-byte HEADER, which must
decode to the camera's nO response:
    decoded_header[2] = nO[178]
    decoded_header[3] = nO[179] | 0x40
    decoded_header[5] = nO[181] ^ 0x40
    decoded_header[6] = nO[182] & 0xF0
    decoded_header[7] = nO[183] & 0x01
The header is `TransCodePartial(static[0:12] ++ R ++ 00 00)` where `R` is a
2-byte per-session value (`GenShortRandomID`, also copied to plaintext[20:22]).
For a chosen nO there is a UNIQUE R that yields the required header; it is
recovered with a precomputed 64K lookup table (`build_R_table`).

XOR frame key (== TransCodePartial of zeros): 6e2e8d8c40d040ca2d6d280c40e4cad8
"""

import ctypes
import ctypes.util
import os
import socket
import struct
import sys
import threading
import time

# ── _AV_MID / client fingerprint: derived from the local NIC MAC ─────────────
# The 6-byte client fingerprint (probe plaintext [58:64]; AV/DATA plaintext
# [22:28]) is computed at frame-build time from the host's MAC: getifaddrs()
# reads sll_addr of the FIRST non-loopback AF_PACKET interface, then a fixed
# BYTE PERMUTATION is applied.
#
#     _AV_MID = [mac[1], mac[0], mac[5], mac[4], mac[3], mac[2]]
#             = mac[0:2] byte-swapped  ++  reverse(mac[2:6])
#   0123456789ab -> 2301ab896745
# It is a positional permutation (value-independent), so it generalises to any
# host. Computing it dynamically keeps the pure transport portable across hosts.
_AVMID_PERM     = (1, 0, 5, 4, 3, 2)
_AVMID_FALLBACK = bytes.fromhex("000000000000")   # neutral fallback; used only iff the local MAC read fails
_AF_PACKET      = 17       # sockaddr_ll.sll_family on Linux
_AF_LINK        = 18       # sockaddr_dl.sdl_family on macOS/BSD
_IFF_LOOPBACK   = 0x8      # net/if.h IFF_LOOPBACK — same value on Linux and macOS/BSD
_IS_DARWIN      = sys.platform == "darwin"

# `struct ifaddrs` is layout-compatible on Linux and macOS/BSD (next, name, flags,
# addr, netmask, dstaddr/ifu, data); ctypes inserts the 4-byte pad after the 32-bit
# ifa_flags on LP64 automatically, so one definition works for both.
class _ifaddrs(ctypes.Structure):
    pass
_ifaddrs._fields_ = [
    ("ifa_next",    ctypes.POINTER(_ifaddrs)),
    ("ifa_name",    ctypes.c_char_p),
    ("ifa_flags",   ctypes.c_uint),
    ("ifa_addr",    ctypes.c_void_p),
    ("ifa_netmask", ctypes.c_void_p),
    ("ifa_ifu",     ctypes.c_void_p),
    ("ifa_data",    ctypes.c_void_p),
]


def _parse_link_mac(addr, darwin):
    """Extract a 6-byte link-layer MAC from a sockaddr at address `addr`, or None.

    The two platforms differ in BOTH the family encoding and the sockaddr layout:

    * Linux  — `struct sockaddr_ll` (AF_PACKET=17). `sa_family` is a 2-byte field
      at offset 0 (no `sa_len`). MAC: sll_halen@11, sll_addr@12.
    * macOS/BSD — `struct sockaddr_dl` (AF_LINK=18). BSD sockaddrs lead with a
      1-byte `sdl_len`@0 then a 1-byte `sdl_family`@1. The link address is variable-
      offset: sdl_nlen@5 (interface-name length), sdl_alen@6 (address length),
      sdl_data@8 holds the name THEN the address, so MAC starts at 8 + sdl_nlen.
    """
    if not addr:
        return None
    u8 = lambda off: ctypes.cast(addr + off, ctypes.POINTER(ctypes.c_ubyte)).contents.value
    if darwin:
        if u8(1) != _AF_LINK:                       # sdl_family
            return None
        nlen, alen = u8(5), u8(6)                   # sdl_nlen, sdl_alen
        if alen != 6:
            return None
        base = addr + 8 + nlen                      # sdl_data + name
    else:
        fam = ctypes.cast(addr, ctypes.POINTER(ctypes.c_ushort)).contents.value
        if fam != _AF_PACKET:
            return None
        if u8(11) != 6:                             # sll_halen
            return None
        base = addr + 12                            # sll_addr
    mac = bytes((ctypes.c_ubyte * 6).from_address(base))
    return mac if mac != b"\x00" * 6 else None      # skip all-zero MACs


def _local_mac_via_getifaddrs():
    """First non-loopback link-layer MAC in getifaddrs() order, or None.

    Uses getifaddrs + the first non-lo link-layer interface's address.
    Cross-platform: AF_PACKET/sockaddr_ll on Linux (ens18, eth0, …) and
    AF_LINK/sockaddr_dl on macOS (en0, en1, …). Loopback is skipped via the
    IFF_LOOPBACK flag (robust across both naming conventions), and all-zero MACs
    (loopback / virtual ifaces like awdl0/utun*) are skipped in _parse_link_mac.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        libc.getifaddrs.restype = ctypes.c_int
        libc.getifaddrs.argtypes = [ctypes.POINTER(ctypes.POINTER(_ifaddrs))]
        libc.freeifaddrs.argtypes = [ctypes.POINTER(_ifaddrs)]
    except Exception:
        return None
    head = ctypes.POINTER(_ifaddrs)()
    if libc.getifaddrs(ctypes.byref(head)) != 0:
        return None
    try:
        cur = head
        while cur:
            ifa = cur.contents
            if not (ifa.ifa_flags & _IFF_LOOPBACK):       # skip loopback (lo / lo0)
                mac = _parse_link_mac(ifa.ifa_addr, _IS_DARWIN)
                if mac is not None:
                    return mac
            cur = ifa.ifa_next
        return None
    finally:
        libc.freeifaddrs(head)


def _local_mac_via_sysfs():
    """Fallback: lowest-ifindex non-lo MAC from /sys/class/net, or None."""
    best = None
    try:
        for nm in os.listdir("/sys/class/net"):
            if nm == "lo":
                continue
            try:
                idx = int(open("/sys/class/net/%s/ifindex" % nm).read())
                mac = bytes.fromhex(open("/sys/class/net/%s/address" % nm).read().strip().replace(":", ""))
                if len(mac) == 6 and mac != b"\x00" * 6 and (best is None or idx < best[0]):
                    best = (idx, mac)
            except Exception:
                continue
    except Exception:
        return None
    return best[1] if best else None


def _local_mac_via_uuid():
    """Cross-platform last-resort MAC (incl. Windows, where getifaddrs and
    /sys/class/net are both absent): `uuid.getnode()`.

    Skipped when getnode() returns the RFC-4122 random fallback it generates if no
    NIC MAC is available — that value has the multicast bit (LSB of octet 0, i.e.
    bit 40 of the 48-bit big-endian integer) set, which a real station MAC never has.
    """
    try:
        import uuid
        node = uuid.getnode()
        if (node >> 40) & 1:                # multicast bit ⇒ random/unusable, not a NIC MAC
            return None
        return node.to_bytes(6, "big")
    except Exception:
        return None


def compute_av_mid():
    """Return the 6-byte _AV_MID for this host (perm of local NIC MAC).

    Cross-platform: getifaddrs (Linux AF_PACKET / macOS AF_LINK) → /sys/class/net
    (Linux) → uuid.getnode() (Windows + universal). Falls back to a neutral value
    only if every method fails, so import never raises; a stderr note is emitted
    on that final fallback. On Linux/macOS getifaddrs wins.
    """
    mac = (_local_mac_via_getifaddrs()      # Linux (AF_PACKET) / macOS (AF_LINK)
           or _local_mac_via_sysfs()        # Linux /sys/class/net fallback
           or _local_mac_via_uuid())        # cross-platform incl. Windows
    if mac is None:
        sys.stderr.write("[cuboai_pure] WARN: no NIC MAC found; "
                         "using fallback _AV_MID %s\n" % _AVMID_FALLBACK.hex())
        return _AVMID_FALLBACK
    return bytes(mac[i] for i in _AVMID_PERM)


_AV_MID_DYNAMIC = compute_av_mid()

# ── obfuscation / framing ────────────────────────────────────────────────────

_XOR_KEY = bytes.fromhex("6e2e8d8c40d040ca2d6d280c40e4cad8")   # == transcode(zeros)
_TRANS_KEY = b"Charlie is the designer of P2P!!"               # iotc_trans_arr
_K16 = _TRANS_KEY[:16]                                         # only first 16 used


def xor_frame(data: bytes) -> bytes:
    """Legacy 'decode/encode' = XOR with the repeating frame key (== transcode(0))."""
    k = _XOR_KEY
    return bytes(b ^ k[i % 16] for i, b in enumerate(data))


def _ror32(v, r):
    r &= 31
    return ((v >> r) | (v << (32 - r))) & 0xFFFFFFFF


def _block_transform(blk: bytes) -> bytes:
    """One 16-byte block of TUTK TransCodePartial (the real obfuscation 'F')."""
    k0, k4, k8, k12 = struct.unpack("<IIII", _K16)
    w0, w1, w2, w3 = struct.unpack("<IIII", blk)
    A = _ror32(w0, 1) ^ k0
    B = _ror32(w1, 5) ^ k4
    C = _ror32(w2, 9) ^ k8
    D = _ror32(w3, 13) ^ k12
    a0, a1, a2, a3 = A & 0xFF, (A >> 8) & 0xFF, (A >> 16) & 0xFF, (A >> 24) & 0xFF
    b0, b1, b2, b3 = B & 0xFF, (B >> 8) & 0xFF, (B >> 16) & 0xFF, (B >> 24) & 0xFF
    c0, c1, c2, c3 = C & 0xFF, (C >> 8) & 0xFF, (C >> 16) & 0xFF, (C >> 24) & 0xFF
    d0, d1, d2, d3 = D & 0xFF, (D >> 8) & 0xFF, (D >> 16) & 0xFF, (D >> 24) & 0xFF
    ecx = (d2 << 24) | (d0 << 16) | (c2 << 8) | d1
    r10 = (a0 << 24) | (b1 << 16) | (a1 << 8) | a2
    r8  = (d3 << 24) | (c0 << 16) | (c1 << 8) | c3
    r9  = (a3 << 24) | (b3 << 16) | (b0 << 8) | b2
    return struct.pack("<IIII", _ror32(r8, 3), _ror32(ecx, 7),
                       _ror32(r10, 11), _ror32(r9, 15))


def transcode(plain: bytes) -> bytes:
    """Full TUTK TransCodePartial: 16-byte block transform + tail XOR.

    This maps the real plaintext av-connect to the exact bytes that go on the
    wire. The account, password, header and every structured field live in full
    16-byte blocks, so they are transcoded by the block transform.

    The trailing `len & 0xF` bytes are a plain XOR: `K16[i] ^ plain[i]`. TUTK has
    a `Swap` byte-permutation applied to the tail for tail lengths 2/4/8, but the
    IOTC SendMessage path used for these LAN frames does NOT apply it — every
    frame type matches plain XOR (the 88-byte probe/ACK with trailer
    `63041313040c0c63`, the 76-byte IOCTL request, the 598-byte av-connect, and
    the IOCTL responses). Do NOT re-add Swap here — it corrupts the probe tail
    and breaks connect.
    """
    n = len(plain)
    full = n - (n & 0xF)
    out = bytearray(n)
    for off in range(0, full, 16):
        out[off:off + 16] = _block_transform(plain[off:off + 16])
    for i in range(full, n):
        out[i] = _K16[i - full] ^ plain[i]
    return bytes(out)


def _rol32(v, r):
    r &= 31
    return ((v << r) | (v >> (32 - r))) & 0xFFFFFFFF


def _inv_block_transform(blk: bytes) -> bytes:
    """Inverse of `_block_transform` — recover plaintext from a 16-byte wire block.

    This is how the camera reads the packet (inverse-TransCodePartial). It reveals
    the true plaintext (UID, R, fingerprint) that `xor_frame` decodes to garbage in
    the structured regions.
    """
    k0, k4, k8, k12 = struct.unpack("<IIII", _K16)
    o0, o1, o2, o3 = struct.unpack("<IIII", blk)
    r8, ecx, r10, r9 = _rol32(o0, 3), _rol32(o1, 7), _rol32(o2, 11), _rol32(o3, 15)
    d2, d0, c2, d1 = (ecx >> 24) & 0xFF, (ecx >> 16) & 0xFF, (ecx >> 8) & 0xFF, ecx & 0xFF
    a0, b1, a1, a2 = (r10 >> 24) & 0xFF, (r10 >> 16) & 0xFF, (r10 >> 8) & 0xFF, r10 & 0xFF
    d3, c0, c1, c3 = (r8 >> 24) & 0xFF, (r8 >> 16) & 0xFF, (r8 >> 8) & 0xFF, r8 & 0xFF
    a3, b3, b0, b2 = (r9 >> 24) & 0xFF, (r9 >> 16) & 0xFF, (r9 >> 8) & 0xFF, r9 & 0xFF
    A = a0 | (a1 << 8) | (a2 << 16) | (a3 << 24)
    B = b0 | (b1 << 8) | (b2 << 16) | (b3 << 24)
    C = c0 | (c1 << 8) | (c2 << 16) | (c3 << 24)
    D = d0 | (d1 << 8) | (d2 << 16) | (d3 << 24)
    w0 = _rol32(A ^ k0, 1)
    w1 = _rol32(B ^ k4, 5)
    w2 = _rol32(C ^ k8, 9)
    w3 = _rol32(D ^ k12, 13)
    return struct.pack("<IIII", w0, w1, w2, w3)


def inv_transcode(wire: bytes) -> bytes:
    """Full inverse of `transcode` (what the camera computes on receive)."""
    n = len(wire)
    full = n - (n & 0xF)
    out = bytearray(n)
    for off in range(0, full, 16):
        out[off:off + 16] = _inv_block_transform(wire[off:off + 16])
    for i in range(full, n):
        out[i] = _K16[i - full] ^ wire[i]
    return bytes(out)


# ── AAC-ADTS helpers ──────────────────────────────────────────────────────────
# Each camera audio AV unit is ONE AAC-ADTS frame followed by a 24-byte TUTK
# FRAMEINFO trailer (codec_id 0x0088), so on the wire avlen == adts_frame_len + 24.
# Each audio unit must be truncated to its self-declared ADTS frame length; emitting
# the whole unit would append the 24-byte trailer to every ADTS frame and corrupt
# the AAC.

def _adts_frame_len(b: bytes):
    """Length (bytes) of the ADTS frame at the start of `b`, or None if not ADTS."""
    if len(b) < 7 or b[0] != 0xFF or (b[1] & 0xF6) != 0xF0:
        return None
    return ((b[3] & 0x03) << 11) | (b[4] << 3) | ((b[5] >> 5) & 0x07)


# ── TUTK FRAMEINFO trailer (24 bytes appended to every AV unit) ────────────────
# The camera appends a 24-byte TUTK FRAMEINFO_t to each AV access unit (the same
# trailer the AUDIO path drops by truncating to the self-declared ADTS length).
# The video trailer must be stripped: concatenated verbatim it leaves emitted HEVC
# AUs ending with [...NALs...][24-byte FRAMEINFO]; software decoders ignore trailing
# bytes but HARDWARE decoders (e.g. Apple VideoToolbox via Safari/Chrome) reject the
# malformed over-long final NAL -> black picture (kVTVideoDecoderBadDataErr -12909).
# _strip_frameinfo (CUBOAI_STRIP_FRAMEINFO) drops it.
#
# LAYOUT (little-endian):
#   [0:2]   u16  codec_id      0x0050 = HEVC video (0x0088 = AAC audio) — the sanity gate
#   [2]     u8   keyframe flag 0x01 on IDR/IRAP AUs, 0x00 on P — the authoritative IDR marker
#   [3]     u8   reserved (0)
#   [4:8]   u32  ~2 + a toggling top bit (cam/channel index 2 + a per-frame flag) — not used
#   [8:10]  u16  videoWidth   2560
#   [10:12] u16  videoHeight  1440
#   [12:16] u32  timestamp_sec   unix epoch seconds, +1 ~every second
#   [16:20] u32  timestamp_ms    milliseconds-within-the-second (0..999, ~67ms/frame, resets each sec)
#   [20:24] u32  frame_no        monotonic frame counter (+1 per frame)
# -> frame timestamp (ms) = timestamp_sec*1000 + timestamp_ms  (used for PTS / A-V sync).
_FRAMEINFO_LEN = 24
_FRAMEINFO_CODEC_HEVC = 0x0050        # codec_id at offset 0 for an HEVC video FRAMEINFO
# ── codec_id -> codec name (ThroughTek MEDIA_CODEC enum) ──
# The codec name drives the TS stream_type (cuboai_mpegts) and go2rtc media, so a future
# H264/other CuboAI camera works with no code change. HEVC=0x50 and AAC=0x88 are confirmed
# on this camera (HEVC video AUs / 0x0088 audio trailers); the rest are the standard
# ThroughTek values.
_FRAMEINFO_CODEC = {
    0x004C: 'mpeg4', 0x004D: 'h263', 0x004E: 'h264', 0x004F: 'mjpeg', 0x0050: 'hevc',   # video
    0x0086: 'adpcm', 0x0087: 'pcm', 0x0088: 'aac', 0x0089: 'g711u', 0x008A: 'g711a',     # audio
    0x008B: 'g726', 0x008C: 'speex', 0x008D: 'mp3',
}
_FRAMEINFO_VIDEO_CODECS = frozenset((0x004C, 0x004D, 0x004E, 0x004F, 0x0050))   # video codec_id range
_FRAMEINFO_AUDIO_CODECS = frozenset((0x0086, 0x0087, 0x0088, 0x0089, 0x008A,    # audio codec_id range
                                     0x008B, 0x008C, 0x008D))                    # (aac=0x0088 here)
# resolution plausibility RANGE for the strip sanity gate — a RANGE, not the literal 2560x1440,
# so a different-resolution camera still passes the gate (else its trailer isn't stripped → black).
_FRAMEINFO_RES_MIN, _FRAMEINFO_RES_MAX = 64, 8192
# AUDIO FRAMEINFO repurposes the video width/height slot as sample_rate/channels: [8:10]=
# sample_rate, [10:12]=channels, [12:16]=ts_sec (the SAME unix-epoch clock as video → A/V sync),
# [16:24]=garbage (NOT a usable sub-second/frame_no). These gate a candidate trailer as audio.
_FRAMEINFO_AUDIO_RATES = frozenset((8000, 11025, 12000, 16000, 22050, 24000, 32000, 44100, 48000))

def _frameinfo_codec_name(codec_id: int) -> str:
    return _FRAMEINFO_CODEC.get(codec_id, f'unknown_0x{codec_id:04x}')

def _looks_like_frameinfo(fi: bytes) -> bool:
    """Sanity-gate before stripping: codec_id is a known VIDEO codec AND a plausible width/height.
    codec_id alone has a ~1/65536 false-positive chance of matching slice data per AU; the
    width/height ([8:12]) range guard makes a real-slice false strip effectively impossible while
    still passing EVERY genuine video FRAMEINFO regardless of codec (HEVC/H264/...) or resolution."""
    if len(fi) < _FRAMEINFO_LEN or struct.unpack_from('<H', fi, 0)[0] not in _FRAMEINFO_VIDEO_CODECS:
        return False
    w = struct.unpack_from('<H', fi, 8)[0]; h = struct.unpack_from('<H', fi, 10)[0]
    return _FRAMEINFO_RES_MIN <= w <= _FRAMEINFO_RES_MAX and _FRAMEINFO_RES_MIN <= h <= _FRAMEINFO_RES_MAX

def _parse_frameinfo(fi: bytes) -> dict:
    """Decode a 24-byte TUTK video FRAMEINFO trailer into its fields (offsets above)."""
    sec = struct.unpack_from('<I', fi, 12)[0]
    ms = struct.unpack_from('<I', fi, 16)[0]
    cid = struct.unpack_from('<H', fi, 0)[0]
    kf = bool(fi[2] & 0x01)
    return {
        'codec_id': cid,
        'codec': _frameinfo_codec_name(cid),   # codec NAME (drives TS stream_type / go2rtc media)
        'keyframe': kf,                         # authoritative IDR marker
        'is_keyframe': kf,                      # alias
        'width': struct.unpack_from('<H', fi, 8)[0],
        'height': struct.unpack_from('<H', fi, 10)[0],
        'ts_sec': sec,
        'ts_ms_field': ms,
        'frame_no': struct.unpack_from('<I', fi, 20)[0],
        'timestamp_ms': sec * 1000 + ms,    # monotonic ms timestamp (drives PTS in cuboai_pts)
        # ~10% of AUs carry a garbage [16:24] (ts_ms/frame_no) while [0:16] (codec/kf/w/h/ts_sec)
        # is valid — the 24B strip is still correct (ffmpeg: 0 invalid-NAL); ts_valid flags whether
        # the sub-second timestamp/frame_no are trustworthy (the PTS clock interpolates when false).
        'ts_valid': ms <= 999,
    }


def _looks_like_audio_frameinfo(fi: bytes) -> bool:
    """Sanity-gate a candidate trailer as an AUDIO FRAMEINFO: audio codec_id + a plausible
    sample_rate ([8:10]) and channel count ([10:12]). Mirrors _looks_like_frameinfo for video, so a
    partial/garbage tail can't be mis-read as audio timing."""
    if len(fi) < _FRAMEINFO_LEN or struct.unpack_from('<H', fi, 0)[0] not in _FRAMEINFO_AUDIO_CODECS:
        return False
    sr = struct.unpack_from('<H', fi, 8)[0]; ch = struct.unpack_from('<H', fi, 10)[0]
    return sr in _FRAMEINFO_AUDIO_RATES and 1 <= ch <= 2


def _parse_audio_frameinfo(fi: bytes) -> dict:
    """Decode an audio FRAMEINFO trailer. ts_sec is the SAME unix-epoch second-clock as
    video → A/V sync on a shared PTS base. The sub-second field [16:20] is garbage for audio, so
    ts_valid marks only the SECOND as trustworthy; the consumer adds the intra-second AAC cadence
    (1024 samples / sample_rate) and re-anchors each second → drift-free, NOT a free-running counter."""
    cid = struct.unpack_from('<H', fi, 0)[0]
    return {
        'codec_id': cid,
        'codec': _frameinfo_codec_name(cid),   # 'aac' → TS stream_type 0x0F
        'is_audio': True,
        'is_keyframe': True,                    # every AAC-LC frame is independently decodable
        'sample_rate': struct.unpack_from('<H', fi, 8)[0],
        'channels': struct.unpack_from('<H', fi, 10)[0],
        'ts_sec': struct.unpack_from('<I', fi, 12)[0],
        'ts_valid': True,                       # the second is the trustworthy anchor (sub-second is garbage)
    }


# AudioSpecificConfig for AAC-LC 16 kHz mono (objectType=2, sfIndex=8, channels=1):
#   00010 1000 0001 -> 0x14 0x08.  Needed as mp4 `esds` extradata when stream-copying
#   the camera's ADTS audio into an MP4 (ADTS headers are stripped for MP4).
_AAC_LC_16K_MONO_ASC = bytes.fromhex("1408")


# ── media helpers (PyAV) ──────────────────────────────────────────────────────
# These convert the raw HEVC/AAC the camera streams into shareable files. PyAV is
# imported lazily so the transport itself stays dependency-free; only the file-
# producing helpers require it (`pip install av`).

# ── video codec detection ─────────────────────────────────────────────────────
# Gen3 cameras stream HEVC (H.265); older Gen1/Gen2 units stream H.264 (AVC). Both
# arrive as Annex-B access units, so the framing/reassembly is identical — only the
# decoder/muxer format differs. We sniff the codec from the first NAL header that
# follows the Annex-B start code (00 00 00 01 / 00 00 01):
#   • H.264 NAL header is 1 byte: nal_unit_type = byte & 0x1F. A keyframe AU starts
#     with SPS (type 7 → 0x67) or an IDR slice (type 5 → 0x65).
#   • HEVC  NAL header is 2 bytes: nal_unit_type = (byte >> 1) & 0x3F. A keyframe AU
#     starts with VPS (32 → 0x40), SPS (33 → 0x42) or PPS (34 → 0x44).
# The two are unambiguous on the parameter-set bytes the camera always sends first.
def _nal_start_offset(au: bytes) -> int:
    if au[:4] == b"\x00\x00\x00\x01":
        return 4
    if au[:3] == b"\x00\x00\x01":
        return 3
    return 0

def _iter_nal_headers(au: bytes):
    """Yield the first header byte of every Annex-B NAL in `au`."""
    n, i = len(au), 0
    while i < n - 2:
        if au[i] == 0 and au[i + 1] == 0 and au[i + 2] == 1:
            j = i + 3
            if j < n:
                yield au[j]
            i = j
        elif (au[i] == 0 and au[i + 1] == 0 and i + 3 < n
              and au[i + 2] == 0 and au[i + 3] == 1):
            j = i + 4
            if j < n:
                yield au[j]
            i = j
        else:
            i += 1

def detect_video_codec(au: bytes, default: str = "hevc") -> str:
    """Return 'hevc' or 'h264' for an Annex-B access unit.

    Disambiguates on the PARAMETER SETS, which are unambiguous: an H.264 keyframe
    carries an SPS (NAL type 7); an HEVC keyframe carries a VPS/SPS (types 32/33).
    The single-NAL slice bytes overlap (e.g. H.264 0x41 aliases HEVC VPS under the
    6-bit type field), so we scan every NAL in the AU and decide on the first
    parameter-set / IDR we recognise. Falls back to `default` (Gen3 = HEVC) for a
    P-frame-only AU with no parameter set — detection is normally done on a keyframe.
    """
    h264_idr = hevc_pic = None
    for b in _iter_nal_headers(au):
        if b & 0x80:                                   # forbidden bit set
            continue
        if (b & 0x1F) in (7, 8):                       # H.264 SPS / PPS — decisive
            return "h264"
        if ((b >> 1) & 0x3F) in (32, 33, 34):          # HEVC VPS/SPS/PPS — decisive
            return "hevc"
        if h264_idr is None and (b & 0x1F) == 5:       # H.264 IDR slice
            h264_idr = True
        if hevc_pic is None and ((b >> 1) & 0x3F) in (19, 20, 21):  # HEVC IDR/CRA
            hevc_pic = True
    if hevc_pic:
        return "hevc"
    if h264_idr:
        return "h264"
    return default


def hevc_to_jpeg(au: bytes, quality: int = 90) -> bytes:
    """Decode the first picture of a raw H.264/HEVC access unit and return JPEG bytes.

    The codec (h264 vs hevc) is auto-detected from the NAL header, so this works for
    both Gen1/Gen2 (H.264) and Gen3 (HEVC) cameras. Uses PyAV's mjpeg encoder (no
    Pillow dependency). `quality` is 1-100. (Name kept for back-compat.)
    """
    import io
    import av  # lazy: only needed for snapshot-to-JPEG

    codec = detect_video_codec(au)
    container = av.open(io.BytesIO(au), format=codec)
    frame = None
    try:
        for frame in container.decode(video=0):
            break
    finally:
        container.close()
    if frame is None:
        raise RuntimeError(f"no decodable {codec.upper()} picture in access unit")

    out = io.BytesIO()
    oc = av.open(out, mode="w", format="mjpeg")
    try:
        st = oc.add_stream("mjpeg", rate=1)
        st.width, st.height = frame.width, frame.height
        st.pix_fmt = "yuvj420p"
        # libavcodec mjpeg uses qscale: ~ (100-quality) mapped into 2..31
        st.codec_context.qmin = st.codec_context.qmax = max(2, min(31, 32 - quality * 30 // 100))
        for pkt in st.encode(frame.reformat(format="yuvj420p")):
            oc.mux(pkt)
        for pkt in st.encode(None):
            oc.mux(pkt)
    finally:
        oc.close()
    return out.getvalue()


def _is_video_keyframe(unit: bytes, codec: str) -> bool:
    """True if an Annex-B access unit contains a keyframe (parameter set / IDR).
    HEVC keyframes carry VPS/SPS/PPS (types 32/34) or an IDR/CRA picture (19-21);
    H.264 keyframes carry an SPS (7)/PPS (8) or an IDR slice (5). Scans all NALs so a
    leading access-unit-delimiter/SEI doesn't hide the keyframe."""
    for b in _iter_nal_headers(unit):
        if b & 0x80:
            continue
        if codec == "hevc":
            if ((b >> 1) & 0x3F) in (32, 33, 34, 19, 20, 21):
                return True
        else:
            if (b & 0x1F) in (7, 8, 5):
                return True
    return False


def mux_to_mp4(path: str, video_units, audio_units, video_fps: float = 15.0,
               audio_rate: int = 16000):
    """Mux raw H.264/HEVC access units + AAC-ADTS frames into a playable .mp4
    (stream copy, no re-encode).

    `video_units` / `audio_units` are iterables of raw access-unit / ADTS-frame
    bytes (as produced by `av_frames`). The video codec (h264 vs hevc) is
    auto-detected from the first unit's NAL header, so both Gen1/Gen2 (H.264) and
    Gen3 (HEVC) cameras mux correctly. Video timestamps are synthesised at
    `video_fps`; audio timestamps from the 1024-sample ADTS cadence. The ADTS
    headers are stripped and an AAC-LC AudioSpecificConfig is written so the audio
    track is valid inside MP4.
    """
    import io
    import fractions
    import av

    video_units = list(video_units)
    audio_units = [a for a in audio_units if _adts_frame_len(a)]
    codec = detect_video_codec(video_units[0]) if video_units else "hevc"

    out = av.open(path, "w")
    try:
        ov = oa = None
        if video_units:
            # derive the parameter sets (hvcC/avcC) from the first keyframe
            vin = av.open(io.BytesIO(b"".join(video_units)), format=codec)
            ov = out.add_stream_from_template(vin.streams.video[0])
            vin.close()
        if audio_units:
            oa = out.add_stream("aac", rate=audio_rate)
            oa.codec_context.extradata = _AAC_LC_16K_MONO_ASC

        if ov is not None:
            vtb = fractions.Fraction(1, 1000)
            step = int(round(1000.0 / max(1e-3, video_fps)))
            for i, unit in enumerate(video_units):
                pkt = av.Packet(unit)
                pkt.stream = ov
                pkt.time_base = vtb
                pkt.pts = pkt.dts = i * step
                pkt.duration = step
                if _is_video_keyframe(unit, codec):
                    pkt.is_keyframe = True
                out.mux(pkt)

        if oa is not None:
            atb = fractions.Fraction(1, audio_rate)
            for j, frame in enumerate(audio_units):
                fl = _adts_frame_len(frame)
                hdr = 9 if (frame[1] & 0x01) == 0 else 7   # protection_absent -> CRC
                pkt = av.Packet(frame[hdr:fl])
                pkt.stream = oa
                pkt.time_base = atb
                pkt.pts = pkt.dts = j * 1024
                pkt.duration = 1024
                out.mux(pkt)
    finally:
        out.close()
    return path


def mux_to_mp4_timed(path: str, video_items, audio_items, audio_rate: int = 16000):
    """Mux raw video AUs + AAC-ADTS frames into a playable .mp4 with TRUE camera-clock A/V sync.

    Unlike mux_to_mp4 (which synthesises video PTS at a fixed fps and runs a free-running j·1024
    audio counter — both drift on loss), each item here carries its own PTS on a SHARED ms epoch
    (from cuboai_pts.AVTimeline), so audio and video stay aligned exactly as the live streamer proved.
      video_items: iterable of (au_bytes, pts_ms)
      audio_items: iterable of (adts_bytes, pts_ms)
    Both streams share time_base 1/1000, so the inter-track offset = the pts_ms difference. Stream
    copy (no re-encode); ADTS headers stripped + an AAC-LC AudioSpecificConfig written for valid MP4.
    """
    import io
    import fractions
    import av

    video_items = list(video_items)
    audio_items = [(a, p) for (a, p) in audio_items if _adts_frame_len(a)]
    vbytes = [u for u, _ in video_items]
    codec = detect_video_codec(vbytes[0]) if vbytes else "hevc"
    all_pts = [p for _, p in video_items] + [p for _, p in audio_items]
    t0 = min(all_pts) if all_pts else 0.0            # normalise the earliest PTS to 0 (mp4 wants ≥0)
    tb = fractions.Fraction(1, 1000)

    out = av.open(path, "w")
    try:
        ov = oa = None
        if vbytes:
            vin = av.open(io.BytesIO(b"".join(vbytes)), format=codec)
            ov = out.add_stream_from_template(vin.streams.video[0])
            vin.close()
        if audio_items:
            oa = out.add_stream("aac", rate=audio_rate)
            oa.codec_context.extradata = _AAC_LC_16K_MONO_ASC

        def _mux(stream, items, default_dur, strip_adts):
            last = -1
            n = len(items)
            for i, (buf, pms) in enumerate(items):
                if strip_adts:
                    fl = _adts_frame_len(buf)
                    hdr = 9 if (buf[1] & 0x01) == 0 else 7   # protection_absent → 7B hdr, else +2B CRC
                    payload = buf[hdr:fl]
                else:
                    payload = buf
                p = int(round(pms - t0))
                if p <= last:                            # force strictly-monotonic per-stream PTS
                    p = last + 1
                last = p
                nxt = items[i + 1][1] if i + 1 < n else pms + default_dur
                pkt = av.Packet(payload)
                pkt.stream = stream
                pkt.time_base = tb
                pkt.pts = pkt.dts = p
                pkt.duration = max(1, int(round(nxt - pms)))
                if not strip_adts and _is_video_keyframe(payload, codec):
                    pkt.is_keyframe = True
                out.mux(pkt)

        if ov is not None:
            _mux(ov, video_items, 67, strip_adts=False)   # ~15 fps video default tail-duration
        if oa is not None:
            _mux(oa, audio_items, 64, strip_adts=True)     # 1024-sample AAC frame ≈ 64 ms
    finally:
        out.close()
    return path


def _clean_gop_video_items(items):
    """Generator over (kind, data, fi) tuples that suppresses the poisoned GOP tail for the .mp4
    recorder: drop any incomplete VIDEO AU (fi is None) and every subsequent VIDEO AU until the next
    clean IDR keyframe, so a recorded clip never carries a broken GOP under loss. Audio passes
    through untouched. Starts DESYNCED so the clip begins on the first complete IDR. Used only when
    CUBOAI_RECORD_CLEAN_GOP is set; the default record path is unchanged."""
    synced = False
    for kind, data, fi in items:
        if kind == 'video':
            if fi is None:
                synced = False
                continue
            if not synced:
                if fi.get('is_keyframe'):
                    synced = True
                else:
                    continue
        yield (kind, data, fi)


# ── LAN-search probe / ACK (IOTC type 0x601) ──────────────────────────────────
# These are NOT "xor_frame'd with a random nonce". They are real plaintext run
# through TransCodePartial (`transcode`), exactly like the av-connect.
#
#   * The per-session field is R = a short random id in [1, 0xFFFE] (the lib's
#     `GenShortRandomID` = (tutk_platform_rand()+time()) mod 0xFFFF), placed at
#     plaintext[56:58] (little-endian). The UID is in the clear at [16:36] and the
#     6-byte client fingerprint sits at [58:64].
#   * The camera reads (R | fingerprint) as the client random id, creates a
#     pre-session keyed by it, echoes R back inside nO, and — when it later
#     receives the ACK — drives the session to the "connected" status the av-login
#     gate requires. (No KNOCK packet is involved; the device replies directly.)
#     The probe [48:54] region is the transcode encoding of R + the fingerprint,
#     so both must be built correctly: a corrupt R or fingerprint here leaves the
#     pre-session incoherently keyed and the av is silently dropped.
#   * The av-connect header R (plaintext[12:14] == [20:22]) MUST equal the probe R,
#     and the av fingerprint (`_AV_MID`) MUST equal the probe fingerprint.
#     header_R_for_nO(nO) also recovers R because the camera echoes our R in nO.

_LS_HEAD16          = bytes.fromhex("04021a02480000000106210000000000")  # [0:16], type 0x601 @ [8]
_LS_MID8            = bytes.fromhex("0000000001010204")                  # [48:56]
_CLIENT_FINGERPRINT = _AV_MID_DYNAMIC                                    # [58:64]; == _AV_MID (perm of local MAC)
_LS_TRAILER8        = bytes.fromhex("63041313040c0c63")                  # [80:88]
_DEFAULT_UID        = b"YOUR_20CHAR_UID_HERE"   # 20-char device UID; override with your own

_KEEPALIVE_DEC = bytes([
    0x01, 0x42, 0x10, 0x60, 0x00, 0x01, 0x00, 0x00,
    0x00, 0x40, 0xa0, 0x21, 0x80, 0x0a, 0x00, 0x00,
    0x5f, 0xe2, 0x43, 0xc9, 0xfe, 0xce, 0x1d, 0xc4,
])
# `_KEEPALIVE_DEC[16:20]` (5fe243c9) is NOT a local IP and NOT a universal
# constant — it is an opaque session-identity token. Native sets it to
#   (host-derived fingerprint << 16) | (client_random_id + 1)
# so its own value differs per host AND per connect. The camera never validates
# it — it STORES the client's token and ECHOES it in every keepalive probe.
# Pure exploits that: it seeds this one template token at connect (the init
# keepalive, connect() step 6), so the camera echoes it for pure's session on any
# host. The keepalive REPLY echoes [16:24] from the probe; build_close echoes the
# captured token (TUTKDirectSession._session_fp) when a probe has been seen, else
# falls back to this template (== the seeded token ⇒ identical wire). The probe
# DETECTOR keys on the host-independent window below.
# Host-independent keepalive-PROBE signature = decoded [8:16] (probe-request marker
# 1040c021 + family tag 800a0000); uniquely tags cam→cli probes vs close/reply
# (0040a021...).
_KEEPALIVE_PROBE_SIG = bytes.fromhex("1040c021800a0000")   # decoded [8:16]


def gen_R() -> int:
    """A fresh short client-random-id in [1, 0xFFFE].

    Matches the lib's GenShortRandomID = ((tutk_platform_rand()+time()) mod 0xFFFF),
    remapping 0 -> 1.  The full 16-bit range is valid (native R's such as 0xd64d
    have bit15 set); the camera does not constrain it.
    """
    return (struct.unpack("<H", os.urandom(2))[0] % 0xFFFE) + 1


def _build_ls_plaintext(uid, R: int, ack: bool) -> bytes:
    """REAL 88-byte LAN-search plaintext (pre-transcode). probe: [64]=01, ACK: [64]=02."""
    u = uid.encode() if isinstance(uid, str) else uid
    if len(u) > 20:
        raise ValueError("UID longer than 20 bytes")
    t = bytearray(88)
    t[0:16] = _LS_HEAD16
    t[16:16 + len(u)] = u                              # UID in the clear at [16:36]
    t[48:56] = _LS_MID8
    t[56:58] = struct.pack("<H", R & 0xFFFF)           # R == GenShortRandomID
    t[58:64] = _CLIENT_FINGERPRINT                      # the camera keys the pre-session on (R | this)
    t[64] = 0x02 if ack else 0x01                      # the ONLY probe/ACK difference
    t[80:88] = _LS_TRAILER8
    return bytes(t)


def build_probe(uid: bytes, R: int) -> bytes:
    """88-byte LAN-search probe wire packet (transcode of the true plaintext)."""
    return transcode(_build_ls_plaintext(uid, R, ack=False))


def build_ack(uid: bytes, R: int) -> bytes:
    """88-byte LAN-search ACK wire packet (probe with plaintext[64]=0x02)."""
    return transcode(_build_ls_plaintext(uid, R, ack=True))


# ── IOTC LAN device-identity query (message type 0x0402) ──────────────────────
# DIAGNOSTIC ONLY, default OFF: this is NOT part of the working protocol flow.
# Native's IOTC_Connect_UDP sends this 52-byte frame ~0.1 s into establishment
# (host 0x0402 -> camera 0x0404 response), carrying UID + R + client MID; pure
# does not. It is an incidental latency-triggered device query, not an arming
# gate: a native session arms without it, and injecting it from pure draws the
# camera's 0x0404 reply but does not change arming. Builder kept (gated) for
# wire-fidelity experiments.
# Decoded 52B layout: HEAD16 | UID[16:36] | R[36:38] | MID[38:44] | 00000000 | TAIL
_LQ_HEAD16 = bytes.fromhex("04021a02240000000204330000000000")  # type 0x0402 @ [8:10]
_LQ_TAIL8  = bytes.fromhex("00000000201e231b")                   # [44:52], session-constant


def build_lan_query(uid, R: int, mid: bytes = None) -> bytes:
    """52-byte IOTC type-0x0402 device-identity query (UID+R+MID); transcode of plaintext."""
    u = uid.encode() if isinstance(uid, str) else uid
    if len(u) > 20:
        raise ValueError("UID longer than 20 bytes")
    t = bytearray(52)
    t[0:16] = _LQ_HEAD16
    t[16:16 + len(u)] = u
    t[36:38] = struct.pack("<H", R & 0xFFFF)
    t[38:44] = mid if mid is not None else _CLIENT_FINGERPRINT
    t[44:52] = _LQ_TAIL8
    return transcode(bytes(t))


def build_close(R: int, session_fp: bytes = None) -> bytes:
    """24-byte IOTC session-close control frame (== native's IOTC_Session_Close send).

    Native ends a session by emitting this single frame 3x as the very last packets,
    then closing the socket. The frame shares its first 20 bytes with the keepalive
    (`_KEEPALIVE_DEC[:20]`); the trailing 4 bytes `[20:24]` are session-specific,
    derived from R:
        [20] = (R >> 8)   ^ 0x28
        [21] = 0xce       [22] = 0x1d                  (constant discriminator)
        [23] = (R & 0xff) ^ 0x89
    Sending it lets the camera free its session slot promptly (its own alive-timeout
    is the fallback), so a subsequent reconnect is clean. xor_frame is self-inverse,
    so this both builds and would decode the wire frame.

    [16:20] is an opaque session-identity token, NOT a local IP. Native sets it to
        (host-derived fingerprint << 16) | (client_random_id + 1)
    — a host fingerprint in the high half plus a counter that increments on every
    native connect in the low half. The camera never validates it; it merely STORES
    the client's token and ECHOES it back in every keepalive probe.

    Pure does NOT derive this from the host. It seeds one opaque token — the template
    `_KEEPALIVE_DEC[16:20]` — into the init keepalive it sends at connect (connect()
    step 6), so the camera stores that token for pure's session on any host and echoes
    it back in probes; `build_keepalive_reply` echoes it again. So pure is
    self-consistent and host-portable; the value is used as an arbitrary session id.

    `session_fp`: when the caller has observed the camera's echoed token for THIS
    session (the probe's `[16:20]`, captured into `TUTKDirectSession._session_fp`), pass
    it here and `[16:20]` mirrors exactly what the camera holds for the session. When
    None (no probe seen yet — the common case for short sessions) `[16:20]` falls back to
    the template, which equals the token pure itself seeded at connect ⇒ identical wire.
    Teardown impact is low either way: the camera's alive-timeout reclaims the slot even
    if the token is stale.
    """
    if session_fp is not None and len(session_fp) != 4:
        raise ValueError("session_fp must be the 4-byte [16:20] session token")
    plain = bytearray(_KEEPALIVE_DEC[:20])
    if session_fp is not None:
        plain[16:20] = session_fp                  # echo the camera's stored token
    plain += bytes([(R >> 8) & 0xFF ^ 0x28, 0xCE, 0x1D, (R & 0xFF) ^ 0x89])
    return xor_frame(bytes(plain))


def _unwrap_index(idx_u16, done_upto):
    """Lift a u16 wire AV message-index into `done_upto`'s unbounded monotonic space so the
    reassembly accept-window survives the 65536 wrap (audit H1). Identical to the raw value while
    no wrap has occurred; across the wrap it continues monotonically (65535 -> 65536). A
    late/duplicate index maps far forward so the gate still rejects it."""
    return done_upto + ((idx_u16 - done_upto) & 0xFFFF)


def is_keepalive_probe(raw: bytes) -> bool:
    """True iff `raw` is the camera's 24-byte IOTC keepalive (alive-check) probe.

    Mid-session the camera periodically (~1 probe / ~2 s) sends a 24-byte IOTC
    control frame to check the session is alive; native answers each with a
    keepalive reply (see `build_keepalive_reply`).

    Matched on the decoded `[8:16]` (`_KEEPALIVE_PROBE_SIG` = `1040c021800a0000`) —
    the probe-request marker plus the family tag. This is host-independent and uniquely
    tags the camera's probe (close/reply carry `0040a021...`). Matching on `[16:20]`
    would be wrong: that region is the per-host session token, so it would make probe
    detection fail on other hosts. The 24-byte length already excludes AV/ACK frames.
    """
    if len(raw) != 24:
        return False
    try:
        return xor_frame(bytes(raw))[8:16] == _KEEPALIVE_PROBE_SIG
    except Exception:
        return False


def build_keepalive_reply(probe_raw: bytes) -> bytes:
    """24-byte keepalive REPLY to the camera's keepalive probe (== native's reply).

    The camera sends a probe; native answers within a few packets with
    `_KEEPALIVE_DEC[:20]` with byte[0]=0x02 and the probe's session tail `[20:24]`
    echoed back. `build_keepalive_reply` from a captured probe is byte-for-byte
    identical to native's reply.

    Answering these probes does NOT unlock AV retransmit — it is wired in as
    native-fidelity (the liveness probe was previously dropped at the `len(raw) < 30`
    guard) and to keep long sessions healthy; it is not a retransmit fix. The resend
    refusal is a camera-side per-session gate.

    `[16:20]` is the client's per-host session token, not a constant: the camera
    stores it per-client and echoes it in the probe, so the full session tail
    `[16:24]` is echoed from the probe rather than templated from `_KEEPALIVE_DEC`.
    `[0:16]` is the reply HEADER: host-independent, and it differs from the probe at
    `[0]` and `[8:12]`, so it stays templated.
    """
    pd = xor_frame(bytes(probe_raw))
    plain = bytearray(_KEEPALIVE_DEC[:16])  # [0:16] reply HEADER (host-independent)
    plain[0] = 0x02
    plain += pd[16:24]                       # echo [16:24] = session token + tail
    return xor_frame(bytes(plain))


# ── av-connect plaintext template (the REAL pre-transcode buffer) ────────────
# Only three things vary per session/channel:
#   [6]      = channel (0..3)
#   [12:14] == [20:22] = R  (GenShortRandomID; chosen so header matches nO)
#   [48:52]  = rand() token (LE, low byte += channel&1) -> becomes AV[56:62] tag
# plus channel-flag bytes [29]/[46]. Everything else (incl. credentials) static.

_AV_HEAD12 = bytes.fromhex("04021a0a4602000007042100")     # plaintext[0:12]
_AV_MID    = _AV_MID_DYNAMIC                                # plaintext[22:28] (perm of local MAC)


def _build_R_table():
    """Map (nO header constraints) -> the 2-byte R that produces that header.

    decoded_header[2,3,5,6&0xF0,7&1] must equal the nO-derived values; for a
    given nO this R is unique. Precomputed once (65536 block transforms ~0.5s).
    """
    table = {}
    base = bytearray(16)
    base[0:12] = _AV_HEAD12
    base[3] |= 2                       # iotc_SendMessage sets plain[3] |= 2
    for r in range(0x10000):
        base[12] = r & 0xFF
        base[13] = (r >> 8) & 0xFF
        w = _block_transform(bytes(base))
        d = bytes(w[i] ^ _XOR_KEY[i] for i in range(16))   # decoded header
        table[(d[2], d[3], d[5], d[6] & 0xF0, d[7] & 1)] = bytes([r & 0xFF, (r >> 8) & 0xFF])
    return table


_R_TABLE = None


def nO_recover_R(nO_raw: bytes):
    """Recover the session R from the camera's nO (LAN_SEARCH_R / 0x0206) response.

    The camera's nO builder echoes the client's probe {R, fingerprint} verbatim
    into the response payload — R at plaintext[188:190] (LE u16), client fingerprint
    at [190:196]. So the recovery is simply `inv_transcode(nO_wire)[188:190]`.

    This is MID-INDEPENDENT (works for any client fingerprint), unlike the legacy
    `header_R_for_nO` xor-table heuristic which was tuned to one fingerprint and
    missed on a changed MAC. Returns R as int, or None if `nO_raw` is too short to
    contain the echo.
    """
    if len(nO_raw) < 190:
        return None
    return struct.unpack("<H", inv_transcode(bytes(nO_raw))[188:190])[0]


def header_R_for_nO(nO_dec: bytes):
    """Legacy alternative to `nO_recover_R` (prefer that). Return the 2-byte session
    value R = [12:14] from an xor-table correlation on the *xor_frame*-decoded nO at
    [178:184]. This works only because those wire bytes sit in the SAME 16-byte
    transcode block [176:192] as the cleanly-echoed R, so the block transform diffuses
    R into [178:184] consistently — but ONLY for a fixed device name + fingerprint,
    hence it is MAC-tuned. Prefer `nO_recover_R(nO_raw)`."""
    global _R_TABLE
    if _R_TABLE is None:
        _R_TABLE = _build_R_table()
    key = (nO_dec[178], (nO_dec[179] | 0x40) & 0xFF, (nO_dec[181] ^ 0x40) & 0xFF,
           nO_dec[182] & 0xF0, nO_dec[183] & 1)
    return _R_TABLE.get(key)


def build_av_connect(nonce: bytes, nO_dec: bytes, channel: int,
                     account: bytes = b"admin@YOUR_ACCOUNT",
                     password: bytes = b"YOUR_PASSWORD",
                     token: bytes = None, R: int = None) -> bytes:
    """Build the 598-byte AV CONNECT wire packet (real plaintext -> transcode).

    `R` is the per-session short id (== the probe R == GenShortRandomID); when None
    it is recovered from `nO_dec` (the camera echoes our probe R back in nO, so the
    two agree).  `nonce` is unused (kept for API/oracle compatibility).  `token`
    defaults to a random 4-byte value (this is what the native lib does — it is NOT
    validated).
    """
    if R is None:
        Rb = header_R_for_nO(nO_dec)
        if Rb is None:
            raise RuntimeError("no R for this nO (header cannot match) — bad nO?")
        R = struct.unpack("<H", Rb)[0]
    Rb = struct.pack("<H", R & 0xFFFF)
    if token is None:
        token = os.urandom(4)

    p = bytearray(598)
    p[0:12] = _AV_HEAD12
    p[6] = channel
    p[12:14] = Rb
    p[16:20] = bytes.fromhex("0c000000")
    p[20:22] = Rb
    p[22:28] = _AV_MID
    p[30] = 0x0B
    p[44] = 0x22
    p[45] = 0x02
    p[46] = 0x01 if (channel % 2 == 0) else 0x00
    p[29] = 0x00 if (channel % 2 == 0) else 0x20
    # Native builds the av-login token ONCE per session, not per channel: it
    # generates a 31-bit `tutk_platform_rand()` value the first time and REUSES it
    # for all channels. On the wire the transport stamps a per-FRAME parity: even
    # frames carry the base T, odd frames T+1, where +1 is a FULL 32-bit
    # little-endian increment (it carries past byte0 when byte0 == 0xFF), and
    # [49:52] is constant across channels except in that carry case. The token is
    # not a connect gate.
    base = int.from_bytes(token[:4], "little")
    p[48:52] = struct.pack("<I", (base + (channel % 2)) & 0xFFFFFFFF)
    p[52:52 + len(account)] = account
    p[309:309 + len(password)] = password
    # static tail
    p[566] = 0x01
    p[570] = 0x04
    p[574] = 0xFB
    p[575] = 0x07
    p[576] = 0x1F
    p[588] = 0x03
    p[594] = 0x01
    p[3] |= 2                           # iotc_SendMessage modification, then transcode
    return transcode(bytes(p))


# ── 0x2043 IOTC session-registration exchange ───────────────────────────────
# 52-byte XOR-framed packet the native lib sends when the camera does not grant
# the av-connect immediately (absent from clean/instant successes; present in a
# "struggling" session). The decoded 52 bytes are CONSTANT except offsets [44]
# and [47], which are lifted from the av-connect's session-header region
# (xor_frame(av_wire)[28] and [31], a pure function of R).
#
# Replaying this on its own does not unlock the connect: the camera ignores a
# byte-identical python 0x2043. Kept here as a validated artifact.
_X2043_CONST = bytes.fromhex(
    "20431020000000000040a02140020002"   # [0:16]  type 0x2043, req marker 1020
    "751353425574a54545382435a43982e5"   # [16:32] constant IOTC client token
    "aa57130010000000930413136b41c385"   # [32:48] ([44],[47] patched per session)
    "0d58cfe5")                          # [48:52]


def build_x2043(av_wire: bytes) -> bytes:
    """Build the 52-byte 0x2043 session-registration packet for a session.

    `av_wire` is any built av-connect for the same nO; the two session-specific
    bytes are derived from its session-header region.
    """
    avd = xor_frame(av_wire)
    p = bytearray(_X2043_CONST)
    p[40] = avd[24] | 0x13      # session-specific
    p[44] = avd[28]
    p[45] = avd[29] | 0x40      # session-specific (avd29=00->45=40, avd29=01->45=41)
    p[47] = avd[31]
    return xor_frame(bytes(p))


# ── AV / IOCTL data frames (post-connect) ───────────────────────────────────
# Once the session is granted, avSendIOCtrl rides the IOTC LAN *data channel*
# (IOTC packet type 0x0407 client->cam, 0x0408 cam->client). Each frame is the
# real plaintext run through `transcode` (NOT xor_frame), exactly like connect.
# Layout (a GET_HW_CONTROL request reproduces native's 76-byte wire packet):
#
#   [0:2]   04 02                       constant
#   [2:4]   1a 0a                       SEND flags (RECV uses 1d 0a)
#   [4:6]   <u16 LE>  = len(frame) - 16
#   [6:8]   <u16 LE>  outbound data-channel sequence (continues from connect)
#   [8:12]  07 04 21 00                 IOTC data type 0x0407 (+0x21)
#   [12:14] <R LE>   [14:16] 00 00      R doubles as the "session id" post-connect
#   [16]    0c                          (then 00 00 00)
#   [20:22] <R LE>   [22:28] <fingerprint>  R + client fingerprint (_AV_MID)
#   [28]    sub-type: 0x0c = DATA (IOCTL), 0x09 = ACK    [30:32] 0b 00
#   [32:34] <u16 LE> piggyback cumulative-ACK    [44:46] 00 70
#   [46:48] <u16 LE> per-direction app-message index (0,1,2,...)
#   [48:52] 01 00 00 00                 [52:54] <u16 LE> AVIOCtrl length = 4+len(payload)
#   [64:68] <u32 LE> IOCTL io_type      [68:]  IOCTL payload
#
# The camera's IOCTL RESPONSE is the same shape with the response io_type
# (== request | 1) at [64:68] and the response payload at [68:68+avlen-4].

def build_ioctl_data(R, seq, relseq, frmno, io_type, payload):
    """Build the wire IOCTL request DATA frame (transcode of the real plaintext).

      [6:8]   seq    — packet counter (bumps on every send incl. retransmit).
      [32:34] relseq — the SENDER's own reliable-frame sequence: a single counter that
                       increments by 1 for EVERY reliable frame this side sends (DATA
                       and ACK alike), reused unchanged on a retransmit. Native's DATA
                       frames carry 0,19,21,23,… (the gaps are the ACKs sent between
                       requests). It is NOT a cumulative-ACK.
      [34:36] 0      — uninitialised/don't-care; the camera ignores it. Native wire
                       carries 0x0000, which is what is emitted here (consistent with
                       build_data_ack / build_resend_req).
      [40:44] 0      — DATA frames carry no data-ack (that rides on ACK frames).
      [46:48] frmno  — IOCtrl FrmNo (0,1,2,…), per request.
      [56:58] frmno  — native mirrors the FrmNo here as the data message-index.
    """
    pl_len = len(payload)
    total = 68 + pl_len
    p = bytearray(total)
    p[0:4] = b"\x04\x02\x1a\x0a"
    struct.pack_into("<H", p, 4, total - 16)
    struct.pack_into("<H", p, 6, seq & 0xFFFF)
    p[8:12] = b"\x07\x04\x21\x00"
    struct.pack_into("<H", p, 12, R & 0xFFFF)
    p[16] = 0x0C
    struct.pack_into("<H", p, 20, R & 0xFFFF)
    p[22:28] = _CLIENT_FINGERPRINT
    p[28] = 0x0C
    p[30] = 0x0B
    struct.pack_into("<H", p, 32, relseq & 0xFFFF)         # sender's reliable-frame seq
    # [34:36] left 0 (uninitialised/don't-care; native real-wire=0; camera ignores)
    p[45] = 0x70
    struct.pack_into("<H", p, 46, frmno & 0xFFFF)
    p[48] = 0x01
    struct.pack_into("<H", p, 52, 4 + pl_len)
    struct.pack_into("<H", p, 56, frmno & 0xFFFF)          # FrmNo mirror == data message-index
    struct.pack_into("<I", p, 64, io_type)
    p[68:68 + pl_len] = payload
    return transcode(bytes(p))


def build_data_ack(R, seq, relseq, ackord, C, D, data_ack=0, sack=None, ts=None, ts32=None, win=0):
    """IOTC data-channel ACK (sub-type 0x09). TWO ack channels are multiplexed here.

    The camera multiplexes TWO logical reliable streams onto the data channel, each
    acked by a DIFFERENT field of this one ACK frame:

      * The **AV fragment stream** (video/audio) is acked by the **C/D pair at
        [36:40]**: D = highest camera AV fragment-seq ([46:48]) seen, C = the previous
        ACK's D. This advances the camera's AV send-window and keeps the video flowing;
        C==D==0xFFFF is the idle sentinel before any AV arrives.
      * The **reliable IO/control stream** (IOCTL/status RESPONSES) is acked by the
        cumulative **[40:44]** = highest contiguous camera IO message-index ([56:58])
        received. This MUST advance: after a status read, the camera holds each IOCTL
        response in its reliable send-FIFO and RETRANSMITS it until [40:44] covers it;
        only then does it free the FIFO and start sending video. Leaving it 0 => the
        camera never advances past the last IOCTL response and 0 video frames ever
        arrive. Native climbs [40:44] 0..N across the N IOCTL responses, then HOLDS it
        (no more IO frames) while C/D carries the video.

      [32:34] relseq — the SENDER's own reliable-frame sequence (+1 per reliable frame).
      [34:36] 0      — client low edge (0 in the data phase).
      [36:38] C      — previous ACK's D (the prior low edge);  C[n] == D[n-1].
      [38:40] D      — highest camera AV fragment-seq ([46:48]) seen (gaps skipped).
      [40:44] data_ack — cumulative ack of the camera's reliable IO message-index.
      [46:48] ackord — our own monotonic ACK ordinal.
      [48:50] 0x1a22 stamp word.  [50:52] rolling 16-bit ms timestamp.

    The [48:52] timestamp is a 32-bit ~uptime-ms stamp stored word-swapped:
    wire[48:50] is the slowly-drifting high word (which reads as 0x1a22 in a short
    session) and wire[50:52] is the fast-rolling low-16 ms timestamp. The camera does
    NOT validate the timestamp, so it is byte-fidelity only.
    """
    # The [42:44] field is the OUT-OF-ORDER fragment count and, when non-zero, native
    # APPENDS that many 2-byte (frag_seq - C) SACK entries at wire[50:]. frame_len =
    # 50 + 2*count with [4:6] (IOTC content length) = frame_len-16. count==0 => a
    # 52-byte ACK with the [50:52] ms timestamp.
    # The SACK list is EFFICIENCY-ONLY (it does NOT gate camera resend — count=0
    # already requests the whole C+1..D range), so `sack` is only ever non-empty in
    # resend_mode under loss; the pure-born best-effort path passes sack=None.
    # `sack` is a list of ABSOLUTE camera fragment-seqs (or None); this function
    # encodes each as its 2-byte (frag_seq - C) wire offset in the loop below, so
    # build_data_ack owns the full wire encoding. The len/[42:44]/[4:6] structure is
    # byte-exact; the entry VALUES are best-effort (native's exact OOO-FIFO ordering
    # is server-side and not reproduced, but SACK is efficiency-only).
    n = len(sack) if sack else 0
    frame_len = 50 + 2 * max(n, 1)                          # n==0 -> 52 (timestamp); n>=1 -> 50+2n
    p = bytearray(frame_len)
    p[0:4] = b"\x04\x02\x1a\x0a"
    struct.pack_into("<H", p, 4, frame_len - 16)           # [4:6] IOTC content length (grows w/ SACK)
    struct.pack_into("<H", p, 6, seq & 0xFFFF)
    p[8:12] = b"\x07\x04\x21\x00"
    struct.pack_into("<H", p, 12, R & 0xFFFF)
    p[16] = 0x0C
    struct.pack_into("<H", p, 20, R & 0xFFFF)
    p[22:28] = _CLIENT_FINGERPRINT
    p[28] = 0x09
    p[30] = 0x0B
    struct.pack_into("<H", p, 32, relseq & 0xFFFF)          # sender's reliable-frame seq
    struct.pack_into("<H", p, 34, win & 0xFFFF)            # [34:36] (0 default; native leaks 0x5838 in establishment only)
    struct.pack_into("<H", p, 36, C & 0xFFFF)              # C = previous D (low edge)
    struct.pack_into("<H", p, 38, D & 0xFFFF)              # D = highest camera frag-seq seen
    struct.pack_into("<H", p, 40, data_ack & 0xFFFF)       # [40:42] = cumulative IO msg-index ack
    struct.pack_into("<H", p, 42, n & 0xFFFF)              # [42:44] = OOO/SACK entry count
    struct.pack_into("<H", p, 46, ackord & 0xFFFF)         # our ack ordinal
    ms16 = (int(time.time() * 1000) if ts is None else ts) & 0xFFFF
    if n == 0:
        if ts32 is not None:
            # Full-fidelity timestamp: native's [48:52] is a 32-bit ms value V stored
            # WORD-SWAPPED — [48:50]=high16(V), [50:52]=low16(V). V = now_ms - reference.
            # [48:50] is constant 0x1a22 across a short session (V's high word; ticks to
            # 0x1a23 only after the low word wraps ~every 65 s) and [50:52] advances
            # ~1 ms/ms. Pure passes V=_ts_word() so high16 starts at 0x1a22 and low16
            # climbs ~1 ms/ms, using a single stable session reference (no mid-stream
            # reset). The camera IGNORES this field, so it is byte-fidelity only.
            struct.pack_into("<H", p, 48, (ts32 >> 16) & 0xFFFF)   # [48:50] high16(V)
            struct.pack_into("<H", p, 50, ts32 & 0xFFFF)          # [50:52] low16(V) ~1ms/ms
        else:
            struct.pack_into("<H", p, 48, 0x1A22)             # [48:50] stamp hi-word (count==0)
            struct.pack_into("<H", p, 50, ms16)               # [50:52] rolling ms low-word
    else:
        # count>0: native displaces [50:52] with the first SACK entry and carries the
        # ms low-word at [48:50] (~15ms steps with ackord — not the 0x1a22 stamp word).
        struct.pack_into("<H", p, 48, ms16)               # [48:50] ms low-word
        for i, frag in enumerate(sack):                    # [50:50+2n] per-frag SACK: wire = (frag_seq - C)
            struct.pack_into("<H", p, 50 + 2 * i, (frag - C) & 0xFFFF)
    return transcode(bytes(p))


# ── retransmit (NAK) signalling ────────────────────────────────────────────────
# These two builders reproduce, byte-for-byte in their headers, the resend-control
# frames native sends ~8×/s during streaming.
#
# They are wired into _av_reader via maybe_nak/_send_nak under self._resend_mode
# (default ON), and are fired from pre-video too — without actually signalling a gap
# the camera is never told what to resend. The reader is stall-guarded (50 ms
# forward-skip + a depth cap, see _GAP_STALE / maybe_nak) so the held-C edge can no
# longer dead-stall the stream under loss. The byte maps are the held-C ACK, the
# `[40:44]` cumulative reliable-ack, and the 0x0a/0x0b pair below.

def build_resend_req(R, seq, relseq, highwater=0, ts=None,
                     resend_timeout_ms=0, recv_count=None, win=0):
    """Native resend-control / AV-statistic frame (sub 0x0a, [29]=0x08). 44 bytes.

    Byte map:
      [28]=0x0a sub, [29]=0x08 marker, [30:32]=0x000b reliable-chan id (immediates).
      [32:34] relseq  — shared reliable-frame seq (post-incr +1 per frame).
      [34:36] uninit  — stack leftover; pure zeroes it (harmless).
      [36:38] ms-clock— session millisecond clock low-16 (monotonic).
      [38:40] EWMA resend-TIMEOUT estimate in ms (`new = 0.15*old + 0.85*sample_ms`;
                        used as the resend age-threshold). Live: 27-50 ms, jittery.
      [40:44] 0 in steady-state telemetry; native sets it nonzero only on the
                        loss-triggered resend path (`highwater`).

    Params: `resend_timeout_ms` populates [38:40] (default 0). `recv_count` is a
    deprecated alias for the same field, kept so older call-sites keep working.
    """
    p = bytearray(44)
    p[0:4] = b"\x04\x02\x1a\x0a"
    struct.pack_into("<H", p, 4, 28)
    struct.pack_into("<H", p, 6, seq & 0xFFFF)
    p[8:12] = b"\x07\x04\x21\x00"
    struct.pack_into("<H", p, 12, R & 0xFFFF)
    p[16] = 0x0C
    struct.pack_into("<H", p, 20, R & 0xFFFF)
    p[22:28] = _CLIENT_FINGERPRINT
    p[28] = 0x0A
    p[29] = 0x08
    p[30] = 0x0B
    struct.pack_into("<H", p, 32, relseq & 0xFFFF)
    struct.pack_into("<H", p, 34, win & 0xFFFF)              # [34:36] (native 0x0a leaks 0x5838 in establishment only; 0 default)
    ms = int(time.time() * 1000) if ts is None else ts
    struct.pack_into("<H", p, 36, ms & 0xFFFF)
    field_3840 = recv_count if recv_count is not None else resend_timeout_ms
    struct.pack_into("<H", p, 38, field_3840 & 0xFFFF)        # [38:40] EWMA resend-timeout
    struct.pack_into("<I", p, 40, highwater & 0xFFFFFFFF)     # [40:44] 0 steady / loss-only
    return transcode(bytes(p))


def build_resend_b(R, seq, recv_count, ts=None):
    """Resend-control companion (sub 0x0b). 48 bytes. Sent immediately before each 0x0a.

    UNRELIABLE — [32:34] is always 0 (does not consume a reliable-frame seq). Fields:
      [28]=0x0b, [30:32]=0x000b, [32:36]=0, [36:38]=rolling ms ts,
      [38:40]/[40:42]=a small receive-rate count (native 2-12, usually equal), [42:48]=0.
    """
    p = bytearray(48)
    p[0:4] = b"\x04\x02\x1a\x0a"
    struct.pack_into("<H", p, 4, 32)
    struct.pack_into("<H", p, 6, seq & 0xFFFF)
    p[8:12] = b"\x07\x04\x21\x00"
    struct.pack_into("<H", p, 12, R & 0xFFFF)
    p[16] = 0x0C
    struct.pack_into("<H", p, 20, R & 0xFFFF)
    p[22:28] = _CLIENT_FINGERPRINT
    p[28] = 0x0B
    p[30] = 0x0B
    ms = int(time.time() * 1000) if ts is None else ts
    struct.pack_into("<H", p, 36, ms & 0xFFFF)
    struct.pack_into("<H", p, 38, recv_count & 0xFFFF)
    struct.pack_into("<H", p, 40, recv_count & 0xFFFF)
    return transcode(bytes(p))


# ── two-way talk: AAC-LC av-data uplink on a reversed-role talk channel ──────────
# Talk is the av-connect handshake REVERSED on a separate channel (default ch1): the camera logs
# into US (we are the av-server) and pulls audio. NB a reliable-IO / G.711-µ-law uplink on ch0
# ([29]=0x05) is ACKed by the camera but never DECODED — the working uplink is AAC-LC on ch1
# modelled on the camera's OWN downlink audio av-data frame.
_TALK_GRANT_CAP_DEFAULT = b"\xe0\xfe\xfe\x01"   # 4.3.x capability word; fallback if connect() didn't capture it


def _aac_units(path, rate=16000, gain=1.0, format=None, options=None):
    """Transcode any audio file -> a list of AAC-LC ADTS frames via PyAV (no ffmpeg binary — same
    dependency as snapshot/record; stays ffmpeg-agnostic). Each frame is self-describing (7-byte
    ADTS header), which is the camera's downlink format and what the talk uplink mirrors.
    `gain` is a linear amplitude multiplier (1.0 = unchanged, <1 quieter, >1 louder), applied via
    libav's `volume` filter — the only reliable talk-volume lever (the camera's speaker_level is
    firmware-managed)."""
    import av
    import io as _io
    buf = _io.BytesIO()
    out = av.open(buf, mode='w', format='adts')          # the ADTS muxer writes the AAC-LC headers
    ostream = out.add_stream('aac', rate=rate)
    try:
        ostream.bit_rate = 32000
    except Exception:
        pass
    resampler = av.AudioResampler(format='fltp', layout='mono', rate=rate)  # AAC encoder input fmt
    graph = None
    if gain != 1.0:                                       # apply volume via libav's filter (no numpy)
        graph = av.filter.Graph()
        _src = graph.add_abuffer(format='fltp', sample_rate=rate, layout='mono')
        _vol = graph.add('volume', volume=str(gain))
        _snk = graph.add('abuffersink')
        _src.link_to(_vol); _vol.link_to(_snk); graph.configure()

    fifo = av.AudioFifo()
    def _process_fifo(flush=False):
        while True:
            if fifo.samples >= 1024:
                gf = fifo.read(1024)
                gf.pts = None
                _encode_frame(gf)
            elif flush and fifo.samples > 0:
                gf = fifo.read(fifo.samples)
                gf.pts = None
                _encode_frame(gf)
                break
            else:
                break

    pts_counter = 0
    def _encode_frame(fr):
        nonlocal pts_counter
        fr.pts = pts_counter
        pts_counter += fr.samples
        for pkt in ostream.encode(fr):
            out.mux(pkt)

    with av.open(path, format=format, options=options) as inp:
        for frame in inp.decode(audio=0):
            frame.pts = None
            for rf in resampler.resample(frame):
                if graph is None:
                    fifo.write(rf)
                    _process_fifo()
                else:
                    graph.push(rf)
                    while True:
                        try:
                            gf = graph.pull()
                        except (av.error.BlockingIOError, av.error.EOFError):
                            break
                        gf.pts = None
                        _encode_frame(gf)
                        
        for _ in range(55):
            silent = av.AudioFrame(format='fltp', layout='mono', samples=1024)
            silent.sample_rate = rate
            if graph is None:
                fifo.write(silent)
                _process_fifo()
            else:
                graph.push(silent)
                while True:
                    try:
                        gf = graph.pull()
                    except (av.error.BlockingIOError, av.error.EOFError):
                        break
                    gf.pts = None
                    _encode_frame(gf)

        _process_fifo(flush=True)
        for pkt in ostream.encode(None):                 # flush the encoder
            out.mux(pkt)
    out.close()
    adts = buf.getvalue()
    units, i = [], 0
    while i + 7 <= len(adts):
        if adts[i] != 0xFF or (adts[i + 1] & 0xF6) != 0xF0:
            break
        flen = ((adts[i + 3] & 0x03) << 11) | (adts[i + 4] << 3) | ((adts[i + 5] >> 5) & 0x07)
        if flen < 7 or i + flen > len(adts):
            break
        units.append(adts[i:i + flen])                   # keep the whole ADTS frame
        i += flen
    return units


def _talk_frameinfo(ts_sec, rate=16000):
    """24-B audio FRAMEINFO trailer mirroring the camera's downlink audio: codec_id 0x0088 @[0:2],
    sample_rate @[8:10], channels=1 @[10:12], ts_sec @[12:16]. The camera reads its length from the
    talk-audio frame's [50:52] (==24)."""
    b = bytearray(24)
    struct.pack_into('<H', b, 0, 0x0088)
    struct.pack_into('<H', b, 8, rate)
    struct.pack_into('<H', b, 10, 1)
    struct.pack_into('<I', b, 12, ts_sec & 0xFFFFFFFF)
    return bytes(b)


def build_talk_grant(R, channel, seq, login_dec, cap=None):
    """88-byte talk-channel GRANT (host->cam), modelled on the camera's own 4.3.x av-connect grant.
    Session R @[12:14]/[20:22]; talk channel @[14]; the capability word @[32:36] is the value the
    camera advertised in its own grant (passed via `cap`; defaults to the proven 4.3.x constant).
    The [48:52] token is echoed from the camera's talk-login (per-session)."""
    Rb = struct.pack('<H', R & 0xFFFF)
    p = bytearray(88)
    p[0:4] = b'\x04\x02\x1a\x0a'                  # host->cam
    struct.pack_into('<H', p, 4, 88 - 16)
    struct.pack_into('<H', p, 6, seq & 0xFFFF)
    p[8:12] = b'\x07\x04\x21\x00'
    p[12:14] = Rb
    p[14] = channel & 0xFF                         # talk channel
    p[16:20] = b'\x0c\x00\x00\x00'
    p[20:22] = Rb
    p[22:28] = _CLIENT_FINGERPRINT
    p[28] = 0x00                                   # sub = connect/grant
    p[29] = 0x21
    p[30] = 0x0B
    p[32:36] = cap or _TALK_GRANT_CAP_DEFAULT      # mirror the camera's advertised capability word
    struct.pack_into('<I', p, 44, 0x24)
    p[48:52] = (login_dec[48:52] if login_dec and len(login_dec) >= 52 else os.urandom(4))
    p[56:60] = b'\x00\x01\x00\x01'
    p[60:64] = b'\x01\x00\x00\x00'
    p[64:68] = b'\x04\x00\x00\x00'
    p[68:72] = b'\xfb\x07\x1f\x00'
    p[80:84] = b'\x63\x06\x13\x10'
    p[84:88] = b'\x04\x0c\x0c\x63'
    return transcode(bytes(p))


def build_talk_audio(R, channel, seq, relseq, frag, msgidx, au):
    """Uplink audio AV-DATA frame on the talk channel — modelled on the camera's OWN downlink audio
    av-data frame so the camera routes it to the audio decoder (the reliable-IO layout build_ioctl_data
    uses, [45]=0x70/[29]=0x05, delivered but did NOT decode).
      [14]=channel  [28]=0x0c  [29]=0x01 (audio av-data marker)  [44:46]=0x0103 (av-data type, vs 0x7000
      IO)  [50:52]=24 (FRAMEINFO trailer len)  avlen@[52:54]  msgidx@[56:58]  [60:64]=msgidx+1.
    `au` is the AAC-LC ADTS frame followed by the 24-B _talk_frameinfo trailer."""
    p = bytearray(64 + len(au))
    p[0:4] = b'\x04\x02\x1a\x0a'
    struct.pack_into('<H', p, 4, len(p) - 16)
    struct.pack_into('<H', p, 6, seq & 0xFFFF)
    p[8:12] = b'\x07\x04\x21\x00'
    struct.pack_into('<H', p, 12, R & 0xFFFF)
    p[14] = channel & 0xFF
    p[16:20] = b'\x0c\x00\x00\x00'
    struct.pack_into('<H', p, 20, R & 0xFFFF)
    p[22:28] = _CLIENT_FINGERPRINT
    p[28] = 0x0C
    p[29] = 0x01
    p[30] = 0x0B
    struct.pack_into('<H', p, 32, relseq & 0xFFFF)
    p[34] = 0x0B
    p[39] = 0x14
    p[40] = 0x01
    p[44] = 0x03
    p[45] = 0x01
    struct.pack_into('<H', p, 46, frag & 0xFFFF)
    p[48] = 0x01
    p[50] = 0x18
    struct.pack_into('<H', p, 52, len(au) & 0xFFFF)
    struct.pack_into('<H', p, 56, msgidx & 0xFFFF)
    struct.pack_into('<I', p, 60, (msgidx + 1) & 0xFFFFFFFF)
    p[64:64 + len(au)] = au
    return transcode(bytes(p))


# ── session ───────────────────────────────────────────────────────────────────

class _StreamGet:
    """A mid-stream GET request handed to the reader thread.

    While streaming, the reader is the SOLE socket sender (a second sender would race its
    sequence numbers and the recvfrom drain). So a thread that wants a camera GET *during*
    a stream fills one of these slots; the reader sends it and captures the matching
    response, and the requester waits on `done`. Read-only telemetry path — see
    get_during_stream().
    """
    __slots__ = ('io_type', 'payload', 'resp_type', 'sent', 'last_tx', 'done', 'result')

    def __init__(self, io_type, payload):
        self.io_type = io_type
        self.payload = payload
        self.resp_type = io_type | 1          # GET req (even) -> resp = req | 1
        self.sent = False
        self.last_tx = 0.0
        self.done = threading.Event()
        self.result = None


def stats_delta(prev, cur):
    """Per-interval view of two get_stats() snapshots (the deltas + rates).

    get_stats() returns CUMULATIVE counters plus a wall-clock 't'; this turns a (prev, cur)
    pair into the interval values: fps/bitrate over the interval, the interval loss% and
    recovery%, and the raw deltas. Pure function. With prev=None (the first sample) it
    reports the cumulative values as the interval.
    """
    if prev is None:
        prev = {k: 0 for k in cur}
        prev['t'] = cur.get('t', 0.0) - 1.0          # avoid div-by-zero on the first tick
    dt = max(1e-6, cur.get('t', 0.0) - prev.get('t', 0.0))

    def d(k):
        return cur.get(k, 0) - prev.get(k, 0)

    d_recv = d('frags_recv'); d_rec = d('resend_recovered')
    d_holes = d('frags_lost')
    d_first = max(0, d_recv - d_rec)                 # frags delivered on first transmission
    d_total = d_first + d_holes                      # all distinct frags the camera sent
    d_vid = d('au_video')
    d_bytes = d('bytes_video') + d('bytes_audio')
    return {
        'interval_s': dt,
        'fps': d_vid / dt,
        'au_video': d_vid,
        'au_audio': d('au_audio'),
        'bitrate_kbps': d_bytes * 8.0 / 1000.0 / dt,
        'loss_pct': (100.0 * d_holes / d_total) if d_total else 0.0,
        'frags_recv': d_recv,
        'frags_lost': d_holes,
        'resend_req': d('resend_req'),
        'resend_recovered': d_rec,
        'recovery_pct': (100.0 * d_rec / d_holes) if d_holes else 100.0,
        'recovery_events': d_rec,
        'au_incomplete': d('au_incomplete'),
        'kf_total': d('kf_total'),
        'kf_incomplete': d('kf_incomplete'),
        'ts_garbage': d('ts_garbage'),
        'ts_regress': d('ts_regress'),
        'gap_cap_jumps': d('gap_cap_jumps'),
        'lone_skips': d('lone_skips'),
    }


class TUTKDirectSession:
    """Pure-Python TUTK LAN session (no native library).

    The handshake (working end-to-end):
        1. pick R = gen_R()            (a fresh short id in [1,0x7FFF])
        2. probe  -> camera:32761      (transcode of the UID/R/fingerprint plaintext)
        3. nO     <- camera            (echoes our R; we sanity-check header_R_for_nO)
        4. ACK    -> camera P2P port   (= probe with plaintext[64]=0x02)
        5. av0/av1-> camera P2P port   (header R == probe R)
        6. 2041   <- camera            (success; session_hdr = reply[16:32])
        7. av2/av3 + keepalive
    The camera reaches "connected" (status==2) at step 4 via LAN_SEARCH_R_3, which
    is gated on the R/fingerprint client-id the corrected probe finally carries.
    """

    def __init__(self, camera_ip=None, camera_port=39099,
                 account=b"admin@YOUR_ACCOUNT", password=b"YOUR_PASSWORD",
                 uid=_DEFAULT_UID, channels=None, verbose=False,
                 full_fidelity=True,
                 defer_stream_start=None, defer_video_start_late=None):
        self.camera_ip = camera_ip
        self.camera_port = camera_port
        self.account = account
        self.password = password
        self.uid = uid
        # Which AV channels to open in the handshake. Native opens ch0..39 (44
        # av-connect frames, ch{2k,2k+1} pairs @50ms; ch0 = video). The lib allocates
        # one avIndex per avClientStart call; the channel split is an app convention.
        # Pure's RECEIVE path is channel-AGNOSTIC — it demuxes video/audio by content,
        # never by a channel id — so the channel SET changes only the handshake, not
        # decode (ch1-only streams full video+audio). Default = range(40) to be
        # byte-faithful to native's full av-connect burst; functionally [0,1,2,3] is
        # equivalent.
        self._channels = list(channels) if channels else list(range(40))
        # Optional human-readable trace of connect()/streaming (off by default; never
        # changes the wire). See _vlog.
        self._verbose = verbose
        # full_fidelity is the master wire-fidelity flag (default True per the standing
        # "always match native's on-wire behaviour" preference). It folds in the IOCTL
        # cadence AND gates the three remaining native↔pure wire divergences (all
        # camera-IGNORED for arming, so these are byte-fidelity only, never an arming
        # lever):
        #   (1) ACK timestamp [48:52]   — native's word-swapped 32-bit clock (_ts_word /
        #       build_data_ack ts32) vs pure's const-0x1a22 + free-run ms.
        #   (2) NAK cadence _nak_interval — native ~4.8 0x0b/0x0a PAIRS/s (0x0a≈4.3-4.7/s,
        #       0x0b≈4.6-4.8/s, total ~9.1-9.6 FRAMES/s) => 0.19 s. See _nak_interval below.
        #   (3) SACK list _compute_sack — native lists EVERY out-of-order fragment (full
        #       OOO range, frame_len 50+2N); pure's best-effort path truncated at
        #       _FRAG_WINDOW. Under fidelity pure emits the full list.
        # When full_fidelity is False (the fast-start / low-latency path) every one of
        # these reverts to the simpler/faster behaviour so the ~0.5 s-TTFF shipping path
        # is unchanged. Arming is firmware-gated either way, so the flag changes ONLY
        # wire-fidelity/latency, never whether AV resend arms.
        self._full_fidelity = full_fidelity
        # Cadence sub-flags, subordinate to full_fidelity: None (the default) => follow
        # full_fidelity; an explicit True/False overrides just that stage.
        #   defer_stream_start     — 0x0300 (AUDIOSTART/stream-start) ~_MID_IOCTL_SECS
        #     after 0x00FF (THE latency lever: True => first frame ~5 s in = native
        #     cadence; False => ~0.5-2 s, camera-bound).
        #   defer_video_start_late — 0x01FF (START) ~_LATE_IOCTL_SECS after 0x0300.
        # so e.g. disabling defer_stream_start keeps timestamp/NAK/SACK fidelity but
        # starts video fast.
        self._defer_stream_start = (full_fidelity if defer_stream_start is None
                                    else defer_stream_start)
        self._defer_video_start_late = (full_fidelity if defer_video_start_late is None
                                        else defer_video_start_late)
        # NAK pair cadence (see _send_nak/maybe_nak). Fidelity => native-matched 0.19 s;
        # fast path => 0.137 s (faster, arming-irrelevant — NAK rate never gates the fast
        # TTFF path either).
        self._nak_interval = 0.19 if full_fidelity else 0.137
        # Per-session reference for the word-swapped ACK timestamp (lazily set on first
        # use by _ts_word so high16 starts at native's 0x1a22).
        self._ts_ref = None
        # The [34:36] field of every reliable ACK/NAK. Native leaves it uninitialised:
        # it carries the leftover stack value 0x5838 for the first ~26 establishment
        # frames (while C==D==0xFFFF) then 0 for the whole data phase (a leak, NOT a
        # buffer-free-space "window"; native streams armed sending [34:36]=0, so 0 is
        # not a stall trigger). Pure writes 0 by default. Set nonzero (env
        # CUBOAI_ADV_WINDOW, e.g. 0x5838) to advertise native's establishment value.
        self._advertise_window = int(os.environ.get("CUBOAI_ADV_WINDOW", "0") or "0", 0)
        self.session_hdr = None
        self._sock = None
        self._R = None
        self._cam = None
        self._seq = 0            # outbound packet counter [6:8] (bumps on every send)
        self._relseq = 0         # OUR reliable-frame seq [32:34] (+1 per reliable frame: DATA+ACK)
        self._frmno = 0          # IOCtrl FrmNo [46:48]==[56:58] on DATA (== __getIOCtrlFrmNo)
        self._ack_ord = 0        # our ACK ordinal [46:48] on ACK frames
        self._data_ack = 0       # cumulative data-ACK: highest contiguous camera msg-index [56:58]
        self._cam_msgs = set()   # camera DATA message-indices received (for contiguity)
        self._got_first = False  # have we seen the camera's msg-index 0 (login/system frame) yet
        self._frag_D = None      # D: highest camera DATA fragment-seq [46:48] received (None=idle)
        self._frag_C = 0xFFFF    # C: previous ACK's D (the low edge, C[n]==D[n-1])
        # ── gap-tracking / resend ──────────────────────────────────────────────
        self._frag_edge = None        # highest CONTIGUOUS frag-seq (low-water; gap detect)
        self._frag_edge_acked = 0xFFFF  # edge value at our last held-D ack (resend_mode C)
        self._frag_received = set()   # recent received frag-seqs (drives the edge advance)
        self._frag_gap_ts = {}        # frag-seq -> time first seen as a gap (stale-skip)
        self._resend_mode = True      # held-C edge ack + 0x0b/0x0a NAK (gap signalling): pure
                                      # SIGNALS a gap — without it C==prev-D every ack and the
                                      # camera is never told what to resend. Stall-safe (50ms
                                      # forward-skip + a hard depth cap, see _GAP_STALE /
                                      # maybe_nak) so it can't dead-stall a stream under loss.
        self._session_fp = None  # camera's echoed [16:20] session token (from probe) for build_close
        # ── NAK 0x0b clock echo (THE arming discriminator) ─────────────────────
        # The camera sends its own ms-clock in cam->host 0x0a [36:38]; native ECHOES
        # that camera clock back in its host->cam 0x0b [36:38] (Δ=0). The camera gates
        # AV-retransmit reliable-peer commitment on this echo: a peer that reflects the
        # camera's heartbeat clock is armed; one that sends its own `now` or any
        # non-matching value floors (cam->host 0x09 0.1/s). Echo ON = parity with native
        # (default; arms at ~8.7/s).
        self._cam_clock = None        # latest camera ms-clock from cam->host 0x0a [36:38]
        self._cam_clock_ts = None     # local time.time() when _cam_clock was captured
        self._echo_cam_clock = os.environ.get("CUBOAI_ECHO_CAMCLOCK", "1") != "0"
        # ── gap-hold (resend wait) — close the recovery gap on the armed peer ──────
        # How long pure waits for a missing fragment's RESEND before forward-skipping the
        # held-C edge PAST it (= telling the camera "delivered", abandoning the gap). The
        # armed camera resends at ~370 ms for pure / ~140 ms for native. Holding longer
        # than the camera's resend latency lets resends land while the gap is still
        # requested. BOUNDED by _gap_depth_cap (jump near high-water when too many holes
        # pile up) so it can NEVER dead-stall, just trades a little forward-latency for
        # loss recovery. env CUBOAI_GAP_HOLD_MS (default = class _GAP_STALE) /
        # CUBOAI_GAP_DEPTH_CAP (default _GAP_DEPTH_CAP).
        self._gap_hold = float(os.environ.get("CUBOAI_GAP_HOLD_MS", "")
                               or self._GAP_STALE * 1000) / 1000.0
        self._gap_depth_cap = int(os.environ.get("CUBOAI_GAP_DEPTH_CAP", "")
                                  or self._GAP_DEPTH_CAP)
        # ── native-style SELECTIVE-REPEAT loss recovery (at native parity) ─────────
        # Once the clock echo arms the session the camera WILL resend for pure — but only
        # if pure signals losses the way native does:
        #   * The host→cam 0x09 SACK is a RESEND-REQUEST list — the camera resends EXACTLY
        #     the frag-seqs it carries (native recovers 82% of real losses, redundancy
        #     1.10×). The SACK must list the MISSING frags (holes), not the received ones
        #     (which only wastes resends on already-delivered frags). _compute_holes lists
        #     the holes.
        #   * Entries are (hole − C) u16 at [50:], so C must be ≤ every hole ⇒ C = una
        #     (contiguous edge); D = high-water. The una must HOLD at a genuine hole until
        #     its resend FILLS it (selective mode disables maybe_nak's 50ms GAP_STALE skip;
        #     the _gap_depth_cap is the only backstop) — else the hole leaves the (una,hw]
        #     window before it can be requested.
        #   * The camera only honours entries when count≥2 (a count-1 frame's [50:52] is
        #     the timestamp) — so lone holes wait for a second.
        #   * Each hole is (re)listed at most once per _RESEND_REQ_INTERVAL so it is
        #     requested ~once per resend round (native-like redundancy), and a still-missing
        #     hole is re-asked next round (covers a lost resend).
        #   * _send_nak drops to native's telemetry highwater=0; ACK_INTERVAL tightens to 0.04.
        # Result: recovery 76% (native 82%), redundancy 1.07× (native 1.10×), camera honours
        # the SACK (83% of listed frags resent), video healthy, armed 8.8/s. Default ON;
        # set env CUBOAI_SELECTIVE_ACK=0 to revert to the held-edge path (no selective repeat).
        self._selective_ack = os.environ.get("CUBOAI_SELECTIVE_ACK", "1") != "0"
        self._hole_req_ts = {}   # frag-seq -> last resend-request time (per-hole dedup)
        # ── adaptive resend-request interval state (default OFF) ─────────────────
        self._adaptive_rtt = os.environ.get("CUBOAI_ADAPTIVE_RTT", "0") != "0"
        self._hole_first_req = {}  # frag-seq -> time first SACK-listed (for the latency sample)
        self._rtt_ewma = None      # EWMA of first-request->arrival resend latency (seconds)
        self._rtt_n = 0            # clean samples folded into the EWMA so far
        # ── scale the reassembly grace with resend-latency × AU-rate ─────────────
        # grace=2 finalises an AU after only 2 AU-indices (≈<100 ms at ~24 AU/s), but a
        # recovered fragment lands ~RTT+camera-timer (~140 ms LAN) later → past the grace
        # → reassembly DROPS it → incomplete AU (14-24% even at moderate loss). The
        # dynamic grace holds an AU open ceil(EWMA·AU_rate)+1 indices so the resend still
        # lands inside the window. Reuses the adaptive EWMA (armed below even when the
        # adaptive *interval* is off). Bounded by _grace_max: a permanently-lost frag
        # finalises-INCOMPLETE after the cap, never stalling the AU forever. DEFAULT ON
        # (+13-19 pts LAN decode, zero transport cost); CUBOAI_GRACE_SCALE=0 to disable,
        # CUBOAI_GRACE_MAX to tune the cap.
        self._grace_scale = os.environ.get("CUBOAI_GRACE_SCALE", "1") != "0"
        # The LAN decode lift needs only grace≈5 (EWMA~140ms·24/s, +~125ms hold); a larger
        # high-RTT grace≈14 (+~500ms hold) buys little (recovery, not grace, is the binding
        # constraint there). Cap at 8 = keep the LAN win, bound the worst-case added latency
        # to ~250ms. Tunable via CUBOAI_GRACE_MAX.
        self._grace_max = int(os.environ.get("CUBOAI_GRACE_MAX", "") or "8")
        # ── lone-hole / count-1 SACK gate (gated CUBOAI_LONE_HOLE, default OFF) ─────
        # A LONE outstanding hole emits a count-1 SACK, which the camera reads as a
        # timestamp ([50:52], count<2 gate) -> never resent. When exactly ONE hole is
        # fresh, PAD the SACK to count>=2 (duplicate the hole = benign 2nd entry) so the
        # camera honours it and RESENDS the lone hole promptly (-> lands inside the scaled
        # grace -> keyframe completes -> GOP cascade not seeded). Skip-fallback (advance
        # the contiguous edge past the hole) ONLY after _lone_skip_rounds padded requests
        # fail to fill it (a genuinely-lost frag). Recover first, unfreeze last. OFF path
        # is byte-identical to the grace-scale-ON shipped default.
        self._lone_hole = os.environ.get("CUBOAI_LONE_HOLE", "0") != "0"
        self._lone_pad = os.environ.get("CUBOAI_LONE_PAD", "dup")   # "dup" | "plus1"
        self._lone_skip_rounds = int(os.environ.get("CUBOAI_LONE_SKIP_ROUNDS", "") or "6")
        self._hole_req_count = {}   # frag-seq -> padded-request count (lone-hole skip-fallback)
        # ── keyframe-aware grace (gated CUBOAI_KF_GRACE, default OFF) ──────────────
        # The keyframe (GOP root, ~69 frags) almost always loses >=1 frag at >=1% loss;
        # with the short scaled grace (~8 AU-idx ~0.3s) the una abandons its holes
        # (gap_depth_cap / count-1) -> incomplete root -> the whole GOP cascades. KF-grace
        # HOLDS an incomplete keyframe AU head-of-line up to _kf_hold AU-indices (~one GOP),
        # keeps the una at its holes (gap_depth_cap + lone-skip suppressed) so the camera
        # keeps resending them (reliable ~91-99%/round) + pads its lone hole to count>=2, and
        # seals on COMPLETE (then the GOP decodes) or at the GOP boundary (give up; next
        # keyframe re-syncs). Trades ~one-GOP keyframe latency for keyframe survival;
        # P-frames keep the short grace. OFF path byte-identical (_holding_kf stays False).
        self._kf_grace = os.environ.get("CUBOAI_KF_GRACE", "0") != "0"
        self._kf_hold = int(os.environ.get("CUBOAI_KF_HOLD", "") or "40")   # AU-indices ~ one GOP
        if self._kf_grace:
            self._lone_hole = True       # REQUIRED: pad the keyframe's final lone hole to count>=2
        self._holding_kf = False         # reader: True while head-of-line-holding an incomplete kf
        # per-AU fate trace (gated CUBOAI_AU_LOG) — emit/skip/reject/dropclassify, to
        # localize POC gaps (a gap = a video AU never emitted -> greys a refs=1 GOP).
        self._au_log = [] if os.environ.get("CUBOAI_AU_LOG") else None
        # ── NEVER-DROP / in-order emit (gated CUBOAI_NODROP, default OFF) ──────────
        # At ~1% loss most video-AU "gaps" are COMPLETE-but-dropped: a higher idx seals
        # first (short grace) -> done_upto jumps past AU K -> K's (complete) frags then hit
        # the idx<=done_upto reject -> hard POC gap -> greys the refs=1 GOP. FIX: seal
        # STRICTLY in order (done_upto+1 only) at grace-expiry, emitting the PARTIAL slice
        # if still incomplete, and NEVER sealing a higher idx first — so a slightly-late AU
        # is waited for (within grace) and emitted instead of skipped+rejected. Matches
        # native (which never gaps). OFF path byte-identical.
        self._nodrop = os.environ.get("CUBOAI_NODROP", "0") != "0"
        # H1 fix (default ON): the camera AV message-index ([56:58]) is a u16 that WRAPS at
        # 65536, but `done_upto` is an unbounded monotonic int. _idx_modular lifts each incoming
        # idx into done_upto's space (modular forward unwrap, _unwrap_index) so the reassembly
        # accept-window survives the wrap. Byte-identical pre-wrap; OFF reverts to the historical
        # non-modular gate, which dead-stalls a continuous stream ~every 46 min at the wrap.
        self._idx_modular = os.environ.get("CUBOAI_IDX_MODULAR", "1") != "0"
        # minimum in-order wait (AU-indices) before advancing past an absent AU — the
        # scaled grace (~4 at LAN) is too short to wait out a >grace-late frag-burst, so
        # the late-but-complete AU is still skipped+rejected; this floor holds each AU long
        # enough to land.
        self._nodrop_grace = int(os.environ.get("CUBOAI_NODROP_GRACE", "") or "12")
        # emit-on-complete (NODROP): emit an AU the instant it is provably complete (marker-led,
        # gap-free, bounded by the next AU's first frag) instead of waiting out the grace window
        # — native-like low latency; grace only ever applies to a still-INCOMPLETE AU.
        self._emit_complete = os.environ.get("CUBOAI_EMIT_COMPLETE", "1") != "0"
        # ── clean-truncation partial (gated CUBOAI_TRUNCATE_PARTIAL, default OFF) ───
        # When an incomplete AU is emitted, assemble() BRIDGES the hole (frags after the gap are
        # concatenated onto frags before -> false start codes / garbage syntax -> cu_qp_delta /
        # invalid-NAL decoder choke that propagates on a refs=1 stream). Instead emit ONLY the
        # contiguous prefix up to the first missing frag — a clean, byte-aligned, terminated slice
        # the decoder conceals. OFF path byte-identical.
        self._truncate_partial = os.environ.get("CUBOAI_TRUNCATE_PARTIAL", "0") != "0"
        # ── strip+parse the 24-byte TUTK FRAMEINFO video trailer (gated
        # CUBOAI_STRIP_FRAMEINFO, default OFF) ────────────────────────────────────────────
        # Hardware decoders (e.g. Apple VideoToolbox via Safari/Chrome) reject the over-long
        # final NAL the trailer creates -> black picture (-12909); software decoders ignore
        # it. ON: for a COMPLETE video AU only, sanity-check the trailing 24B are a FRAMEINFO
        # (codec_id==0x0050) then drop them, and parse the keyframe flag + frame timestamp
        # into self._last_frameinfo. OFF path byte-identical.
        self._strip_frameinfo = os.environ.get("CUBOAI_STRIP_FRAMEINFO", "0") != "0"
        # most-recent parsed video FRAMEINFO (keyframe/timestamp_ms/frame_no/w/h). NOTE: this
        # is the LATEST trailer the reader thread has seen — it runs AHEAD of the consumer by
        # the out_q depth, so it is "current stream state", not reliably the AU the consumer
        # just dequeued.
        self._last_frameinfo = None
        self._frameinfo_skips = 0       # count of complete video AUs whose tail wasn't a FRAMEINFO
        # ── gated FRAMEINFO/codec census (CUBOAI_LOG_FRAMEINFO, default OFF) ───────
        # When ON, _av_reader's seal_one emits one stderr line per assembled AU BEFORE any
        # audio-truncation / video-FRAMEINFO-strip / consumer kind-filter, carrying the
        # candidate 24-byte trailer, its codec_id, the keyframe byte, the [8:12] bytes, the
        # content classifier's verdict and the total length — so a live run shows whether
        # audio AUs (codec_id 0x0088 / ADTS sync) are interleaved with video on the same
        # channel. The block is fully behind the flag and touches nothing emitted, so the
        # OFF path is byte-identical.
        self._log_frameinfo = os.environ.get("CUBOAI_LOG_FRAMEINFO", "0") != "0"
        # CUBOAI_LOG_FRAMEINFO_MAX (default 0 = unlimited): stop the census after N AUs.
        # The enable_debug_logs option runs producers with the census on permanently, so a
        # cap keeps go2rtc.log bounded while still covering the whole startup window
        # (connect → first keyframe → mux) where an unknown model diverges (issue #85).
        self._log_frameinfo_max = int(os.environ.get("CUBOAI_LOG_FRAMEINFO_MAX", "") or "0")
        self._ficensus_n = 0            # census line counter (diagnostic only)
        # ── una C-lag (gated CUBOAI_UNA_LAG, default 0=off) ────────────────────────
        # Native keeps its reported una C a STEADY ~11 frags BEHIND the high-water D. Pure
        # reports C≈D (gap med 0) so the camera believes everything <=D is delivered and
        # DISCARDS its resend buffer up to D — then a hole pure requests late gets NO resend.
        # Capping the REPORTED C at D-UNA_LAG keeps the camera's resend buffer ~LAG deep so
        # it can still honour pure's requests for recent holes (matches native). Internal
        # _frag_edge is unchanged; only the wire-reported C is lagged. OFF (=0)
        # byte-identical.
        self._una_lag = int(os.environ.get("CUBOAI_UNA_LAG", "") or "0")
        # ── recovery-HOLD (gated CUBOAI_RECOVERY_HOLD; default == _nodrop_grace = off) ──
        # With persistence the camera DOES resend a residual hole, but the resend lands
        # ~0.9-1s later while the NODROP grace emits the incomplete AU at ~0.5s (too early).
        # Native holds ~1s (its buffer) and catches it. So hold a PRESENT-but-incomplete
        # done_upto+1 AU up to _recovery_hold AU-idx (emit-on-complete still fires the instant
        # the resend fills it); only a still-incomplete AU at expiry is emitted (truncated).
        # Default == _nodrop_grace -> identical to current; raise (~36 ≈ 1.5s) to catch late
        # resends.
        self._recovery_hold = int(os.environ.get("CUBOAI_RECOVERY_HOLD", "") or str(self._nodrop_grace))
        self._av_reader_thread = None   # background _av_reader thread (while streaming)
        self._av_stop_evt = None        # its stop Event (set by _stop_reader/disconnect)

        # ── read-only transport/decode counters (cumulative; see get_stats) ──────────────
        # Plain ints, written ONLY by the reader thread on paths that already run, read
        # lock-free from any thread (an int read/write is atomic and a stats snapshot
        # tolerates a one-tick skew between fields). Pure side-effect increments next to the
        # existing logic, so the emitted bytes/timing are unchanged whether or not anything
        # reads them. The single source --benchmark and verbose mode both read via get_stats().
        self._stat_frags_recv = 0       # distinct AV fragments received (incl. recovered resends)
        self._stat_holes = 0            # distinct fragments detected missing (first resend-request)
        self._stat_resend_req = 0       # resend requests SENT (SACK entries, incl. re-asks)
        self._stat_resend_recovered = 0 # distinct requested holes that arrived (resend honoured)
        self._stat_au_video = 0         # video access units emitted
        self._stat_au_audio = 0         # audio access units emitted
        self._stat_au_incomplete = 0    # video AUs emitted with a missing fragment (partial)
        self._stat_kf_total = 0         # keyframe (IDR/VPS/SPS/PPS-led) video AUs emitted
        self._stat_kf_incomplete = 0    # of those, emitted incomplete (the decode-band seed)
        self._stat_bytes_video = 0      # emitted video bytes (post strip/truncate) -> bitrate
        self._stat_bytes_audio = 0      # emitted audio bytes -> bitrate
        self._stat_gap_max = 0          # high-water of the (D-edge) hole gap depth seen
        self._stat_gap_cap_jumps = 0    # times the gap-depth-cap backstop jumped the edge
        self._stat_lone_skips = 0       # times a lone hole was abandoned after the skip rounds
        self._stat_ts_valid = 0         # video AUs whose FRAMEINFO carried a valid timestamp
        self._stat_ts_garbage = 0       # video AUs whose FRAMEINFO timestamp was garbage (~10%)
        self._stat_ts_regress = 0       # video AUs whose camera timestamp went backwards (monotonic)
        self._stat_last_ts = None       # last valid video timestamp_ms (for the monotonic check)
        # mid-stream GET injection slot (None in every default path -> the reader hooks are
        # inert and the stream is byte-identical). Set by get_during_stream, run by the reader.
        self._get_inject = None
        self._inject_lock = threading.Lock()   # M1: one mid-stream GET-inject at a time (no slot clobber)
        if not self.camera_ip:
            self.camera_ip = "255.255.255.255"
        self._stat_keepalive_err = 0           # L1: keepalive-reply send failures (surfaces a wedged socket)
        # Talk (two-way audio): the camera advertises a 4.3.x capability word at [32:36] of its own
        # av-connect grant; we mirror it into OUR talk grant so the camera accepts us as an av-server.
        # Captured live in connect(); None until then (falls back to the proven constant if absent).
        self._cam_grant_cap = None
        self._talk_stop = False                # cooperative stop flag for a looping send_audio_file / stop_audio()

    def _vlog(self, msg):
        """Print a connect/stream trace line when verbose is on (never wire-affecting)."""
        if self._verbose:
            print(msg, flush=True)

    def connect(self, timeout=8.0, attempts=8):
        """Run the LAN handshake; returns True and sets .session_hdr on success.

        Each attempt picks a FRESH R (== the lib's GenShortRandomID), so retries
        do not collide with the camera's ~20s client-random-id dedup
        (CheckRecentClientRandomID). The camera occasionally drops a grant under
        rapid load; the retry loop covers that.

        The nO->R sanity check uses the verbatim echo (`nO_recover_R`,
        MID-independent) rather than the legacy 64K xor-table heuristic, so the
        one-time `_build_R_table()` is not built here.
        """
        # camera_ip is REQUIRED: the pure backend sends a unicast LAN-search probe to
        # the camera's IP — there is no broadcast auto-discovery (a blank IP is NOT a
        # "discover" mode; see cuboai_validate._validate_startup, which rejects it the
        # same way). Fail fast here with a clear message so a missing/None IP can never
        # reach the `::ffff:` formatter / probe sendto below as a raw TypeError.
        pass

        deadline = time.time() + timeout

        def recv_match(s, pred, t):
            """Low-latency recv via select (reacts in <1ms)."""
            import select
            end = time.time() + t
            while True:
                remaining = end - time.time()
                if remaining <= 0:
                    return None, None, None
                r, _, _ = select.select([s], [], [], remaining)
                if not r:
                    return None, None, None
                try:
                    raw, addr = s.recvfrom(1024)
                except BlockingIOError:
                    continue
                d = xor_frame(raw)
                if pred(raw, d):
                    return raw, d, addr

        for _attempt in range(attempts):
            if time.time() > deadline:
                break
            # Native's AV socket is AF_INET6 with IPV6_V6ONLY=0 (dual-stack), bound
            # to '::', talking to the IPv4 camera via the v4-mapped address
            # ::ffff:<ip>. Because the destination is v4-mapped, the kernel still
            # emits IPv4/UDP on the wire — identical bytes to an AF_INET send. Fall
            # back to AF_INET where IPv6 is unavailable (the v4-mapped wire bytes are
            # the same either way).
            try:
                s = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
                try:
                    s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
                except OSError:
                    pass
                bind_addr = ("::", 0)
                disc_host = self.camera_ip if ":" in self.camera_ip else "::ffff:" + self.camera_ip
            except OSError:
                s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                bind_addr, disc_host = ("", 0), self.camera_ip
            s.setblocking(False)
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            # Large RX buffer: the camera bursts a keyframe as ~70 back-to-back 1 KB
            # fragments; a small buffer drops the tail under any consumer hiccup, which
            # then opens a permanent contiguity gap and stalls the send-window.
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            except OSError:
                pass
            s.bind(bind_addr)                      # pin one source port per session
            self._sock = s

            R = gen_R()                            # fresh per attempt (== GenShortRandomID)
            probe = build_probe(self.uid, R)
            ack = build_ack(self.uid, R)
            self._vlog(f"[connect] attempt {_attempt + 1}/{attempts}  R=0x{R:04x}  "
                       f"target {self.camera_ip}:32761")

            # 1+2. discover: probe -> camera:32761 (unicast; broadcast also works),
            #      twice back-to-back, until nO arrives.
            nO = cam = nO_raw = None
            pend = min(deadline, time.time() + 2.5)
            while time.time() < pend and nO is None:
                s.sendto(probe, (disc_host, 32761))
                s.sendto(probe, (disc_host, 32761))
                raw, d, addr = recv_match(s, lambda r, dd: len(r) >= 184, 0.12)
                if d is not None:
                    nO, nO_raw, cam = d, raw, addr
            if nO is None:
                s.close()
                self._sock = None   # M2: never leave a closed fd in _sock on a failed attempt
                continue

            # The camera echoes our R verbatim inside nO; recover it as a FAST SANITY
            # CHECK that this nO is the response to OUR probe (and not a foreign/stale
            # nO from another client on a shared LAN).
            #
            # The camera's nO builder copies our probe's {R, fingerprint} verbatim to
            # nO plaintext[188:196], so `nO_recover_R(raw) = inv_transcode(raw)[188:190]`
            # is exact and MID-INDEPENDENT. This replaces the `header_R_for_nO` xor-table
            # heuristic, which was tuned to one fingerprint and returned None on a changed
            # MAC. We abort only on a recovered R that POSITIVELY DISAGREES with ours.
            R_echo = nO_recover_R(nO_raw)
            if R_echo is not None and R_echo != R:
                s.close()
                self._sock = None   # M2: never leave a closed fd in _sock on a failed attempt
                continue
            self._vlog(f"[connect] nO received ({len(nO_raw)}B) from {cam[0]}:{cam[1]}  "
                       f"R echo {'ok' if R_echo == R else 'absent'}")

            # Pre-build av-connects (header R == probe R) so the ACK->av gap is
            # bounded by the network, not by Python.
            # ONE shared random token base for all channels (native rule).
            # build_av_connect derives each channel's [48:52] = base + (channel%2)
            # as a full 32-bit LE value ([49:52] constant bar carry). Native's base
            # is a 31-bit nonzero random value, so match that range (bit31 clear,
            # nonzero) rather than a raw 32-bit urandom.
            # Build only the requested channels (default range(40)). Native sends
            # ch0,ch1 BEFORE the 0x2041 grant and the rest after; we mirror that
            # split — the first two of self._channels go pre-grant, the rest
            # post-grant.
            tok = struct.pack("<I", (int.from_bytes(os.urandom(4), "little") & 0x7FFFFFFF) or 1)
            avc = {c: build_av_connect(None, nO, c, self.account, self.password,
                                       token=tok, R=R) for c in self._channels}
            pre = self._channels[:2]
            post = self._channels[2:]
            if self._verbose:
                self._vlog(f"[connect] channels to open: {self._channels}")
                for c in self._channels:
                    odd = c % 2
                    self._vlog(f"[connect]   ch{c} {'odd ' if odd else 'even'}  "
                               f"[29]=0x{0x20 if odd else 0x00:02x}  "
                               f"({'ARMING path' if odd else 'non-arming'})")

            # 3+4. ACK then the pre-grant channels (~8ms gap, as native does).
            s.sendto(ack, cam)
            time.sleep(0.008)
            for c in pre:
                s.sendto(avc[c], cam)
            # Diagnostic, env-gated, default OFF -> wire byte-identical: inject
            # native's IOTC 0x0402 device-identity query at its faithful timing
            # (after ACK + pre-grant channels, ~0.1s). It is not the arming trigger
            # (see build_lan_query header). Leave OFF for normal use.
            if os.environ.get('CUBOAI_INJECT_LANQUERY'):
                s.sendto(build_lan_query(self.uid, R), cam)
                self._vlog("[connect] injected IOTC 0x0402 LAN device-query")
            self._vlog(f"[connect] sent: ACK + {' + '.join('ch%d' % c for c in pre)}"
                       f"  ; waiting for 0x2041 grant...")

            # 5. wait for the 88-byte 0x2041 success.
            raw, d, addr = recv_match(
                s, lambda r, dd: len(r) == 88 and dd[0:2] == b"\x20\x41", 0.6)
            if d is not None:
                self.session_hdr = bytes(d[16:32])
                # Talk: the same raw grant frame inv_transcodes to the camera's av-connect grant,
                # whose [32:36] is the 4.3.x capability word our talk grant must mirror. Capture it
                # here (best-effort) so send_audio_file echoes the camera's OWN value rather than a
                # hardcoded constant — robust across firmwares. Falls back to the constant if absent.
                try:
                    _cap = bytes(inv_transcode(raw)[32:36])
                    if _cap != b"\x00\x00\x00\x00":
                        self._cam_grant_cap = _cap
                except Exception:
                    pass
                # Full grant field decode. [29]=0x21 = arming subtype echoed back;
                # [52]=0 = auth PASS (3 = wrong pw).
                if self._verbose:
                    self._vlog(f"[connect] grant 0x2041 (88B):")
                    self._vlog(f"[connect]   [6]={d[6]} [28]=0x{d[28]:02x} "
                               f"[29]=0x{d[29]:02x}"
                               f"{'  arming subtype' if d[29] == 0x21 else ''} "
                               f"[30]=0x{d[30]:02x}")
                    self._vlog(f"[connect]   [44]={d[44]} [52]={d[52]}"
                               f"{'  auth PASS' if d[52] == 0 else '  auth/grant nonzero'} "
                               f"[56:64]={d[56:64].hex()}")
                    self._vlog(f"[connect]   nonzero offsets: "
                               f"{sorted(i for i in range(88) if d[i] != 0)}")
                # 6. remaining channels + keepalive, as native does.
                for c in post:
                    s.sendto(avc[c], cam)
                s.sendto(xor_frame(_KEEPALIVE_DEC), cam)
                self._vlog(f"[connect] sent: "
                           f"{' + '.join('ch%d' % c for c in post) or '(none)'} + keepalive")
                # Post-connect session state for the AV/IOCTL layer (see ioctl()).
                # The av-connects carry packet seq [6:8]=channel, so the next free
                # data seq is max(channel)+1 (== 40 for the default range(40)).
                self._R = R
                self._cam = cam
                self._seq = max(self._channels) + 1   # av-connect [6:8]==channel
                self._relseq = 0         # IOCTL-phase reliable counter starts at 0 (native)
                self._frmno = 0
                self._ack_ord = 0
                self._data_ack = 0
                self._cam_msgs = set()
                self._got_first = False
                self._frag_D = None
                self._frag_C = 0xFFFF
                self._frag_edge = None            # gap-tracking reset
                self._frag_edge_acked = 0xFFFF
                self._frag_received = set()
                self._frag_gap_ts = {}
                self._hole_req_ts = {}             # retransmit-request schedule reset
                self._hole_first_req = {}          # adaptive-RTT: clear pending latency samples
                # NB: keep _rtt_ewma/_rtt_n across a reconnect — the link's latency does not
                # change when the session restarts, so the learned interval stays valid.
                self._ts_ref = None               # re-anchor the ACK timestamp clock
                self._vlog(f"[connect] session ready  R=0x{R:04x}  "
                           f"channels={self._channels}  next_seq={self._seq}")
                return True
            self._vlog("[connect] no 0x2041 grant this attempt — retrying")
            s.close()
            self._sock = None
        return False

    # ── AV / IOCTL layer ──────────────────────────────────────────────────────
    # Plausible distance for the fragment-seq high-water; rejects out-of-band system
    # frames (which carry junk [46:48] values thousands away) while still allowing D to
    # jump over a small burst of genuinely-lost fragments.
    _FRAG_WINDOW = 256
    _SACK_MAX_FID = 631         # full-fidelity SACK list cap = native's own loop bound
                                # (stops at frame_len 0x4ff => 50+2*631). Bounds a
                                # pathological OOO gap's frame size.
    _GAP_STALE = 0.05           # forward-skip a stale gap fast so the held-C edge stays
                                # close to high-water and the camera's window never fills
                                # enough to dead-stall the stream under loss.
    _GAP_DEPTH_CAP = 50         # if the held-C edge ever falls >this many frags behind
                                # high-water, jump it to high-water-10 at once (hard backstop
                                # against a deep stall when many holes pile up under heavy loss).
    _NAK_INTERVAL = 0.19        # DEFAULT (overridden per-instance by self._nak_interval, set
                                # from full_fidelity in __init__). 0x0b/0x0a resend-control
                                # cadence. _send_nak emits a PAIR (0x0b+0x0a = 2 frames/fire),
                                # so wire NAK rate = 2/interval. Native = ~4.6-4.8 PAIRS/s
                                # (0x0a≈4.3-4.7/s + 0x0b≈4.6-4.8/s = ~9.1-9.6 FRAMES/s); 0.19
                                # -> ~4.8 pairs/s == native. The fast path uses 0.137 (~7.3
                                # pairs/s ≈ 1.5x native), kept only on the non-fidelity path.
                                # Arming-INVARIANT — fidelity only.
    _RESEND_TIMEOUT_MS = 35     # 0x0a [38:40] EWMA resend-timeout (live range 27-50)
    _RESEND_REQ_INTERVAL = 0.15 # min gap before re-listing the SAME hole in the SACK
                                # (≈ camera resend latency ~146ms) so each hole is requested
                                # ~once per resend round -> native-like 1.1x redundancy.
                                # This is the STATIC baseline; when _adaptive_rtt is on it
                                # is recomputed per-instance from the measured resend-latency
                                # EWMA (see _resend_req_interval / _rtt_floor / _rtt_ceil / _rtt_k).
    # ── adaptive resend-request interval (gated CUBOAI_ADAPTIVE_RTT) ───────────────
    # The camera's resend latency is BIMODAL: a fast SACK-honored / reorder cluster
    # (~15-25 ms median) + a slow intrinsic-timer cluster at ~125-145 ms (p90; matches
    # native's 137-146 ms). The live EWMA of the first-request->arrival latency is
    # dominated by the slow cluster and is ~135 ms on the LAN — so K*EWMA ≈ 0.20 s, just
    # ABOVE the camera's intrinsic resend latency, which is the correct place for the
    # interval. On a high-RTT link the EWMA climbs with ~2x the one-way delay (261/346/788 ms
    # at +50/+150/+300 ms) and the interval tracks it (0.39/0.52/0.60-ceil).
    #   * FLOOR is a safety net that rarely binds (LAN interval is ~0.20 s > floor); it only
    #     matters if reorder-false-holes ever pull the EWMA below ~0.10 s, preventing a flood.
    #   * Adaptive holds the per-hole SACK REQUEST COUNT near 1 across the RTT sweep (fixed
    #     climbs to ~2.0; adaptive ~1.1) and is no-regression on the LAN. It does NOT collapse
    #     the high-RTT redundancy (the dominant high-RTT resend driver is the camera's own
    #     timer firing for in-flight frags, not pure re-requesting), so this knob buys
    #     native-faithful request cadence at high RTT, not a redundancy fix. Video health
    #     (~9 v/s) is equal fixed-vs-adaptive at every delay. Default OFF; set
    #     CUBOAI_ADAPTIVE_RTT=1 to enable.
    _RTT_FLOOR = 0.15           # safety floor (rarely binds; LAN interval is ~0.20s)
    _RTT_CEIL  = 0.60           # cap so a pathological link can't stall recovery indefinitely
    _RTT_K     = 1.5            # interval = K * EWMA(resend latency); 1.5 covers jitter above
                                # the mean so we clear the slow-timer cluster, not just the mean
    _RTT_ALPHA = 0.125          # EWMA smoothing (1/8), native-RTT-estimator style
    _RTT_MIN_SAMPLES = 8        # use the static 0.15 s until this many clean samples seen

    @staticmethod
    def _is_io_frame(dec):
        """True if a camera DATA frame is a reliable IO/control response (not AV).

        The camera tags AV fragments with a non-zero AV-unit id at [58:64]; reliable
        IO/control RESPONSES (IOCTL/status answers, login/system frames) carry an
        all-zero [58:64]. The [58:64]==0 frames bear the IO message-indices 0..N and
        every AV fragment — including continuation fragments whose payload is not a
        start code — has a non-zero id. This is the discriminator that splits the two
        ack channels.
        """
        return len(dec) >= 64 and dec[58:64] == b"\x00\x00\x00\x00\x00\x00"

    def _note_cam_data(self, dec):
        """Record a camera DATA frame and advance the windowed-ACK state.

        The camera runs TWO reliable streams, acked by two different fields, so we route
        each frame by `_is_io_frame`:
        * Reliable IO/control responses ([58:64]==0): advance `_data_ack` = highest
          contiguous IO message-index ([56:58]). Reported in the ACK's [40:44]; this is
          what frees the camera's IO send-FIFO so it proceeds from a status read to
          video. AV fragment-seq state is left untouched (native keeps C/D idle while
          serving IOCTLs).
        * AV fragments ([58:64]!=0): advance the **C/D pair** from the fragment-seq
          [46:48]: D = highest fragment-seq seen (gaps skipped), C = the previous ACK's
          D. A real, advancing D keeps the camera's AV send-window open.
        """
        if len(dec) < 68:
            return
        if self._is_io_frame(dec):
            idx = struct.unpack("<H", dec[56:58])[0]
            self._cam_msgs.add(idx)
            if idx == 0:
                self._got_first = True
            while (self._data_ack + 1) in self._cam_msgs:
                self._data_ack += 1
            return
        # AV fragment: D = highest fragment-seq [46:48], modular-u16, skipping gaps/junk.
        frag = struct.unpack("<H", dec[46:48])[0]
        if self._frag_D is None:
            self._frag_D = frag
        else:
            fwd = (frag - self._frag_D) & 0xFFFF             # forward distance in u16 space
            if 0 < fwd <= self._FRAG_WINDOW:
                self._frag_C = self._frag_D                  # C = prior D (low edge)
                self._frag_D = frag
        # gap-tracking: maintain the CONTIGUOUS low-water edge separately from the
        # high-water D so the 0x0a NAK can name a missing range. PURE BOOKKEEPING — it
        # does NOT change the wire C/D above (default streaming stays byte-identical to
        # native); it only feeds the gated resend_mode (held-D ack + _send_nak). Runs in
        # the single _av_reader thread (no lock needed). The edge advances over received
        # frags but never past D (high-water), so it holds at a genuine gap.
        # adaptive-RTT: if this frag was an outstanding SACK-requested hole, fold its
        # first-request->arrival latency into the resend-latency EWMA (no-op when adaptive
        # is off — _hole_first_req stays empty). Before .add so a duplicate resend of an
        # already-received frag can't double-count (the anchor is popped on first arrival).
        if (self._adaptive_rtt or self._grace_scale) and frag not in self._frag_received:
            self._record_resend_latency(frag)   # also feeds the grace EWMA
        if self._lone_hole and frag not in self._frag_received:
            self._hole_req_count.pop(frag, None)   # hole filled -> reset its padded-request count
        if frag not in self._frag_received:        # stats: first arrival of this fragment
            self._stat_frags_recv += 1
            if frag in self._hole_req_ts:          # it was an outstanding requested hole -> recovered
                self._stat_resend_recovered += 1
        self._frag_received.add(frag)
        if self._frag_edge is None:
            self._frag_edge = frag
        else:
            while ((self._frag_D - self._frag_edge) & 0xFFFF) != 0:   # edge < high-water
                nxt = (self._frag_edge + 1) & 0xFFFF
                if nxt in self._frag_received:
                    self._frag_edge = nxt
                    self._frag_gap_ts.pop(nxt, None)
                else:
                    break                                    # genuine gap — hold the edge
        if len(self._frag_received) > 1024:                  # bound memory (u16 wrap)
            hw = self._frag_D
            self._frag_received = {f for f in self._frag_received
                                   if (hw - f) & 0xFFFF <= 1024}

    def _ts_word(self):
        """Native's word-swapped 32-bit ACK timestamp value V = now_ms - reference.

        build_data_ack packs it [48:50]=high16(V), [50:52]=low16(V) (= wordswap(V)).  The
        reference is captured once per session (lazily, on first ACK) as
        `now_ms - 0x1A220000` so that high16(V) starts at native's constant 0x1a22 (held
        for the whole short session, ticking to 0x1a23 only after low16 wraps ~every 65 s)
        and low16(V) climbs ~1 ms/ms from ~0.  Native re-anchors its reference periodically
        (the ~900-3000 ms backward "reset" jumps in [50:52]) but the exact cadence is not
        statically pinnable, so pure uses one stable reference — the dominant structure
        (word-swap, const-ish high word, ~1 ms/ms low word) matches and the field is
        camera-IGNORED, so this is byte-fidelity only."""
        now_ms = int(time.time() * 1000)
        if self._ts_ref is None:
            self._ts_ref = (now_ms - 0x1A220000) & 0xFFFFFFFF
        return (now_ms - self._ts_ref) & 0xFFFFFFFF

    def _compute_sack(self, D_wire):
        """The per-fragment SACK list for build_data_ack's [42:44]/[50:].

        Returns the ABSOLUTE camera fragment-seqs the receiver holds out-of-order —
        those received strictly above the acked edge (wire D) up to the high-water
        `_frag_D` — matching native's OOO FIFO. build_data_ack encodes each as its
        2-byte (frag_seq - C) wire offset (the -C lives in build_data_ack so it owns the
        full wire encoding). Returns None when there is no gap (edge == high-water) =>
        build_data_ack emits the byte-identical 52B ACK.

        Under full_fidelity pure emits the FULL out-of-order list native sends (every
        received frag strictly above the wire edge up to high-water, ascending), giving
        frame_len = 50+2N, capped only at the lib's own loop bound _SACK_MAX_FID — NO
        _FRAG_WINDOW truncation (a junk high-water still can't inflate the list, since
        only genuinely-received frags are listed). When full_fidelity is off, a
        best-effort path is used: a _FRAG_WINDOW-bounded dense run, and spans beyond the
        window drop the list entirely. (Native's real entries under heavy loss are
        STRIDED with wrapped tail values; pure's are the genuinely-received set —
        efficiency-only, camera-IGNORED for arming.)

        Only called from the resend_mode branch of _send_ack: holding the cumulative
        ack at the edge is what makes a SACK list coherent (the camera still has the
        frags above it). In best-effort mode (D = high-water) there is nothing above
        D to SACK, and pure forgives gaps via the advancing D — so no list is sent.
        This list is efficiency-only (does NOT gate resend); pure-born never reaches here.
        """
        hw = self._frag_D
        if hw is None or D_wire == 0xFFFF:
            return None
        span = (hw - D_wire) & 0xFFFF                       # frags above the acked edge
        if span == 0:
            return None
        if self._full_fidelity:
            # FULL OOO list: every received frag in (edge, high-water], ascending u16
            # distance, capped at the lib's bound. No window truncation.
            sack = sorted(
                (f for f in self._frag_received
                 if 0 < ((f - D_wire) & 0xFFFF) <= span),
                key=lambda f: (f - D_wire) & 0xFFFF,
            )[:self._SACK_MAX_FID]
            return sack or None
        # best-effort path: window-bounded dense run
        if span > self._FRAG_WINDOW:
            return None
        sack = [f
                for f in ((D_wire + k) & 0xFFFF for k in range(1, span + 1))
                if f in self._frag_received]
        return sack or None

    def _resend_req_interval(self):
        """The live per-hole re-request interval.

        Static path (default, _adaptive_rtt off): the constant _RESEND_REQ_INTERVAL
        (0.15 s) — the validated LAN behaviour.

        Adaptive path (CUBOAI_ADAPTIVE_RTT=1): clamp(K * EWMA(resend latency), floor, ceil).
        The EWMA tracks the first-request->arrival latency of recovered holes (fed in
        _record_resend_latency): ~135 ms on the LAN (=> interval ~0.20 s, just above the
        camera's intrinsic resend latency, no LAN regression) climbing to 261/346/788 ms at
        +50/+150/+300 ms RTT (=> interval 0.39/0.52/0.60-ceil), which holds the per-hole
        request count near 1 across the sweep. Until _RTT_MIN_SAMPLES clean samples are in,
        fall back to the static constant."""
        if not self._adaptive_rtt or self._rtt_ewma is None or self._rtt_n < self._RTT_MIN_SAMPLES:
            return self._RESEND_REQ_INTERVAL
        target = self._RTT_K * self._rtt_ewma
        return min(self._RTT_CEIL, max(self._RTT_FLOOR, target))

    def _record_resend_latency(self, frag):
        """A requested hole `frag` just arrived — fold its first-request->arrival
        latency into the resend-latency EWMA (the adaptive interval's only signal).

        Sample = now - the time the hole was FIRST SACK-listed (re-requests do NOT reset
        that anchor, so a re-asked hole still yields one clean first-request->first-arrival
        sample). Runs on the reader thread inside _note_cam_data, same thread as _send_ack,
        so no lock is needed. The anchor dict is populated in _send_ack."""
        t0 = self._hole_first_req.pop(frag, None)
        if t0 is None:
            return
        lat = time.time() - t0
        if lat <= 0 or lat > self._RTT_CEIL * 4:     # guard against clock jitter / stale anchors
            return
        if self._rtt_ewma is None:
            self._rtt_ewma = lat
        else:
            self._rtt_ewma += self._RTT_ALPHA * (lat - self._rtt_ewma)
        self._rtt_n += 1

    def _compute_holes(self, C):
        """The MISSING fragment-seqs (HOLES) in (C, high-water] — frags the camera sent
        (frag-seq <= high-water, so we know they exist) that we did NOT receive. This is
        the SACK the camera acts on: the camera RE-SENDS the frags listed in the SACK
        (native's listed frags are resent 95% and native recovers 82% of its real losses).
        The SACK is a RESEND-REQUEST (missing) list, NOT a received list. Called with
        C = the una (contiguous edge) so every hole is above C and build_data_ack's (hole − C)
        offset is positive; the caller dedups by _RESEND_REQ_INTERVAL and only emits when ≥2
        holes are fresh (the camera ignores a count-1 entry). Capped at _SACK_MAX_FID."""
        hw = self._frag_D
        if hw is None or C == 0xFFFF:
            return None
        span = (hw - C) & 0xFFFF
        if span == 0 or span > 0x8000:                     # no window / wrapped-backwards guard
            return None
        rcv = self._frag_received
        holes = [(C + k) & 0xFFFF
                 for k in range(1, span + 1)
                 if ((C + k) & 0xFFFF) not in rcv][:self._SACK_MAX_FID]
        return holes or None

    def _send_ack(self):
        """Emit a data-channel ACK (consumes one reliable-frame seq).

        Carries the C/D cumulative-ACK. Before any camera DATA frame has been seen,
        sends the idle sentinel C==D==0xFFFF, matching the native client.
        """
        sack = None
        if self._frag_D is None:
            C = D = 0xFFFF
        elif self._resend_mode and self._frag_edge is not None:
            # Held-D: ack only the CONTIGUOUS edge so the camera sees us stalled at the
            # gap; the 0x0a NAK (highwater=high-water) names the high edge -> targeted
            # resend (RETX recovery ~313 ms on an armed session). DEVIATES from native's
            # data-ack (D=high-water) and would stall an UNARMED stream on any unrecovered
            # loss — used only in resend_mode.
            if self._selective_ack:
                # Native selective REPEAT — the SACK is a RESEND-REQUEST (missing) list:
                # the camera resends exactly the frag-seqs it carries (native's listed frags
                # are resent 95% → native recovers 82% of real losses).
                # C = una (contiguous edge) so every outstanding hole is ABOVE C and its
                # (hole−C) offset is positive; D = high-water; SACK = outstanding holes in
                # (una, high-water] NOT requested within _RESEND_REQ_INTERVAL. The camera only
                # honours entries when count≥2 (a count-1 frame's [50:52] is read as the
                # timestamp), so a lone hole waits for a second. The per-hole request-timer
                # lists each hole ~once per resend round → native-like 1.10× redundancy, and
                # re-asks a still-missing hole next round (covers a lost resend).
                C = self._frag_edge if self._frag_edge is not None else self._frag_C
                D = self._frag_D
                if (self._una_lag and D is not None and C != 0xFFFF
                        and ((D - C) & 0xFFFF) < self._una_lag):
                    C = (D - self._una_lag) & 0xFFFF   # C2: lag reported una -> camera keeps buffer deep
                now = time.time()
                holes = self._compute_holes(C) or []
                req_interval = self._resend_req_interval()    # static 0.15s or adaptive
                fresh = [h for h in holes
                         if now - self._hole_req_ts.get(h, 0.0) > req_interval]
                if len(fresh) >= 2:
                    for h in fresh:
                        if h not in self._hole_req_ts:        # stats: first request of this hole
                            self._stat_holes += 1
                        self._stat_resend_req += 1            # stats: a resend request sent
                        self._hole_req_ts[h] = now
                        # adaptive-RTT: anchor the FIRST request time per hole (never
                        # overwritten while outstanding) so _record_resend_latency yields a
                        # clean first-request->arrival sample. No-op when adaptive is off.
                        if self._adaptive_rtt or self._grace_scale:   # also anchors for grace EWMA
                            self._hole_first_req.setdefault(h, now)
                        if self._lone_hole:
                            self._hole_req_count[h] = self._hole_req_count.get(h, 0) + 1
                    sack = fresh[:self._SACK_MAX_FID]
                elif len(fresh) == 1 and self._lone_hole:
                    # lone-hole fix: pad the single hole to count>=2 so the camera honours
                    # it (count<2 would be read as a timestamp). After _lone_skip_rounds failed
                    # padded requests the frag is treated as genuinely lost -> advance the
                    # contiguous edge past it (RECOVER first, unfreeze last).
                    h = fresh[0]
                    n = self._hole_req_count.get(h, 0)
                    if n >= self._lone_skip_rounds and not self._holding_kf:
                        self._stat_lone_skips += 1                     # stats: lone hole abandoned
                        self._frag_edge = h                            # abandon -> reopen the window
                        while ((self._frag_D - self._frag_edge) & 0xFFFF) != 0:
                            nx = (self._frag_edge + 1) & 0xFFFF
                            if nx in self._frag_received: self._frag_edge = nx
                            else: break
                        self._hole_req_ts.pop(h, None); self._hole_first_req.pop(h, None)
                        self._hole_req_count.pop(h, None)
                        sack = None
                    else:
                        if h not in self._hole_req_ts:        # stats: first request of this hole
                            self._stat_holes += 1
                        self._stat_resend_req += 1            # stats: a resend request sent
                        self._hole_req_ts[h] = now
                        if self._adaptive_rtt or self._grace_scale:
                            self._hole_first_req.setdefault(h, now)
                        self._hole_req_count[h] = n + 1
                        # benign 2nd entry: "dup" duplicates the hole (resends only the needed
                        # frag); "plus1" adds h+1.
                        pad = h if self._lone_pad == "dup" else (h + 1) & 0xFFFF
                        sack = [h, pad]
                else:
                    sack = None
                if len(self._hole_req_ts) > 2048:   # prune filled holes (bound memory)
                    self._hole_req_ts = {h: t for h, t in self._hole_req_ts.items()
                                         if h not in self._frag_received}
                # bound _hole_first_req too — a hole that is truly lost (never arrives)
                # is never popped by _record_resend_latency, so drop anchors that have fallen
                # out of the current (una, high-water] window (their frag can no longer return).
                if len(self._hole_first_req) > 2048:
                    hw = self._frag_D
                    self._hole_first_req = {
                        h: t for h, t in self._hole_first_req.items()
                        if hw is not None and ((hw - h) & 0xFFFF) <= 1024
                        and h not in self._frag_received}
                self._frag_edge_acked = self._frag_edge
            else:
                C, D = self._frag_edge_acked, self._frag_edge
                self._frag_edge_acked = self._frag_edge
                sack = self._compute_sack(D)      # OOO frag seqs above edge (abs; encoded frag-C)
        else:
            C, D = self._frag_C, self._frag_D     # native-faithful: D=high-water, C=prev-D
        # under full_fidelity emit native's word-swapped 32-bit ACK timestamp (ts32);
        # else the const-0x1a22 + free-running-ms low word.
        ts32 = self._ts_word() if self._full_fidelity else None
        self._sock.sendto(
            build_data_ack(self._R, self._seq, self._relseq,
                           self._ack_ord, C, D, self._data_ack, sack=sack, ts32=ts32,
                           win=self._advertise_window),
            self._cam)
        self._seq += 1
        self._relseq += 1
        self._ack_ord += 1

    def _send_nak(self):
        """Emit the 0x0b/0x0a resend-control pair. The 0x0a carries highwater=high-water
        (highest frag-seq seen) + the EWMA resend-timeout; paired with the held-D ack
        (resend_mode) it asks the camera to resend the gap [edge+1 .. high-water]. On an
        armed session this collapses resend latency to ~313 ms (targeted, min 33 ms);
        ignored by the camera otherwise. Consumes seqs like native (0x0b: no relseq;
        0x0a: +1 relseq)."""
        # echo the camera's ms-clock (captured from cam->host 0x0a) in our 0x0b [36:38],
        # advanced by elapsed wall time — THE arming discriminator. Native does exactly
        # this; the camera gates AV-retransmit commitment on it. Falls back to `now` until
        # the first camera 0x0a is seen (harmless; camera sends them ~t=0.07s).
        ts_b = None
        if self._echo_cam_clock and self._cam_clock is not None:
            ts_b = (self._cam_clock + int((time.time() - self._cam_clock_ts) * 1000)) & 0xFFFF
        self._sock.sendto(build_resend_b(self._R, self._seq, 8, ts=ts_b), self._cam)
        self._seq += 1
        # in selective_ack (native) mode the 0x0a carries highwater=0 (native's
        # steady-state telemetry value) — the SACK in the 0x09 ACK drives resends, so an
        # explicit highwater=high-water here would RE-REQUEST the whole (.., high-water]
        # range every _nak_interval ON TOP of the SACK and flood the camera with duplicate
        # resends. The held-edge mode keeps highwater=high-water (it has no SACK-driven
        # resend and needs the explicit ask).
        nak_hw = 0 if self._selective_ack else (self._frag_D or 0)
        self._sock.sendto(
            build_resend_req(self._R, self._seq, self._relseq,
                             highwater=nak_hw,
                             resend_timeout_ms=self._RESEND_TIMEOUT_MS,
                             win=self._advertise_window),
            self._cam)
        self._seq += 1
        self._relseq += 1

    def _send_video_start_mid(self):
        """Emit the deferred 0x0300 (AUDIOSTART / stream-start) IOCTL.

        Native sends 0x0300 ~5 s after 0x00FF rather than at connect time. Pure defers
        it to here, called exactly
        once from _av_reader's loop _MID_IOCTL_SECS after stream-open. 0x0300 is what
        actually starts the video stream, so this is purely time-based (there are no
        video frames to count before it). Consumes one reliable-frame seq + FrmNo like
        any IOCTL DATA frame. Runs on the reader thread (the SOLE sender during
        streaming), so there is no send/seq race with maybe_ack / maybe_nak.
        """
        io_type, pl = self._VIDEO_START_MID
        self._sock.sendto(
            build_ioctl_data(self._R, self._seq, self._relseq,
                             self._frmno, io_type, pl),
            self._cam)
        self._seq += 1
        self._relseq += 1
        self._frmno += 1

    def _send_video_start_late(self):
        """Emit the deferred 0x01FF video-start IOCTL (see _VIDEO_START_LATE).

        Native sends 0x01FF ~5 s after the stream is already flowing rather than at
        connect time; pure defers it to here, called exactly once from _av_reader's
        loop when either _LATE_IOCTL_FRAMES video units have arrived or
        _LATE_IOCTL_SECS have elapsed since stream-start. Consumes one reliable-frame
        seq + FrmNo like any IOCTL DATA frame. Runs on the reader thread (the SOLE
        sender during streaming), so there is no send/seq race with maybe_ack /
        maybe_nak.
        """
        io_type, pl = self._VIDEO_START_LATE
        self._sock.sendto(
            build_ioctl_data(self._R, self._seq, self._relseq,
                             self._frmno, io_type, pl),
            self._cam)
        self._seq += 1
        self._relseq += 1
        self._frmno += 1

    def ioctl(self, type_code, payload, timeout_ms=5000):
        """Send an AV IOCTL and return (response_type_code, response_bytes).

        Pure Python over the LAN data channel (no native library): builds the
        0x0407 DATA frame carrying <io_type><payload>, sends it to the camera P2P
        port, ACKs the camera's reliable stream while waiting, and returns the
        response payload (response io_type == type_code | 1).

        Multiple IOCTLs run on ONE connection via the windowed ACK: our ACK frames
        carry [40:44] = the highest contiguous camera DATA message-index
        received, which advances the camera's response send-window so it serves
        request after request. A stalled call triggers one reconnect-and-retry.
        """
        last_err = None
        for attempt in range(3):
            if self._sock is None or self.session_hdr is None:
                self.disconnect()
                if not self.connect():
                    last_err = "handshake failed (no 0x2041)"
                    time.sleep(0.2)
                    continue
            try:
                return self._ioctl_once(type_code, payload, timeout=3.0)
            except TimeoutError as e:
                last_err = str(e)
                self.disconnect()           # reconnect for the retry
                time.sleep(0.2)
        raise TimeoutError(
            f"IOCTL 0x{type_code:04x} failed after retries: {last_err}")

    def _ioctl_once(self, type_code, payload, timeout=3.0):
        import select
        s = self._sock
        resp_type = type_code | 1                       # GET req even -> resp = req+1
        frmno = self._frmno
        # the request DATA frame consumes one reliable-frame seq (kept on retransmit)
        req_relseq = self._relseq
        req = build_ioctl_data(self._R, self._seq, req_relseq, frmno, type_code, payload)
        self._seq += 1
        self._relseq += 1
        self._frmno += 1
        s.sendto(req, self._cam)
        t0 = time.time()
        last_tx = t0
        while time.time() - t0 < timeout:
            r, _, _ = select.select([s], [], [], 0.05)
            now = time.time()
            if now - last_tx > 0.4:                     # retransmit (same relseq/frmno, new pkt seq)
                req = build_ioctl_data(self._R, self._seq, req_relseq, frmno,
                                       type_code, payload)
                self._seq += 1
                s.sendto(req, self._cam)
                last_tx = now
            if not r:
                continue
            try:
                raw, addr = s.recvfrom(4096)
            except BlockingIOError:
                continue
            if len(raw) < 16:
                continue
            if is_keepalive_probe(raw):                 # answer the liveness probe
                self._session_fp = xor_frame(raw)[16:20]  # camera's echoed session token
                try:
                    s.sendto(build_keepalive_reply(raw), addr)
                except OSError:
                    pass
                continue
            dec = inv_transcode(raw)
            if len(dec) < 30:
                continue
            sub = dec[28]
            if sub == 0x0C and len(dec) >= 68:
                self._note_cam_data(dec)                # advance data-ACK on DATA frames
                io = struct.unpack("<H", dec[64:66])[0]
                self._send_ack()                        # ack the camera's reliable stream
                if io == resp_type:
                    avlen = struct.unpack("<H", dec[52:54])[0]
                    end = min(len(dec), 68 + max(0, avlen - 4))
                    return resp_type, bytes(dec[68:end])
            elif sub in (0x09, 0x0A):
                self._send_ack()                        # respond to ack/NAK probes
        raise TimeoutError(f"no response to IOCTL 0x{type_code:04x}")

    # ── high-level GET commands ───────────────────────────────────────────────
    # Thin wrappers over ioctl(): each builds the request with the cuboai_messages
    # builder, sends it on the (multi-IOCTL-capable) data channel, and parses the
    # response with the SHARED cuboai_messages parser — so the pure backend and the
    # native cuboai_tutk.TUTKSession decode byte-identical wire data to identical
    # dicts. GET only (no SET commands).
    def _cubo_get(self, name):
        import cuboai_messages as cm
        builder, want_resp, parser = cm.GET_METHODS[name]
        io_type, payload = builder()
        rt, data = self.ioctl(io_type, payload)
        result = parser(data)
        if rt != want_resp:
            result['resp_type'] = rt
            result['warning'] = f"unexpected resp type {rt} (wanted {want_resp})"
        return result

    def get_hw_control(self):        return self._cubo_get('get_hw_control')
    def get_light_style(self):       return self._cubo_get('get_light_style')
    def get_sleep_safety(self):      return self._cubo_get('get_sleep_safety')
    def get_sleep_mode(self):        return self._cubo_get('get_sleep_mode')
    def get_lullaby(self):           return self._cubo_get('get_lullaby')
    def get_cry_detection(self):     return self._cubo_get('get_cry_detection')
    def get_cough_detection(self):   return self._cubo_get('get_cough_detection')
    def check_firmware_update(self): return self._cubo_get('check_firmware_update')
    def get_connected_users(self):   return self._cubo_get('get_connected_users')
    # additional GETs — confirmed responding on fw 3.0.1369
    def get_temp_humidity(self):        return self._cubo_get('get_temp_humidity')
    def get_night_light(self):          return self._cubo_get('get_night_light')
    def get_status_light(self):         return self._cubo_get('get_status_light')
    def get_hw_policy(self):            return self._cubo_get('get_hw_policy')
    def get_sleep_safety_setting(self): return self._cubo_get('get_sleep_safety_setting')
    def get_auto_capture(self):         return self._cubo_get('get_auto_capture')
    def get_smart_temp_config(self):    return self._cubo_get('get_smart_temp_config')
    def get_lullaby_schedule(self):     return self._cubo_get('get_lullaby_schedule')
    def get_light_way_config(self):     return self._cubo_get('get_light_way_config')
    def get_detection_zone_v2(self):    return self._cubo_get('get_detection_zone_v2')
    # further camera GET endpoints
    def get_event_list(self):              return self._cubo_get('get_event_list')
    def get_wifi(self):                    return self._cubo_get('get_wifi')
    def get_danger_zone(self):             return self._cubo_get('get_danger_zone')
    def get_danger_zone2(self):            return self._cubo_get('get_danger_zone2')
    def get_detection_zone(self):          return self._cubo_get('get_detection_zone')
    def get_media_profiles(self):          return self._cubo_get('get_media_profiles')
    def get_lightweight_status(self):      return self._cubo_get('get_lightweight_status')
    def get_lullaby_schedules(self):       return self._cubo_get('get_lullaby_schedules')
    def get_lullaby_schedule_action(self): return self._cubo_get('get_lullaby_schedule_action')
    def get_mat_config(self):              return self._cubo_get('get_mat_config')
    def get_mat_info(self):                return self._cubo_get('get_mat_info')
    def get_smart_temp_info(self):         return self._cubo_get('get_smart_temp_info')
    def get_feature_support(self):         return self._cubo_get('get_feature_support')
    # undocumented telemetry GETs: the camera's own per-session stream stats + connected users
    def get_session_stats(self):           return self._cubo_get('get_session_stats')
    def get_user_list(self):               return self._cubo_get('get_user_list')

    # ── read-only stats snapshot ──────────────────────────────────────────────
    def get_stats(self):
        """Cumulative read-only snapshot of the transport/decode counters.

        The single source of truth for diagnostics: --benchmark and the streamer's verbose
        mode both read this (and pair successive snapshots through the module helper
        stats_delta() for per-interval fps/bitrate/loss/recovery). No socket I/O and no locks
        — the reader thread is the only writer and every field is an atomic int, so this is
        safe to call from any thread while streaming. Counters cover fragments
        (received/lost/loss%), resend requests (sent/honoured), recovery, the decode band
        (incomplete/keyframe-incomplete AUs), frames + bytes (for fps/bitrate), gaps over the
        depth cap, and PTS health (garbage-timestamp share + camera-clock monotonic regressions).
        """
        recv = self._stat_frags_recv
        rec = self._stat_resend_recovered
        holes = self._stat_holes
        first = max(0, recv - rec)            # frags delivered without a resend
        total = first + holes                 # all distinct frags the camera sent
        req = self._stat_resend_req
        fd = self._frag_D
        fe = self._frag_edge
        gap_now = ((fd - fe) & 0xFFFF) if (fd is not None and fe is not None) else 0
        return {
            't': time.time(),
            'frags_recv': recv,
            'frags_lost': holes,
            'loss_pct': round(100.0 * holes / total, 3) if total else 0.0,
            'resend_req': req,
            'resend_recovered': rec,
            'recovery_pct': round(100.0 * rec / holes, 1) if holes else 100.0,
            'recovery_events': rec,
            'au_video': self._stat_au_video,
            'au_audio': self._stat_au_audio,
            'au_incomplete': self._stat_au_incomplete,
            'kf_total': self._stat_kf_total,
            'kf_incomplete': self._stat_kf_incomplete,
            'bytes_video': self._stat_bytes_video,
            'bytes_audio': self._stat_bytes_audio,
            'gap_now': gap_now,
            'gap_max': self._stat_gap_max,
            'gap_cap_jumps': self._stat_gap_cap_jumps,
            'lone_skips': self._stat_lone_skips,
            'keepalive_err': self._stat_keepalive_err,   # L1
            'ts_valid': self._stat_ts_valid,
            'ts_garbage': self._stat_ts_garbage,
            'ts_regress': self._stat_ts_regress,
            'rtt_ewma_ms': round(self._rtt_ewma * 1000.0, 1) if self._rtt_ewma else None,
            'rtt_samples': self._rtt_n,
            'selective_ack': self._selective_ack,
        }

    def get_during_stream(self, name, timeout=2.5):
        """Read a GET_METHODS endpoint safely while a stream is running.

        During a stream the reader thread is the sole socket sender, so a direct ioctl()
        would race it. This hands the request to the reader (which sends it and captures the
        response) and blocks for the parsed dict — used by --benchmark / verbose to poll the
        camera's WiFi signal + session-stats at a modest cadence without perturbing the AV
        channel. With no reader running it falls back to a direct ioctl(). Returns the parsed
        dict, or None on timeout. Read-only (GET) only.
        """
        import cuboai_messages as cm
        th = self._av_reader_thread
        if th is None or not th.is_alive():
            return self._cubo_get(name)             # no reader: a direct ioctl is safe
        builder, want_resp, parser = cm.GET_METHODS[name]
        io_type, payload = builder()
        with self._inject_lock:    # M1: serialize injects — a 2nd caller waits instead of clobbering
            gi = _StreamGet(io_type, payload)
            self._get_inject = gi                       # publish to the reader thread
            try:
                if not gi.done.wait(timeout):
                    return None                         # camera didn't answer in time
                result = parser(gi.result)
                if result is not None and gi.resp_type != want_resp:
                    result['warning'] = f"unexpected resp type {gi.resp_type} (wanted {want_resp})"
                return result
            finally:
                self._get_inject = None                 # retire the slot

    # ── video / snapshot ────────────────────────────────────────────────────
    # IOTYPEs (from cuboai_messages): SETRESOLUTION=0x00FF, AUDIOSTART=0x0300,
    # START=0x01FF. Start payload = 00 00 00 00 04 00 01 00 (per native start_video).
    #
    # Wire-fidelity to native (these are not the arming gate): native does NOT fire
    # all three at connect time. It spaces them ~5 s apart, in order 0x00FF -> 0x0300
    # -> 0x01FF (0x00ff@0.08s -> 0x0300@5.13s = stream-start -> 0x01ff@10.16s; the
    # first AV fragment arrives just AFTER 0x0300, BEFORE 0x01FF). 0x0300 is what
    # actually starts the stream; 0x01FF only follows once video flows.
    #
    # The deferral is controlled by two INDEPENDENT, default-True flags (constructor
    # args, see __init__) so the cadence matches native by default yet a fast path
    # survives:
    #   * 0x00FF: always up front (connect time),
    #   * 0x0300: if defer_stream_start -> ~_MID_IOCTL_SECS after 0x00FF (purely
    #     TIME-based; it is the stream-start trigger, so no video frames exist yet to
    #     count against); else up front,
    #   * 0x01FF: if defer_video_start_late -> ~_LATE_IOCTL_SECS after 0x0300 once
    #     video flows (_LATE_IOCTL_FRAMES access units OR _LATE_IOCTL_SECS, whichever
    #     first; the time cap guarantees it is always sent even if video is sparse, so
    #     deferral can never deadlock); else up front (or riding 0x0300 if that is
    #     deferred — it must never precede stream-start).
    # COST when defer_stream_start=True (default, matches native): time-to-first-video-
    # frame ~5 s. FAST PATH (defer_stream_start=False): first frame as soon as the
    # camera responds (~0.5-2 s, camera-bound), and sub-5 s streams work.
    # Arming is firmware-gated either way, so the flags change ONLY fidelity/latency.
    _VIDEO_START = [
        (0x00FF, b"\x00\x00"),
    ]
    _VIDEO_START_MID = (0x0300, bytes([0, 0, 0, 0, 4, 0, 1, 0]))
    _VIDEO_START_LATE = (0x01FF, bytes([0, 0, 0, 0, 4, 0, 1, 0]))
    _MID_IOCTL_SECS = 5.0        # send 0x0300 (stream-start) this long after 0x00FF (native ~5 s)
    _LATE_IOCTL_FRAMES = 100     # send 0x01FF after this many video access units (~native ~5 s)...
    _LATE_IOCTL_SECS = 5.0       # ...or this long after 0x0300/stream-start, whichever first (no-deadlock cap)

    def snapshot(self, timeout_sec=20.0):
        """Capture one video keyframe (raw bytes) over the pure-Python data channel.

        Returns the first complete keyframe access unit. On Gen3 (HEVC) that is
        VPS+SPS+PPS+IDR starting `00000001 40` (NAL type 32 = VPS). On older Gen1/Gen2
        (H.264) it starts `00000001 67` (SPS, NAL type 7). The
        keyframe is detected codec-agnostically; convert to JPEG downstream with PyAV.

        Drains the AV stream (which starts video and runs the C/D windowed-ACK reader)
        and returns as soon as the first complete keyframe access unit arrives.
        """
        for kind, unit, _fi in self._read_av_units(timeout=timeout_sec):
            if kind == 'video' and _is_video_keyframe(unit, detect_video_codec(unit)):
                return unit
        raise TimeoutError(
            "no video keyframe within %.0fs" % timeout_sec)

    # ── continuous AV streaming (background reader + C/D windowed-ACK) ──
    # How many message-indices ahead of the last-finalised one we hold before forcing a
    # message out — a small reorder/retransmit window so a fragment that arrives late
    # (after the next picture has begun) still lands in the right access unit instead of
    # truncating it. Also bounds how far a junk [56:58] can drag the high-water.
    _MSG_GRACE = 2

    def _av_reader(self, s, out_q, stop_evt):
        """Background thread: drain the socket, ACK continuously, reassemble access units.

        Decoupling the receive/ACK loop from the consumer is what makes streaming robust:
        a slow sink (go2rtc/ffmpeg pipe, on-the-fly muxing) can never stall our ACKs, so
        the camera's send-window stays open and it never throttles. Completed
        ('video'|'audio', bytes) units are pushed to `out_q`; on overflow the oldest is
        dropped (favour live freshness).

        Reassembly (verified live): each camera DATA frame carries an AV message-index at
        [56:58]; ONE access unit (a whole HEVC picture, or one AAC-ADTS frame) is split
        across ALL DATA frames sharing that index. Payload bytes are dec[64:64+avlen]
        (avlen at [52:54]); the Annex-B start code 00 00 00 01 is at [64:68], and for
        audio [64:66] is the FF F1 ADTS sync. A message is finalised once `_MSG_GRACE`
        higher indices have appeared (reorder/retransmit slack). Units are classified by
        content; system/login frames are skipped.
        """
        import select
        import queue as _queue
        import math                 # FIX#5: ceil() for the dynamic grace
        msgs = {}                   # message-index -> {fragment-seq[46:48]: chunk}
        done_upto = -1              # highest message-index already finalised
        au_times = []               # FIX#5: recent AU-finalisation wall times -> live AU-rate
        kf_idxs = set()             # KF-grace: msg-indices identified as keyframes (GOP roots)
        last_ack = time.time()
        gmax = None                 # highest accepted global fragment-seq [46:48]
        # ── ACK rate-limiting — match native's ~10 ACKs/s ──
        # We pace ACKs to native's cadence instead of per-datagram (~113/s): still call
        # _note_cam_data on EVERY datagram (C/D state stays exact), but gate the actual
        # _send_ack to at most one per ACK_INTERVAL, and only when D has advanced since
        # the last ACK we sent (an idle ack still goes out periodically before any data,
        # to prompt the stream, matching native's pre-video idle ACKs). Live this holds
        # ~8/s — within native's range. Pacing buys wire-fidelity to native (and ~14x
        # less ACK traffic); the ACK rate does not gate retransmit or decode.
        # In selective_ack mode ACK promptly on every edge-advance (≈25/s cap) so the
        # camera gets fill-confirmation BEFORE its ~116 ms resend timer re-fires —
        # otherwise it resends each hole several times before pure's una-advance lands.
        # Held-edge mode keeps native's ~10/s pacing.
        ACK_INTERVAL = (0.04 if self._selective_ack else 0.10)
        last_acked_D = self._frag_D     # _frag_D value at our most recent ACK
        last_acked_da = self._data_ack  # _data_ack value at our most recent ACK
        last_acked_edge = self._frag_edge  # _frag_edge value at our most recent ACK
        last_nak = time.time()          # last 0x0b/0x0a resend-control send
        t_stream0 = time.time()         # verbose: stream start (for the [stream] t= clock)
        last_vlog = 0.0                 # verbose: last [stream] status line (rate-limit ~1/s)
        nframes = 0                     # verbose: camera AV fragments seen this session
        video_frame_count = 0           # completed VIDEO access units (gates the deferred 0x01FF)
        # cadence-flag-aware init. If a stage is NOT deferred it was already sent
        # up front (_read_av_units), so mark it done here. t_mid (the 0x01FF timer base)
        # is stream-open when 0x0300 went up front, else set when 0x0300 fires below.
        mid_ioctl_sent = not self._defer_stream_start
        t_mid = t_stream0 if mid_ioctl_sent else None
        late_ioctl_sent = mid_ioctl_sent and not self._defer_video_start_late

        def maybe_ack():
            nonlocal last_ack, last_acked_D, last_acked_da, last_acked_edge, last_vlog
            now = time.time()
            if now - last_ack < ACK_INTERVAL:
                return
            # Ack when the AV window advanced (D), OR the reliable-IO ack advanced
            # (data_ack — needed at the status->video handoff to free the camera's IOCTL
            # responses), OR we are still idle pre-AV (_frag_D is None) and must keep
            # prompting the camera. Otherwise stay quiet (rate-limit to native's ~10/s).
            advanced = not (self._frag_D is not None and self._frag_D == last_acked_D
                            and self._data_ack == last_acked_da)
            # in resend_mode the wire D IS the contiguous edge, and maybe_nak's
            # forward-skip advances that edge WITHOUT high-water moving (the camera is
            # window-stalled). Unless we re-ack on edge-advance the skipped-forward D
            # never reaches the camera, so the window never reopens — the 2.6/s deadlock.
            # Re-ack whenever the edge moved so the camera always sees our latest D.
            if (self._resend_mode and self._frag_edge is not None
                    and self._frag_edge != last_acked_edge):
                advanced = True
            if not advanced:
                return
            self._send_ack()
            last_ack = now
            last_acked_D = self._frag_D
            last_acked_da = self._data_ack
            last_acked_edge = self._frag_edge
            if self._verbose and now - last_vlog >= 1.0:
                last_vlog = now
                if self._frag_D is None:
                    self._vlog(f"[stream] t={now - t_stream0:4.1f}s  idle (no AV yet)  "
                               f"data_ack={self._data_ack}")
                else:
                    gap = ((self._frag_D - self._frag_edge) & 0xFFFF
                           if self._frag_edge is not None else 0)
                    self._vlog(f"[stream] t={now - t_stream0:4.1f}s  edge={self._frag_edge} "
                               f"hi={self._frag_D}  OOO/sack={gap}  data_ack={self._data_ack}  "
                               f"frags={nframes}")

        def maybe_nak():
            """resend_mode gap signalling: forward-skip a stale gap (so the held-C ack's
            window reopens) and emit the 0x0b/0x0a resend pair at ~8/s. No-op unless
            resend_mode is enabled (default ON — see _send_ack). Stall guards: a 50ms
            forward-skip (_GAP_STALE) plus a hard depth cap (_GAP_DEPTH_CAP) that jumps the
            edge near high-water when holes pile up, so held-C can no longer dead-stall the
            stream under heavy loss."""
            nonlocal last_nak
            if not self._resend_mode:
                return
            now = time.time()
            # native emits the 0x0b/0x0a resend-control pair from BEFORE the first
            # AV fragment (pre-video). Pure used to wait until _frag_D was set (first
            # fragment seen), so its NAK loop started late. With no fragment yet there is
            # no gap to name, so just emit the pair on the NAK cadence (build_resend_req
            # uses highwater=(_frag_D or 0)=0 = native's no-loss steady-state value).
            # This runs on the reader thread (the sole sender during streaming) -> no
            # socket/seq race; a separate connect()-spawned NAK thread WOULD race with
            # maybe_ack's _send_ack (and with ioctl()), so that approach is NOT used.
            if self._frag_D is None or self._frag_edge is None:
                if now - last_nak < self._nak_interval:
                    return
                last_nak = now
                self._send_nak()
                return
            gap = (self._frag_D - self._frag_edge) & 0xFFFF
            if gap > self._stat_gap_max:                                # stats: high-water hole depth
                self._stat_gap_max = gap
            if gap != 0:                                                # a gap exists
                if gap > self._gap_depth_cap and not self._holding_kf:  # backstop; KF-grace holds the una at a kf's holes
                    self._stat_gap_cap_jumps += 1                       # stats: depth-cap backstop fired
                    # Too many holes piled up: jump the edge to within 10 of high-water so
                    # the camera's send-window can't fill and dead-stall, then advance over
                    # anything already received in that tail. Bounds the signalled gap to
                    # <=10 frags — one persistent hole can no longer sink the stream.
                    self._frag_edge = (self._frag_D - 10) & 0xFFFF
                    self._frag_gap_ts.clear()
                    while ((self._frag_D - self._frag_edge) & 0xFFFF) != 0:
                        n2 = (self._frag_edge + 1) & 0xFFFF
                        if n2 in self._frag_received:
                            self._frag_edge = n2
                        else:
                            break
                # in selective_ack mode DON'T forward-skip a stale gap — the una must
                # HOLD at a genuine hole until its resend FILLS it (the camera resends the
                # holes we SACK, native-style), otherwise the 50ms skip drops the hole out of
                # the (una, high-water] request window before it can be recovered. The
                # _gap_depth_cap above is the only backstop in selective mode (jumps the una
                # near high-water if >cap holes pile up so the camera's send-window can never
                # dead-stall). Held-edge mode keeps the skip.
                if not self._selective_ack:
                    nxt = (self._frag_edge + 1) & 0xFFFF
                    t = self._frag_gap_ts.get(nxt)
                    if t is None:
                        self._frag_gap_ts[nxt] = now
                    elif now - t > self._gap_hold:                     # held past resend wait -> skip
                        if self._verbose:
                            self._vlog(f"[stream] SKIP gap@{nxt} stale "
                                       f"({int((now - t) * 1000)}ms)  edge {self._frag_edge}->{nxt}")
                        self._frag_edge = nxt
                        self._frag_gap_ts.pop(nxt, None)
                        while ((self._frag_D - self._frag_edge) & 0xFFFF) != 0:
                            n2 = (self._frag_edge + 1) & 0xFFFF
                            if n2 in self._frag_received:
                                self._frag_edge = n2
                                self._frag_gap_ts.pop(n2, None)
                            else:
                                break
            if now - last_nak < self._nak_interval:
                return
            last_nak = now
            self._send_nak()

        def cur_grace():
            # dynamic reassembly grace = ceil(resend_latency_EWMA * AU_rate) + 1,
            # clamped to [_MSG_GRACE, _grace_max].  Off / un-warmed (no EWMA sample yet or
            # too few AUs to time a rate) -> the static _MSG_GRACE, i.e. byte-identical to
            # the shipped behaviour, so CUBOAI_GRACE_SCALE=0 is a clean A/B baseline.
            if not self._grace_scale or self._rtt_ewma is None or len(au_times) < 8:
                return self._MSG_GRACE
            span = au_times[-1] - au_times[0]
            if span <= 0:
                return self._MSG_GRACE
            au_rate = (len(au_times) - 1) / span            # AUs/s (video+audio share the idx)
            g = math.ceil(self._rtt_ewma * au_rate) + 1
            return max(self._MSG_GRACE, min(self._grace_max, g))

        def assemble(frag_map):
            # Concatenate a message's fragments in fragment-seq order, NOT arrival order:
            # a retransmitted fragment arrives late (after later fragments) and must still
            # land in its correct position, or the NAL bytes scramble and the picture fails
            # to decode. The dict keys also dedupe retransmits.
            return b"".join(frag_map[k] for k in sorted(frag_map))

        def is_marker(p):
            # First fragment of every access unit starts with an AV marker: the HEVC
            # Annex-B start code, or the FF Fx AAC-ADTS sync.
            return (p[:4] == b"\x00\x00\x00\x01" or
                    (len(p) >= 2 and p[0] == 0xFF and (p[1] & 0xF6) == 0xF0))

        def is_kf_marker(p):
            # KF-grace: first frag of a KEYFRAME AU — Annex-B start code then an HEVC
            # VPS(32)/SPS(33)/PPS(34) or IDR(19,20)/CRA(21) NAL (nal_type=(byte4>>1)&0x3f).
            return (len(p) >= 5 and p[:4] == b"\x00\x00\x00\x01"
                    and ((p[4] >> 1) & 0x3f) in (32, 33, 34, 19, 20, 21))

        def kf_complete(fm):
            # KF-grace: a keyframe's fragments are consecutive frag-seqs; complete = no
            # interior gap (misses a trailing-frag loss, the minority case).
            ks = sorted(fm)
            return bool(ks) and (ks[-1] - ks[0] + 1) == len(ks)

        def classify(b):
            if b[:4] == b"\x00\x00\x00\x01":
                return 'video'
            if len(b) >= 2 and b[0] == 0xFF and (b[1] & 0xF6) == 0xF0:
                return 'audio'
            return None             # system/login frame — skip

        def emit(kind, unit, fi=None):
            # queue (kind, unit, frameinfo) so the parsed FRAMEINFO travels WITH its AU
            # (the racy self._last_frameinfo runs ahead of the consumer by the queue depth). The
            # back-compat APIs drop the 3rd element; the *_timed APIs yield it. fi is None for
            # audio / unparsed AUs. The queued bytes are unchanged -> annexb output byte-identical.
            item = (kind, unit, fi)
            try:
                out_q.put_nowait(item)
            except _queue.Full:
                try:
                    out_q.get_nowait()
                except _queue.Empty:
                    pass
                try:
                    out_q.put_nowait(item)
                except _queue.Full:
                    pass

        def seal_one(m, fm, lag=-1):
            # Finalise one AU (m=msg-idx, fm=frag-map): assemble + classify + emit, emitting the
            # PARTIAL slice verbatim if still incomplete (never drops a PRESENT AU). Shared by the
            # in-order (NODROP) and the legacy seal paths. lag = hi-m at emit (latency proxy, AU-idx).
            nonlocal video_frame_count
            if self._truncate_partial:
                _ks = sorted(fm)
                if (_ks and (_ks[-1] - _ks[0] + 1) != len(_ks)
                        and fm[_ks[0]][:4] == b"\x00\x00\x00\x01"):
                    # C1 clean-truncation: contiguous prefix up to the first hole (no bridge).
                    _pref = []; _exp = _ks[0]
                    for _k in _ks:
                        if _k != _exp: break
                        _pref.append(fm[_k]); _exp += 1
                    unit = b"".join(_pref)
                else:
                    unit = assemble(fm)
            else:
                unit = assemble(fm)
            au_times.append(time.time())             # feed the live AU-rate
            if len(au_times) > 128:
                del au_times[0]
            ks = sorted(fm)                                       # frag-seqs (hoisted: au_log + FRAMEINFO strip)
            comp = bool(ks) and (ks[-1] - ks[0] + 1) == len(ks)   # contiguous fragments == COMPLETE AU
            # gated codec/FRAMEINFO census, emitted at the EARLIEST
            # point a full AU exists — before audio-truncation, video-FRAMEINFO-strip, or the
            # consumer's kind filter. One stderr line per AU. Gated (default OFF) => byte-identical.
            if self._log_frameinfo and (not self._log_frameinfo_max
                                        or self._ficensus_n < self._log_frameinfo_max):
                _tail = unit[-_FRAMEINFO_LEN:] if len(unit) >= _FRAMEINFO_LEN else unit
                _cid = struct.unpack_from('<H', _tail, 0)[0] if len(_tail) >= 2 else -1
                _kfb = _tail[2] if len(_tail) >= 3 else -1
                _b8 = _tail[8:12].hex() if len(_tail) >= 12 else ''
                _kc = classify(unit)
                self._ficensus_n += 1
                print(f"FICENSUS n={self._ficensus_n} idx={m} len={len(unit)} comp={int(comp)} "
                      f"kind={_kc or 'sys'} head={unit[:8].hex()} codec_id=0x{_cid:04x} "
                      f"codec={_frameinfo_codec_name(_cid)} kf={_kfb} b8_12={_b8} "
                      f"tail24={_tail.hex()}", file=sys.stderr, flush=True)
                if self._ficensus_n == self._log_frameinfo_max:
                    print(f"FICENSUS capped after {self._ficensus_n} AUs "
                          f"(CUBOAI_LOG_FRAMEINFO_MAX)", file=sys.stderr, flush=True)
            if self._au_log is not None:
                self._au_log.append(('emit', m, comp, len(fm), unit[:4] == b"\x00\x00\x00\x01", lag))
                if ks and unit[:4] == b"\x00\x00\x00\x01":   # video: log emit-time + interior holes
                    miss = [q for q in range(ks[0] + 1, ks[-1]) if q not in fm]
                    self._au_log.append(('emitv', m, time.time(), ks[0], ks[-1], tuple(miss)))
            kind = classify(unit)
            if kind is None:
                return
            # stats: per-AU completeness + keyframe accounting (the decode-band signal).
            if kind == 'video':
                self._stat_au_video += 1
                _is_kf = bool(ks) and is_kf_marker(fm[ks[0]])
                if not comp:
                    self._stat_au_incomplete += 1
                if _is_kf:
                    self._stat_kf_total += 1
                    if not comp:
                        self._stat_kf_incomplete += 1
            else:
                self._stat_au_audio += 1
            _au_fi = None        # the parsed FRAMEINFO for THIS au (set below when stripped)
            if kind == 'audio':
                fl = _adts_frame_len(unit)
                # surface the audio FRAMEINFO ts (gated CUBOAI_STRIP_FRAMEINFO, like
                # video). A complete audio AU is [ADTS frame (fl)] + [24B trailer]; parse the trailer
                # at [fl:fl+24] (audio codec_id + plausible rate/channels) for its ts_sec BEFORE
                # truncating to the ADTS frame. The emitted bytes (unit[:fl]) are UNCHANGED, so the
                # strip-off / mux-audio-off path stays byte-identical; only the 3rd timed-API element
                # (was always None for audio) now carries the audio ts for A/V-synced PTS.
                if (self._strip_frameinfo and comp and fl and 7 <= fl
                        and len(unit) >= fl + _FRAMEINFO_LEN):
                    _afi = unit[fl:fl + _FRAMEINFO_LEN]
                    if _looks_like_audio_frameinfo(_afi):
                        _au_fi = _parse_audio_frameinfo(_afi)
                if fl and 7 <= fl <= len(unit):
                    unit = unit[:fl]
            if kind == 'video':
                video_frame_count += 1               # gates the deferred 0x01FF
                # Strip+parse the 24-byte TUTK FRAMEINFO trailer (gated CUBOAI_STRIP_FRAMEINFO).
                # COMPLETE AUs only (`comp`): a complete AU ends with the trailer; an incomplete /
                # TRUNCATE_PARTIAL unit does not. Sanity-check a video codec_id + plausible w/h before
                # cutting so an edge case can't slice into a real NAL; on mismatch skip+log. OFF
                # path byte-identical. The parsed FRAMEINFO travels with this AU for PTS.
                if self._strip_frameinfo and comp and len(unit) >= _FRAMEINFO_LEN + 5:
                    _fi = unit[-_FRAMEINFO_LEN:]
                    if _looks_like_frameinfo(_fi):       # known video codec_id + plausible w/h
                        _au_fi = _parse_frameinfo(_fi)
                        self._last_frameinfo = _au_fi
                        unit = unit[:-_FRAMEINFO_LEN]
                        # stats: engine-level PTS health — garbage-ts share + camera-clock
                        # monotonicity (the same per-AU FRAMEINFO the muxer's PTSClock reads).
                        if _au_fi.get('ts_valid'):
                            self._stat_ts_valid += 1
                            _tsm = _au_fi.get('timestamp_ms')
                            if _tsm is not None:
                                if self._stat_last_ts is not None and _tsm < self._stat_last_ts:
                                    self._stat_ts_regress += 1
                                self._stat_last_ts = _tsm
                        else:
                            self._stat_ts_garbage += 1
                    else:
                        self._frameinfo_skips += 1
                        if self._frameinfo_skips <= 20:
                            print(f"[strip_frameinfo] AU idx={m}: complete but trailing 24B not a "
                                  f"FRAMEINFO (codec_id=0x{struct.unpack_from('<H', _fi, 0)[0]:04x}) "
                                  f"— NOT stripping", file=sys.stderr, flush=True)
            # stats: emitted bytes (post strip/truncate = what the consumer sees) -> bitrate
            if kind == 'video':
                self._stat_bytes_video += len(unit)
            else:
                self._stat_bytes_audio += len(unit)
            emit(kind, unit, _au_fi)

        try:
            while not stop_evt.is_set():
                r, _, _ = select.select([s], [], [], 0.04)
                maybe_ack()                        # rate-limited (~10/s) periodic ACK
                maybe_nak()                        # resend_mode gap NAK (no-op if off)
                # send the deferred 0x0300 (stream-start) ~_MID_IOCTL_SECS after
                # stream-open, matching native (0x0300 @~5 s, reliable-seq ~68). Purely
                # time-based: 0x0300 is the trigger that starts video, so there are no
                # frames to count before it. Sent here on the reader thread (sole sender)
                # -> no seq race. Video begins flowing only after this fires. Skipped
                # when defer_stream_start is off (then 0x0300 went up front; mid_ioctl_
                # sent is already True). When 0x01FF is NOT deferred but 0x0300 IS, the
                # 0x01FF rides immediately after 0x0300 here (it must not precede it).
                if self._defer_stream_start and not mid_ioctl_sent \
                        and time.time() - t_stream0 >= self._MID_IOCTL_SECS:
                    self._send_video_start_mid()
                    mid_ioctl_sent = True
                    t_mid = time.time()
                    self._vlog(f"[stream] deferred 0x0300 (stream-start) sent  "
                               f"(t={t_mid - t_stream0:.1f}s)")
                    if not self._defer_video_start_late and not late_ioctl_sent:
                        self._send_video_start_late()
                        late_ioctl_sent = True
                        self._vlog("[stream] 0x01FF sent (rides 0x0300; defer off)")
                # then 0x01FF ~_LATE_IOCTL_SECS AFTER 0x0300 once video flows
                # (after _LATE_IOCTL_FRAMES video units OR _LATE_IOCTL_SECS since 0x0300),
                # matching native's ~5-s spacing. Gated on defer_video_start_late AND on
                # 0x0300 having been sent so the timer measures from stream-start, not
                # connect. The time cap fires even when video is sparse, so a stream that
                # needs 0x01FF can never deadlock.
                if self._defer_video_start_late and not late_ioctl_sent and mid_ioctl_sent and (
                        video_frame_count >= self._LATE_IOCTL_FRAMES
                        or time.time() - t_mid >= self._LATE_IOCTL_SECS):
                    self._send_video_start_late()
                    late_ioctl_sent = True
                    self._vlog(f"[stream] deferred 0x01FF sent  "
                               f"(video_frame_count={video_frame_count}, "
                               f"t={time.time() - t_stream0:.1f}s)")
                # Mid-stream GET injection (benchmark/verbose telemetry): the reader is the
                # SOLE socket sender during a stream, so a requested GET is sent here and its
                # response captured in the recv path below. Inert when nothing is pending
                # (self._get_inject is None), so the default stream stays byte-identical.
                gi = self._get_inject
                if gi is not None and not gi.done.is_set():
                    now2 = time.time()
                    if not gi.sent or now2 - gi.last_tx > 0.4:   # initial send / retransmit
                        self._sock.sendto(
                            build_ioctl_data(self._R, self._seq, self._relseq,
                                             self._frmno, gi.io_type, gi.payload),
                            self._cam)
                        self._seq += 1                           # every (re)tx burns one pkt seq
                        if not gi.sent:                          # the request reliable-seq/FrmNo once
                            self._relseq += 1
                            self._frmno += 1
                            gi.sent = True
                        gi.last_tx = now2
                if not r:
                    continue
                # Drain every datagram queued this wake-up so the kernel RX buffer never
                # backs up (a stalled drain opens a contiguity gap and stops the stream).
                while True:
                    try:
                        raw, addr = s.recvfrom(8192)
                    except (BlockingIOError, OSError):
                        break
                    if len(raw) < 30:
                        # Answer the camera's 24-byte IOTC keepalive (alive-check)
                        # probe, as native does. Native replies to every probe;
                        # otherwise the session is a silent non-responder to liveness
                        # checks. Byte-identical reply; does NOT affect AV retransmit —
                        # fidelity/liveness only.
                        if is_keepalive_probe(raw):
                            self._session_fp = xor_frame(raw)[16:20]  # echoed token
                            try:
                                s.sendto(build_keepalive_reply(raw), addr)
                            except OSError:
                                self._stat_keepalive_err += 1   # L1: observable instead of silent
                        continue
                    dec = inv_transcode(raw)
                    if len(dec) < 40:
                        continue
                    if dec[28] != 0x0C or len(dec) < 68:
                        # capture the camera's ms-clock from cam->host 0x0a [36:38]
                        # so _send_nak can echo it in our 0x0b (THE arming discriminator).
                        if dec[28] == 0x0A and len(dec) >= 38:
                            self._cam_clock = struct.unpack_from('<H', dec, 36)[0]
                            self._cam_clock_ts = time.time()
                        maybe_ack()                    # ack ACK/NAK probes (rate-limited)
                        continue
                    self._note_cam_data(dec)           # advance C/D or data_ack
                    maybe_ack()                        # rate-limited; D/data_ack may have advanced
                    if self._is_io_frame(dec):
                        # Reliable IO/control response (IOCTL retransmit, login/system
                        # frame): acked via [40:44] above, never part of an AV unit.
                        gi = self._get_inject       # capture an injected GET's response
                        if (gi is not None and gi.sent and not gi.done.is_set()
                                and len(dec) >= 68
                                and struct.unpack_from("<H", dec, 64)[0] == gi.resp_type):
                            avlen = struct.unpack_from("<H", dec, 52)[0]
                            gi.result = bytes(dec[68:min(len(dec), 68 + max(0, avlen - 4))])
                            gi.done.set()
                        continue
                    idx = struct.unpack("<H", dec[56:58])[0]
                    if self._idx_modular:                 # H1: lift the u16 index into done_upto's
                        idx = _unwrap_index(idx, done_upto)  # space so the gate survives the wrap
                    frag = struct.unpack("<H", dec[46:48])[0]
                    avlen = struct.unpack("<H", dec[52:54])[0]
                    chunk = bytes(dec[64:64 + max(0, avlen)])
                    nframes += 1                       # verbose: camera AV fragment counter
                    if self._kf_grace and idx not in kf_idxs and is_kf_marker(chunk):
                        kf_idxs.add(idx)               # KF-grace: this AU is a keyframe (GOP root)
                    # ── filter out-of-band frames before reassembly ──
                    # The camera multiplexes non-AV system/login frames onto the same
                    # channel; they alias the AV message-index (e.g. the keyframe's idx 0)
                    # and would corrupt the picture they land in. Reject them three ways:
                    #  1. avlen > 1024 — a real AV fragment is MTU-capped at 1024; the
                    #     "0500…" status frames are 1044, so this cleanly drops them.
                    #  2. seed the global fragment-seq only on an access-unit START (a
                    #     marker fragment) — skips the tiny login frame that precedes the
                    #     keyframe's first fragment and would otherwise be merged in.
                    #  3. a fragment-seq far from the running high-water (stuck at 0, or a
                    #     wild value thousands away) is out-of-band — drop it.
                    if avlen > 1024:
                        continue
                    if gmax is None:
                        if not is_marker(chunk):
                            continue
                        gmax = frag
                    else:
                        fwd = (frag - gmax) & 0xFFFF
                        back = (gmax - frag) & 0xFFFF
                        if fwd <= 256:
                            gmax = frag
                        elif back <= 128:
                            pass                       # in-window retransmit fills a gap
                        else:
                            continue                   # out-of-band — drop
                    # Accept only a plausibly-forward message-index (rejects any residual
                    # out-of-band index that would truncate the picture in flight).
                    if done_upto < idx <= done_upto + 256:
                        msgs.setdefault(idx, {})[frag] = chunk
                    elif self._au_log is not None and ((idx - done_upto) & 0xFFFF) > 0x8000:
                        # a frag arrived for an AU done_upto already SEALED past -> rejected
                        # (the hard-gap cause on a refs=1 stream); note if it's a video marker
                        self._au_log.append(('reject', idx, chunk[:4] == b"\x00\x00\x00\x01"))

                    if msgs:
                        hi = max(msgs)
                        grace = cur_grace()                  # FIX#5: dynamic (==_MSG_GRACE when off)
                        if self._kf_grace:
                            self._holding_kf = False
                        # Seal buffered AUs ascending. KF-grace: HOLD the line at an incomplete
                        # keyframe (don't seal it or the GOP above it) until it COMPLETES or hi
                        # reaches it+_kf_hold. Without KF-grace this is exactly the old loop:
                        # seal every m <= hi-grace.
                        if self._nodrop:
                            # IN-ORDER never-skip seal (finalize-drop fix): emit done_upto+1
                            # at grace-expiry (PARTIAL if incomplete via seal_one), advancing past
                            # a truly-absent AU only after ITS grace; never seal a higher idx first,
                            # so a slightly-late AU is waited for + emitted, not skipped+rejected.
                            while True:
                                m = done_upto + 1
                                fm = msgs.get(m)
                                if self._kf_grace and m in kf_idxs:
                                    eff = self._kf_hold
                                elif fm is not None:
                                    eff = max(grace, self._recovery_hold)   # present-incomplete: wait for resend
                                else:
                                    eff = max(grace, self._nodrop_grace)
                                # emit-on-complete: a present, marker-led, gap-free AU that is
                                # BOUNDED by m+1's first frag (a[-1]+1 == min(msgs[m+1]) -> no
                                # straggler can still belong to m) is DONE -> emit NOW, no grace wait.
                                if self._emit_complete:
                                    if fm is not None and (m + 1) in msgs:
                                        a = sorted(fm)
                                        if (a[-1] - a[0] + 1) == len(a) and a[-1] + 1 == min(msgs[m + 1]) \
                                                and is_marker(fm[a[0]]):
                                            seal_one(m, msgs.pop(m), hi - m); done_upto = m; kf_idxs.discard(m)
                                            continue
                                if m > hi - eff:             # else hold within grace (in order)
                                    if self._kf_grace and m in kf_idxs and m in msgs:
                                        self._holding_kf = True
                                    break
                                if m in msgs:
                                    seal_one(m, msgs.pop(m), hi - m)
                                done_upto = m
                                kf_idxs.discard(m)
                        else:
                            # legacy seal: every m <= hi-grace ascending; min() SKIPS absent idxs
                            # -> done_upto jumps -> late frags rejected = the POC-gap bug.
                            while msgs:
                                m = min(msgs)
                                if self._kf_grace and m in kf_idxs:
                                    if not kf_complete(msgs[m]) and (hi - m) < self._kf_hold:
                                        self._holding_kf = True   # head-of-line hold
                                        break
                                elif m > hi - grace:
                                    break                         # P-frame still within grace
                                if self._au_log is not None and m > done_upto + 1:
                                    for _gap in range(done_upto + 1, m):
                                        self._au_log.append(('skip', _gap))
                                seal_one(m, msgs.pop(m), hi - m)
                                done_upto = max(done_upto, m)
                                kf_idxs.discard(m)
        finally:
            out_q.put(None)         # signal end-of-stream to the consumer

    def _read_av_units(self, timeout=None, max_items=None):
        """Yield ('video'|'audio', access_unit_bytes) tuples from the camera stream.

        Sends the video-start IOCTLs, then spawns `_av_reader` to receive/ACK/reassemble
        in the background and yields completed units from a queue. The C/D windowed-ACK
        (see build_data_ack) keeps the camera streaming continuously; without the reader
        running independently of this generator, a slow consumer would stall the ACKs and
        the camera would throttle to a stop after its initial ~2 s burst.
        """
        import threading
        import queue as _queue
        if self._sock is None or self.session_hdr is None:
            self.disconnect()
            if not self.connect():
                raise RuntimeError("handshake failed (no 0x2041)")
        s = self._sock

        # build the up-front IOCTL batch from the cadence flags. 0x00FF always
        # goes up front. 0x0300 / 0x01FF go up front ONLY when their deferral is off
        # (the fast path); otherwise _av_reader emits them on its timers. Sending
        # 0x01FF up front only makes sense when 0x0300 is also up front (it must not
        # precede stream-start), so it is gated on BOTH flags being off.
        start = list(self._VIDEO_START)                       # [0x00FF]
        if not self._defer_stream_start:
            start.append(self._VIDEO_START_MID)               # 0x0300 up front (fast path)
            if not self._defer_video_start_late:
                start.append(self._VIDEO_START_LATE)          # 0x01FF up front too
        for io_type, pl in start:
            s.sendto(build_ioctl_data(self._R, self._seq, self._relseq,
                                      self._frmno, io_type, pl), self._cam)
            self._seq += 1
            self._relseq += 1
            self._frmno += 1
            time.sleep(0.02)

        out_q = _queue.Queue(maxsize=600)
        stop_evt = threading.Event()
        reader = threading.Thread(target=self._av_reader,
                                  args=(s, out_q, stop_evt), daemon=True)
        # Publish to self so disconnect() can stop the reader before closing the
        # socket out from under it (e.g. an interrupted/aborted stream).
        self._av_stop_evt = stop_evt
        self._av_reader_thread = reader
        reader.start()

        emitted = 0
        t0 = time.time()
        try:
            while True:
                if timeout is not None and time.time() - t0 >= timeout:
                    return
                if max_items is not None and emitted >= max_items:
                    return
                try:
                    item = out_q.get(timeout=0.2)
                except _queue.Empty:
                    if not reader.is_alive():
                        return
                    continue
                if item is None:               # reader ended
                    return
                yield item
                emitted += 1
        finally:
            stop_evt.set()
            reader.join(timeout=1.5)
            if self._av_reader_thread is reader:
                self._av_reader_thread = None
                self._av_stop_evt = None

    def _av_stream(self, duration=None, max_items=None):
        """Yield ('video'|'audio', bytes) access units (see _read_av_units)."""
        for kind, unit, _fi in self._read_av_units(timeout=duration, max_items=max_items):
            yield (kind, unit)

    # ── consumer API ──────────────────────────────────────────────────────────
    # The reader queues (kind, bytes, frameinfo) 3-tuples (Part A). The classic APIs below drop
    # the frameinfo (back-compat, byte-identical output); the *_timed variants yield it so a
    # consumer (cuboai_stream_video's mpegts path) can drive PTS from the real per-frame timestamp.
    def av_frames(self, duration=None):
        """Yield ('video'|'audio', bytes) tuples for `duration` seconds (or forever)."""
        for kind, unit, _fi in self._read_av_units(timeout=duration):
            yield (kind, unit)

    def av_frames_timed(self, duration=None):
        """Like av_frames but yields (kind, bytes, frameinfo); frameinfo is the parsed FRAMEINFO
        dict for that AU (video, when CUBOAI_STRIP_FRAMEINFO is on) or None (audio/unparsed)."""
        yield from self._read_av_units(timeout=duration)

    def video_frames(self, max_frames=None):
        """Yield raw video access-unit bytes (video only; H.264 or HEVC Annex-B)."""
        n = 0
        for kind, data, _fi in self._read_av_units():
            if kind != 'video':
                continue
            yield data
            n += 1
            if max_frames is not None and n >= max_frames:
                return

    def video_frames_timed(self, duration=None, max_frames=None):
        """Yield (video_bytes, frameinfo) for video AUs only — the per-AU FRAMEINFO travels with
        its bytes (Part A), enabling PTS assignment (cuboai_pts) without the racy _last_frameinfo."""
        n = 0
        for kind, data, fi in self._read_av_units(timeout=duration):
            if kind != 'video':
                continue
            yield (data, fi)
            n += 1
            if max_frames is not None and n >= max_frames:
                return

    def audio_frames(self, max_frames=None):
        """Yield raw AAC-ADTS frame bytes (audio only)."""
        n = 0
        for kind, data, _fi in self._read_av_units():
            if kind != 'audio':
                continue
            yield data
            n += 1
            if max_frames is not None and n >= max_frames:
                return

    # ── file-producing media helpers ─────────────────────────────────────────
    def save_snapshot(self, path, timeout_sec=20.0, quality=90):
        """Capture one keyframe and save it as a JPEG. Returns the path.

        Decodes the HEVC keyframe from snapshot() to JPEG via PyAV (`pip install av`).
        """
        raw = self.snapshot(timeout_sec=timeout_sec)
        jpeg = hevc_to_jpeg(raw, quality=quality)
        path = os.path.expanduser(path)
        with open(path, "wb") as f:
            f.write(jpeg)
        return path

    def record_video(self, path, duration_sec=10.0):
        """Record video+audio for `duration_sec` and mux to a playable .mp4 with TRUE camera-clock
        A/V sync.

        Drains av_frames_timed() (per-AU FRAMEINFO) and assigns PTS through cuboai_pts.AVTimeline —
        video from the FRAMEINFO timestamp, audio from its ts_sec via the drift-free AudioTimeline, on
        ONE shared base (ts_valid-gated, so a garbage timestamp can't shift the timeline). The clip
        then holds the same A/V alignment the live streamer proved (~0.2 ms/min), instead of the old
        synthesised-fps video + free-running j·1024 audio counter that drifted on loss. Stream copy,
        no re-encode. Returns the path.

        Run under the production profile (FRAMEINFO strip + recovery — the cuboai_validate /
        cuboai_stream_video default) for a clean playable file; with CUBOAI_STRIP_FRAMEINFO=0 (e.g.
        --raw) the AUs carry their trailer and the PTS is interpolated (unprocessed bitstream).
        """
        from cuboai_pts import AVTimeline
        path = os.path.expanduser(path)
        av_tl = AVTimeline()
        video_items, audio_items = [], []
        # CUBOAI_RECORD_CLEAN_GOP (default OFF -> byte-identical to the historical recorder):
        # when set, drop incomplete video AUs + the poisoned GOP tail until the next IDR, the same
        # suppression the live MPEG-TS path applies, so a recorded clip has no decode band on loss.
        _items = self.av_frames_timed(duration=duration_sec)
        if os.environ.get("CUBOAI_RECORD_CLEAN_GOP", "1") != "0":  # default ON (set =0 for raw recorder)
            _items = _clean_gop_video_items(_items)
        for kind, data, fi in _items:
            if kind == "video":
                video_items.append((data, av_tl.video(fi)['pts_ms']))
            elif kind == "audio":
                audio_items.append((data, av_tl.audio(fi)['pts_ms']))
        if not video_items:
            raise RuntimeError(
                "no video frames captured — camera sent no stream (retry on a "
                "fresh connection; the camera throttles repeated stream starts)")
        mux_to_mp4_timed(path, video_items, audio_items)
        return path

    def record_audio(self, path, duration_sec=10.0):
        """Record audio for `duration_sec` to a raw AAC-ADTS (.aac) file.

        Writes the camera's ADTS frames straight to disk (already valid AAC-LC
        16 kHz mono — playable directly / by any AAC decoder). Returns the path.
        """
        path = os.path.expanduser(path)
        n = 0
        with open(path, "wb") as f:
            for kind, data in self.av_frames(duration=duration_sec):
                if kind == "audio":
                    f.write(data)
                    n += 1
        return path

    # ── SET commands ──────────────────────────────────────────────────────────
    # Each builds the request with a cuboai_messages builder and sends it on the
    # (multi-IOCTL-capable) data channel. The camera echoes a response which we
    # return as (resp_type, resp_bytes) so callers can confirm.
    def _cubo_set(self, builder_result):
        io_type, payload = builder_result
        return self.ioctl(io_type, payload)

    def set_night_light(self, on):
        """Turn the night light on/off."""
        import cuboai_messages as cm
        return self._cubo_set(cm.build_set_night_light(bool(on)))

    def set_light_brightness(self, brightness):
        """Set night-light brightness (0-100)."""
        import cuboai_messages as cm
        return self._cubo_set(cm.build_set_light_style_brightness(int(brightness)))

    def set_sleep_mode(self, enabled):
        """Enable/disable sleep (privacy) mode. NOTE: ON suspends the AV stream."""
        import cuboai_messages as cm
        return self._cubo_set(cm.build_set_sleep_mode(bool(enabled)))

    def set_lullaby(self, sound_id, volume=None, duration=None):
        """Play a lullaby and optionally set volume + sleep timer.

        `sound_id`: 1-based index into cuboai_messages.LULLABY_CATALOG (or a full
        UUID string). `volume`: 0-100 (optional). `duration`: minutes for the sleep
        timer, 0/None = repeat forever (optional).
        """
        import cuboai_messages as cm
        if isinstance(sound_id, str) and "-" in sound_id:
            uuid = sound_id
        else:
            uuids = list(cm.LULLABY_CATALOG.keys())
            i = int(sound_id) - 1
            if not 0 <= i < len(uuids):
                raise ValueError(f"sound_id {sound_id} out of range 1..{len(uuids)}")
            uuid = uuids[i]
        resp = self._cubo_set(cm.build_set_lullaby_play(uuid))
        if volume is not None or duration is not None:
            timer = cm.LULLABY_TIMER_REPEAT if not duration else (int(duration) * 60)
            resp = self._cubo_set(
                cm.build_set_lullaby_vol_duration(int(volume or 0), timer))
        return resp

    def set_lullaby_stop(self):
        """Stop the currently playing lullaby."""
        import cuboai_messages as cm
        uuid = ""
        try:
            cur = self.get_lullaby()
            uuid = cur.get("uuid") or ""
        except Exception:
            pass
        return self._cubo_set(cm.build_set_lullaby_stop(uuid))

    def set_cry_detection(self, enabled=None, sensitivity=None):
        """Set cry-detection enable (cry_alert) and/or sensitivity.

        Read-modify-write: GETs the current 40-byte cry struct, echoes every field
        back, and changes only the passed ones. `sensitivity` lands at the real
        cry_alert_sensitivity slot (SET@32 / GET@36), fixing the old builder that
        wrote it into the audio-filter words (accepted-but-ignored)."""
        import cuboai_messages as cm
        _, data = self.ioctl(*cm.build_get_cry_detect())
        return self._cubo_set(cm.build_set_cry_detect(
            data, enabled=enabled, sensitivity=sensitivity))

    def set_cough_detection(self, enabled=None, in_crib=None, sensitivity=None):
        """Set cough detection enable / mode / sensitivity (read-modify-write).

        coughAlert is a bitmask: bit0=enabled, bit1=in-crib-only ('Always Alert' when
        clear). GETs the current 16-byte struct, echoes it, changes only what you pass.
          enabled:     master on/off
          in_crib:     True = 'Only when baby is in crib', False = 'Always Alert'
          sensitivity: 1=High, 2=Medium, 3=Low"""
        import cuboai_messages as cm
        _, data = self.ioctl(*cm.build_get_cough_setting())
        return self._cubo_set(cm.build_set_cough_setting(
            data, enabled=enabled, in_crib=in_crib, sensitivity=sensitivity))

    def set_auto_capture(self, mode):
        """Set the auto event-snapshot mode (SET_AUTO_CAPTURE / AutoSnapshot).
        mode: 0=off, 1=motion, 2=schedule, 3=both (bitmask)."""
        import cuboai_messages as cm
        return self._cubo_set(cm.build_set_auto_capture(int(mode)))

    def set_lullaby_schedule(self, volume=None, duration=None):
        """Set the lullaby schedule volume / sleep-timer (read-modify-write of the
        GET_LULLABY_SCHEDULE echo via SET_LULLABY_VOL_DURATION). `volume` 0-100;
        `duration` minutes (0/None = repeat forever)."""
        import cuboai_messages as cm
        _, data = self.ioctl(*cm.build_get_lullaby_schedule())
        return self._cubo_set(cm.build_set_lullaby_schedule(
            volume=volume, duration=duration, get_resp_bytes=data))

    # ── lullaby SCHEDULE-TABLE add/delete (SET_LULLABY_SCHEDULE, 0x0990) ───────
    # Distinct from set_lullaby_schedule() above (which is the mis-named vol/timer
    # setter): these write a single alarm-clock schedule ROW. WRITES device state —
    # UNTESTED on the camera; the CLI gates them behind --i-understand-this-is-unsafe.
    def add_lullaby_schedule(self, name, *, song=None, uuid=None, days_mask=0x7f,
                             start_hour=0, start_minute=0, duration_min=None,
                             duration_sec=None, enable=True, ai=False,
                             new_name=None, use_local_time=False):
        """Add (or edit) one lullaby schedule row. The camera keys rows on `name`; to
        edit/rename pass the existing `name` and a `new_name`. See
        cuboai_messages.build_set_lullaby_schedule_entry for the full argument map."""
        import cuboai_messages as cm
        return self._cubo_set(cm.build_set_lullaby_schedule_entry(
            name, song=song, uuid=uuid, days_mask=days_mask, start_hour=start_hour,
            start_minute=start_minute, duration_min=duration_min,
            duration_sec=duration_sec, enable=enable, ai=ai, new_name=new_name,
            use_local_time=use_local_time, action=cm.SCHEDULE_ACT_ADD))

    def delete_lullaby_schedule(self, name):
        """Delete the lullaby schedule row whose `name` matches."""
        import cuboai_messages as cm
        return self._cubo_set(cm.build_set_lullaby_schedule_entry(
            name, action=cm.SCHEDULE_ACT_DELETE))

    def set_sleep_safety_setting(self, **kw):
        """Set safe-sleep alert toggles (read-modify-write of SET_SLEEP_SAFETY_SETTING).
        Keywords: safety_alert, cover_alert, sensitivity, baby_presence_alert. Omitted
        fields are echoed from the current setting."""
        import cuboai_messages as cm
        _, data = self.ioctl(*cm.build_get_sleep_safety_setting())
        return self._cubo_set(cm.build_set_sleep_safety_setting(data, **kw))

    # ── hardware-control SET (read-modify-write of the 96-byte HW struct) ──────
    # The SET_HW_CONTROL struct reorders / drops fields vs the GET response, so
    # we always GET the current struct first and echo every field back unchanged,
    # modifying only the requested ones. night_vision_mode: 0=auto,1=on,2=off.
    def set_hw_control(self, **kw):
        """Modify HW-control fields (night_vision_mode, status_light_on,
        video_v_flip, night_light_on, mic_level, speaker_level, camera_angle,
        stand_type). GETs the current struct, changes only the passed fields,
        and sends SET_HW_CONTROL. Returns (resp_type, resp_bytes)."""
        import cuboai_messages as cm
        _, data = self.ioctl(*cm.build_get_hw_control())
        return self._cubo_set(cm.build_set_hw_control(data, **kw))

    def set_night_vision(self, mode):
        """Night-vision/IR mode via SET_HW_CONTROL. mode: 0=auto, 1=on, 2=off
        (accepts the strings 'auto'/'on'/'off' too)."""
        m = {'auto': 0, 'on': 1, 'off': 2}.get(mode, mode)
        return self.set_hw_control(night_vision_mode=int(m))

    def set_video_flip(self, on):
        """Vertical image flip via SET_HW_CONTROL (0=normal, 1=flipped)."""
        return self.set_hw_control(video_v_flip=1 if on else 0)

    def set_mic_volume(self, value):
        """Microphone level via SET_HW_CONTROL (the standalone SET_MIC_VOLUME
        IOCTL is firmware-dead on this device)."""
        return self.set_hw_control(mic_level=int(value))

    def set_speaker_volume(self, value):
        """Speaker level via SET_HW_CONTROL (the standalone SET_SPEAKER_VOLUME
        IOCTL is firmware-dead on this device)."""
        return self.set_hw_control(speaker_level=int(value))

    def set_status_light(self, on):
        """Turn the camera-body status LED on/off (SET_STATUS_LIGHT_ON_OFF)."""
        import cuboai_messages as cm
        return self._cubo_set(cm.build_set_status_light(bool(on)))

    def set_sleep_safety(self, safety_alert, cover_alert, sensitivity,
                         baby_presence_alert):
        """Set the safe-sleep alert toggles (SET_SLEEP_SAFETY_SETTING)."""
        import cuboai_messages as cm
        return self._cubo_set(cm.build_set_sleep_safety(
            safety_alert, cover_alert, sensitivity, baby_presence_alert))

    def set_detection_zone(self, **kw):
        """Set the normalized motion-detection box (SET_DETECTION_ZONEV2).
        Keywords x_min/x_max/y_min/y_max (floats 0-1) and measurement; any omitted
        coordinate is echoed from the current zone (read-modify-write)."""
        import cuboai_messages as cm
        _, data = self.ioctl(*cm.build_get_detection_zone_v2())
        return self._cubo_set(cm.build_set_detection_zone_v2(data, **kw))

    def set_danger_zone(self, *, enable=None, name=None, points=None,
                        roi_index=0, version=1):
        """Set the danger-zone config (SET_DANGERZONE 2314 / v2 4614) — read-modify-
        write. GETs the current zone, echoes it, and changes only what you pass:
          enable: 0/1 toggle roi.enable (this is the app's switch path)
          name:   ASCII zone name (≤63 chars)
          points: 8 ints [x1,y1,x2,y2,x3,y3,x4,y4] (v1 only)
        Drawing a brand-new polygon also needs the region grid bitmap (not built);
        enable/disable/rename + same-value echo are exact. Returns (resp_type, bytes)."""
        import cuboai_messages as cm
        builder = cm.build_get_danger_zone if version == 1 else cm.build_get_danger_zone2
        _, data = self.ioctl(*builder())
        return self._cubo_set(cm.build_set_danger_zone(
            data, enable=enable, name=name, points=points,
            roi_index=roi_index, version=version))

    def set_environment_alert(self, **kw):
        """Set temperature/humidity comfort-alert thresholds (SET_HW_POLICY).
        Keywords: temp_alert, temp_low, temp_high, humi_alert, humi_low,
        humi_high, dev_pull_alert, dev_pull_sensitivity, dev_pull_count.
        Omitted fields are echoed from the current policy (read-modify-write)."""
        import cuboai_messages as cm
        _, data = self.ioctl(*cm.build_get_hw_policy())
        return self._cubo_set(cm.build_set_hw_policy(data, **kw))

    # ── two-way audio (talk-to-baby) ──────────────────────────────────────────
    def send_audio_file(self, path, channel=1, loop=False, max_secs=None, rate=16000,
                        warmup=2.5, on_status=None, gain=1.0, format=None, options=None):
        """Talk: play an audio file out the camera speaker (pure-Python two-way audio, no native lib).

        Talk is the av-connect handshake REVERSED on a separate channel: we open an av-SERVER, the
        camera logs into us and pulls AAC-LC audio. Flow:
          1. ensure a session, then enter LiveStreamState (talk only runs while a stream is live);
          2. SPEAKERSTART 0x0350 {channel};
          3. on the camera's talk-login (sub=0x00 on `channel`) reply with build_talk_grant, mirroring
             the camera's advertised capability word (self._cam_grant_cap, captured in connect());
          4. stream the file as AAC-LC ADTS av-data frames (build_talk_audio), paced at the AAC frame
             duration (~64 ms), honouring the camera's resend (0x09 SACK) requests;
          5. SPEAKERSTOP 0x0351 and tear the talk channel down.

        This method is the SOLE socket sender for its duration (it stops any streaming reader first),
        mirroring the engine's single-sender rule. Returns the number of audio frames delivered.

        Args:
          channel   talk channel (default 1; live video is ch0).
          loop      repeat the file until max_secs (or forever if max_secs is None).
          max_secs  hard stop after this many seconds (None = until the file/loop ends).
          rate      AAC sample rate to transcode to (camera expects 16 kHz mono).
          warmup    seconds of live stream before SPEAKERSTART (camera must be in LiveStreamState).
          on_status optional callback(dict) for progress (sent, delivered, decoding, resends).
          gain      linear volume multiplier (1.0 = unchanged, <1 quieter, >1 louder) — the reliable
                    talk-volume lever, since the camera's speaker_level is firmware-managed.
        """
        if path != "pipe:0" and hasattr(path, "read") is False and not path.startswith("http"):
            path = os.path.expanduser(path)
            if not os.path.exists(path):
                raise FileNotFoundError(f"Audio file not found: {path}")
        units = _aac_units(path, rate, gain, format=format, options=options)
        if not units:
            raise RuntimeError(f"no AAC-LC frames produced from {path} (empty or unsupported audio?)")

        if self._sock is None or self.session_hdr is None:
            self.disconnect()
            if not self.connect():
                raise RuntimeError("handshake failed (no 0x2041)")
        import select
        # Be the sole socket sender: a streaming reader thread would race seq/relseq and double-drain.
        self._stop_reader()
        s, R, cam = self._sock, self._R, self._cam
        cap = self._cam_grant_cap            # mirror the camera's own capability word (None -> constant)

        # 1. enter LiveStreamState (the camera only accepts talk while a stream is live).
        for io, pl in [(0x00FF, b'\x00\x00'),
                       (0x0300, bytes([0, 0, 0, 0, 4, 0, 1, 0])),
                       (0x01FF, bytes([0, 0, 0, 0, 4, 0, 1, 0]))]:
            s.sendto(build_ioctl_data(R, self._seq, self._relseq, self._frmno, io, pl), cam)
            self._seq += 1; self._relseq += 1; self._frmno += 1
            time.sleep(0.02)

        spk_sent = grant_sent = False
        cam_react = 0
        audio_i = 0                          # index into units[] — WRAPS on loop (the audio content)
        talk_frag = 0                        # MONOTONIC frag-seq / message-index — never wraps with the
                                             # content, or the camera rejects looped frames as already-seen
        talk_relseq = 0
        delivered = -1                       # camera's AV-DATA D high-water (the decoder path)
        resends_sent = 0                     # frags re-sent to satisfy the camera's 0x09 SACKs (link health)
        last_ack = 0.0
        next_audio = None                    # ABSOLUTE send schedule (anchored at the first frame); a
                                             # `now`-relative timer drifts ~+8ms/frame -> the camera
                                             # underruns (measured 72ms vs the 64ms it plays at) -> breakage
        period = 1024.0 / rate               # AAC frame duration (== 64 ms at 16 kHz): feed at EXACTLY this
        finished = False
        sent_buf = {}                        # talk_frag -> au, for resend on the camera's 0x09 SACK
        t0 = time.time()
        self._talk_stop = False              # cooperative stop flag (set by stop_audio())
        try:
            while not self._talk_stop:
                now = time.time()
                if max_secs is not None and now - t0 >= max_secs:
                    break
                # Wake precisely when the next audio frame / ACK is due, so pacing stays tight (the loop
                # also returns early on any camera packet, which we then drain below).
                waits = [0.1, (last_ack + 0.1) - now]
                if next_audio is not None:
                    waits.append(next_audio - now)
                r, _, _ = select.select([s], [], [], max(0.0, min(waits)))
                now = time.time()
                if now - last_ack > 0.1:                         # keep the live stream alive
                    try: self._send_ack()
                    except Exception: pass
                    last_ack = now
                if not spk_sent and now - t0 > warmup:           # SPEAKERSTART {channel}
                    pl = struct.pack('<I', channel) + bytes([0, 0, 0, 0])
                    s.sendto(build_ioctl_data(R, self._seq, self._relseq, self._frmno, 0x0350, pl), cam)
                    self._seq += 1; self._relseq += 1; self._frmno += 1
                    spk_sent = True
                # Pump audio on an ABSOLUTE 64ms grid (advance next_audio by period, never reset to now).
                if grant_sent and cam_react > 0:
                    if next_audio is None:
                        next_audio = now                         # anchor the grid at the first audio frame
                    while now >= next_audio and not self._talk_stop:
                        if audio_i >= len(units):
                            if not loop:
                                finished = True; break
                            audio_i = 0                          # loop the file CONTENT (frag keeps climbing)
                        au = units[audio_i] + _talk_frameinfo(int(now), rate)
                        s.sendto(build_talk_audio(R, channel, self._seq, talk_relseq, talk_frag, talk_frag, au), cam)
                        sent_buf[talk_frag] = au
                        if len(sent_buf) > 128:
                            sent_buf.pop(min(sent_buf), None)    # bound the resend buffer (~8 s)
                        self._seq += 1; talk_relseq += 1; talk_frag += 1; audio_i += 1
                        next_audio += period                     # advance the grid — no cumulative drift
                        if now - next_audio > 8 * period:        # fell far behind -> resync, don't burst
                            next_audio = now + period
                        if on_status and talk_frag % 16 == 0:
                            on_status(dict(sent=talk_frag, delivered=delivered + 1,
                                           decoding=delivered >= 0, resends=resends_sent))
                    if finished:
                        break
                if not r:
                    continue
                while True:
                    try: raw, _ = s.recvfrom(8192)
                    except (BlockingIOError, OSError): break
                    if len(raw) < 30: continue
                    try: dec = inv_transcode(raw)
                    except Exception: continue
                    if len(dec) < 30: continue
                    sub = dec[28]; ch = dec[14] if len(dec) > 14 else 0
                    if sub == 0x0C and len(dec) >= 68 and ch == 0:
                        self._note_cam_data(dec)                 # advance the live (ch0) C/D
                    elif sub == 0x00 and ch == channel and len(dec) >= 300:   # camera's talk-login
                        if not grant_sent:
                            s.sendto(build_talk_grant(R, channel, self._seq, dec, cap), cam)
                            self._seq += 1; grant_sent = True
                    elif ch == channel and grant_sent and sub in (0x09, 0x0A):
                        cam_react += 1
                        if sub == 0x09 and len(dec) >= 52:
                            dD = struct.unpack_from('<H', dec, 38)[0]   # AV-DATA D (decoder high-water)
                            if dD != 0xFFFF and dD < 0x8000 and dD > delivered:
                                delivered = dD
                            # Honour the camera's RESEND-REQUEST: like the host->cam downlink SACK, the
                            # camera's 0x09 lists MISSING frags as (frag - C) at [50:], count at [42:44],
                            # C (contiguous base) at [36:38].
                            cnt = struct.unpack_from('<H', dec, 42)[0]
                            C = struct.unpack_from('<H', dec, 36)[0]
                            if 0 < cnt < 256 and C != 0xFFFF:
                                for k in range(min(cnt, (len(dec) - 50) // 2)):
                                    frag = (C + struct.unpack_from('<H', dec, 50 + 2 * k)[0]) & 0xFFFF
                                    au = sent_buf.get(frag)
                                    if au is not None:
                                        s.sendto(build_talk_audio(R, channel, self._seq, talk_relseq, frag, frag, au), cam)
                                        self._seq += 1; talk_relseq += 1; resends_sent += 1
        finally:
            if spk_sent:                                          # SPEAKERSTOP {channel}
                try:
                    pl = struct.pack('<I', channel) + bytes([0, 0, 0, 0])
                    s.sendto(build_ioctl_data(R, self._seq, self._relseq, self._frmno, 0x0351, pl), cam)
                    self._seq += 1; self._relseq += 1; self._frmno += 1
                except Exception:
                    pass
        return talk_frag                     # total audio frames sent (monotonic; spans loops)

    def stop_audio(self):
        """Ask an in-flight send_audio_file (e.g. a looping talk stream) to stop at the next tick."""
        self._talk_stop = True

    def _stop_reader(self):
        """Stop the background AV reader thread (if streaming) and join it.

        Called before the socket is closed so the reader never select()s/recvfrom()s
        a closed fd. Idempotent: a no-op when no reader is running (the common
        IOCTL-only path), and safe if _read_av_units already cleared the refs.
        """
        ev = self._av_stop_evt
        th = self._av_reader_thread
        if ev is not None:
            ev.set()
        if th is not None and th is not threading.current_thread() and th.is_alive():
            th.join(timeout=1.5)
        self._av_stop_evt = None
        self._av_reader_thread = None

    def disconnect(self):
        """Tear down the session the way native does, then release all state.

        Native's IOTC_Session_Close sends a 24-byte session-close control frame 3x
        as the very last packets, then closes the socket (see build_close).
        Replicating it lets the camera free its session slot promptly instead of
        waiting for its alive-timeout, which makes an immediate reconnect clean.

        Order matters: stop the background reader first (so it never touches a
        closed socket), then send the close, then close the socket, then reset all
        per-session counters/state so a later connect() starts from a clean slate.
        """
        # 1. stop the streaming reader thread before touching the socket.
        self._stop_reader()
        # 2. best-effort session-close (3x, as native). UDP: unacked is fine. Guard
        #    on having a live socket + the session R/peer the close frame needs.
        if self._sock is not None and self._R is not None and self._cam is not None:
            try:
                # Echo the camera's stored session token if we observed it in a probe;
                # else build_close falls back to the seeded template (same wire).
                frame = build_close(self._R, session_fp=self._session_fp)
                for _ in range(3):
                    self._sock.sendto(frame, self._cam)
            except OSError:
                pass
        # 3. close the socket.
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None
        # 4. reset every per-session field (mirror of __init__ / connect()).
        self.session_hdr = None
        self._R = None
        self._cam = None
        self._seq = 0
        self._relseq = 0
        self._frmno = 0
        self._ack_ord = 0
        self._data_ack = 0
        self._cam_msgs = set()
        self._got_first = False
        self._frag_D = None
        self._frag_C = 0xFFFF
        self._session_fp = None


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python3 cuboai_pure.py <camera-ip>")
        sys.exit(2)
    ip = sys.argv[1]
    print(f"Connecting pure-Python TUTK to {ip} ...")
    sess = TUTKDirectSession(ip)
    if sess.connect(timeout=8.0):
        print(f"\n✅ Connected!  session_hdr = {sess.session_hdr.hex()}")
    else:
        print("\n❌ Connection failed.")
        sys.exit(1)

