#!/usr/bin/env python3
"""
cuboai_pure.py — Pure Python TUTK/IOTC LAN transport for CuboAI camera.

Reverse-engineered from libIOTCAPIs_ALL.so v4.2.1.1-H (static + dynamic, 2026-05-29).
No native library required for building the LAN packets.

============================================================================
THE "AV[56:62] TAG" MYSTERY — SOLVED (and the old premise was WRONG)
============================================================================
The handoff assumed AV[56:62] was a *time-limited, validated* authenticator
`F(nonce, timestamp, key)`. That is false. The truth, confirmed by reversing
`iotc_SendMessage` / `TransCodePartial` / `Swap` and by dynamic capture:

  * The 598-byte wire packet is NOT "plaintext XOR a 16-byte key". It is the
    REAL plaintext av-connect (which contains the account + password in the
    clear) run through TUTK's `TransCodePartial` block-scramble.  The repeating
    "XOR frame key" 6e2e8d8c... that earlier work used is simply
    `TransCodePartial(<16 zero bytes>)` — so XOR-ing it back only "decodes"
    the all-zero regions; the structured regions are real transformed data.

  * `TransCodePartial` (function `F` below) processes the buffer in independent
    16-byte blocks:  for each block of 4 LE u32 words w0..w3:
        A=ror32(w0,1)^K0 ; B=ror32(w1,5)^K4 ; C=ror32(w2,9)^K8 ; D=ror32(w3,13)^K12
    then a fixed byte-shuffle, then ror 3/7/11/15 on the four output words.
    The trailing (len % 16) bytes are just XOR'd with the key.  The constant
    key K is the classic TUTK easter-egg string:
        K = b"Charlie is the designer of P2P!!"   (only first 16 bytes used)

  * AV[56:62] sits in the block at offset 48, whose plaintext is
        [4-byte token] ++ "admin@YOURAC"   (first 12 bytes of the account)
    The 4-byte token is literally `rand()` (LE; low byte += channel).  So the
    "tag" is just the transform of a RANDOM client token; the camera CANNOT and
    does NOT validate it.  When the camera inverse-transforms the block it only
    cares that bytes [4:16] still spell the account.  There is no timestamp and
    no key/nonce in the tag.  AV[60]==0x16 etc. are artifacts of transforming
    the constant account bytes.

  * The "few-second window" the handoff measured was the IOTC *handshake*
    freshness (a replayed STALE handshake expires), NOT a tag timestamp.

WHAT THE CAMERA ACTUALLY VALIDATES in the av-connect: the 16-byte HEADER, which
must decode to the camera's nO response:
    decoded_header[2] = nO[178]
    decoded_header[3] = nO[179] | 0x40
    decoded_header[5] = nO[181] ^ 0x40
    decoded_header[6] = nO[182] & 0xF0
    decoded_header[7] = nO[183] & 0x01
The header is `TransCodePartial(static[0:12] ++ R ++ 00 00)` where `R` is a
2-byte per-session value (`GenShortRandomID`, also copied to plaintext[20:22]).
For a chosen nO there is a UNIQUE R that yields the required header; we recover
it with a precomputed 64K lookup table (`build_R_table`).  Verified: this R
reproduces the *accepted* native av-connect byte-for-byte across all 9 logged
sessions.

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
# libIOTCAPIs_ALL.so computes the 6-byte client fingerprint (probe plaintext
# [58:64]; AV/DATA plaintext [22:28]) at frame-build time from the host's MAC:
# it calls getifaddrs() and reads sll_addr of the FIRST non-loopback AF_PACKET
# interface, then applies a fixed BYTE PERMUTATION.
#
# Permutation proven (session 22) against THREE independent (MAC, _AV_MID)
# samples — the real ens18 MAC plus two Frida-injected synthetic MACs — all
# matching exactly:
#     _AV_MID = [mac[1], mac[0], mac[5], mac[4], mac[3], mac[2]]
#             = mac[0:2] byte-swapped  ++  reverse(mac[2:6])
#   aabbccddeeff -> bbaaffeeddcc · aabbccddeeff -> bbaaffeeddcc
#   0123456789ab -> 2301ab896745
# It is a positional permutation (value-independent), so it generalises to any
# host. Computing it dynamically makes the pure transport portable across hosts;
# the old hardcoded 000000000000 was correct ONLY on this VM (see AV_HANDOFF S21).
_AVMID_PERM     = (1, 0, 5, 4, 3, 2)
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

    Byte-for-byte the source the native lib uses (getifaddrs + the first non-lo
    link-layer iface's address; confirmed by Frida getMac trace, session 22).
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
    (Linux) → uuid.getnode() (Windows + universal). If every method fails (e.g. a
    network-isolated container with no NIC at all), a fresh random 6-byte fingerprint
    is generated instead — the camera doesn't validate this value's structure or
    origin (confirmed MID-independent, see [[cuboai-avmid-is-host-mac]]), so any
    6 bytes work equally well, and a random value avoids every such host presenting
    the same fixed, identifiable fingerprint. Import never raises; a stderr note is
    emitted on that fallback. On Linux/macOS getifaddrs wins, so behaviour is unchanged.
    """
    mac = (_local_mac_via_getifaddrs()      # Linux (AF_PACKET) / macOS (AF_LINK)
           or _local_mac_via_sysfs()        # Linux /sys/class/net fallback
           or _local_mac_via_uuid())        # cross-platform incl. Windows
    if mac is None:
        fallback = os.urandom(6)
        sys.stderr.write("[cuboai_pure] WARN: no NIC MAC found; "
                         "using random fallback _AV_MID %s\n" % fallback.hex())
        return fallback
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


def transcode(plain: bytes, swap_tail: bool = True) -> bytes:
    """Full TUTK TransCodePartial: 16-byte block transform + tail transform.

    Maps REAL plaintext to the exact wire bytes, byte-identical to the native
    `TransCodePartial` (0x275fa0) for ALL lengths (fuzzed against the real lib via
    ctypes: block 40k+ buffers + tail all lengths, zero mismatches). The account,
    password, header and every structured field live in full 16-byte blocks.

    TAIL: the trailing `len & 0xF` bytes are `K16[i] ^ plain[i]`, AND — for the frames
    the camera sends/receives via `iotc_SendMessage`→`TransCodePartial` (the post-connect
    DATA channel: IOCTLs, AV, data-acks, resends) — for tail lengths 2/4/8 the lib's
    `Swap` byte-permutation (0x2714f0) is applied on top: `wire_tail =
    Swap(plain_tail XOR K16)` (see `_tail_swap`). REQUIRED: a lullaby-schedule SET
    (216-byte frame, tail 8) whose duration lived in the tail read back as a ~10-yr
    default until the Swap was applied.

    `swap_tail=False` reproduces the NO-Swap wire of the pre-session SEARCH/broadcast
    frames (probe / search-ack / lan-query), which native sends via a different path that
    does NOT run the Swap — verified against native captures (e.g. the 88-byte probe
    `[80:88]`). Direct-send frames (close, keepalive; lib `_GetSendPath`, no
    `TransCodePartial`) never swap either and are built with `xor_frame`, not here.
    Tails of len 6 (av-connect) / 12 (IOCTL GET) are Swap-identity, so the flag is moot.

    (An earlier docstring claimed the tail was plain XOR *everywhere* and that adding Swap
    "breaks connect" — WRONG for the data channel; the nuance is: swap on the data channel,
    no swap on search/direct frames.)
    """
    n = len(plain)
    full = n - (n & 0xF)
    out = bytearray(n)
    for off in range(0, full, 16):
        out[off:off + 16] = _block_transform(plain[off:off + 16])
    tl = n - full
    if tl:
        xored = bytes(_K16[i] ^ plain[full + i] for i in range(tl))
        out[full:] = _tail_swap(xored, tl) if swap_tail else xored
    return bytes(out)


# TUTK TransCodePartial / ReverseTransCodePartial apply a `Swap` byte-permutation
# (lib 0x2714f0) to the partial-block TAIL, but ONLY for tail lengths 2/4/8 (identity
# for every other length). Reversed from the .so and confirmed byte-for-byte against
# the real lib via ctypes for ALL lengths: encode `wire_tail = Swap(plain_tail XOR K)`;
# decode `plain_tail = Swap(wire_tail) XOR K` (Swap is an involution). This is why a
# 148-byte schedule SET (216-byte frame, tail 8) whose duration lived in the tail read
# back mangled when the tail was a plain XOR; tails 6/12 (av-connect, IOCTL GET) are
# identity, so they were always correct.
_TAIL_SWAP = {2: (1, 0), 4: (2, 3, 0, 1), 8: (7, 4, 3, 2, 1, 6, 5, 0)}


def _tail_swap(buf, length):
    """Apply the TransCodePartial tail `Swap` permutation (identity unless length is
    2/4/8; an involution). Used by BOTH `transcode` and `inv_transcode` so the wire
    encode/decode match native TransCodePartial / ReverseTransCodePartial exactly."""
    perm = _TAIL_SWAP.get(length)
    if perm is None:
        return buf
    return bytes(buf[j] for j in perm)


def _rol32(v, r):
    r &= 31
    return ((v << r) | (v >> (32 - r))) & 0xFFFFFFFF


def _inv_block_transform(blk: bytes) -> bytes:
    """Inverse of `_block_transform` — recover plaintext from a 16-byte wire block.

    This is how the CAMERA reads the packet (inverse-TransCodePartial). It reveals
    the true plaintext (UID, R, fingerprint) that `xor_frame` decodes to garbage in
    the structured regions. Validated: _inv_block_transform(_block_transform(x))==x.
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
    """Full inverse of `transcode` — byte-identical to the native library's
    `ReverseTransCodePartial` (lib 0x2720d0), verified against it for every fixture
    frame (2001/2001 of the tail-2/4/8 datagrams; the old plain-XOR matched 0/2001).
    The tail (len & 0xF == 2/4/8) is un-Swapped then XOR'd: `plain_tail = Swap(wire_tail)
    XOR K16` (Swap is an involution, so the same `_tail_swap` inverts the encode)."""
    n = len(wire)
    full = n - (n & 0xF)
    out = bytearray(n)
    for off in range(0, full, 16):
        out[off:off + 16] = _inv_block_transform(wire[off:off + 16])
    tl = n - full
    if tl:
        swapped = _tail_swap(wire[full:], tl)      # involution: undo the encode-side Swap
        out[full:] = bytes(_K16[i] ^ swapped[i] for i in range(tl))
    return bytes(out)


# ── AAC-ADTS helpers ──────────────────────────────────────────────────────────
# Each camera audio AV unit is ONE AAC-ADTS frame followed by a 24-byte TUTK
# FRAMEINFO trailer (codec_id 0x0088 — see the handoff's "codec_id reading 0x0088"
# note). So on the wire avlen == adts_frame_len + 24. Session-12 emitted the whole
# `dec[64:64+avlen]` as the "audio frame", which appended that 24-byte trailer to
# every ADTS frame and corrupted the AAC (no decoder was available then to catch
# it). The fix: truncate each audio unit to its self-declared ADTS frame length.

def _adts_frame_len(b: bytes):
    """Length (bytes) of the ADTS frame at the start of `b`, or None if not ADTS."""
    if len(b) < 7 or b[0] != 0xFF or (b[1] & 0xF6) != 0xF0:
        return None
    return ((b[3] & 0x03) << 11) | (b[4] << 3) | ((b[5] >> 5) & 0x07)


# ── TUTK FRAMEINFO trailer (24 bytes appended to every AV unit) ────────────────
# The camera appends a 24-byte TUTK FRAMEINFO_t to each AV access unit (the same
# trailer the AUDIO path already drops by truncating to the self-declared ADTS
# length). The VIDEO path historically concatenated it verbatim, so emitted HEVC
# AUs ended with [...NALs...][24-byte FRAMEINFO]; software decoders ignore trailing
# bytes but HARDWARE decoders (Apple VideoToolbox via Safari/Chrome) reject the
# malformed over-long final NAL -> black picture (kVTVideoDecoderBadDataErr -12909).
# Native (the WYZE lib) strips it. _strip_frameinfo (CUBOAI_STRIP_FRAMEINFO) drops it.
#
# LAYOUT decoded EMPIRICALLY (overnight #2, /tmp/ov2_frameinfo_dump.py — 60 consecutive
# AUs; little-endian), confirmed against the keyframe-NAL correlation and the known
# 2560x1440 resolution:
#   [0:2]   u16  codec_id      0x0050 = HEVC video (0x0088 = AAC audio) — the sanity gate
#   [2]     u8   keyframe flag 0x01 on IDR/IRAP AUs, 0x00 on P — the AUTHORITATIVE IDR marker
#   [3]     u8   reserved (0)
#   [4:8]   u32  ~2 + a toggling top bit (cam/channel index 2 + a per-frame flag) — not used
#   [8:10]  u16  videoWidth   2560
#   [10:12] u16  videoHeight  1440
#   [12:16] u32  timestamp_sec   unix epoch seconds (e.g. 1780920911), +1 ~every second
#   [16:20] u32  timestamp_ms    milliseconds-within-the-second (0..999, ~67ms/frame, resets each sec)
#   [20:24] u32  frame_no        monotonic frame counter (+1 per frame)
# -> frame timestamp (ms) = timestamp_sec*1000 + timestamp_ms  (surfaced for future PTS/A-V sync).
_FRAMEINFO_LEN = 24
_FRAMEINFO_CODEC_HEVC = 0x0050        # codec_id at offset 0 for an HEVC video FRAMEINFO
# ── codec_id -> codec name (ThroughTek MEDIA_CODEC enum) — the SINGLE source of truth ──
# A future H264/other CuboAI camera works with NO code change: the codec name drives the TS
# stream_type (cuboai_mpegts) and go2rtc media. HEVC=0x50 and AAC=0x88 are CONFIRMED on this
# camera (HEVC video AUs / 0x0088 audio trailers); the rest are best-known ThroughTek values
# (the native libs here are stripped — verify H264=0x4E live if/when an H264 unit appears).
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
# AUDIO FRAMEINFO repurposes the video width/height slot as sample_rate/channels (S91): [8:10]=
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
        'is_keyframe': kf,                      # alias (Part A API name)
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
    """Decode an audio FRAMEINFO trailer (S91 layout). ts_sec is the SAME unix-epoch second-clock as
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
    """Generator over (kind, data, fi) tuples that suppresses the poisoned GOP tail.

    Mirrors cuboai_stream_video.mux_timed_stream's clean_gop logic (the live MPEG-TS path)
    for the .mp4 RECORDER: drop any incomplete VIDEO AU (fi is None) and every subsequent
    VIDEO AU until the next clean IDR keyframe, so a recorded clip never carries a broken
    GOP (the decode band) under loss. Audio AUs pass through untouched (audio has no GOP).

    Starts DESYNCED (like the streamer: synced = not clean_gop) so the clip begins on the
    first complete IDR. Pure passthrough generator — used ONLY when CUBOAI_RECORD_CLEAN_GOP
    is set; the default record path does not wrap with this, so its output is unchanged.
    """
    synced = False
    for kind, data, fi in items:
        if kind == 'video':
            if fi is None:                       # incomplete AU -> poison the GOP tail
                synced = False
                continue
            if not synced:
                if fi.get('is_keyframe'):        # clean IDR -> resume a decodable GOP
                    synced = True
                else:
                    continue                     # still in the poisoned tail
        yield (kind, data, fi)


# ── LAN-search probe / ACK (IOTC type 0x601) — *** THE SESSION-9 FIX *** ──────
# These are NOT "xor_frame'd with a random nonce". They are REAL plaintext run
# through TransCodePartial (`transcode`), exactly like the av-connect. The whole
# 8-session blocker lived here:
#
#   * The per-session field is R = a short random id in [1, 0xFFFE] (the lib's
#     `GenShortRandomID` = (tutk_platform_rand()+time()) mod 0xFFFF), placed at
#     plaintext[56:58] (little-endian). The UID is in the clear at [16:36] and the
#     6-byte client fingerprint `000000000000` sits at [58:64].
#   * The camera reads (R | fingerprint) as the *client random id*, creates a
#     pre-session keyed by it, echoes R back inside nO, and — when it later
#     receives the ACK — drives the session to "connected" (gSessionInfo+0x19==2)
#     via IOTC_Handler_MSG_LAN_SEARCH_R_3 -> _SetSendPath. THAT is the status==2
#     the av-login gate (`_avProcessLoginPacket`/`IOTC_Check_Session_Status`)
#     requires. (No KNOCK packet is involved — `AddSendKnockRWhenDeviceNotResponse`
#     schedules one that never fires because the device replies.)
#   * The OLD code built the probe as xor_frame(<constant template>) and scribbled
#     a random 6-byte "nonce" into WIRE [48:54] — but that region is the transcode
#     ENCODING of R + the fingerprint. Randomising it handed the camera a random R
#     AND a CORRUPTED fingerprint, so the client-id the camera stored for the
#     pre-session (garbage R | garbage fingerprint) never matched the av-connect's
#     id (which carries the correct `_AV_MID` fingerprint). The pre-session was
#     never coherently keyed, status never reached 2, and the av was silently dropped.
#   * The av-connect header R (plaintext[12:14] == [20:22]) MUST equal the probe R,
#     and the av fingerprint (`_AV_MID`) MUST equal the probe fingerprint.
#     header_R_for_nO(nO) also recovers R because the camera echoes our R in nO.
#
# Verified live (session 9, 2026-05-30): pure-Python now gets 0x2041 + a real
# session_hdr, byte-for-byte template-identical to native's accepted probe/ACK.

_LS_HEAD16          = bytes.fromhex("04021a02480000000106210000000000")  # [0:16], type 0x601 @ [8]
_LS_MID8            = bytes.fromhex("0000000001010204")                  # [48:56]
_CLIENT_FINGERPRINT = _AV_MID_DYNAMIC                                    # [58:64]; == _AV_MID (perm of local MAC)
_LS_TRAILER8        = bytes.fromhex("63041313040c0c63")                  # [80:88]
_DEFAULT_UID        = b"YOUR_20CHAR_UID_HERE"

_KEEPALIVE_DEC = bytes([
    0x01, 0x42, 0x10, 0x60, 0x00, 0x01, 0x00, 0x00,
    0x00, 0x40, 0xa0, 0x21, 0x80, 0x0a, 0x00, 0x00,
    0x5f, 0xe2, 0x43, 0xc9, 0xfe, 0xce, 0x1d, 0xc4,
])
# NOTE (S47/S49): `_KEEPALIVE_DEC[16:20]` (5fe243c9) is NOT a local IP and NOT a
# universal constant — it is an OPAQUE session-identity token.  Disasm
# (IOTC_Connect_UDP_Inner@0x28903e): native sets it to
#   (gsLocalNetworkInfo[0xac] << 16) | (snClientRandomID + 1)
# = a host-derived fingerprint (high 16) + a per-connect counter (low 16); native's
# own value thus differs per host AND per connect (5fe243c9 here, 82bd5556 on .165).
# The camera never validates it — it STORES the client's token and ECHOES it in every
# keepalive probe.  Pure exploits that: it seeds this ONE template token at connect (the
# init keepalive, connect() step 6), so the camera echoes `5fe243c9` for pure's session
# on ANY host.  ⇒ pure is self-consistent/portable here regardless of host.  The
# keepalive REPLY echoes [16:24] from the probe; build_close echoes the captured token
# (TUTKDirectSession._session_fp) when a probe has been seen, else falls back to this
# template (== the seeded token ⇒ identical wire).  The probe DETECTOR keys on the
# host-independent window below.
# Host-independent keepalive-PROBE signature = decoded [8:16] (probe-request marker
# 1040c021 + family tag 800a0000); uniquely tags cam→cli probes vs close/reply
# (0040a021...), byte-identical across the .124 and .165 hosts.
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
    """88-byte LAN-search probe wire packet (transcode of the true plaintext).
    swap_tail=False: pre-session search frames are NOT Swap-permuted on the wire
    (verified vs native `[80:88]`), unlike the post-connect data channel."""
    return transcode(_build_ls_plaintext(uid, R, ack=False), swap_tail=False)


def build_ack(uid: bytes, R: int) -> bytes:
    """88-byte LAN-search ACK wire packet (probe with plaintext[64]=0x02)."""
    return transcode(_build_ls_plaintext(uid, R, ack=True), swap_tail=False)  # search: no Swap


# ── S68: IOTC LAN device-identity query (message type 0x0402) — TESTED-NEGATIVE ─
# DIAGNOSTIC ONLY, default OFF: this is NOT part of the working protocol flow. Kept
# (gated) to document a refuted arming hypothesis so it isn't re-investigated.
# Native's IOTC_Connect_UDP sends this 52-byte frame ~0.1 s into establishment
# (host 0x0402 -> camera 0x0404 response), carrying UID + R + client MID; pure never
# does. S68 hypothesis: on the LAN-direct path (no master) this registers client
# identity and is the missing reliable-peer/arming trigger (cam->host 0x09 ~4 vs ~315).
# REFUTED BOTH WAYS (S68): (a) a native control armed (oracle 360, RETX 471) sending
# ZERO 0x0402 — only the *paced* sub-path emits it, ByUID arms without it; (b) pure
# WITH this injected got the camera's 0x0404 reply yet the oracle stayed at 4 (no arm).
# So 0x0402 is an incidental latency-triggered device query, NOT the arming gate; the
# reliable-peer registration is sub-wire (S69: set by the lib's RUDP recv machinery,
# AVctx+0x1f8c, which pure never runs). Builder kept for wire-fidelity experiments.
# Decoded 52B layout: HEAD16 | UID[16:36] | R[36:38] | MID[38:44] | 00000000 | TAIL
# Byte-validated: transcode(build_lan_query(uid,0x2ac2)) == native s65_udp wire.
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
    return transcode(bytes(t), swap_tail=False)  # search/broadcast frame: no Swap on the tail


def build_close(R: int, session_fp: bytes = None) -> bytes:
    """24-byte IOTC session-close control frame (== native's IOTC_Session_Close send).

    Reversed from three native `--no-status` teardown captures: native ends a session
    by emitting this single frame 3x as the very last packets, then closing the socket.
    The frame shares its first 20 bytes with the keepalive (`_KEEPALIVE_DEC[:20]`); the
    trailing 4 bytes `[20:24]` are session-specific, derived from R — validated
    byte-for-byte against EIGHT native closes (S46: R=0x65dc/0x74e4/0x9c57/0xa9b7/0xcc69
    + S15's 0x654e/0x935b/0xe6cf):
        [20] = (R >> 8)   ^ 0x28
        [21] = 0xce       [22] = 0x1d                  (constant discriminator)
        [23] = (R & 0xff) ^ 0x89
    Sending it lets the camera free its session slot promptly (its own alive-timeout
    is the fallback), so a subsequent reconnect is clean. xor_frame is self-inverse,
    so this both builds and would decode the wire frame.

    *** [16:20] — the opaque session-identity token (S49 corrects S46/S47) ***
    The disasm settles what `[16:20]` actually is, and it is NOT a local IP.
    `_IOTC_Send_P2PClose`@0x27b690 copies plaintext `[16:20]` from `gSessionInfo[sid]+0x20`,
    which `IOTC_Connect_UDP_Inner`@0x28903e sets to:
        session+0x20 = (gsLocalNetworkInfo[0xac] << 16) | (snClientRandomID + 1)
                        └─ host netinfo, high 16 ─┘       └─ per-connect counter, low 16 ┘
    i.e. an OPAQUE token = a host-derived fingerprint in the high half + a counter that
    INCREMENTS on every native connect in the low half — NOT an IPv4 address (the
    template value `5fe243c9` decodes to 95.226.67.201, not the LAN IP) and NOT
    recoverable from getsockname().  The camera never validates it; it merely STORES the
    client's token and ECHOES it back in every keepalive probe (S47 echo proof).

    Pure does NOT derive this from the host.  It seeds ONE opaque token — the template
    `_KEEPALIVE_DEC[16:20]` (`5fe243c9`) — into the init keepalive it sends at connect
    (connect() step 6), so the camera stores `5fe243c9` for pure's session on ANY host
    and echoes it back in probes; `build_keepalive_reply` echoes it again.  So pure is
    already self-consistent and host-PORTABLE here — the value being the dev host's
    native fingerprint is cosmetic; pure uses it as an arbitrary session id everywhere.

    `session_fp` (S49): when the caller has observed the camera's echoed token for THIS
    session (the probe's `[16:20]`, captured into `TUTKDirectSession._session_fp`), pass
    it here and `[16:20]` mirrors exactly what the camera holds for the session.  When
    None (no probe seen yet — the common case for short sessions) `[16:20]` falls back to
    the template, which equals the token pure itself seeded at connect ⇒ identical wire.
    So the echo is strictly-more-faithful with ZERO regression.  Teardown impact is LOW
    either way: the camera's alive-timeout reclaims the slot even if the token is stale.
    """
    if session_fp is not None and len(session_fp) != 4:
        raise ValueError("session_fp must be the 4-byte [16:20] session token")
    plain = bytearray(_KEEPALIVE_DEC[:20])
    if session_fp is not None:
        plain[16:20] = session_fp                  # echo the camera's stored token (S49)
    plain += bytes([(R >> 8) & 0xFF ^ 0x28, 0xCE, 0x1D, (R & 0xFF) ^ 0x89])
    return xor_frame(bytes(plain))


def _unwrap_index(idx_u16, done_upto):
    """Lift a u16 wire AV message-index ([56:58], 0..65535) into `done_upto`'s unbounded monotonic
    space so the reassembly accept-window survives the 65536 wrap (audit H1).

    For a forward index (idx ahead of done_upto by < 32768) the result is exactly the raw value
    while no wrap has occurred, so default streaming is byte-identical until the first wrap; across
    the wrap (idx 65535 -> 0 while done_upto ~65535) it continues monotonically (65535 -> 65536). A
    late/duplicate index (behind done_upto) maps far forward (> +32768) so the gate still rejects it.
    """
    return done_upto + ((idx_u16 - done_upto) & 0xFFFF)


def _unwrap_index_back(idx_u16, cur):
    """Lift a u16 wire counter BACKWARD into `cur`'s unbounded monotonic space: the nearest value
    at-or-below `cur` congruent to idx_u16 (mod 65536). The mirror of `_unwrap_index`, for wire
    values that always refer to something ALREADY SENT (a resend request naming one of our own
    recent frames). While `cur` < 65536 this is the identity, so the wire is unchanged below the
    wrap; past it the lookup keeps resolving instead of silently missing.
    """
    return cur - ((cur - idx_u16) & 0xFFFF)


def is_keepalive_probe(raw: bytes) -> bool:
    """True iff `raw` is the camera's 24-byte IOTC keepalive (alive-check) probe.

    Mid-session the camera periodically (~1 probe / ~2 s) sends a 24-byte IOTC
    control frame to check the session is alive; native answers each with a
    keepalive reply (see `build_keepalive_reply`).

    *** S47 ***: matched on the decoded `[8:16]` (`_KEEPALIVE_PROBE_SIG` =
    `1040c021800a0000`) — the probe-request marker plus the family tag.  This is
    HOST-INDEPENDENT (byte-identical on the .124 and .165 capture hosts) and uniquely
    tags the camera's probe (close/reply carry `0040a021...`).  The previous match on
    `_KEEPALIVE_DEC[12:20]` included byte `[16:20]`, which is the host fingerprint
    (5fe243c9 on this host, 82bd5556 on another) — that made probe detection silently
    fail on any other host.  The 24-byte length already excludes AV/ACK frames.
    """
    if len(raw) != 24:
        return False
    try:
        return xor_frame(bytes(raw))[8:16] == _KEEPALIVE_PROBE_SIG
    except Exception:
        return False


def build_keepalive_reply(probe_raw: bytes) -> bytes:
    """24-byte keepalive REPLY to the camera's keepalive probe (== native's reply).

    Reversed from `native_transition.txt` (session-31): the camera sends a probe
    `2241008000000000…5fe243c9<tail>`; native answers within a few packets with
    `0242106000010000…5fe243c9<tail>` — i.e. `_KEEPALIVE_DEC[:20]` with byte[0]=0x02
    and the probe's session tail `[20:24]` echoed back. `build_keepalive_reply` from
    a captured probe is byte-for-byte identical to native's reply (verified).

    NOTE (session-31): answering these probes does NOT unlock AV retransmit — pure
    replied 9/9 byte-identically to native across 3 live loss-injection runs and the
    camera still resent nothing (retx 0-2 noise, == baseline). It is wired in purely
    as native-fidelity (pure previously dropped every liveness probe at the
    `len(raw) < 30` guard) and to keep long sessions healthy; it is NOT a retransmit
    fix. The resend refusal is the camera-side per-session gate (see handoff S16-S30).

    *** S47 (disasm + multi-host capture) ***: `[16:20]` is the client's host
    network-identity fingerprint, not a constant (native builds the tail from
    `gSessionInfo[sid]+0x20`, populated by `IOTC_Connect_UDP_Inner`/
    `UpdateLocalNetworkInfo` from the local interface IP/MAC).  The camera stores it
    per-client and echoes it in the probe, so we now echo the full session tail
    `[16:24]` from the probe rather than templating `[16:20]` from `_KEEPALIVE_DEC`.
    Proof (s43_badpw): CAM-PROBE, CLI-REPLY and CLOSE all share `[16:20]=5fe243c9` and
    the reply echoes the probe's `[20:24]`; a probe to a different host
    (native_probe.pcap, .165) carries `[16:20]=82bd5556`.  This is byte-IDENTICAL on
    the capture host (5fe243c9 either way — S31's 9/9 is preserved) and correct on any
    other host.  `[0:16]` is the reply HEADER: host-independent (verified .124≡.165)
    and it differs from the probe at `[0]` and `[8:12]`, so it stays templated.
    """
    pd = xor_frame(bytes(probe_raw))
    plain = bytearray(_KEEPALIVE_DEC[:16])  # [0:16] reply HEADER (host-independent; .124 ≡ .165)
    plain[0] = 0x02
    plain += pd[16:24]                       # echo [16:24] = host fingerprint + session tail (S47)
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

    DISASM-PROVEN (S46): the camera's nO builder `_IOTC_Send_Search_R` @ 0x277560
    echoes the client's probe {R, fingerprint} VERBATIM into the response payload —
    R at plaintext[188:190] (LE u16), client fingerprint at [190:196] (it copies the
    8-byte {R,fingerprint} the camera stored from our probe into struct[0xbc:0xc4]).
    So the robust recovery is simply `inv_transcode(nO_wire)[188:190]`.

    Verified == probe R on ~15 native+pure captures spanning many R values and three
    distinct client MIDs (000000000000 / bbaaffeeddcc / 24bda0..) — i.e. it is
    MID-INDEPENDENT, unlike the legacy `header_R_for_nO` xor-table heuristic which was
    tuned to one fingerprint and missed on a changed MAC (the S23 portability bug).
    Returns R as int, or None if `nO_raw` is too short to contain the echo.
    """
    if len(nO_raw) < 190:
        return None
    return struct.unpack("<H", inv_transcode(bytes(nO_raw))[188:190])[0]


def header_R_for_nO(nO_dec: bytes):
    """LEGACY (S46: superseded by `nO_recover_R`).  Return the 2-byte session value
    R = [12:14] from a fragile xor-table correlation on the *xor_frame*-decoded nO at
    [178:184].  This works only because those wire bytes sit in the SAME 16-byte
    transcode block [176:192] as the cleanly-echoed R (struct[0xbc:0xbe], block-offset
    12 — the same block-offset R occupies in the av-connect header block), so the block
    transform diffuses R into [178:184] consistently — but ONLY for a fixed
    gDeviceName+fingerprint, hence its MAC-tuning.  Prefer `nO_recover_R(nO_raw)`."""
    global _R_TABLE
    if _R_TABLE is None:
        _R_TABLE = _build_R_table()
    key = (nO_dec[178], (nO_dec[179] | 0x40) & 0xFF, (nO_dec[181] ^ 0x40) & 0xFF,
           nO_dec[182] & 0xF0, nO_dec[183] & 1)
    return _R_TABLE.get(key)


def build_av_connect(nonce: bytes, nO_dec: bytes, channel: int,
                     account: bytes = b"admin@YOUR_ACCOUNT",
                     password: bytes = b"YOUR_PASSWORD",
                     token: bytes = None, R: int = None,
                     iotc_channel: int = 0) -> bytes:
    """Build the 598-byte AV CONNECT wire packet (real plaintext -> transcode).

    `R` is the per-session short id (== the probe R == GenShortRandomID); when None
    it is recovered from `nO_dec` (the camera echoes our probe R back in nO, so the
    two agree).  `nonce` is unused (kept for API/oracle compatibility).  `token`
    defaults to a random 4-byte value (this is what the native lib does — it is NOT
    validated).

    WIRE MODEL (2026-07-16, native-capture confirmed): `[14]` = the IOTC channel
    (0 = the live channel, N = a camera-assigned DVR PLAYBACK channel from
    SMsgAVIoctrlPlayRecordResp.result); `[6:8]` (= the `channel` arg here) = the
    per-channel AV sub-index / send-seq.  For the LIVE connect the app opens sub 0..3
    on IOTC channel 0, so `iotc_channel=0` (the default) → `[14]=0` → BYTE-IDENTICAL to
    the pre-2026-07-16 output.  For playback pass `iotc_channel=N` to route the
    channel-open to the assigned channel (this is the mid-session framing pure was
    missing; native puts N in [14], NOT in [6:8]).
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
    p[14] = iotc_channel & 0xFF        # IOTC channel: 0 (default) = live/byte-identical; N = playback
    p[12:14] = Rb
    p[16:20] = bytes.fromhex("0c000000")
    p[20:22] = Rb
    p[22:28] = _AV_MID
    p[30] = 0x0B
    p[44] = 0x22
    p[45] = 0x02
    p[46] = 0x01 if (channel % 2 == 0) else 0x00
    p[29] = 0x00 if (channel % 2 == 0) else 0x20
    # S37/S38/S44: native builds the av-login token ONCE per session, not per
    # channel.  Disasm (avConnect_inner @ 0xf9640): `mov 0x22e0(%rbx),%eax;
    # test %eax,%eax; jne ...` generates `tutk_platform_rand()` (== glibc rand,
    # 31-bit) ONLY when ctx+0x22e0 == 0, then REUSES the stored value; live
    # capture confirms exactly ONE token-write per session feeding all 4
    # channels.  On the wire the transport stamps a per-FRAME parity: even
    # frames carry the base T, odd frames T+1, where +1 is a FULL 32-bit
    # little-endian increment (it carries past byte0 when byte0 == 0xFF), and
    # [49:52] is constant across channels except in that carry case.  (S43's
    # "4 INDEPENDENT random tokens per channel" was a misread of an early-pure
    # capture — pre-S38 pure used os.urandom() per channel; the live native lib
    # and the disasm both show ONE shared base.  Token is a proven non-gate.)
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
# the av-connect immediately (absent from clean/instant successes such as
# native_ts2; present in every "struggling" session, e.g. relay_log7).
# Structure recovered by diffing two independent native sessions: the decoded
# 52 bytes are CONSTANT except offsets [44] and [47], which are lifted from the
# av-connect's session-header region (xor_frame(av_wire)[28] and [31], a pure
# function of R). Verified to reproduce native's 0x2043 wire byte-for-byte.
#
# IMPORTANT: replaying this does NOT unlock a pure-Python connect.  In live
# tests the camera silently ignores a byte-identical python 0x2043 (it never
# sends the 0x20431040 reply), exactly as it ignores python's av-connects —
# while accepting native's identical bytes.  Kept here as a validated artifact;
# the remaining blocker is non-packet (see the connect() docstring).
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
    p[40] = avd[24] | 0x13      # session-specific (verified across relay_log7 + live nat_wire);
                                # was previously left at the stale template value (a real bug)
    p[44] = avd[28]
    p[45] = avd[29] | 0x40      # session-specific (derived nat_used avd29=00->45=40,
                                # natctl avd29=01->45=41); was left at template 0x41 -> a real bug
                                # that made the camera silently ignore python's 0x2043.
    p[47] = avd[31]
    return xor_frame(bytes(p))


# ── AV / IOCTL data frames (post-connect) ───────────────────────────────────
# Once the session is granted, avSendIOCtrl rides the IOTC LAN *data channel*
# (IOTC packet type 0x0407 client->cam, 0x0408 cam->client). Each frame is the
# real plaintext run through `transcode` (NOT xor_frame), exactly like connect.
# Full layout recovered + validated byte-for-byte against the native lib (a
# GET_HW_CONTROL request reproduces native's 76-byte wire packet exactly):
#
#   [0:2]   04 02                       constant
#   [2:4]   1a 0a                       SEND flags (RECV uses 1d 0a)
#   [4:6]   <u16 LE>  = len(frame) - 16
#   [6:8]   <u16 LE>  outbound data-channel sequence (continues from connect)
#   [8:12]  07 04 21 00                 IOTC data type 0x0407 (+0x21)
#   [12:14] <R LE>   [14:16] 00 00      R doubles as the "session id" post-connect
#   [16]    0c                          (then 00 00 00)
#   [20:22] <R LE>   [22:28] 000000000000   R + client fingerprint (_AV_MID)
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

    *** SESSION-12: protocol re-derived byte-for-byte from a native capture where the
    camera served all 11 IOCTLs on one connection (Frida hook of sendto/recvfrom +
    __getIOCtrlFrmNo, /tmp/frames_full.txt). ***

      [6:8]   seq    — packet counter (bumps on every send incl. retransmit).
      [32:34] relseq — the SENDER's own reliable-frame sequence: a single counter that
                       increments by 1 for EVERY reliable frame this side sends (DATA
                       and ACK alike), reused unchanged on a retransmit. Native's DATA
                       frames carry 0,19,21,23,… (the gaps are the ACKs sent between
                       requests). It is NOT a cumulative-ACK (the old code's mistake).
      [34:36] 0      — uninitialised/don't-care (S48). `_sendIOorIOInnerFrame`@0xea610
                       does NOT write content[6:8] before the payload memcpy — it is
                       leftover stack, same as the 0x09 ACK's [34:36] (S45). The camera
                       ignores it. Real native sendto-wire (native_transition.txt, 14/14
                       client IOCTLs across all io_types) shows 0x0000; the old "0xFFFF
                       marker" came from one Frida run (frames_full.txt) where the slot
                       happened to hold 0xFFFF. Zeroed here to match the dominant native
                       wire and to be consistent with build_data_ack / build_resend_req.
      [40:44] 0      — DATA frames carry no data-ack (that rides on ACK frames).
      [46:48] frmno  — IOCtrl FrmNo (== __getIOCtrlFrmNo return: 0,1,2,…), per request.
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
    # [34:36] left 0 (uninitialised/don't-care; native real-wire=0; camera ignores — S48)
    p[45] = 0x70
    struct.pack_into("<H", p, 46, frmno & 0xFFFF)
    p[48] = 0x01
    struct.pack_into("<H", p, 52, 4 + pl_len)
    struct.pack_into("<H", p, 56, frmno & 0xFFFF)          # FrmNo mirror == data message-index
    struct.pack_into("<I", p, 64, io_type)
    p[68:68 + pl_len] = payload
    return transcode(bytes(p))


def build_data_ack(R, seq, relseq, ackord, C, D, data_ack=0, sack=None, ts=None, ts32=None, win=0):
    """IOTC data-channel ACK (sub-type 0x09). *** SESSION-14/24: TWO ack channels. ***

    The camera multiplexes TWO logical reliable streams onto the data channel, each
    acked by a DIFFERENT field of this one ACK frame (proven byte-for-byte from a
    native status->video capture, session 24, /tmp/native_transition.txt):

      * The **AV fragment stream** (video/audio) is acked by the **C/D pair at
        [36:40]** (session-14): D = highest camera AV fragment-seq ([46:48]) seen,
        C = the previous ACK's D. This advances the camera's AV send-window and keeps
        the video flowing; C==D==0xFFFF is the idle sentinel before any AV arrives.
      * The **reliable IO/control stream** (IOCTL/status RESPONSES) is acked by the
        cumulative **[40:44]** = highest contiguous camera IO message-index ([56:58])
        received. *** SESSION-24 FIX ***: this was wrongly pinned to 0 in session 14
        (correct for video-only, where there is no IO response to clear). After a
        status read, the camera holds each IOCTL response in its reliable send-FIFO
        and RETRANSMITS it forever until [40:44] covers it; only then does it free the
        FIFO and start sending video. Leaving it 0 => the camera never advances past
        the last IOCTL response and 0 video frames ever arrive (the S23 "status kills
        the stream" bug). Native climbs [40:44] 0..N across the N IOCTL responses, then
        HOLDS it (no more IO frames) while C/D carries the video.

      [32:34] relseq — the SENDER's own reliable-frame sequence (+1 per reliable frame).
      [34:36] 0      — client low edge (0 in the data phase).
      [36:38] C      — previous ACK's D (the prior low edge);  C[n] == D[n-1].
      [38:40] D      — highest camera AV fragment-seq ([46:48]) seen (gaps skipped).
      [40:44] data_ack — cumulative ack of the camera's reliable IO message-index.
      [46:48] ackord — our own monotonic ACK ordinal.
      [48:50] 0x1a22 stamp word.  [50:52] rolling 16-bit ms timestamp.

    *** SESSION-45 (disasm + 3 client-TX captures): the [48:52] timestamp field was
    SWAPPED.  The native client (proven on the TX side of frames_full.txt, s40_nat_baseline
    and retx_capture — all client->camera 0x09 ACKs) writes wire[48:50] = 0x1a22 and
    wire[50:52] = the fast-rolling low-16 ms timestamp.  (Structurally it is a 32-bit
    ~uptime-ms stamp stored word-swapped: [48:50] is the slowly-drifting high word, which
    sits at 0x1a2x for the whole 5-day-uptime window and reads as 0x1a22 in a short session;
    [50:52] is the fast low word.)  Pure previously emitted ms in [48:50] and 0x1a22 in
    [50:52] — the exact inverse (the "0x1a22 in the wrong halfword" bug first noted in S25).
    `_sendAVIOFrameACK` @ 0xe86c0 confirms every other ACK field (sub-type 0x09 immediate,
    [30]=0x0b immediate, relseq=ctx+0x1f38++, ackord=ctx+0x1f3e++, C=ctx+0x1f4e, D=ctx+0x1f40,
    idle sentinel 0xFFFF = the ctx init value).  The camera does NOT validate the timestamp
    (S17), so this is a byte-fidelity fix, not a functional one.
    """
    # *** SESSION-62 (T1-D): the [42:44] field is the OUT-OF-ORDER fragment count and,
    # when non-zero, native APPENDS that many 2-byte (frag_seq - C) SACK entries at
    # wire[50:].  Proven from `_sendAVIOFrameACK`@0xe886f (loop: FifoCount of the OOO
    # recv-FIFO ctx+0x2050, one entry per frag, `mov %cx,0x56(%rsp,%rsi,2)` where
    # cx = frag-C) and EMPIRICAL native ACKs (arm_native.pkl): frame_len = 50 + 2*count
    # with [4:6] (IOTC content length) = frame_len-16 — observed 52(count 0)/68(9)/198(74),
    # [4:6] = 36/52/182 exactly.  count==0 => the proven 52-byte ACK with the [50:52] ms
    # timestamp (S45).  S60/S61: the SACK list is EFFICIENCY-ONLY (it does NOT gate camera
    # resend — pure's count=0 already requests the whole C+1..D range), so `sack` is only
    # ever non-empty in resend_mode under loss (native-born+pure-continued); the pure-born
    # best-effort path passes sack=None and stays byte-identical to S45.  (The brief's
    # range-merged "compute_sack_count" was REFUTED — native lists EVERY OOO fragment.)
    # *** SESSION-63: `sack` is a list of ABSOLUTE camera fragment-seqs (or None); this
    # function encodes each as its 2-byte (frag_seq - C) wire offset in the loop below.
    # (S62 passed pre-subtracted relative offsets; moving the -C in here makes
    # build_data_ack own the FULL wire encoding and is byte-identical on the wire — the
    # caller's wire C is the same value the -C subtracts.  See _compute_sack.)
    # CAVEAT (S63 empirical, arm_native/s40 captures, p['len'] = true on-wire length;
    # stored 'dec' is snaplen-truncated to ~80B): native's real entries under the loss
    # oracle are STRIDED (~16, the 1/16 drop pattern) with wrapped/duplicate tail values,
    # NOT the dense list _compute_sack builds.  Reproducing those exact values is moot for
    # the legacy received-list path (_compute_sack); the live resend path lists MISSING
    # frags (_compute_holes, S87) which the camera honours.  Here the len/[42:44]/[4:6]
    # structure is byte-exact and the received-list entry VALUES are best-effort.
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
    struct.pack_into("<H", p, 34, win & 0xFFFF)            # [34:36] (S83: 0=pure default; native leak=0x5838 establishment-only)
    struct.pack_into("<H", p, 36, C & 0xFFFF)              # C = previous D (low edge)
    struct.pack_into("<H", p, 38, D & 0xFFFF)              # D = highest camera frag-seq seen
    struct.pack_into("<H", p, 40, data_ack & 0xFFFF)       # [40:42] = cumulative IO msg-index ack
    struct.pack_into("<H", p, 42, n & 0xFFFF)              # [42:44] = OOO/SACK entry count
    struct.pack_into("<H", p, 46, ackord & 0xFFFF)         # our ack ordinal
    ms16 = (int(time.time() * 1000) if ts is None else ts) & 0xFFFF
    if n == 0:
        if ts32 is not None:
            # *** SESSION-82 full-fidelity timestamp ***: native's [48:52] is a 32-bit
            # ms value V stored WORD-SWAPPED — [48:50]=high16(V), [50:52]=low16(V) —
            # i.e. wordswap(uint32(V)).  V = now_ms - reference (the lib's _sendAVIOFrameACK
            # @0xe86c0 computes now_ms via gettimeofday; the reference makes V a multi-day
            # camera-clock-scale value).  EMPIRICAL (s72_native/s65_udp count==0 ACKs):
            # [48:50] is CONSTANT 0x1a22 across a short session (V's high word; ticks to
            # 0x1a23 only after the low word wraps ~every 65 s) and [50:52] advances ~1 ms/ms
            # with periodic backward "reset" jumps (the reference being re-anchored).  Pure
            # passes V=_ts_word() so high16 starts at 0x1a22 and low16 climbs ~1 ms/ms; the
            # exact reference-reset cadence is NOT statically pinnable (S74) so pure uses a
            # single stable session reference (no mid-stream reset) — strictly more faithful
            # than the const-0x1a22 + free-running-wallclock scheme below, and the camera
            # IGNORES this field anyway (S17/S74/S77), so it is byte-fidelity only.
            struct.pack_into("<H", p, 48, (ts32 >> 16) & 0xFFFF)   # [48:50] high16(V)
            struct.pack_into("<H", p, 50, ts32 & 0xFFFF)          # [50:52] low16(V) ~1ms/ms
        else:
            struct.pack_into("<H", p, 48, 0x1A22)             # [48:50] stamp hi-word (S45, count==0)
            struct.pack_into("<H", p, 50, ms16)               # [50:52] rolling ms low-word
    else:
        # count>0: native displaces [50:52] with the first SACK entry and carries the
        # ms low-word at [48:50] (EMPIRICAL: arm_native.pkl count=9/74 had [48:50]=0x4bab/
        # 0x4b9c, advancing ~15ms with ackord — not the 0x1a22 stamp word).
        struct.pack_into("<H", p, 48, ms16)               # [48:50] ms low-word
        for i, frag in enumerate(sack):                    # [50:50+2n] per-frag SACK: wire = (frag_seq - C)
            struct.pack_into("<H", p, 50 + 2 * i, (frag - C) & 0xFFFF)
    return transcode(bytes(p))


# ── reversed retransmit (NAK) signalling — SESSION 16, RESOLVED S86/S87 ─────────
# These two builders reproduce, byte-for-byte, native's resend-control pair (the AV
# packet TYPE field at content[0]/wire[28]): build_resend_req = type 0x0a (AVStatistic
# "NAK"), build_resend_b = type 0x0b (AVStatisticACK). They were reversed from a native
# video session under ~3 % simulated fragment loss. They ARE wired into _av_reader via
# maybe_nak/_send_nak under self._resend_mode (default ON since S58), and fire pre-video
# from S71.
#
# *** S86/S87 — THE CAMERA DOES HONOUR AV RETRANSMIT FOR A PURE-BORN SESSION. *** The
# 30-session "firmware-gated / no wire signature" verdict (the old note here) was WRONG
# and is superseded:
#   * ARMING is a CLIENT WIRE FIELD, not firmware-internal (S86): the camera sends its
#     ms-clock in cam->host type-0x0a [36:38]; the reliable peer must ECHO it in the
#     host->cam type-0x0b [36:38] (build_resend_b, via _send_nak + _echo_cam_clock,
#     default ON). Pure used to send its own epoch clock -> camera withheld commitment
#     -> the perennial RETX floor. Echo it and pure ARMS (cam->host 0x09 oracle 8.8/s).
#   * THE RESEND-REQUEST is the type-0x09 ACK's SACK MISSING-list, NOT these 0x0a/0x0b
#     frames (S87): _send_ack/_compute_holes list the missing frag-seqs ([42:44]=count,
#     entries (frag-C) at [50:], count>=2) and the camera re-sends EXACTLY those, as
#     type-0x0c DATA, byte-verbatim. Live: pure recovers ~76-84 % of real losses at
#     ~1.07x redundancy = native parity (82 % / 1.10x). See RESEND_FRAME_INVESTIGATION.md
#     + AV_HANDOFF S86/S87. In selective mode _send_nak drops the 0x0a highwater to 0 (the
#     SACK drives resends; an explicit highwater would flood duplicate resends, S87-T5).
# The S58 reader stall-guards (forward-skip + depth cap, see _gap_hold/_gap_depth_cap)
# remain the backstop. NOTE: AV packet TYPE 0x07 is a defined-but-UNUSED data-frame slot
# (no sender in the lib, never on the wire) — it is NOT a resend frame.

def build_resend_req(R, seq, relseq, highwater=0, ts=None,
                     resend_timeout_ms=0, recv_count=None, win=0):
    """Native resend-control / AV-statistic frame (TYPE 0x0a, [29]=0x08). 44 bytes.
    *** WIRED (S58) via _send_nak; the camera DOES honour resend once armed (S86/S87) —
    see module note above. This 0x0a is the AVStatistic "NAK"; the actual resend-REQUEST
    rides the type-0x09 ACK SACK (_send_ack), and ARMING rides its 0x0b companion. ***

    Byte map proven from disasm S48 — `_sendAVFrameFifo`@0xe9240, frame built at
    stack `rsp+0x80` (= content base = wire[28]) then sent via the IOTC channel vtable:
      [28]=0x0a sub, [29]=0x08 marker, [30:32]=0x000b reliable-chan id (immediates).
      [32:34] relseq  — shared reliable-frame seq (ctx+0x1f38, post-incr +1 per frame).
      [34:36] uninit  — NOT written in this branch (stack leftover; native shows 22584
                        pre-video → 0). pure zeroes it — harmless.
      [36:38] ms-clock— session millisecond clock low-16 (monotonic).
      [38:40] ctx+0x192c — the client's EWMA-smoothed RTT / resend-TIMEOUT estimate in
                        ms (`new = 0.15*old + 0.85*sample_ms`, doubles 1.5/8.5/10.0 at
                        .rodata 0x2f6758; used as the resend age-threshold compared
                        against elapsed time at `_doAVTransNew`@0xec196 `cmp 0x192c,%dx`).
                        Live: 27-50 ms, NON-monotonic, jittery.
                        *** S48 CORRECTION: this is a TIMING estimate, NOT a "recv-count"
                        (the S38/S45 label was inferred from its 28-52 magnitude and is
                        wrong) and NOT `ms>>4` (the prior pure bug, now fixed). ***
      [40:44] 0 in steady-state telemetry (not written in the e9be0 branch); native sets
                        it nonzero only on the loss-triggered resend path (`highwater`).

    Params: `resend_timeout_ms` populates [38:40] (default 0). `recv_count` is a DEPRECATED
    alias for the same field, kept so older call-sites / the S48 verification snippet that
    pass `recv_count=` keep working.
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
    struct.pack_into("<H", p, 34, win & 0xFFFF)              # [34:36] (S83: native 0x0a leak=0x5838 establishment-only; 0 default)
    ms = int(time.time() * 1000) if ts is None else ts
    struct.pack_into("<H", p, 36, ms & 0xFFFF)
    field_3840 = recv_count if recv_count is not None else resend_timeout_ms
    struct.pack_into("<H", p, 38, field_3840 & 0xFFFF)        # [38:40] EWMA resend-timeout
    struct.pack_into("<I", p, 40, highwater & 0xFFFFFFFF)     # [40:44] 0 steady / loss-only
    return transcode(bytes(p))


def build_resend_b(R, seq, recv_count, ts=None):
    """AVStatisticACK companion (TYPE 0x0b). 48 bytes. Sent immediately before each 0x0a.
    *** THIS FRAME CARRIES THE ARMING GATE (S86). ***

    UNRELIABLE — [32:34] is always 0 (does not consume a reliable-frame seq). Fields:
      [28]=0x0b, [30:32]=0x000b, [32:36]=0,
      [36:38]=THE CAMERA-CLOCK ECHO — the arming discriminator. NOT a local "rolling ms
              ts": the camera gates reliable-peer/resend commitment on this echoing its
              own cam->host type-0x0a [36:38] clock. _send_nak passes ts=(cam_clock +
              elapsed_ms)&0xffff when _echo_cam_clock is on (default); only the `now`
              fallback (before the first camera 0x0a) is a local clock. (Reversed in the
              lib as the AVStatisticACK built at _doAVTransNew@0xeb9ee.)
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
# ([29]=0x05; the old build_audio_data layout, see AV_HANDOFF.md) is ACKed by the camera but never
# DECODED — the working uplink (proven live 2026-06-27) is AAC-LC on ch1 modelled on the camera's
# OWN downlink audio av-data frame. See cuboai-talkback-pure-solved.
_TALK_GRANT_CAP_DEFAULT = b"\xe0\xfe\xfe\x01"   # 4.3.x capability word; fallback if connect() didn't capture it


def _aac_units(path, rate=16000, gain=1.0):
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

    def _encode_frame(fr):
        for pkt in ostream.encode(fr):
            out.mux(pkt)

    with av.open(path) as inp:
        for frame in inp.decode(audio=0):
            frame.pts = None
            for rf in resampler.resample(frame):
                if graph is None:
                    _encode_frame(rf)
                else:
                    graph.push(rf)
                    while True:
                        try:
                            gf = graph.pull()
                        except (av.error.BlockingIOError, av.error.EOFError):
                            break
                        gf.pts = None
                        _encode_frame(gf)
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
    p[32:36] = cap or _TALK_GRANT_CAP_DEFAULT      # ★ mirror the camera's advertised capability word
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
    uses, [45]=0x70/[29]=0x05, delivered but did NOT decode). Proven live 2026-06-27.
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


# ── read-only stats snapshot (the single source for --benchmark and verbose) ────

class _StreamGet:
    """A mid-stream GET request handed to the reader thread.

    While streaming, `_av_reader` is the SOLE socket sender (a second sender would race
    its seq/relseq/frmno and double-drain recvfrom — see _av_reader). So a thread that
    wants a camera GET *during* a stream cannot call ioctl() itself: it fills one of
    these slots, the reader sends it and captures the matching response, and the
    requester waits on `done`. Read-only telemetry path — see get_during_stream().
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

    get_stats() returns CUMULATIVE counters plus a wall-clock 't'; this turns a
    (prev, cur) pair into the interval values both --benchmark and verbose print:
    fps/bitrate over the interval, the interval loss% and recovery%, and the raw
    deltas. Pure function (no state) so it is the one shared delta computation. With
    prev is None (the first sample) it reports the cumulative values as the interval.
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


# ── session ───────────────────────────────────────────────────────────────────

class TUTKDirectSession:
    """Pure-Python TUTK LAN session (no native library).

    The handshake (session 9, working end-to-end):
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

    def __init__(self, camera_ip="192.0.2.10", camera_port=39099,
                 account=b"admin@YOUR_ACCOUNT", password=b"YOUR_PASSWORD",
                 uid=_DEFAULT_UID, channels=None, verbose=False,
                 full_fidelity=True,
                 defer_stream_start=None, defer_video_start_late=None):
        self.camera_ip = camera_ip
        self.camera_port = camera_port
        self.account = account
        self.password = password
        self.uid = uid
        # S62: which AV channels to open in the handshake.  Native opens ch0..39
        # (S65/S66 birthdiff: 44 av-connect frames, ch{2k,2k+1} pairs @50ms; ch0 =
        # video, S33).  The lib allocates ONE avIndex per avClientStart call (disasm
        # avClientStart_inner@e6310 -> _allocAVIndexLocked); the channel split is an
        # APP convention.  Pure's RECEIVE path is channel-AGNOSTIC — it demuxes
        # video/audio by content, never by a channel id (T1-C: _note_cam_data/
        # _av_reader read dec[28]/[58:64]/[56:58] only) — so the channel SET changes
        # only the handshake, not decode.  S62/S65/S67 PROVED the set is arming-
        # INVARIANT (ch1-only streams full video+audio; ch0..39 from connect = RETX 5,
        # same noise floor as 4).  Default = range(40) to be byte-faithful to native's
        # full av-connect burst (S70 decision); functionally [0,1,2,3] is equivalent.
        self._channels = list(channels) if channels else list(range(40))
        # S62: optional human-readable trace of connect()/streaming (off by default;
        # never changes the wire).  See _vlog / --verbose in cuboai_validate.py.
        self._verbose = verbose
        # *** SESSION-82: full_fidelity is the MASTER wire-fidelity flag *** (default
        # True per the standing "always match native's on-wire behaviour" preference).
        # It folds in the S81 IOCTL cadence AND gates the three remaining native↔pure
        # wire divergences audited in S74/S77 (all camera-IGNORED for arming — S17/S74/
        # S77 proved corrupting native's every one of them on a live armed session keeps
        # it armed — so these are byte-fidelity only, never an arming lever):
        #   (1) ACK timestamp [48:52]   — native's word-swapped 32-bit clock (S82
        #       _ts_word / build_data_ack ts32) vs pure's const-0x1a22 + free-run ms.
        #   (2) NAK cadence _nak_interval — native ~4.8 0x0b/0x0a PAIRS/s (S82 re-measure
        #       of s72_native/s65_udp: 0x0a≈4.3-4.7/s, 0x0b≈4.6-4.8/s, total ~9.1-9.6
        #       FRAMES/s) => 0.19 s (S72-verified == native).  NB: the brief's 0.137 s
        #       was REJECTED — it yields 7.3 pairs/s ≈ 1.5x native (anti-parity); see
        #       _nak_interval below.
        #   (3) SACK list _compute_sack — native lists EVERY out-of-order fragment
        #       (full OOO range, frame_len 50+2N); pure's best-effort path truncated at
        #       _FRAG_WINDOW.  Under fidelity pure emits the full list.
        # When full_fidelity is False (the --fast-start / low-latency path) every one of
        # these reverts to the simpler/faster pre-S82 behaviour so the ~0.5 s-TTFF
        # shipping path is unchanged.  Arming rides the type-0x0b camera-clock echo (S86,
        # self._echo_cam_clock, default ON) regardless of this flag, so full_fidelity
        # changes ONLY wire-fidelity/latency, never whether AV resend arms.
        self._full_fidelity = full_fidelity
        # S81 cadence sub-flags, now subordinate to full_fidelity: None (the default)
        # => FOLLOW full_fidelity; an explicit True/False overrides just that stage
        #   defer_stream_start     — 0x0300 (AUDIOSTART/stream-start) ~_MID_IOCTL_SECS
        #     after 0x00FF (THE latency lever: True => first frame ~5 s in = native
        #     cadence; False => ~0.5-2 s, camera-bound).
        #   defer_video_start_late — 0x01FF (START) ~_LATE_IOCTL_SECS after 0x0300 (S71).
        # so e.g. --no-defer-stream-start keeps timestamp/NAK/SACK fidelity but starts
        # video fast.  Toggled via get_session(...)/--fast-start in cuboai_validate.py.
        self._defer_stream_start = (full_fidelity if defer_stream_start is None
                                    else defer_stream_start)
        self._defer_video_start_late = (full_fidelity if defer_video_start_late is None
                                        else defer_video_start_late)
        # S82: NAK pair cadence (see _send_nak/maybe_nak).  Fidelity => native-matched
        # 0.19 s; fast path => the brief's 0.137 s (faster, arming-irrelevant — NAK rate
        # never gates the fast TTFF path either, so this is purely to honour the gating).
        self._nak_interval = 0.19 if full_fidelity else 0.137
        # S82: per-session reference for the word-swapped ACK timestamp (lazily set on
        # first use by _ts_word so high16 starts at native's empirical 0x1a22).
        self._ts_ref = None
        # *** SESSION-83 (experiment): the [34:36] field of every reliable ACK/NAK. ***
        # Native leaves it UNINITIALISED — its ACK builder (_sendAVIOFrameACK@0xe86c0,
        # T0 disasm) never writes packet offset 0x46, so it carries the leftover stack
        # value 0x5838 for the first ~26 establishment frames (while C==D==0xFFFF) then a
        # clean step to 0 for the whole data phase (T1: s72_native = 0x5838 x26 then 0
        # x1135, ONE distinct nonzero value -> a leak, NOT a buffer-free-space "window";
        # and native streams armed for 1135 frames sending [34:36]=0, so 0 is provably not
        # a stall trigger).  Pure writes 0 (cleaner).  Default 0 keeps that.  Set nonzero
        # (env CUBOAI_ADV_WINDOW, e.g. 0x5838) to TEST the S83 KCP-zero-window hypothesis:
        # does advertising native's non-zero establishment value in [34:36] from packet 1
        # arm pure?  (T0/T1 + S77-RUNG2 predict FLOOR; this is the direct empirical check.)
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
        # ── S54 gap-tracking / resend (gated) ──────────────────────────────────
        self._frag_edge = None        # highest CONTIGUOUS frag-seq (low-water; gap detect)
        self._frag_edge_acked = 0xFFFF  # edge value at our last held-D ack (resend_mode C)
        self._frag_received = set()   # recent received frag-seqs (drives the edge advance)
        self._frag_gap_ts = {}        # frag-seq -> time first seen as a gap (stale-skip)
        self._resend_mode = True      # S58: held-C edge ack + 0x0b/0x0a NAK (gap signalling).
                                      # Flipped ON (was OFF S54-S57) so pure actually SIGNALS a
                                      # gap — without it C==prev-D every ack and the camera is
                                      # never told what to resend, so RETX=0 is guaranteed
                                      # regardless of arming. Made stall-safe in S58 (50ms
                                      # forward-skip + a hard depth cap, see _GAP_STALE / maybe_nak)
                                      # so it no longer dead-stalls an unarmed stream under loss.
        self._session_fp = None  # camera's echoed [16:20] session token (from probe; S49) for build_close
        # ── S86: NAK 0x0b clock echo (THE arming discriminator) ────────────────
        # The camera sends its own ms-clock in cam->host 0x0a [36:38]; native ECHOES
        # that camera clock back in its host->cam 0x0b [36:38] (proven Δ=0, 120/120).
        # The camera gates AV-retransmit reliable-peer commitment on this echo: a peer
        # that reflects the camera's heartbeat clock is armed; one that sends its own
        # `now` (pure pre-S86) or any non-matching value floors (cam->host 0x09 0.1/s).
        # PROVEN S86-T1 on native: 0x0b[36:38]=now -> FLOOR 0.09/s; native echo -> ARM
        # 8.7/s (single-variable, on-wire). Echo ON = parity with native (default).
        self._cam_clock = None        # latest camera ms-clock from cam->host 0x0a [36:38]
        self._cam_clock_ts = None     # local time.time() when _cam_clock was captured
        self._echo_cam_clock = os.environ.get("CUBOAI_ECHO_CAMCLOCK", "1") != "0"
        # ── S87: gap-hold (resend wait) — close the recovery gap on the now-ARMED peer ──
        # How long pure waits for a missing fragment's RESEND before forward-skipping the
        # held-C edge PAST it (= telling the camera "delivered", abandoning the gap). S58
        # set this to 50 ms for the UN-ARMED era (the camera never resent for pure, so any
        # hold only stalled the send-window — the W200/W400 dead-stall). Since S86 the
        # camera ARMS and DOES resend, at ~370 ms for pure / ~140 ms for native. With a
        # 50 ms hold the edge advances ~165 ms after a loss (ACK rate-limit + skip-one-at-
        # a-time), still WELL before the resend lands -> S87-T3b: 98 % of losses abandoned
        # early (resend prevented/wasted), only 2 % truly aged out. Holding longer than the
        # camera's resend latency lets resends land while the gap is still requested.
        # BOUNDED by _gap_depth_cap (jump near high-water when too many holes pile up) so it
        # can NEVER dead-stall, just trades a little forward-latency for loss recovery.
        # env CUBOAI_GAP_HOLD_MS (default = class _GAP_STALE) / CUBOAI_GAP_DEPTH_CAP (default _GAP_DEPTH_CAP).
        self._gap_hold = float(os.environ.get("CUBOAI_GAP_HOLD_MS", "")
                               or self._GAP_STALE * 1000) / 1000.0
        self._gap_depth_cap = int(os.environ.get("CUBOAI_GAP_DEPTH_CAP", "")
                                  or self._GAP_DEPTH_CAP)
        # ── S87: native-style SELECTIVE-REPEAT loss recovery (VALIDATED at native parity) ──
        # Once S86's echo arms the session the camera WILL resend for pure — but only if pure
        # signals losses the way native does. The S87 reverse-engineering (drop-independent):
        #   * The host→cam 0x09 SACK is a RESEND-REQUEST list — the camera resends EXACTLY the
        #     frag-seqs it carries (native's listed frags are resent 95% → native recovers 82%
        #     of real losses, redundancy 1.10×).  Pure historically put the RECEIVED frags in
        #     the SACK, so the camera wasted resends on already-delivered frags and recovered
        #     ~0% of true losses (the old "RETX %" was all waste).  _compute_holes lists the
        #     MISSING frags (holes) instead.
        #   * Entries are (hole − C) u16 at [50:], so C must be ≤ every hole ⇒ C = una
        #     (contiguous edge); D = high-water.  The una must HOLD at a genuine hole until its
        #     resend FILLS it (selective mode disables maybe_nak's 50ms GAP_STALE skip; the
        #     _gap_depth_cap is the only backstop) — else the hole leaves the (una,hw] window
        #     before it can be requested.
        #   * The camera only honours entries when count≥2 (a count-1 frame's [50:52] is the
        #     timestamp, like native's) — so lone holes wait for a second.
        #   * Each hole is (re)listed at most once per _RESEND_REQ_INTERVAL so it is requested
        #     ~once per resend round (native-like redundancy), and a still-missing hole is
        #     re-asked next round (covers a lost resend — strictly better than native's one-shot).
        #   * _send_nak drops to native's telemetry highwater=0; ACK_INTERVAL tightens to 0.04.
        # LIVE RESULT (S87): actual recovery 76% (native 82%), redundancy 1.07× (native 1.10×),
        # camera honours the SACK (83% of listed frags resent), video healthy, armed 8.8/s.
        # DEFAULT ON (S87): native-parity loss recovery AND more native-faithful than the old
        # held-edge path, per the match-native preference.  Set env CUBOAI_SELECTIVE_ACK=0 to
        # revert to the S86 held-edge path (no selective repeat).
        self._selective_ack = os.environ.get("CUBOAI_SELECTIVE_ACK", "1") != "0"
        self._hole_req_ts = {}   # frag-seq -> last resend-request time (per-hole dedup)
        # ── S88: adaptive resend-request interval state (default OFF, clean A/B) ──────
        self._adaptive_rtt = os.environ.get("CUBOAI_ADAPTIVE_RTT", "0") != "0"
        self._hole_first_req = {}  # frag-seq -> time first SACK-listed (for the latency sample)
        self._rtt_ewma = None      # EWMA of first-request->arrival resend latency (seconds)
        self._rtt_n = 0            # clean samples folded into the EWMA so far
        # ── FIX#5 (S90): scale the reassembly grace with resend-latency × AU-rate ─────
        # grace=2 finalises an AU after only 2 AU-indices (≈<100 ms at ~24 AU/s), but a
        # recovered fragment lands ~RTT+camera-timer (~140 ms LAN) later → past the grace
        # → reassembly DROPS it → incomplete AU (S90: 14-24% even at moderate loss). The
        # dynamic grace holds an AU open ceil(EWMA·AU_rate)+1 indices so the resend still
        # lands inside the window. Reuses the adaptive EWMA (armed below even when the
        # adaptive *interval* is off). Bounded by _grace_max: a permanently-lost frag
        # finalises-INCOMPLETE after the cap, never stalling the AU forever. DEFAULT ON
        # (S90-verify: +13-19 pts LAN decode, zero transport cost); CUBOAI_GRACE_SCALE=0 to
        # disable, CUBOAI_GRACE_MAX to tune the cap.
        self._grace_scale = os.environ.get("CUBOAI_GRACE_SCALE", "1") != "0"
        # S90-verify: the LAN decode lift (+13-19 pts) needs only grace≈5 (EWMA~140ms·24/s,
        # +~125ms hold); the high-RTT grace≈14 (+~500ms hold) bought only +2-6 pts (recovery,
        # not grace, is the binding constraint there). So cap at 8 = keep the LAN win, bound
        # the worst-case added latency to ~250ms. Tunable via CUBOAI_GRACE_MAX.
        self._grace_max = int(os.environ.get("CUBOAI_GRACE_MAX", "") or "8")
        # ── FIX (S90): lone-hole / count-1 SACK gate (gated CUBOAI_LONE_HOLE, default OFF) ──
        # A LONE outstanding hole emits a count-1 SACK, which the camera reads as a timestamp
        # ([50:52], S87 count<2 gate) -> never resent. S90-residual proved this is the decode-
        # band ROOT: 76% of unfilled holes are never-resent lone holes, and every keyframe's
        # last hole goes lone (count->1) so 26/26 keyframes were incomplete -> the GOP-cascade
        # seed. FIX: when exactly ONE hole is fresh, PAD the SACK to count>=2 (duplicate the
        # hole = benign 2nd entry) so the camera honours it and RESENDS the lone hole promptly
        # (-> lands inside FIX#5's grace -> keyframe completes -> cascade not seeded). Skip-
        # fallback (advance the contiguous edge past the hole) ONLY after _lone_skip_rounds
        # padded requests fail to fill it (a genuinely-lost frag -> breaks the S90-soak freeze).
        # RECOVER first, unfreeze last. OFF path is byte-identical to the FIX#5-ON shipped.
        self._lone_hole = os.environ.get("CUBOAI_LONE_HOLE", "0") != "0"
        self._lone_pad = os.environ.get("CUBOAI_LONE_PAD", "dup")   # "dup" | "plus1" (T1 probe)
        self._lone_skip_rounds = int(os.environ.get("CUBOAI_LONE_SKIP_ROUNDS", "") or "6")
        self._hole_req_count = {}   # frag-seq -> padded-request count (lone-hole skip-fallback)
        # ── FIX (S90): keyframe-aware grace (gated CUBOAI_KF_GRACE, default OFF) ──────────
        # The keyframe (GOP root, ~69 frags) almost always loses >=1 frag at >=1% loss; today
        # it is sealed at the short FIX#5 grace (~8 AU-idx ~0.3s) while the una abandons its
        # holes (gap_depth_cap / count-1) -> incomplete root -> the whole GOP cascades. KF-grace
        # HOLDS an incomplete keyframe AU head-of-line up to _kf_hold AU-indices (~one GOP),
        # keeps the una at its holes (gap_depth_cap + lone-skip suppressed) so the camera keeps
        # resending them (reliable ~91-99%/round) + pads its lone hole to count>=2, and seals on
        # COMPLETE (then the GOP decodes) or at the GOP boundary (give up; next keyframe
        # re-syncs). Trades ~one-GOP keyframe latency for keyframe survival; P-frames keep the
        # short grace. OFF path byte-identical (_holding_kf stays False, seal loop equivalent).
        self._kf_grace = os.environ.get("CUBOAI_KF_GRACE", "0") != "0"
        self._kf_hold = int(os.environ.get("CUBOAI_KF_HOLD", "") or "40")   # AU-indices ~ one GOP
        if self._kf_grace:
            self._lone_hole = True       # REQUIRED: pad the keyframe's final lone hole to count>=2
        self._holding_kf = False         # reader: True while head-of-line-holding an incomplete kf
        # S90: per-AU fate trace (gated CUBOAI_AU_LOG) — emit/skip/reject/dropclassify, to
        # localize POC gaps (a gap = a video AU never emitted -> greys a refs=1 GOP).
        self._au_log = [] if os.environ.get("CUBOAI_AU_LOG") else None
        # ── FIX (S90): NEVER-DROP / in-order emit (gated CUBOAI_NODROP, default OFF) ──────
        # S90-localize: at ~1% loss 47/48 video-AU "gaps" were COMPLETE-but-dropped — a higher
        # idx sealed first (short grace) -> done_upto jumped past AU K -> K's (complete) frags
        # then hit the idx<=done_upto reject -> hard POC gap -> greys the refs=1 GOP. FIX: seal
        # STRICTLY in order (done_upto+1 only) at grace-expiry, emitting the PARTIAL slice if
        # still incomplete, and NEVER sealing a higher idx first — so a slightly-late AU is
        # waited for (within grace) and emitted instead of skipped+rejected. Inverse of
        # finalize-and-drop; matches native (which never gaps). OFF path byte-identical.
        self._nodrop = os.environ.get("CUBOAI_NODROP", "0") != "0"
        # H1 fix (default ON): the camera AV message-index ([56:58]) is a u16 that WRAPS at 65536,
        # but `done_upto` is an unbounded monotonic int. _idx_modular lifts each incoming idx into
        # done_upto's space (modular forward unwrap, _unwrap_index) so the reassembly accept-window
        # survives the wrap. Byte-identical pre-wrap (the unwrap == raw idx while no wrap has
        # occurred). OFF reverts to the historical non-modular gate, which dead-stalls a continuous
        # stream ~every 46 min when idx wraps 65535->0 (audit H1, reproduced live 2026-06-10).
        # ⚠ REGRESSION SWITCH, NOT A TUNABLE: OFF (=0) reopens a proven silent bug — keep it ON.
        self._idx_modular = os.environ.get("CUBOAI_IDX_MODULAR", "1") != "0"
        # H1-SIBLING (default ON, 2026-07-23): the SECOND-STREAM dead-read. `done_upto` is a
        # per-reader local that starts at -1, but the camera's AV message-index is SESSION-scoped
        # and keeps advancing whether or not we are reading (live audio runs on ch0 even while
        # video is stopped). So the FIRST _read_av_units of a session starts with idx ~0 and works,
        # while a LATER one meets idx > 255, every fragment falls outside the accept window
        # `done_upto < idx <= done_upto+256`, done_upto never advances, and the reader emits ZERO
        # AUs — forever, silently (frags_recv climbs at the full live rate, au_video stays 0,
        # au_incomplete 0, gap_now 0). This is the path a playback session's restore_live() lands
        # on, i.e. the "always restore live" safety path. FIX: seed the window from the first
        # accepted access-unit START instead of assuming the stream begins at index 0. NO-OP when
        # the first index is already in window (every fresh-session stream, so every replay
        # fixture) -> live/replay output byte-identical. `=0` reverts.
        # ⚠ REGRESSION SWITCH, NOT A TUNABLE: OFF (=0) reopens the second-stream dead-read — keep ON.
        self._idx_seed = os.environ.get("CUBOAI_IDX_SEED", "1") != "0"
        # bound ioctl()'s reconnect-under-loss (see ioctl's docstring). Off the AV path.
        # DEFAULT OFF: the premise that motivated it was REFUTED on the wire — see ioctl().
        self._ioctl_fast_reconnect = os.environ.get("CUBOAI_IOCTL_FAST_RECONNECT", "0") == "1"
        # H1-sibling — IO cumulative-ACK wrap fix (gated, default OFF pending live confirmation).
        # `_data_ack` (wire [40:42], the camera's reliable IO/control message-index cum-ack) is an
        # unbounded monotonic int advanced by contiguity over a set of RAW u16 indices — so when the
        # camera's index wraps 65535->0 the `+1` never appears and _data_ack FREEZES at 65535, i.e.
        # the wire [40:42] pins at 0xFFFF forever. Per S60 the camera prunes its resend FRAME FIFO
        # (`_resendAVFrameFifo` / `resendBufferUsage`) against wire[40:42]; a frozen ack => the FIFO
        # never drains => resendBufferUsage pins at 1.0 => new AV sends fail -20006 EXCEED_MAX_SIZE
        # and bulk RDT DATA (manifest) can't be pushed (never_started / HELLOs-only) — the 2026-07-17
        # manifest flake, and (if real) a 24/7 degradation of any long-lived stream. ON: lift each
        # incoming idx into _data_ack's space via _unwrap_index so contiguity survives the wrap
        # ([40:42] then correctly wraps 0xFFFF->0x0000). Byte-identical PRE-wrap (unwrap==raw idx
        # while no wrap has occurred) so validator/SHAs are unchanged; only a session that actually
        # crosses 65536 IO messages diverges. DEFAULT ON (2026-07-23): the fix is unit-proven correct
        # AND pre-wrap byte-identical by test_dataack_wrap.py — (1) WRAP ON _data_ack crosses 65535->
        # 65540 (wire 0xFFFF->0x0004), (2) WRAP OFF freezes at 65535 (reproduces the bug), (3) 399
        # pre-wrap steps + a full build_data_ack frame are byte-identical ON vs OFF. The validator's
        # fixture never reaches the wrap, so 36/0 only proves the tested range is unchanged — the new
        # wrap-crossing test is what licenses the flip (same bar as the H1 _idx_modular default-ON).
        # `=0` reverts to the legacy freeze-at-wrap path.
        # ⚠ REGRESSION SWITCH, NOT A TUNABLE: OFF (=0) reopens the _data_ack freeze-at-wrap — keep ON.
        self._dataack_wrap = os.environ.get("CUBOAI_DATAACK_WRAP", "1") != "0"
        # minimum in-order wait (AU-indices) before advancing past an absent AU — the FIX#5
        # grace (~4 at LAN) is too short to wait out a >grace-late frag-burst, so the late-but-
        # complete AU is still skipped+rejected; this floor holds each AU long enough to land.
        self._nodrop_grace = int(os.environ.get("CUBOAI_NODROP_GRACE", "") or "12")
        # emit-on-complete (NODROP): emit an AU the instant it is provably complete (marker-led,
        # gap-free, bounded by the next AU's first frag) instead of waiting out the grace window
        # — native-like low latency; grace only ever applies to a still-INCOMPLETE AU.
        self._emit_complete = os.environ.get("CUBOAI_EMIT_COMPLETE", "1") != "0"
        # ── C1 (S90): clean-truncation partial (gated CUBOAI_TRUNCATE_PARTIAL, default OFF) ──
        # When an incomplete AU is emitted, assemble() BRIDGES the hole (frags after the gap are
        # concatenated onto frags before -> false start codes / garbage syntax -> cu_qp_delta /
        # invalid-NAL decoder choke that propagates on a refs=1 stream). Instead emit ONLY the
        # contiguous prefix up to the first missing frag — a clean, byte-aligned, terminated slice
        # the decoder conceals. OFF path byte-identical.
        self._truncate_partial = os.environ.get("CUBOAI_TRUNCATE_PARTIAL", "0") != "0"
        # ── Overnight-2: strip+parse the 24-byte TUTK FRAMEINFO video trailer (gated
        # CUBOAI_STRIP_FRAMEINFO, default OFF) ────────────────────────────────────────────
        # Hardware decoders (Apple VideoToolbox via Safari/Chrome) reject the over-long final
        # NAL the trailer creates -> black picture (-12909); software decoders ignore it (which
        # is why it went unseen). Native strips it. ON: for a COMPLETE video AU only, sanity-
        # check the trailing 24B are a FRAMEINFO (codec_id==0x0050) then drop them, and parse
        # the keyframe flag + frame timestamp into self._last_frameinfo. OFF path byte-identical.
        self._strip_frameinfo = os.environ.get("CUBOAI_STRIP_FRAMEINFO", "0") != "0"
        # most-recent parsed video FRAMEINFO (keyframe/timestamp_ms/frame_no/w/h). NOTE: this is
        # the LATEST trailer the reader thread has seen — it runs AHEAD of the consumer by the
        # out_q depth, so it is "current stream state", not reliably the AU the consumer just
        # dequeued. Wiring per-AU PTS would attach it to the queued item (deferred — see SUMMARY).
        self._last_frameinfo = None
        self._frameinfo_skips = 0       # count of complete video AUs whose tail wasn't a FRAMEINFO
        # ── Audio investigation (Phase 2): gated FRAMEINFO/codec census (CUBOAI_LOG_FRAMEINFO,
        # default OFF). When ON, _av_reader's seal_one emits one stderr line per assembled AU
        # BEFORE any audio-truncation / video-FRAMEINFO-strip / consumer kind-filter, carrying the
        # candidate 24-byte trailer, its codec_id, the keyframe byte, the [8:12] bytes, the content
        # classifier's verdict and the total length — so a live run shows whether audio AUs
        # (codec_id 0x0088 / ADTS sync) are interleaved with video on the same channel. The block
        # is fully behind the flag and touches nothing emitted, so the OFF path is byte-identical.
        self._log_frameinfo = os.environ.get("CUBOAI_LOG_FRAMEINFO", "0") != "0"
        self._ficensus_n = 0            # census line counter (diagnostic only)
        # ── C2 (S90): una C-lag (gated CUBOAI_UNA_LAG, default 0=off) ──────────────────────
        # Native keeps its reported una C a STEADY ~11 frags BEHIND the high-water D (s90_sackcmp:
        # gap med 11; persists 7-10 SACKs/hole; 100% recovery). PURE reports C≈D (gap med 0) so the
        # camera believes everything <=D is delivered and DISCARDS its resend buffer up to D —
        # then a hole pure requests late gets NO resend (the residual's "0 tx, camera didn't
        # resend"). Capping the REPORTED C at D-UNA_LAG keeps the camera's resend buffer ~LAG deep
        # so it can still honour pure's requests for recent holes (matches native). Internal
        # _frag_edge is unchanged; only the wire-reported C is lagged. OFF (=0) byte-identical.
        self._una_lag = int(os.environ.get("CUBOAI_UNA_LAG", "") or "0")
        # ── C2b (S90): recovery-HOLD (gated CUBOAI_RECOVERY_HOLD; default == _nodrop_grace = off) ──
        # C1+C2 evidence: with persistence the camera DOES resend a residual hole, but the resend
        # lands ~0.9-1s later while pure's NODROP grace emits the incomplete AU at ~0.5s (too
        # early). Native holds ~1s (its buffer) and catches it. So hold a PRESENT-but-incomplete
        # done_upto+1 AU up to _recovery_hold AU-idx (emit-on-complete still fires the instant the
        # resend fills it); only a still-incomplete AU at expiry is emitted (truncated via C1).
        # Default == _nodrop_grace -> identical to current; raise (~36 ≈ 1.5s) to catch late resends.
        self._recovery_hold = int(os.environ.get("CUBOAI_RECOVERY_HOLD", "") or str(self._nodrop_grace))
        self._av_reader_thread = None   # background _av_reader thread (while streaming)
        self._av_stop_evt = None        # its stop Event (set by _stop_reader/disconnect)
        # ── read-only transport/decode counters (cumulative; see get_stats) ──────────────
        # Plain ints, written ONLY by the reader thread on paths that already run, read
        # lock-free from any thread (CPython int read/write is atomic under the GIL — a
        # stats snapshot tolerates a one-tick skew between fields). They are pure side-effect
        # increments next to existing logic, so the stream's emitted bytes/timing are
        # unchanged whether anything reads them or not. The single source of truth that
        # --benchmark and the streamer's verbose mode both consume via get_stats().
        self._stat_frags_recv = 0       # distinct AV fragments received (incl. recovered resends)
        self._stat_holes = 0            # distinct fragments detected missing (first resend-request)
        self._stat_resend_req = 0       # resend requests SENT (SACK entries, incl. re-asks)
        self._stat_resend_recovered = 0 # distinct requested holes that arrived (resend honoured)
        self._stat_au_video = 0         # video access units emitted
        self._stat_au_audio = 0         # audio access units emitted
        self._stat_au_incomplete = 0    # video AUs emitted with a missing fragment (partial)
        self._stat_kf_total = 0         # keyframe (IDR/VPS/SPS/PPS-led) video AUs emitted
        self._stat_kf_incomplete = 0    # of those, emitted incomplete (the decode-band seed)
        self._stat_bytes_video = 0      # emitted video bytes (post strip/truncate) — for bitrate
        self._stat_bytes_audio = 0      # emitted audio bytes — for bitrate
        self._stat_gap_max = 0          # high-water of the (D-edge) hole gap depth seen
        self._stat_gap_cap_jumps = 0    # times the _gap_depth_cap backstop jumped the edge
        self._stat_lone_skips = 0       # times a lone hole was abandoned after _lone_skip_rounds
        self._stat_ts_valid = 0         # video AUs whose FRAMEINFO carried a valid timestamp
        self._stat_ts_garbage = 0       # video AUs whose FRAMEINFO timestamp was garbage (~10%)
        self._stat_ts_regress = 0       # video AUs whose camera timestamp went backwards (monotonic)
        self._stat_last_ts = None       # last valid video timestamp_ms (for the monotonic check)
        # ── OUTBOUND / MASKED-RECOVERY counters (audit 2026-07-23) ────────────────────────────
        # EVERY wrong verdict in this project came from a rig that could only see ONE SIDE, and
        # every counter above this line is RECEIVE-side: the ledger counts what ARRIVES, so a
        # send-side fault (an ACK that never left, a reconnect papering over a protocol bug) is
        # invisible and the session still reads "healthy". The ghost-conn bug hid for weeks behind
        # exactly this: ioctl()'s auto-reconnect silently recovered it on Linux, so it presented as
        # a macOS-only issue. These make the send side and the SILENT RECOVERIES observable. Pure
        # int increments at existing send points -> no wire bytes, no timing change, no locks
        # (the reader thread is the sole writer of the stream-path ones, as above).
        self._stat_tx_ack = 0           # 0x09 ACK/SACK frames SENT (the reliable-channel heartbeat)
        self._stat_tx_nak = 0           # 0x0a/0x0b NAK frames SENT (gated resend_mode)
        self._stat_tx_ioctl = 0         # IOCTL request frames SENT (incl. retransmits — see tx_ioctl_retx)
        self._stat_tx_ioctl_retx = 0    # of those, RETRANSMITS of an unanswered request (masked loss)
        self._stat_tx_keepalive = 0     # keepalive replies SENT (pair with keepalive_err for a send ratio)
        self._stat_tx_err = 0           # sendto() failures on ANY path (EWOULDBLOCK/OSError) — a wedged socket
        self._stat_talk_resend = 0      # talkback frames re-sent to satisfy the camera's 0x09 SACK
        # masked-recovery visibility: each of these is a path that SILENTLY papers over a failure.
        self._stat_ioctl_timeout = 0    # _ioctl_once calls that timed out (before any retry)
        self._stat_ioctl_retry = 0      # ioctl() retry iterations entered (the concealer)
        self._stat_reconnects = 0       # full session reconnects performed inside ioctl()'s retry loop
        self._stat_reconnect_fail = 0   # reconnect attempts that failed the handshake
        self._stat_reconnect_s = 0.0    # cumulative seconds spent inside those reconnects (recovery cost)
        # mid-stream GET injection slot (None in every default path -> reader hooks inert,
        # stream byte-identical). Set by get_during_stream, serviced by _av_reader.
        self._get_inject = None
        self._inject_lock = threading.Lock()   # M1: one mid-stream GET-inject at a time (no slot clobber)
        self._ctrl_log = None    # gated diagnostic: callback(ts, io_type, hex96) for inbound IO/control frames
        self._stat_keepalive_err = 0   # L1: keepalive-reply send failures (surfaces a wedged socket via stats)
        self._ctrl_log_err = False     # L2: latch so a ctrl_log callback error is reported once, not silently
        # Talk (two-way audio): the camera advertises a 4.3.x capability word at [32:36] of its own
        # av-connect grant; we mirror it into OUR talk grant so the camera accepts us as an av-server.
        # Captured live in connect() (== e0fefe01 on this firmware; proven session-invariant, camera-
        # emitted, not echoed from the login, not in either lib as a literal). None until connect().
        self._cam_grant_cap = None
        self._talk_stop = False         # cooperative stop flag for a looping send_audio_file / stop_audio()

    def _vlog(self, msg):
        """S62: print a connect/stream trace line when verbose is on (never wire-affecting)."""
        if self._verbose:
            print(msg, flush=True)

    def connect(self, timeout=8.0, attempts=8):
        """Run the LAN handshake; returns True and sets .session_hdr on success.

        Each attempt picks a FRESH R (== the lib's GenShortRandomID), so retries
        do not collide with the camera's ~20s client-random-id dedup
        (CheckRecentClientRandomID). The camera occasionally drops a grant under
        rapid load; the retry loop covers that.

        S46: the nO->R sanity check now uses the disasm-proven verbatim echo
        (`nO_recover_R`, MID-independent) instead of the legacy 64K xor-table
        heuristic, so the one-time `_build_R_table()` is no longer built here.
        """
        # camera_ip is REQUIRED: the pure backend sends a unicast LAN-search probe to
        # the camera's IP — there is no broadcast auto-discovery (a blank IP is NOT a
        # "discover" mode; see cuboai_validate._validate_startup, which rejects it the
        # same way). Fail fast here with a clear message so a missing/None IP can never
        # reach the `::ffff:` formatter / probe sendto below as a raw TypeError.
        if not self.camera_ip:
            raise ValueError(
                "camera_ip is required to connect: the pure backend sends a unicast "
                "LAN-search probe to the camera's IP — there is no broadcast "
                "auto-discovery. Obtain the camera's LAN IP from the Cubo account/REST "
                "API and pass it, e.g. TUTKDirectSession(camera_ip='192.0.2.10').")

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
            # S51: native's AV socket is AF_INET6 with IPV6_V6ONLY=0 (dual-stack),
            # bound to '::', talking to the IPv4 camera via the v4-mapped address
            # ::ffff:<ip>.  Because the destination is v4-mapped, the kernel still
            # emits IPv4/UDP on the wire — identical bytes to an AF_INET send — so
            # this only changes the *socket* the camera sees, the last client-side
            # variable not yet pinned to native.  Fall back to AF_INET where IPv6
            # is unavailable (the v4-mapped wire bytes are the same either way).
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
            # S46 (disasm-proven): the camera's nO builder `_IOTC_Send_Search_R`
            # copies our probe's {R, fingerprint} verbatim to nO plaintext[188:196],
            # so `nO_recover_R(raw) = inv_transcode(raw)[188:190]` is exact and
            # MID-INDEPENDENT (verified == R on ~15 captures across 3 client MIDs).
            # This replaces the old `header_R_for_nO` xor-table heuristic, which was
            # tuned to this VM's fingerprint and returned None on a changed MAC (the
            # S23 portability bug — harmless then because we still proceed with our
            # own R, but the clean echo removes the fragility AND the 64K-table cost).
            # We abort only on a recovered R that POSITIVELY DISAGREES with ours.
            R_echo = nO_recover_R(nO_raw)
            if R_echo is not None and R_echo != R:
                s.close()
                self._sock = None   # M2: never leave a closed fd in _sock on a failed attempt
                continue
            self._vlog(f"[connect] nO received ({len(nO_raw)}B) from {cam[0]}:{cam[1]}  "
                       f"R echo {'ok' if R_echo == R else 'absent'}")

            # Pre-build av-connects (header R == probe R) so the ACK->av gap is
            # bounded by the network, not by Python.
            # S38/S44: ONE shared random token base for all channels (native
            # rule, disasm + 4 live sessions).  build_av_connect derives each
            # channel's [48:52] = base + (channel%2) as a full 32-bit LE value
            # ([49:52] constant bar carry).  Native's base is tutk_platform_rand
            # == glibc rand() (31-bit) made nonzero by a retry loop, so match
            # that range (bit31 clear, nonzero) rather than a raw 32-bit urandom.
            # S62: build only the requested channels (default range(40)).  Native
            # sends ch0,ch1 BEFORE the 0x2041 grant and the rest after; we mirror
            # that split — the first two of self._channels go pre-grant, the rest
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
            # S68 diagnostic, TESTED-NEGATIVE (env-gated, default OFF -> wire
            # byte-identical): inject native's IOTC 0x0402 device-identity query at
            # its faithful timing (after ACK + pre-grant channels, ~0.1s). REFUTED as
            # the arming trigger (see build_lan_query header). Leave OFF for normal use.
            if os.environ.get('CUBOAI_INJECT_LANQUERY'):
                s.sendto(build_lan_query(self.uid, R), cam)
                self._vlog("[connect] S68: injected IOTC 0x0402 LAN device-query")
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
                # T1-B (S62): full grant field decode (live). [29]=0x21 = arming
                # subtype echoed back; [52]=0 = auth PASS (3 = wrong pw, S43).
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
                # ADDITIVE (playback, off live path): stash the discovery nonce + av-connect
                # token so a caller can open a FRESH av channel later (e.g. the camera-assigned
                # DVR playback channel from SMsgAVIoctrlPlayRecordResp.result). Storage only —
                # no wire bytes change, live path unaffected.
                self._nO = nO
                self._nO_raw = nO_raw
                self._av_token = tok
                self._seq = max(self._channels) + 1   # av-connect [6:8]==channel
                self._relseq = 0         # IOCTL-phase reliable counter starts at 0 (native)
                self._frmno = 0
                self._ack_ord = 0
                self._data_ack = 0
                self._cam_msgs = set()
                self._got_first = False
                self._frag_D = None
                self._frag_C = 0xFFFF
                self._frag_edge = None            # S54 gap-tracking reset
                self._frag_edge_acked = 0xFFFF
                self._frag_received = set()
                self._frag_gap_ts = {}
                self._hole_req_ts = {}             # S87 retransmit-request schedule reset
                self._hole_first_req = {}          # S88 adaptive-RTT: clear pending latency samples
                # NB: keep _rtt_ewma/_rtt_n across a reconnect — the link's latency does not
                # change when the session restarts, so the learned interval stays valid.
                self._ts_ref = None               # S82: re-anchor the ACK timestamp clock
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
    _SACK_MAX_FID = 631         # S82: full-fidelity SACK list cap = native's own loop bound
                                # (`_sendAVIOFrameACK`@0xe896b stops at frame_len 0x4ff =>
                                # 50+2*631).  Bounds a pathological OOO gap's frame size.
    _GAP_STALE = 0.05           # S58 (was 0.35): forward-skip a stale gap fast so the held-C
                                # edge stays close to high-water and the camera's window never
                                # fills enough to dead-stall the stream under loss.
    _GAP_DEPTH_CAP = 50         # S58: if the held-C edge ever falls >this many frags behind
                                # high-water, jump it to high-water-10 at once (hard backstop
                                # against a deep stall when many holes pile up under heavy loss).
    _NAK_INTERVAL = 0.19        # S54/S72 DEFAULT (now superseded per-instance by
                                # self._nak_interval, set from full_fidelity in __init__).
                                # 0x0b/0x0a resend-control cadence. _send_nak emits a
                                # PAIR (0x0b+0x0a = 2 frames/fire), so wire NAK rate = 2/interval.
                                # S82 RE-MEASURE (s72_native/s65_udp steady 5s bins): native =
                                # ~4.6-4.8 PAIRS/s (0x0a≈4.3-4.7/s + 0x0b≈4.6-4.8/s = ~9.1-9.6
                                # FRAMES/s). 0.19 -> ~4.8 pairs/s == native (S72-verified). The
                                # S82 brief's 0.137 was REJECTED: it gives ~7.3 pairs/s ≈ 1.5x
                                # native (anti-parity); kept only on the non-fidelity fast path.
                                # Arming-INVARIANT (S59/S62/S77) — fidelity only.
    _RESEND_TIMEOUT_MS = 35     # S54: 0x0a [38:40] EWMA resend-timeout (S48 live range 27-50)
    _RESEND_REQ_INTERVAL = 0.15 # S87: min gap before re-listing the SAME hole in the SACK
                                # (≈ camera resend latency ~146ms) so each hole is requested
                                # ~once per resend round -> native-like 1.1x redundancy.
                                # S88: this is the STATIC baseline; when _adaptive_rtt is on it
                                # is recomputed per-instance from the measured resend-latency
                                # EWMA (see _resend_req_interval / _rtt_floor / _rtt_ceil / _rtt_k).
    # ── S88: adaptive resend-request interval (gated CUBOAI_ADAPTIVE_RTT) ───────────
    # T1 (S88, pcap characterization /tmp/s88_t1_charlat.py) found the camera's resend
    # latency is BIMODAL: a fast SACK-honored / reorder cluster (~15-25 ms median) + a slow
    # intrinsic-timer cluster at ~125-145 ms (p90; matches native's 137-146 ms). The live
    # EWMA of the first-request->arrival latency is dominated by the slow cluster and
    # MEASURED ~135 ms on the LAN (S88-T3 B_adapt_lan) — so K*EWMA ≈ 0.20 s, just ABOVE the
    # camera's intrinsic resend latency, which is the correct place for the interval. On a
    # high-RTT link the EWMA climbs with ~2x the one-way delay (S88-T4: 261/346/788 ms at
    # +50/+150/+300 ms) and the interval tracks it (0.39/0.52/0.60-ceil).
    #   * FLOOR is a safety net that rarely binds (LAN interval is ~0.20 s > floor); it only
    #     matters if reorder-false-holes ever pull the EWMA below ~0.10 s, preventing a flood.
    #   * S88-T4 VERDICT: adaptive does exactly what it claims — it holds the per-hole SACK
    #     REQUEST COUNT near 1 across the RTT sweep (fixed climbs to ~2.0; adaptive ~1.1) and
    #     is no-regression on the LAN (T3). BUT it does NOT collapse the high-RTT REDUNDANCY:
    #     that is ~2.1x for BOTH fixed and adaptive at every tested delay (even +50 ms),
    #     because the dominant high-RTT resend driver is the CAMERA's own resend timer
    #     (~140 ms) firing for in-flight frags before pure's RTT-delayed una-advance ACK
    #     confirms delivery — NOT pure re-requesting holes. The re-request interval shaves
    #     only ~2% off redundancy. So this knob buys native-faithful request cadence at high
    #     RTT, not a redundancy fix; the redundancy lever is the ACK/una-advance vs camera
    #     resend-timer race (camera ignores pure's RTO [38:40], S48 — separate work). Video
    #     health (~9 v/s) is equal fixed-vs-adaptive at every delay. Default OFF (decision
    #     deferred); set CUBOAI_ADAPTIVE_RTT=1 to enable.
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
        all-zero [58:64]. Proven against the native status->video capture (session 24):
        exactly the [58:64]==0 frames bear the IO message-indices 0..N and every AV
        fragment — including continuation fragments whose payload is not a start code —
        has a non-zero id. This is the discriminator that splits the two ack channels.
        """
        return len(dec) >= 64 and dec[58:64] == b"\x00\x00\x00\x00\x00\x00"

    def _note_cam_data(self, dec):
        """Record a camera DATA frame and advance the windowed-ACK state.

        The camera runs TWO reliable streams, acked by two different fields, so we route
        each frame by `_is_io_frame` (session-24):
        * Reliable IO/control responses ([58:64]==0): advance `_data_ack` = highest
          contiguous IO message-index ([56:58]). Reported in the ACK's [40:44]; this is
          what frees the camera's IO send-FIFO so it proceeds from a status read to
          video. AV fragment-seq state is left untouched (native keeps C/D idle while
          serving IOCTLs).
        * AV fragments ([58:64]!=0): advance the **C/D pair** from the fragment-seq
          [46:48] (session-14): D = highest fragment-seq seen (gaps skipped), C = the
          previous ACK's D. A real, advancing D keeps the camera's AV send-window open.
        """
        if len(dec) < 68:
            return
        if self._is_io_frame(dec):
            idx = struct.unpack("<H", dec[56:58])[0]
            if idx == 0:
                self._got_first = True
            if self._dataack_wrap:
                # wrap-safe: store the idx lifted into _data_ack's unbounded space, advance the
                # contiguous edge, and drop consumed entries (bounds the set + prevents a stale
                # wrapped value from false-advancing a later epoch). Pre-wrap this is identical to
                # the else-branch (_unwrap_index == raw idx, no discard-driven divergence in the
                # value of _data_ack, which depends only on contiguity).
                self._cam_msgs.add(_unwrap_index(idx, self._data_ack))
                while (self._data_ack + 1) in self._cam_msgs:
                    self._data_ack += 1
                    self._cam_msgs.discard(self._data_ack)
            else:
                self._cam_msgs.add(idx)
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
        # S54 gap-tracking: maintain the CONTIGUOUS low-water edge separately from the
        # high-water D so the 0x0a NAK can name a missing range. PURE BOOKKEEPING — it
        # does NOT change the wire C/D above (default streaming stays byte-identical to
        # native); it only feeds the gated resend_mode (held-D ack + _send_nak). Runs in
        # the single _av_reader thread (no lock needed). The edge advances over received
        # frags but never past D (high-water), so it holds at a genuine gap.
        # S88 adaptive-RTT: if this frag was an outstanding SACK-requested hole, fold its
        # first-request->arrival latency into the resend-latency EWMA (no-op when adaptive
        # is off — _hole_first_req stays empty). Before .add so a duplicate resend of an
        # already-received frag can't double-count (the anchor is popped on first arrival).
        if (self._adaptive_rtt or self._grace_scale) and frag not in self._frag_received:
            self._record_resend_latency(frag)   # FIX#5 also feeds the grace EWMA
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
        """S82: native's word-swapped 32-bit ACK timestamp value V = now_ms - reference.

        build_data_ack packs it [48:50]=high16(V), [50:52]=low16(V) (= wordswap(V)).  The
        reference is captured once per session (lazily, on first ACK) as
        `now_ms - 0x1A220000` so that high16(V) starts at native's empirical constant
        0x1a22 (s72_native/s65_udp count==0 ACKs hold [48:50]=0x1a22 for the whole short
        session, ticking to 0x1a23 only after low16 wraps ~every 65 s) and low16(V) climbs
        ~1 ms/ms from ~0.  The lib (`_sendAVIOFrameACK`@0xe86c0) derives now_ms from
        gettimeofday and subtracts a multi-day-scale reference; native re-anchors that
        reference periodically (the ~900-3000 ms backward "reset" jumps in [50:52]) but the
        exact cadence is NOT statically pinnable (S74), so pure uses one stable reference —
        the dominant structure (word-swap, const-ish high word, ~1 ms/ms low word) matches
        and the field is camera-IGNORED (S17/S74/S77), so this is byte-fidelity only."""
        now_ms = int(time.time() * 1000)
        if self._ts_ref is None:
            self._ts_ref = (now_ms - 0x1A220000) & 0xFFFFFFFF
        return (now_ms - self._ts_ref) & 0xFFFFFFFF

    def _compute_sack(self, D_wire):
        """S62/S63: the per-fragment SACK list for build_data_ack's [42:44]/[50:].

        Returns the ABSOLUTE camera fragment-seqs the receiver holds out-of-order —
        those received strictly above the acked edge (wire D) up to the high-water
        `_frag_D` — matching native's ctx+0x2050 OOO FIFO.  build_data_ack encodes each
        as its 2-byte (frag_seq - C) wire offset (S63: the -C lives in build_data_ack so
        it owns the full wire encoding; byte-identical to S62's relative-offset list).
        Returns None when there is no gap (edge == high-water) => build_data_ack emits
        the byte-identical 52B ACK.

        *** SESSION-82 (full_fidelity) ***: native lists EVERY out-of-order fragment it
        holds above the acked edge (the ctx+0x2050 OOO FIFO walked whole in
        `_sendAVIOFrameACK`@0xe886f), giving frame_len = 50+2N for N held frags.  Under
        full_fidelity pure emits that FULL list (every received frag strictly above the
        wire edge up to high-water, ascending), capped only at the lib's own loop bound
        _SACK_MAX_FID — NO _FRAG_WINDOW truncation (a junk high-water still can't inflate
        the list, since only genuinely-received frags are listed).  When full_fidelity is
        off, the pre-S82 best-effort path is used: a _FRAG_WINDOW-bounded dense run, and
        spans beyond the window drop the list entirely.  (S63 caveat: native's real
        entries under heavy loss are STRIDED with wrapped tail values; pure's are the
        genuinely-received set — efficiency-only, camera-IGNORED for arming, S60/S61.)

        Only called from the resend_mode branch of _send_ack: holding the cumulative
        ack at the edge is what makes a SACK list coherent (the camera still has the
        frags above it).  In best-effort mode (D = high-water) there is nothing above
        D to SACK, and pure forgives gaps via the advancing D — so no list is sent.
        *** SUPERSEDED (S87): this RECEIVED-frag list is the LEGACY non-selective path.
        The S60/S61 "efficiency-only / does NOT gate resend" reading was WRONG — the SACK
        is a RESEND-REQUEST (MISSING) list and DOES gate resend; the live path is
        _compute_holes (selective_ack, default ON). _compute_sack is used only when
        CUBOAI_SELECTIVE_ACK=0 (held-edge mode). ***
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
        # best-effort (pre-S82) path: window-bounded dense run
        if span > self._FRAG_WINDOW:
            return None
        sack = [f
                for f in ((D_wire + k) & 0xFFFF for k in range(1, span + 1))
                if f in self._frag_received]
        return sack or None

    def _resend_req_interval(self):
        """S88: the live per-hole re-request interval.

        Static path (default, _adaptive_rtt off): the S87 constant _RESEND_REQ_INTERVAL
        (0.15 s) — byte-for-byte the validated LAN behaviour.

        Adaptive path (CUBOAI_ADAPTIVE_RTT=1): clamp(K * EWMA(resend latency), floor, ceil).
        The EWMA tracks the first-request->arrival latency of recovered holes (fed in
        _record_resend_latency). MEASURED: ~135 ms on the LAN (=> interval ~0.20 s, just above
        the camera's intrinsic resend latency, no LAN regression) climbing to 261/346/788 ms at
        +50/+150/+300 ms RTT (=> interval 0.39/0.52/0.60-ceil), which holds the per-hole request
        count near 1 across the sweep (S88-T4). Until _RTT_MIN_SAMPLES clean samples are in,
        fall back to the static constant."""
        if not self._adaptive_rtt or self._rtt_ewma is None or self._rtt_n < self._RTT_MIN_SAMPLES:
            return self._RESEND_REQ_INTERVAL
        target = self._RTT_K * self._rtt_ewma
        return min(self._RTT_CEIL, max(self._RTT_FLOOR, target))

    def _record_resend_latency(self, frag):
        """S88: a requested hole `frag` just arrived — fold its first-request->arrival
        latency into the resend-latency EWMA (the adaptive interval's only signal).

        Sample = now - the time the hole was FIRST SACK-listed (re-requests do NOT reset
        that anchor, so a re-asked hole still yields one clean first-request->first-arrival
        sample, per the T2 brief). Runs on the reader thread inside _note_cam_data, same
        thread as _send_ack, so no lock is needed. Cheap and harmless when adaptive is off,
        but only armed when _adaptive_rtt is set (the anchor dict is populated in _send_ack)."""
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
        """S87: the MISSING fragment-seqs (HOLES) in (C, high-water] — frags the camera sent
        (frag-seq <= high-water, so we know they exist) that we did NOT receive.  *** This is
        the SACK the camera actually acts on: the drop-independent S87 test proved the camera
        RE-SENDS the frags listed in the SACK (native's listed frags are resent 95% and native
        recovers 82% of its real losses).  The earlier _compute_sack listed RECEIVED frags, so
        the camera wasted resends on already-delivered frags and recovered ~0% of true losses
        — the SACK is a RESEND-REQUEST (missing) list, NOT a received list.  ***  Called with
        C = the una (contiguous edge) so every hole is above C and build_data_ack's (hole − C)
        offset is positive; the caller dedups by _RESEND_REQ_INTERVAL and only emits when ≥2
        holes are fresh (the camera ignores a count-1 entry).  Capped at _SACK_MAX_FID."""
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

        Carries the C/D cumulative-ACK (session-14). Before any camera DATA frame has
        been seen, sends the idle sentinel C==D==0xFFFF, matching native.
        """
        sack = None
        if self._frag_D is None:
            C = D = 0xFFFF
        elif self._resend_mode and self._frag_edge is not None:
            # S52-HN held-D: ack only the CONTIGUOUS edge so the camera sees us stalled at
            # the gap; the 0x0a NAK (highwater=high-water) names the high edge -> targeted
            # resend (PROVEN: S52 RETX 91 @313 ms on an armed session). DEVIATES from
            # native's data-ack (D=high-water) and would stall an UNARMED stream on any
            # unrecovered loss (the W200/W400 stall, S53) — OFF by default; enable only
            # once session-birth ARMING is solved.
            if self._selective_ack:
                # S87 native selective REPEAT — the SACK is a RESEND-REQUEST (missing) list:
                # the camera resends exactly the frag-seqs it carries (drop-independent proof:
                # native's listed frags are resent 95% → native recovers 82% of real losses).
                # C = una (contiguous edge) so every outstanding hole is ABOVE C and its
                # (hole−C) offset is positive; D = high-water; SACK = outstanding holes in
                # (una, high-water] NOT requested within _RESEND_REQ_INTERVAL. The camera only
                # honours entries when count≥2 (a count-1 frame's [50:52] is read as the
                # timestamp, like native's), so a lone hole waits for a second. The per-hole
                # request-timer lists each hole ~once per resend round → native-like 1.10×
                # redundancy, and re-asks a still-missing hole next round (covers a lost
                # resend). (Earlier impls failed: C=una + RECEIVED list → camera resent
                # already-delivered frags, 0% recovery; rolling-C + holes → lone holes landed
                # in count-1 frames the camera ignored, 8% recovery.)
                C = self._frag_edge if self._frag_edge is not None else self._frag_C
                D = self._frag_D
                if (self._una_lag and D is not None and C != 0xFFFF
                        and ((D - C) & 0xFFFF) < self._una_lag):
                    C = (D - self._una_lag) & 0xFFFF   # C2: lag reported una -> camera keeps buffer deep
                now = time.time()
                holes = self._compute_holes(C) or []
                req_interval = self._resend_req_interval()    # S88: static 0.15s or adaptive
                fresh = [h for h in holes
                         if now - self._hole_req_ts.get(h, 0.0) > req_interval]
                if len(fresh) >= 2:
                    for h in fresh:
                        if h not in self._hole_req_ts:        # stats: first request of this hole
                            self._stat_holes += 1
                        self._stat_resend_req += 1            # stats: a resend request sent
                        self._hole_req_ts[h] = now
                        # S88 adaptive-RTT: anchor the FIRST request time per hole (never
                        # overwritten while outstanding) so _record_resend_latency yields a
                        # clean first-request->arrival sample. No-op when adaptive is off.
                        if self._adaptive_rtt or self._grace_scale:   # FIX#5 also anchors for grace EWMA
                            self._hole_first_req.setdefault(h, now)
                        if self._lone_hole:
                            self._hole_req_count[h] = self._hole_req_count.get(h, 0) + 1
                    sack = fresh[:self._SACK_MAX_FID]
                elif len(fresh) == 1 and self._lone_hole:
                    # S90 lone-hole fix: pad the single hole to count>=2 so the camera honours
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
                        # frag); "plus1" adds h+1 (T1 probe alternative).
                        pad = h if self._lone_pad == "dup" else (h + 1) & 0xFFFF
                        sack = [h, pad]
                else:
                    sack = None
                if len(self._hole_req_ts) > 2048:   # prune filled holes (bound memory)
                    self._hole_req_ts = {h: t for h, t in self._hole_req_ts.items()
                                         if h not in self._frag_received}
                # S88: bound _hole_first_req too — a hole that is truly lost (never arrives)
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
                sack = self._compute_sack(D)      # S62/S63: OOO frag seqs above edge (abs; encoded frag-C)
        else:
            C, D = self._frag_C, self._frag_D     # native-faithful: D=high-water, C=prev-D
        # S82: under full_fidelity emit native's word-swapped 32-bit ACK timestamp
        # (ts32); else the pre-S82 const-0x1a22 + free-running-ms low word.
        ts32 = self._ts_word() if self._full_fidelity else None
        self._sock.sendto(
            build_data_ack(self._R, self._seq, self._relseq,
                           self._ack_ord, C, D, self._data_ack, sack=sack, ts32=ts32,
                           win=self._advertise_window),
            self._cam)
        self._stat_tx_ack += 1                 # outbound visibility (audit 2026-07-23)
        self._seq += 1
        self._relseq += 1
        self._ack_ord += 1

    def _send_nak(self):
        """Emit the 0x0b/0x0a resend-control pair (S54). The 0x0a carries
        highwater=high-water (highest frag-seq seen) + the S48 EWMA resend-timeout;
        paired with the held-D ack (resend_mode) it asks the camera to resend the gap
        [edge+1 .. high-water]. This is the H->HN delta S52 proved collapses resend
        latency 1348->313 ms (targeted, min 33 ms) on an armed session; a no-op on an
        unarmed pure-born session (camera ignores it — S51/S53). Consumes seqs like
        native (0x0b: no relseq; 0x0a: +1 relseq)."""
        # S86: echo the camera's ms-clock (captured from cam->host 0x0a) in our 0x0b
        # [36:38], advanced by elapsed wall time — THE arming discriminator. Native does
        # exactly this; the camera gates AV-retransmit commitment on it. Falls back to
        # `now` until the first camera 0x0a is seen (harmless; camera sends them ~t=0.07s).
        ts_b = None
        if self._echo_cam_clock and self._cam_clock is not None:
            ts_b = (self._cam_clock + int((time.time() - self._cam_clock_ts) * 1000)) & 0xFFFF
        self._sock.sendto(build_resend_b(self._R, self._seq, 8, ts=ts_b), self._cam)
        self._seq += 1
        # S87: in selective_ack (native) mode the 0x0a carries highwater=0 (native's
        # steady-state telemetry value) — the SACK in the 0x09 ACK drives resends, so an
        # explicit highwater=high-water here would RE-REQUEST the whole (.., high-water]
        # range every _nak_interval ON TOP of the SACK and flood the camera with duplicate
        # resends (S87-T5: RETX 53-62% vs native 8%). The held-edge mode keeps the old
        # highwater=high-water (it has no SACK-driven resend and needs the explicit ask).
        nak_hw = 0 if self._selective_ack else (self._frag_D or 0)
        self._sock.sendto(
            build_resend_req(self._R, self._seq, self._relseq,
                             highwater=nak_hw,
                             resend_timeout_ms=self._RESEND_TIMEOUT_MS,
                             win=self._advertise_window),
            self._cam)
        self._stat_tx_nak += 1                 # outbound visibility (audit 2026-07-23)
        self._seq += 1
        self._relseq += 1

    def _send_video_start_mid(self):
        """S81: emit the deferred 0x0300 (AUDIOSTART / stream-start) IOCTL.

        Native sends 0x0300 ~5 s after 0x00FF rather than at connect time (s72_native
        wire: 0x0300 @5.13s, reliable-seq 68). Pure defers it to here, called exactly
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
        """S71: emit the deferred 0x01FF video-start IOCTL (see _VIDEO_START_LATE).

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

        Multiple IOCTLs run on ONE connection (session-12 windowed-ACK fix): our
        ACK frames carry [40:44] = the highest contiguous camera DATA message-index
        received, which advances the camera's response send-window so it serves
        request after request. A stalled call triggers one reconnect-and-retry.

        BOUNDED RECONNECT (audit 2026-07-23, CUBOAI_IOCTL_FAST_RECONNECT, **default OFF**).
        Caps this path's connect() to (attempts=3, timeout=1.5). It exists because the
        send-side work order expected the legacy call to cost "up to ~64 s per reconnect
        (attempts=8 x timeout=8.0), ~3 min across the retry loop". **That premise is
        REFUTED**: connect()'s `deadline` is computed ONCE before the attempt loop, so
        `timeout` is a TOTAL budget SHARED across `attempts`, not per-attempt — the legacy
        worst case is ~8 s per reconnect, and the measured mean at 40% egress loss was
        5.1 s (585 s / 115 reconnects). Recovery was never "minutes".
        Worse, the live A/B did not show this helping. The real pathology at 40% loss is
        not slow reconnects but ENDLESS CHURN — 115 reconnects of which 113 FAILED — and a
        shorter budget makes the loop churn FASTER, not succeed more; it can only reduce
        the handshake's chances. So this ships OFF and unproven: `=1` opts in. Do not flip
        the default without an A/B where the ON arm actually establishes a session
        (the 40%-loss attempt's ON arm never got past its initial connect).

        Every recovery here is COUNTED regardless of the flag (_stat_ioctl_retry /
        _stat_reconnects / _stat_reconnect_s, surfaced by get_stats) — that
        instrumentation is what exposed the 115/113 churn, which previously just looked
        like slow GETs. A retry that succeeds is not evidence
        the underlying operation is sound — on Linux this very path silently masked the
        ghost-conn bug for weeks, so it must never look free.

        CONTRACT (B3-latent, audit 2026-07-24): while a stream is running, `_av_reader` is the
        SOLE socket sender. Calling this ioctl() DIRECTLY mid-stream would add a second sender
        that races seq/relseq/frmno and double-drains recvfrom — corrupting the AV stream. The
        production streamer NEVER SETs mid-stream, so this cannot happen today, which is why there
        is deliberately NO runtime assert here (an assert could only ever be discovered wrong in
        production). Any future mid-stream GET/SET MUST go through get_during_stream()/the reader
        thread, not this method. This note IS the guard.
        """
        last_err = None
        _fast = self._ioctl_fast_reconnect
        _cargs = dict(attempts=3, timeout=1.5) if _fast else {}
        for attempt in range(3):
            if attempt:
                self._stat_ioctl_retry += 1
            if self._sock is None or self.session_hdr is None:
                self.disconnect()
                self._stat_reconnects += 1      # count BEFORE: connect() may raise, and a
                _t0 = time.time()              # reconnect that blew up still happened
                try:
                    _ok = self.connect(**_cargs)
                finally:
                    self._stat_reconnect_s += time.time() - _t0
                if not _ok:
                    self._stat_reconnect_fail += 1
                    last_err = "handshake failed (no 0x2041)"
                    time.sleep(0.2)
                    continue
            try:
                return self._ioctl_once(type_code, payload, timeout=3.0)
            except TimeoutError as e:
                self._stat_ioctl_timeout += 1
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
        self._stat_tx_ioctl += 1
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
                # MASKED-FAILURE VISIBILITY (audit 2026-07-23): this retransmit is the loop that
                # makes SETs loss-resilient — and it is also why a lossy link looks healthy from
                # the outside. Count it so "recovered N times" is visible instead of silent.
                self._stat_tx_ioctl += 1
                self._stat_tx_ioctl_retx += 1
                last_tx = now
            if not r:
                continue
            try:
                raw, addr = s.recvfrom(4096)
            except BlockingIOError:
                continue
            if len(raw) < 16:
                continue
            if is_keepalive_probe(raw):                 # answer liveness probe (session-31)
                self._session_fp = xor_frame(raw)[16:20]  # camera's echoed session token (S49)
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
    # new GETs (2026-05-31) — all confirmed responding on fw 3.0.1369
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
    # second 2026-05-31 batch (APK-DEX-extracted, live-confirmed on fw 3.0.1369)
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
    # UNDOCUMENTED endpoints (discovered 2026-06-09; not used by the native SDK/app —
    # see CAMERA_CAPABILITY_PROBE.md). get_session_stats = the camera's own per-session
    # stream telemetry (frame/keyframe counts, resendBufferUsage, send errors, mode/NAT).
    def get_session_stats(self):           return self._cubo_get('get_session_stats')
    def get_user_list(self):               return self._cubo_get('get_user_list')

    # ── read-only stats snapshot ──────────────────────────────────────────────
    def get_stats(self):
        """Cumulative read-only snapshot of the transport/decode counters.

        The SINGLE source of truth for diagnostics: --benchmark and the streamer's
        verbose mode both read this (and pair successive snapshots through the module
        helper stats_delta() for per-interval fps/bitrate/loss/recovery). No socket
        I/O and no locks — the reader thread is the only writer and every field is an
        atomic int, so this is safe to call from any thread while streaming.

        Counters cover fragments (received/lost/loss%), resend requests (sent/honoured),
        recovery (clean-fill events), the decode band (incomplete/keyframe-incomplete
        AUs), frames + bytes (for fps/bitrate), gaps over the depth cap, and PTS health
        (garbage-timestamp share + camera-clock monotonic regressions).
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
            # fragments / wire loss
            'frags_recv': recv,
            'frags_lost': holes,
            'loss_pct': round(100.0 * holes / total, 3) if total else 0.0,
            # resend / recovery
            'resend_req': req,
            'resend_recovered': rec,
            'recovery_pct': round(100.0 * rec / holes, 1) if holes else 100.0,
            'recovery_events': rec,
            # decode band
            'au_video': self._stat_au_video,
            'au_audio': self._stat_au_audio,
            'au_incomplete': self._stat_au_incomplete,
            'kf_total': self._stat_kf_total,
            'kf_incomplete': self._stat_kf_incomplete,
            'bytes_video': self._stat_bytes_video,
            'bytes_audio': self._stat_bytes_audio,
            # gaps / recovery mechanics
            'gap_now': gap_now,
            'gap_max': self._stat_gap_max,
            'gap_cap_jumps': self._stat_gap_cap_jumps,
            'lone_skips': self._stat_lone_skips,
            'keepalive_err': self._stat_keepalive_err,   # L1
            # ── OUTBOUND (audit 2026-07-23): everything above this line is RECEIVE-side.
            # A rig that can only see one side has produced every wrong verdict in this
            # project, so the send side is counted too: frames we emit, retransmits we
            # issue, and sendto failures (a wedged socket used to be completely invisible).
            'tx_ack': self._stat_tx_ack,
            'tx_nak': self._stat_tx_nak,
            'tx_ioctl': self._stat_tx_ioctl,
            'tx_ioctl_retx': self._stat_tx_ioctl_retx,
            'tx_keepalive': self._stat_tx_keepalive,
            'tx_err': self._stat_tx_err,
            'talk_resend': self._stat_talk_resend,
            # ── SILENT RECOVERIES: a retry that succeeds is not evidence of health. These
            # make a masked fault read as "recovered N times" instead of looking clean.
            'ioctl_timeouts': self._stat_ioctl_timeout,
            'ioctl_retries': self._stat_ioctl_retry,
            'reconnects': self._stat_reconnects,
            'reconnect_fail': self._stat_reconnect_fail,
            'reconnect_s': round(self._stat_reconnect_s, 2),
            # PTS health
            'ts_valid': self._stat_ts_valid,
            'ts_garbage': self._stat_ts_garbage,
            'ts_regress': self._stat_ts_regress,
            # RTT / context
            'rtt_ewma_ms': round(self._rtt_ewma * 1000.0, 1) if self._rtt_ewma else None,
            'rtt_samples': self._rtt_n,
            'selective_ack': self._selective_ack,
        }

    def get_during_stream(self, name, timeout=2.5):
        """Read a GET_METHODS endpoint safely while a stream is running.

        During a stream `_av_reader` is the sole socket sender, so a direct ioctl()
        would race it. This hands the request to the reader (which sends it and captures
        the response) and blocks for the parsed dict — used by --benchmark / verbose to
        poll the camera's WiFi signal + 0x0934 session-stats at a modest cadence without
        perturbing the AV channel. With no reader running it falls back to a direct
        ioctl(). Returns the parsed dict, or None on timeout. Read-only (GET) only.
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
    # Wire-fidelity to native (confirmed-NOT-the-arming-gate, S38/S67/S72): native
    # does NOT fire all three at connect time. It spaces them ~5 s apart, in order
    # 0x00FF -> 0x0300 -> 0x01FF (s72_native wire: 0x00ff@0.08s -> 0x0300@5.13s =
    # stream-start -> 0x01ff@10.16s; S67: first AV fragment arrives just AFTER
    # 0x0300, BEFORE 0x01FF). 0x0300 is what actually starts the stream (handoff
    # S67: "0x00ff then 0x0300 -> first AV fragment"); 0x01FF only follows once
    # video flows.
    #
    # S81 (match native's cadence EXACTLY): originally (S71) pure sent 0x00FF+0x0300
    # up front and deferred only 0x01FF. That made pure's 0x0300 land at reliable-seq
    # 1 (t~0.06s) vs native's 68 (t~5.1s) — see S80/S81; harmless to arming but a
    # wire-fidelity gap, and it started video ~5 s sooner than native. The deferral is
    # now controlled by two INDEPENDENT, default-True flags (constructor args, see
    # __init__) so the cadence matches native by default yet a fast path survives:
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
    # camera responds (~0.5-2 s, camera-bound), and sub-5 s streams work, as before S81.
    # Arming rides the type-0x0b camera-clock echo (S86, _echo_cam_clock, default ON),
    # not the IOCTL cadence, so these flags change ONLY fidelity/latency.
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
        VPS+SPS+PPS+IDR starting `00000001 40` (NAL type 32 = VPS) — verified against
        native (/tmp/native_video.h265, keyframe ~79.6 KB: VPS@0 SPS@28 PPS@77 IDR@88).
        On older Gen1/Gen2 (H.264) it starts `00000001 67` (SPS, NAL type 7). The
        keyframe is detected codec-agnostically; convert to JPEG downstream with PyAV.

        Drains the AV stream (which starts video and runs the C/D windowed-ACK reader)
        and returns as soon as the first complete keyframe access unit arrives.
        """
        for kind, unit, _fi in self._read_av_units(timeout=timeout_sec):
            if kind == 'video' and _is_video_keyframe(unit, detect_video_codec(unit)):
                return unit
        raise TimeoutError(
            "no video keyframe within %.0fs" % timeout_sec)

    # ── continuous AV streaming (session 14: background reader + C/D windowed-ACK) ──
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
        # ── ACK rate-limiting — match native's ~10 ACKs/s (session-20) ──
        # We pace ACKs to native's cadence instead of the old per-datagram ~113/s: still
        # call _note_cam_data on EVERY datagram (C/D state stays exact), but gate the
        # actual _send_ack to at most one per ACK_INTERVAL, and only when D has advanced
        # since the last ACK we sent (an idle ack still goes out periodically before any
        # data, to prompt the stream, matching native's pre-video idle ACKs). Live this
        # holds ~8/s — within native's range, vs 113/s before.
        #
        # SESSION-20 — the last open wire delta (S19 Result 4: "pure over-ACKs ~10x")
        # was tested as a possible retransmit/decode lever. RESULT: it is NOT one.
        #   * Retransmit (loss-injection oracle, drop 1/16 inbound frags): rate-limited
        #     retx = 2,2 (one 7 outlier) vs over-acking retx = 2,3 — statistically
        #     identical noise floor at the time.
        #   * Decode rate: rate-limited 76/50/48/60/75% vs over-acking 75/36/55/48% —
        #     indistinguishable; both swing across the known 55-90% band, dominated by
        #     scene activity + natural UDP-loss timing, NOT by the ACK rate.
        # So pacing buys wire-fidelity to native (and ~14x less ACK traffic). ACK *rate*
        # is correctly NOT the retransmit lever — but *** S86/S87 later found the lever is
        # ACK CONTENT + arming ***: the type-0x0b camera-clock echo arms the camera, and
        # the type-0x09 ACK's SACK missing-list drives resends. With both (default ON) pure
        # recovers ~76-84 % of losses. The S16-S19 "blocker is session-state" reading was
        # superseded; resend is live (see the S86/S87 module note above + _send_ack).
        # Proper retransmit IS reachable from pure (S86/S87, above) and recovers most
        # losses; the residual ~55-90% GOP-decode band is NOT a retransmit-reachability
        # limit but HEVC keyframe-loss exposure (one incomplete keyframe poisons its GOP
        # tail) — not closable client-side (S90: FEC off, resend cap ~2-3). See AV_HANDOFF.
        # S87: in selective_ack mode ACK promptly on every edge-advance (≈25/s cap) so the
        # camera gets fill-confirmation BEFORE its ~116 ms resend timer re-fires — otherwise
        # it resends each hole 3-6× before pure's una-advance lands (S87-T5 over-resend
        # 2.3× vs native 1.1×). Held-edge mode keeps native's ~10/s pacing.
        ACK_INTERVAL = (0.04 if self._selective_ack else 0.10)
        last_acked_D = self._frag_D     # _frag_D value at our most recent ACK
        last_acked_da = self._data_ack  # _data_ack value at our most recent ACK
        last_acked_edge = self._frag_edge  # S58: _frag_edge value at our most recent ACK
        last_nak = time.time()          # S54: last 0x0b/0x0a resend-control send
        t_stream0 = time.time()         # S62 verbose: stream start (for the [stream] t= clock)
        last_vlog = 0.0                 # S62 verbose: last [stream] status line (rate-limit ~1/s)
        nframes = 0                     # S62 verbose: camera AV fragments seen this session
        video_frame_count = 0           # S71: completed VIDEO access units (gates the deferred 0x01FF)
        # S81: cadence-flag-aware init. If a stage is NOT deferred it was already sent
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
            # S58: in resend_mode the wire D IS the contiguous edge, and maybe_nak's
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
            """S54/S58 resend_mode gap signalling: forward-skip a stale gap (so the held-C
            ack's window reopens) and emit the 0x0b/0x0a resend pair at ~8/s. No-op
            unless resend_mode is enabled (default ON since S58 — see _send_ack). S58
            stall guards: a 50ms forward-skip (_GAP_STALE) plus a hard depth cap
            (_GAP_DEPTH_CAP) that jumps the edge near high-water when holes pile up, so
            held-C can no longer dead-stall the stream under heavy loss."""
            nonlocal last_nak
            if not self._resend_mode:
                return
            now = time.time()
            # S71: native emits the 0x0b/0x0a resend-control pair from BEFORE the first
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
                if gap > self._gap_depth_cap and not self._holding_kf:  # S58 backstop; KF-grace holds the una at a kf's holes
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
                # S87: in selective_ack mode DON'T forward-skip a stale gap — the una must
                # HOLD at a genuine hole until its resend FILLS it (the camera resends the
                # holes we SACK, native-style), otherwise the 50ms skip drops the hole out of
                # the (una, high-water] request window before it can be recovered (8-15%
                # recovery vs native 82%). The _gap_depth_cap above is the only backstop in
                # selective mode (jumps the una near high-water if >cap holes pile up so the
                # camera's send-window can never dead-stall). Held-edge mode keeps the skip.
                if not self._selective_ack:
                    nxt = (self._frag_edge + 1) & 0xFFFF
                    t = self._frag_gap_ts.get(nxt)
                    if t is None:
                        self._frag_gap_ts[nxt] = now
                    elif now - t > self._gap_hold:                     # held past resend wait -> skip (S87 knob)
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
            # FIX#5: dynamic reassembly grace = ceil(resend_latency_EWMA * AU_rate) + 1,
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
            # Part A: queue (kind, unit, frameinfo) so the parsed FRAMEINFO travels WITH its AU
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
            au_times.append(time.time())             # FIX#5: feed the live AU-rate
            if len(au_times) > 128:
                del au_times[0]
            ks = sorted(fm)                                       # frag-seqs (hoisted: au_log + FRAMEINFO strip)
            comp = bool(ks) and (ks[-1] - ks[0] + 1) == len(ks)   # contiguous fragments == COMPLETE AU
            # Audio investigation (Phase 2): gated codec/FRAMEINFO census, emitted at the EARLIEST
            # point a full AU exists — before audio-truncation, video-FRAMEINFO-strip, or the
            # consumer's kind filter. One stderr line per AU. Gated (default OFF) => byte-identical.
            if self._log_frameinfo:
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
            _au_fi = None        # Part A: the parsed FRAMEINFO for THIS au (set below when stripped)
            if kind == 'audio':
                fl = _adts_frame_len(unit)
                # S91/Phase-3: surface the audio FRAMEINFO ts (gated CUBOAI_STRIP_FRAMEINFO, like
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
                video_frame_count += 1               # S71: gates the deferred 0x01FF
                # Strip+parse the 24-byte TUTK FRAMEINFO trailer (gated CUBOAI_STRIP_FRAMEINFO).
                # COMPLETE AUs only (`comp`): a complete AU ends with the trailer; an incomplete /
                # TRUNCATE_PARTIAL unit does not. Sanity-check a video codec_id + plausible w/h before
                # cutting so an edge case can't slice into a real NAL; on mismatch skip+log. OFF
                # path byte-identical. The parsed FRAMEINFO travels with this AU (Part A) for PTS.
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
                maybe_nak()                        # S54: resend_mode gap NAK (no-op if off)
                # S81: send the deferred 0x0300 (stream-start) ~_MID_IOCTL_SECS after
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
                # S71/S81: then 0x01FF ~_LATE_IOCTL_SECS AFTER 0x0300 once video flows
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
                        # probe, as native does (session-31). Native replies to every
                        # probe; pure used to drop them here, leaving its session a
                        # silent non-responder to liveness checks. Byte-identical reply;
                        # does NOT affect AV retransmit (tested) — fidelity/liveness only.
                        if is_keepalive_probe(raw):
                            self._session_fp = xor_frame(raw)[16:20]  # echoed token (S49)
                            try:
                                s.sendto(build_keepalive_reply(raw), addr)
                                self._stat_tx_keepalive += 1    # outbound side of keepalive_err
                            except OSError:
                                self._stat_keepalive_err += 1   # L1: observable instead of silent
                                self._stat_tx_err += 1
                        continue
                    dec = inv_transcode(raw)
                    if len(dec) < 40:
                        continue
                    if dec[28] != 0x0C or len(dec) < 68:
                        # S86: capture the camera's ms-clock from cam->host 0x0a [36:38]
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
                        if self._ctrl_log is not None:   # gated diagnostic (default None -> byte-identical)
                            try:
                                _io = struct.unpack_from("<H", dec, 64)[0] if len(dec) >= 66 else -1
                                self._ctrl_log(time.time(), _io, bytes(dec[:min(len(dec), 96)]))
                            except Exception as _e:
                                if not self._ctrl_log_err:    # L2: report the first error, then stay quiet
                                    self._ctrl_log_err = True
                                    print(f"[ctrl_log] callback error (further suppressed): {_e}", file=sys.stderr)
                        gi = self._get_inject       # capture an injected GET's response
                        if (gi is not None and gi.sent and not gi.done.is_set()
                                and len(dec) >= 68
                                and struct.unpack_from("<H", dec, 64)[0] == gi.resp_type):
                            avlen = struct.unpack_from("<H", dec, 52)[0]
                            gi.result = bytes(dec[68:min(len(dec), 68 + max(0, avlen - 4))])
                            gi.done.set()
                        continue
                    idx = raw_idx = struct.unpack("<H", dec[56:58])[0]
                    if self._idx_modular:                 # H1: lift the u16 index into done_upto's
                        idx = _unwrap_index(idx, done_upto)  # space so the gate survives the wrap
                    frag = struct.unpack("<H", dec[46:48])[0]
                    avlen = struct.unpack("<H", dec[52:54])[0]
                    chunk = bytes(dec[64:64 + max(0, avlen)])
                    nframes += 1                       # S62 verbose: camera AV fragment counter
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
                        # SECOND-STREAM dead-read fix (see _idx_seed): this is THIS reader's first
                        # accepted access-unit start. done_upto still holds its -1 initialiser, but
                        # the camera's message-index is session-scoped, so on any read after the
                        # first it is already past the +256 accept window and EVERY fragment would
                        # be rejected from here on. Anchor the window here instead. Fires only when
                        # the index is out of window (never on a fresh-session stream), so the
                        # normal path — and every replay fixture — is byte-identical.
                        if self._idx_seed and not (done_upto < idx <= done_upto + 256):
                            done_upto = raw_idx - 1
                            idx = _unwrap_index(raw_idx, done_upto) if self._idx_modular else raw_idx
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
                            # IN-ORDER never-skip seal (S90 finalize-drop fix): emit done_upto+1
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
                            # -> done_upto jumps -> late frags rejected = the S90 POC-gap bug.
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

        # S81: build the up-front IOCTL batch from the cadence flags. 0x00FF always
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
        # when set, drop incomplete video AUs + the poisoned GOP tail until the next IDR, the
        # same suppression cuboai_stream_video.mux_timed_stream applies to the live stream, so a
        # recorded clip has no decode band under loss. OFF -> _items is the raw timed generator.
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

    def set_verified(self, name, value, readback_timeout=2.5):
        """OPT-IN, SELECTIVE read-back verify for a SET (audit 2026-07-23, task 5).

        A SET that returns a ..._RESP proves the camera ANSWERED, not that it APPLIED the value:
        _ioctl_once retransmits until a response arrives, so the SET is loss-RESILIENT, but
        "accepted but not applied" is a completely different failure from "failed" and nothing in
        the stack could previously tell them apart. This performs the SET, reads the value back
        through the paired GET, and reports the three outcomes DISTINCTLY.

        Deliberately NOT blanket (it costs an extra GET per call, and many IOCTLs are actions with
        no readable state): `name` must be a key of cuboai_messages.SET_READBACK — everything else
        raises rather than pretending to verify. cuboai_messages.SET_READBACK_UNSUPPORTED records
        why each excluded setter has no read-back.

        NEVER touches the AV hot path: the read-back GET goes through get_during_stream(), which
        hands the request to the reader thread when a stream is running (and falls back to a plain
        ioctl when it is not), so verifying a setting mid-stream cannot race the socket.

        Returns a dict; `verified` is the verdict and is None only when the read-back itself could
        not be obtained (never silently True):
          {'name','expected','actual','set_ok','verified','status','confidence','get'}
          status: 'applied'        — SET answered AND the read-back agrees
                  'not_applied'    — SET answered but the read-back DISAGREES  <-- the interesting one
                  'unverified'     — SET answered; the read-back GET failed/returned no field
                  'set_failed'     — the SET itself never got a response (raised)
        """
        import cuboai_messages as cm
        try:
            setter, kwarg, get_name, field, coerce, confidence = cm.SET_READBACK[name]
        except KeyError:
            why = cm.SET_READBACK_UNSUPPORTED.get(f"set_{name}")
            raise ValueError(
                f"'{name}' has no read-back capability"
                + (f" ({why})" if why else "")
                + f"; verifiable: {sorted(cm.SET_READBACK)}")
        out = {'name': name, 'expected': coerce(value), 'actual': None, 'set_ok': False,
               'verified': None, 'status': 'set_failed', 'confidence': confidence, 'get': get_name}
        try:
            getattr(self, setter)(**{kwarg: value})
        except Exception as e:
            out['error'] = repr(e)
            return out
        out['set_ok'] = True
        out['status'] = 'unverified'
        parsed = self.get_during_stream(get_name, timeout=readback_timeout)
        if not isinstance(parsed, dict) or field not in parsed or parsed[field] is None:
            return out                       # read-back unavailable -> verified stays None
        try:
            out['actual'] = coerce(parsed[field])
        except (TypeError, ValueError):
            out['actual'] = parsed[field]
            return out
        out['verified'] = (out['actual'] == out['expected'])
        out['status'] = 'applied' if out['verified'] else 'not_applied'
        return out

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
        cuboai_messages.build_set_lullaby_schedule_entry for the full argument map.

        The 148-byte payload makes a 216-byte frame with an 8-byte partial-block tail
        that carries the duration field; `transcode` applies the TransCodePartial tail
        `Swap` for tail 2/4/8 (as native does), so the duration stores exactly as sent
        (before that fix, a plain tail-XOR mangled it to a ~10-yr default)."""
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
                        warmup=2.5, on_status=None, gain=1.0):
        """Talk: play an audio file out the camera speaker (pure-Python two-way audio, no native lib).

        Talk is the av-connect handshake REVERSED on a separate channel: we open an av-SERVER, the
        camera logs into us and pulls AAC-LC audio. Flow (proven live 2026-06-27):
          1. ensure a session, then enter LiveStreamState (talk only runs while a stream is live);
          2. SPEAKERSTART 0x0350 {channel};
          3. on the camera's talk-login (sub=0x00 on `channel`) reply with build_talk_grant, mirroring
             the camera's advertised capability word (self._cam_grant_cap, captured in connect());
          4. stream the file as AAC-LC ADTS av-data frames (build_talk_audio), paced at the AAC frame
             duration (~64 ms), honouring the camera's resend (0x0a NAK) requests;
          5. SPEAKERSTOP 0x0351 and tear the talk channel down.

        This method is the SOLE socket sender for its duration (it stops any streaming reader first),
        mirroring the engine's single-sender rule. Returns the number of audio frames delivered.

        Args:
          channel   talk channel (default 1; live video is ch0).
          loop      repeat the file until max_secs (or forever if max_secs is None).
          max_secs  hard stop after this many seconds (None = until the file/loop ends).
          rate      AAC sample rate to transcode to (camera expects 16 kHz mono).
          warmup    seconds of live stream before SPEAKERSTART (camera must be in LiveStreamState).
          on_status optional callback(dict) for progress (sent, delivered, decoding) — for a CLI/UI.
          gain      linear volume multiplier (1.0 = unchanged, <1 quieter, >1 louder) — the reliable
                    talk-volume lever, since the camera's speaker_level is firmware-managed.
        """
        path = os.path.expanduser(path)
        if not os.path.exists(path):
            raise FileNotFoundError(f"Audio file not found: {path}")
        units = _aac_units(path, rate, gain)
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
        # ⚠ REGRESSION SWITCH, NOT A TUNABLE: OFF (=0) reopens the talkback resend-lookup wrap death.
        _talk_wrap = os.environ.get("CUBOAI_TALK_WRAP", "1") != "0"   # see the SACK-replay lookup below
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
                            # C (contiguous base) at [36:38]. (The earlier resend was on 0x0a, the camera's
                            # clock frame — wrong subtype, so loss was never recovered.)
                            cnt = struct.unpack_from('<H', dec, 42)[0]
                            C = struct.unpack_from('<H', dec, 36)[0]
                            if 0 < cnt < 256 and C != 0xFFFF:
                                for k in range(min(cnt, (len(dec) - 50) // 2)):
                                    frag = (C + struct.unpack_from('<H', dec, 50 + 2 * k)[0]) & 0xFFFF
                                    if _talk_wrap:
                                        # WRAP FIX (audit 2026-07-23) — FOURTH instance of the u16-vs-
                                        # unbounded-int class (after H1, _data_ack, PlaybackReader).
                                        # `talk_frag` is deliberately MONOTONIC/unbounded (it must not
                                        # restart when a looped file wraps its content), so sent_buf's
                                        # keys are unbounded — but the SACK entry decodes to a u16.
                                        # Past 65536 frames (64 ms/frame => ~70 min of continuous or
                                        # looping talkback) every sent_buf.get(u16) MISSES and talkback
                                        # loss-recovery SILENTLY STOPS (resends_sent just stops rising;
                                        # the wire stays well-formed, so nothing looks wrong). Lift the
                                        # u16 BACKWARD into talk_frag's space — resends are always for
                                        # already-sent frames, so the nearest match at-or-below the
                                        # current frag is the right one. Below the wrap this is the
                                        # identity, so the wire is unchanged.
                                        frag = _unwrap_index_back(frag, talk_frag)
                                    au = sent_buf.get(frag)
                                    if au is not None:
                                        s.sendto(build_talk_audio(R, channel, self._seq, talk_relseq, frag, frag, au), cam)
                                        self._seq += 1; talk_relseq += 1; resends_sent += 1
                                        self._stat_talk_resend += 1    # outbound visibility
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
        as the very last packets, then closes the socket (reversed from three
        --no-status teardown captures; see build_close). Replicating it lets the
        camera free its session slot promptly instead of waiting for its
        alive-timeout, which makes an immediate reconnect clean.

        Order matters: stop the background reader first (so it never touches a
        closed socket), then send the close, then close the socket, then reset all
        per-session counters/state so a later connect() starts from a clean slate.
        """
        # 1. stop the streaming reader thread before touching the socket.
        self._stop_reader()
        # 2. best-effort session-close (3x, as native). UDP: unacked is fine. Guard
        #    on having a live socket + the session R/peer the close frame needs.
        if self._sock is not None and self._R is not None and self._cam is not None:
            # build_close is the camera's session-stop signal (Linux capture: 3x 24-byte build_close then
            # the camera goes silent). Send it BLOCKING so it's guaranteed to leave: the socket is
            # non-blocking, so at teardown (send buffer full from the CACK burst) a plain sendto() can
            # silently EWOULDBLOCK-drop it. (The macOS download ghost was actually an unclosed 2nd RDT
            # conn, fixed in NativeScanSession's teardown; this just makes the session close reliable.)
            try:
                frame = build_close(self._R, session_fp=self._session_fp)
                try: self._sock.setblocking(True)
                except OSError: pass
                for _ in range(3):
                    try: self._sock.sendto(frame, self._cam)
                    except OSError: pass
            except Exception:
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
    ip = sys.argv[1] if len(sys.argv) > 1 else "192.0.2.10"
    print(f"Connecting pure-Python TUTK to {ip} ...")
    sess = TUTKDirectSession(ip)
    if sess.connect(timeout=8.0):
        print(f"\n✅ Connected!  session_hdr = {sess.session_hdr.hex()}")
    else:
        print("\n❌ Connection failed.")
        sys.exit(1)
