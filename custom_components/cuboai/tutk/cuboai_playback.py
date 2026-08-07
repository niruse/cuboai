#!/usr/bin/env python3
"""
cuboai_playback.py — LOCAL DVR / "rewind & watch" for the CuboAI camera (pure Python).

ADDITIVE + OPT-IN. This module is NOT imported by the production live path
(cuboai_stream_video.py / the go2rtc exec entrypoint). Nothing here runs unless a
caller explicitly constructs these helpers, so the live stdout stays byte-identical and
the validator/replay SHAs are unaffected.

Scope (see WORKORDER 2026-07-13 + memory cuboai-playback-dvr-mapped):
  * Footage DISCOVERY  — IOCTL 0x910 SMsgAVIoctrlDownloadFileReq -> 0x911 resp -> RDT pull
    of a per-hour manifest "yyyyMMdd_HH_status.json" (UTC). JSON {"s_log":[per-minute recs]}.
  * COVERAGE model     — which minutes/hours have footage; nearest-available lookup; the
    18-72 h retention boundary.
  * PLAYBACK control   — IOCTL 0x31a SMsgAVIoctrlPlayRecord (command 0x10 START / 0x1 STOP),
    STimeDay from epoch-seconds UTC -> 0x31b resp (result = assigned playback AV channel).

M1 (this file, offline-testable half): builders/parsers + coverage. The RDT receiver
(RdtReceiver) and the live pull/playback glue land in later milestones.
"""
from __future__ import annotations
import struct, json, datetime, time, queue, threading, select, sys
from dataclasses import dataclass, field
from typing import Optional

# ── IOCTL io_types ────────────────────────────────────────────────────────────────
DOWNLOAD_FILE_REQ = 0x910   # resp = 0x911
PLAYRECORD_REQ    = 0x31a   # resp = 0x31b
PLAY_CMD_START    = 0x10    # SMsgAVIoctrlPlayRecord.command START
PLAY_CMD_STOP     = 0x01    # SMsgAVIoctrlPlayRecord.command STOP

# Retention floors (CameraDef): older devices 18h, Gen3 72h. A 7-day const also exists.
RETENTION_18H_S = 0xfd20    # 64800  s
RETENTION_72H_S = 0x3f480   # 259200 s

# ── 0x910 SMsgAVIoctrlDownloadFileReq  (size 0x4c=76) ─────────────────────────────
#   id@0(LE i32)  file_type@4(LE i32)  file_name[64]@8 (null-padded)  reserved[4]@72
_DL_REQ_SIZE = 0x4c
def build_download_req(file_name: str, file_id: int = 0, file_type: int = 1) -> bytes:
    p = bytearray(_DL_REQ_SIZE)
    struct.pack_into("<i", p, 0, file_id)
    struct.pack_into("<i", p, 4, file_type)
    fb = file_name.encode("utf-8")[:0x40]
    p[8:8 + len(fb)] = fb
    return bytes(p)

# ── 0x911 SMsgAVIoctrlDownloadFileResp  (size 0x58=88) ────────────────────────────
#   id@0 result@4 rdtChannel@8 file_size@12 file_type@16 file_name[64]@20 reserved[4]@84
@dataclass
class DownloadFileResp:
    id: int
    result: int
    rdtChannel: int
    file_size: int
    file_type: int
    file_name: str

def parse_download_resp(b: bytes) -> Optional[DownloadFileResp]:
    if len(b) < 20:
        return None
    if len(b) < 88:
        b = b + b"\x00" * (88 - len(b))
    return DownloadFileResp(
        id         = struct.unpack_from("<i", b, 0)[0],
        result     = struct.unpack_from("<i", b, 4)[0],
        rdtChannel = struct.unpack_from("<i", b, 8)[0],
        file_size  = struct.unpack_from("<i", b, 12)[0],
        file_type  = struct.unpack_from("<i", b, 16)[0],
        file_name  = b[20:84].split(b"\x00")[0].decode("utf-8", "replace"),
    )

def manifest_name(dt_utc: datetime.datetime) -> str:
    """Per-hour manifest file name for a UTC datetime: 'yyyyMMdd_HH_status.json'."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=datetime.timezone.utc)
    return dt_utc.astimezone(datetime.timezone.utc).strftime("%Y%m%d_%H") + "_status.json"

# ── STimeDay (8B) + 0x31a SMsgAVIoctrlPlayRecord (25B) ────────────────────────────
#   STimeDay: year(LE s16)@0 month@2 day@3 wday@4 hour@5 minute@6 second@7  (UTC, month 1-based)
def build_stimeday(epoch_seconds: int) -> bytes:
    dt = datetime.datetime.fromtimestamp(epoch_seconds, tz=datetime.timezone.utc)
    p = bytearray(8)
    struct.pack_into("<h", p, 0, dt.year)
    p[2] = dt.month            # 1-based, matches SimpleDateFormat "MM"
    p[3] = dt.day
    # wday: app computes via Calendar; camera recomputes/ignores. Use ISO-ish 0..6 (Sun=0)
    p[4] = (dt.weekday() + 1) % 7   # Python Mon=0..Sun=6  ->  Sun=0..Sat=6
    p[5] = dt.hour
    p[6] = dt.minute
    p[7] = dt.second
    return bytes(p)

#   PlayRecord: channel@0 command@4 Param@8 stTimeDay@12(8B) debug@20 disable_timecontrol@21
#               reserved[2]@22 ; getSize = STimeDay(8)+0x11 = 25, last byte pad
_PLAYREC_SIZE = 8 + 0x11
def build_playrecord(command: int, epoch_seconds: int, channel: int = 0,
                     param: int = 0, disable_timecontrol: int = 0, debug: int = 0) -> bytes:
    p = bytearray(_PLAYREC_SIZE)
    struct.pack_into("<i", p, 0, channel)
    struct.pack_into("<i", p, 4, command)
    struct.pack_into("<i", p, 8, param)
    p[12:20] = build_stimeday(epoch_seconds)
    p[20] = debug & 0xFF
    p[21] = disable_timecontrol & 0xFF
    # reserved[2]@22 + 1 pad left zero
    return bytes(p)

def build_playrecord_start(epoch_seconds: int, disable_timecontrol: int = 0) -> bytes:
    return build_playrecord(PLAY_CMD_START, epoch_seconds,
                            disable_timecontrol=disable_timecontrol)

def build_playrecord_stop(epoch_seconds: int, disable_timecontrol: int = 0) -> bytes:
    return build_playrecord(PLAY_CMD_STOP, epoch_seconds,
                            disable_timecontrol=disable_timecontrol)

# ── 0x31b SMsgAVIoctrlPlayRecordResp (12B): command@0 result@4 reserved[4]@8 ───────
#   NOTE: on success `result` is REPURPOSED as the assigned playback AV channel number.
@dataclass
class PlayRecordResp:
    command: int
    result: int          # >=0 => assigned playback AV channel; <0 => error

def parse_playrecord_resp(b: bytes) -> Optional[PlayRecordResp]:
    if len(b) < 8:
        return None
    return PlayRecordResp(
        command = struct.unpack_from("<i", b, 0)[0],
        result  = struct.unpack_from("<i", b, 4)[0],
    )

# ── Manifest parse + coverage model ───────────────────────────────────────────────
# s_log entry -> Gson model `com.getcubo.app.model.MediaFileItem` (NO @SerializedName; Gson maps
# by field name, so the JSON keys ARE the field names). Full key set verified from the decompiled
# APK (MediaFileItem.smali; MediaFileManager.saveMediaFileLogs Gson.fromJson per s_log element):
#   ts (epoch-sec key, ms-tolerant), te(temp,double), hu(humidity,double),
#   bp(baby present), na(noise level), mo(motion), bw(well-being), be(baby event), pr(sleep/privacy),
#   ni, nm, se, ve.
# CONFIRMED value semantics (from TimelinePageAdapter.getMediaFilePaint + ViewHolder, smali):
#   bp: 1 => present/in-view (colored tick); anything else incl. 2 => absent.   [if-ne bp,1]
#   na: NOISE LEVEL 0..100; >=60 (0x3c) => elevated/high-activity tick.          [if-lt na,0x3c]
#   mo: 2 => motion active.                                                      [if-ne mo,2]
#   bw: 1 => well-being active.                                                  [if-ne bw,1]
#   be: 1 or 2 => baby-event active.                                             [if-eq be,1/if-ne be,2]
#   pr: 1 => CUBO_SLEEP (sleep/privacy mode); else RECORD.                       [ViewHolder]
# ni, nm, se, ve: PRESENT + populated in this firmware's manifests, and the APP itself never reads them
#   (decompile: Room stores them under raw names + hydrates them, but NO query/UI/log/telemetry consumer
#   — each touched 2× = pure Room plumbing vs 3× for rendered fields). "No app consumer" is NOT "no
#   meaning" — the FIRMWARE writes real data the app just ignores. 2026-07-23 LIVE FOOTAGE CONFIRMED:
#     ni = NIGHT-VISION / IR mode (1=IR/dark, 0=daylight)   — real sensor the official app doesn't surface
#     nm = NOISE PEAK (per-minute max; caught a loud noise the na average softened)  — ditto
#     se = per-minute record COUNTER (monotonic +1 across the manifest)              — structural
#     ve = format VERSION (constant 1 across 4293 records)                           — structural
#   So ni/nm are genuinely useful LOCAL sensors surfaced beyond the app; se/ve are structural. Only these
#   two + se/ve; the rendered fields bp/na/mo/bw/be/pr are the app-consumed set (getMediaFilePaint).
# NOTE: "Caregiver Present" (adult recognized in frame) is NOT an s_log field. It is a distinct
#   Region-events feed object {type:"Caregiver", startTs, endTs} (Region.smali RegionType.CAREGIVER),
#   delivered separately from the manifest — so it CANNOT be derived from s_log. Mapping that feed is
#   future work; likewise CRY/COUGH/MOVEMENT timeline icons come from the same Region feed, not s_log.
_EVENT_KEYS = ("mo", "na", "ni", "nm", "pr", "se", "be", "bp", "bw", "ve")

@dataclass
class MinuteRecord:
    ts: int                       # epoch SECONDS (normalized), minute granularity
    flags: dict = field(default_factory=dict)
    temp: Optional[float] = None
    humidity: Optional[float] = None

def _norm_epoch_s(v) -> int:
    v = int(v)
    return v // 1000 if v > 1_000_000_000_000 else v   # ms -> s if needed

def parse_manifest(raw: bytes | str) -> list[MinuteRecord]:
    """Parse a '..._status.json' manifest body into per-minute records (sorted by ts)."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    obj = json.loads(raw)
    out: list[MinuteRecord] = []
    for e in obj.get("s_log", []):
        if "ts" not in e:
            continue
        rec = MinuteRecord(
            ts=_norm_epoch_s(e["ts"]),
            flags={k: e[k] for k in _EVENT_KEYS if k in e},
            temp=e.get("te"), humidity=e.get("hu"),
        )
        out.append(rec)
    out.sort(key=lambda r: r.ts)
    return out

def raw_manifest_keys(raw):
    """Diagnostic: given RAW manifest bytes/str, report the ACTUAL key inventory this firmware emits,
    independent of what parse_manifest models. Answers 'are ni/nm/se/ve (or anything else) present?'
    Returns {top_level: sorted top-level keys, record_keys: sorted union across s_log entries,
    mapped: keys we model, unmapped: keys we DON'T, n_records, numeric_ranges: {key:(min,max,n)}}.
    Never raises on ragged JSON."""
    try:
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", "replace")
        obj = json.loads(raw)
    except Exception:
        return {"error": "not JSON", "top_level": [], "record_keys": [], "unmapped": []}
    modelled = set(_EVENT_KEYS) | {"ts", "te", "hu"}
    entries = obj.get("s_log", []) if isinstance(obj, dict) else []
    record_keys, ranges = set(), {}
    for e in entries:
        if not isinstance(e, dict):
            continue
        for k, v in e.items():
            record_keys.add(k)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                lo, hi, n = ranges.get(k, (v, v, 0))
                ranges[k] = (min(lo, v), max(hi, v), n + 1)
    return {
        "top_level": sorted(obj.keys()) if isinstance(obj, dict) else [],
        "record_keys": sorted(record_keys),
        "mapped": sorted(record_keys & modelled),
        "unmapped": sorted(record_keys - modelled),
        "n_records": len(entries),
        "numeric_ranges": ranges,
    }


class CoverageModel:
    """Which minutes have on-camera footage, assembled from one or more manifests."""
    def __init__(self, retention_s: int = RETENTION_72H_S):
        self._minutes: dict[int, MinuteRecord] = {}   # minute-aligned ts -> record
        self.retention_s = retention_s

    def add_manifest(self, records: list[MinuteRecord]) -> int:
        n = 0
        for r in records:
            key = r.ts - (r.ts % 60)
            self._minutes[key] = r
            n += 1
        return n

    def minutes(self) -> list[int]:
        return sorted(self._minutes)

    @property
    def count(self) -> int:
        return len(self._minutes)

    def span(self) -> Optional[tuple[int, int]]:
        if not self._minutes:
            return None
        ks = self.minutes()
        return ks[0], ks[-1]

    def has_footage(self, epoch_s: int, tol_s: int = 90) -> bool:
        target = epoch_s - (epoch_s % 60)
        return any(abs(m - target) <= tol_s for m in self._minutes)

    def nearest(self, epoch_s: int) -> Optional[int]:
        """Nearest minute with footage to a requested epoch-seconds time (its ts)."""
        if not self._minutes:
            return None
        return min(self._minutes, key=lambda m: abs(m - epoch_s))

    def in_retention(self, epoch_s: int, now_s: int) -> bool:
        return (now_s - epoch_s) <= self.retention_s and epoch_s <= now_s


# ── RDT (Reliable Data Transfer) over an IOTC channel ─────────────────────────────
# RDT rides inside an IOTC channel-data frame on channel = rdtChannel. Reversed from
# libRDTAPIs.so + a LIVE camera capture (wo_rdt_observe): the camera-decoded frame is
#   [0:2]=0402 [2]=1d(recv)/1a(send) [3]=0a [4:6]=len-16 [6:8]=pkt-seq
#   [8:12]=08 04 12 00 (RDT/channel-data frame type)  [12:14]=R  [14]=channel
#   [16]=0c  [20:22]=R  [22:28]=host MID  [28:]=RDT packet
# RDT packet (20-byte header, LE): magic 5a97c2f1@0, type@4, ver=5@5, len@6, seqL@8,
#   seqH@12, byte16@16, conn_id@17, byte18@18, payload@20.  seq is a PACKET counter.
RDT_MAGIC = b"\x5a\x97\xc2\xf1"
RDT_VER   = 0x05
RDT_HELLO, RDT_DATA, RDT_FIN, RDT_CLOSE, RDT_DATA_URGENT = 0x01, 0x02, 0x03, 0x04, 0x10
RDT_ABORT, RDT_EXIT = 0x20, 0x70
RDT_HELLO_ACK, RDT_DATA_ACK, RDT_FIN_ACK, RDT_SACK, RDT_CACK = 0x41, 0x42, 0x43, 0x45, 0x46
_RDT_FRAME_TYPE = b"\x08\x04\x12\x00"

def build_rdt_packet(ptype, seqL=0, seqH=0, conn_id=0, payload=b"", b16=0, b18=0):
    p = bytearray(20)
    p[0:4] = RDT_MAGIC
    p[4] = ptype & 0xFF
    p[5] = RDT_VER
    struct.pack_into("<H", p, 6, len(payload) & 0xFFFF)
    struct.pack_into("<I", p, 8, seqL & 0xFFFFFFFF)
    struct.pack_into("<I", p, 12, seqH & 0xFFFFFFFF)
    p[16] = b16 & 0xFF
    p[17] = conn_id & 0xFF
    struct.pack_into("<H", p, 18, b18 & 0xFFFF)
    return bytes(p) + payload

def rdt_close_packets(native_close, conn_ids):
    """RDT teardown frames, ONE per conn_id — the SINGLE source of truth for RDT close, shared by
    RdtReceiver._close_all and NativeScanSession's pull teardown so the two can never drift again
    (C3 — they had already diverged twice: B-2 was NativeScanSession still sending the malformed
    legacy FIN on the DEFAULT scan path while RdtReceiver had moved to the native CLOSE).

    native_close=True  -> native RDT_Destroy CLOSE(0x04, seq=0)              [2x2 wire-proven: cids advance]
    native_close=False -> legacy FIN(0x03, seq=0xFFFFFFFF)                   [malformed in type AND seq]

    Native sends ONLY the CLOSE (never the FIN), so we emit exactly ONE frame per conn_id — matching
    the app on the wire — rather than the old FIN+CLOSE pair."""
    for c in conn_ids:
        if native_close:
            yield build_rdt_packet(RDT_CLOSE, seqL=0, seqH=0, conn_id=c)
        else:
            yield build_rdt_packet(RDT_FIN, seqL=0xFFFFFFFF, seqH=0xFFFFFFFF, conn_id=c)

def build_rdt_frame(R, mid, seq, channel, rdt_packet):
    """IOTC channel-data frame (host->cam) carrying an RDT packet at [28]; transcode()d
    (data channel => swap_tail default). Mirrors the camera's observed cam->host frame."""
    import cuboai_pure as cp
    total = 28 + len(rdt_packet)
    p = bytearray(total)
    p[0:4] = b"\x04\x02\x1a\x0a"                 # [2]=0x1a send
    struct.pack_into("<H", p, 4, total - 16)
    struct.pack_into("<H", p, 6, seq & 0xFFFF)
    p[8:12] = _RDT_FRAME_TYPE                    # 08 04 12 00
    struct.pack_into("<H", p, 12, R & 0xFFFF)
    p[14] = channel & 0xFF
    p[16] = 0x0C
    struct.pack_into("<H", p, 20, R & 0xFFFF)
    p[22:28] = bytes(mid)
    p[28:] = rdt_packet
    return cp.transcode(bytes(p))

def parse_rdt_packet(dec: bytes):
    """Find + parse the first RDT packet in a decoded IOTC frame. Returns dict or None."""
    i = dec.find(RDT_MAGIC)
    if i < 0 or i + 20 > len(dec):
        return None
    ln = struct.unpack_from("<H", dec, i + 6)[0]
    return dict(
        off=i, type=dec[i + 4], ver=dec[i + 5], length=ln,
        seqL=struct.unpack_from("<I", dec, i + 8)[0],
        seqH=struct.unpack_from("<I", dec, i + 12)[0],
        b16=dec[i + 16], conn_id=dec[i + 17],
        b18=struct.unpack_from("<H", dec, i + 18)[0],
        payload=bytes(dec[i + 20: i + 20 + ln]) if ln > 0 else b"",
    )

class RdtReceiver:
    """Pure-Python RDT RECEIVE side: complete the soft handshake with a camera that is
    already pushing HELLOs on `rdt_channel`, collect DATA packets into the file byte
    stream (ordered by packet seq), cumulative-ACK, and stop at `file_size`.

    Drives an already-connected cuboai_pure.TUTKDirectSession (uses its socket/R/MID +
    IOTC-level keepalive/ACK). ADDITIVE / opt-in — never used by the live path."""
    # per-PROCESS client temp-id counter for native-faithful OPEN (see native_open below); mirrors
    # the native global @0x1c018 (inits 1, post-increment per conn, wrap 0xfe->1). Only consumed
    # when CUBOAI_RDT_NATIVE_OPEN=1 => default path never reads it (byte-identical).
    _client_temp_id = 1

    def __init__(self, inner, rdt_channel, file_size, timeout=25.0,
                 verbose=False, log=None, capture_hello=False):
        import cuboai_pure as cp
        self.cp = cp
        self.inner = inner
        self.channel = rdt_channel
        self.file_size = file_size
        self.timeout = timeout
        self.verbose = verbose
        self._log = log or (lambda *a: None)
        # (1) starve-diff instrumentation (opt-in; default OFF => recv path unchanged): on a starved
        # pull the camera HELLOs ~55x and sends 0 DATA — we have never decoded what's IN those HELLOs.
        # Capture the parsed fields + payload of the first/some HELLOs, the HELLO-ACK bytes WE send,
        # and the first DATA header (served case) so a served vs starved pull can be diffed byte-wise.
        self.capture_hello = capture_hello
        self.hello_log = []          # [(n, {fields}, payload_hex)] sampled across the retries
        self.hello_ack_hex = None    # the RDT HELLO-ACK packet we transmit (pre-frame)
        self.data_first = None       # first DATA packet header fields (present only when served)
        self.mid = bytes(cp._AV_MID)
        self.conn_id = None                 # learned from the camera's HELLO/DATA byte17
        self.conn_ids = set()               # ALL conn_ids the camera opened (must close EVERY one —
                                            # ghost-conn guard, ported from NativeScanSession 2026-07-19)
        self.packets = {}                   # packet-index (seqH<<32|seqL) -> payload
        self.stopped_reason = None
        self.hellos_seen = 0
        self.data_seen = 0
        self.dup_data = 0            # DATA pkts for an idx we already hold => camera RESENT it
        self.sacks_sent = 0
        # conn_id discriminator (2026-07-17): a starved pull re-HELLOs ~57x with 0 DATA. Theory (B):
        # a falsy (0) HELLO conn_id is never latched (see the `if rp["conn_id"]:` guard below) so our
        # CACK carries a stale/0 conn_id the camera ignores → it re-HELLOs forever. Log the inbound
        # HELLO conn_ids + what we echo, for SERVED vs STARVED pulls: mismatch on starved => (B), our
        # bug (cheap local fix); match => (A), camera-side (buffer starvation). Inert instrumentation.
        self.hello_conn_ids = set()  # distinct conn_ids the camera put in its HELLOs this pull
        self.first_hello_cid = None  # conn_id of the very first HELLO
        # RDT recovery: the disasm (libRDTAPIs.so, SACK @0x89b8 / resend @0x76c4) said the camera
        # resends ONLY seqs named in a 0x45 SACK. That was WRONG as an *only* path: a LIVE induced-
        # loss A/B (2026-07-17) proved the camera ALSO has a CUMULATIVE-ACK / Go-Back-N retransmit
        # (resends from the stalled cumulative point) that our CACK-only receiver already triggers —
        # and it is MORE robust under loss than our selective SACK. @30% drop: SACK OFF = 14/15 (93%)
        # vs SACK ON = 10/15 (67%, + 586 SACKs spammed) — selective repeat HURTS (same content-wrong-
        # SACK regression as the DVR path). So this whole added path is DEFAULT OFF (reverts to the
        # proven CACK-only receiver). CUBOAI_RDT_SACK=1 opt-in only for further investigation.
        import os as _os
        self.sack_enabled = _os.environ.get("CUBOAI_RDT_SACK", "0") == "1"
        # native-faithful teardown (2026-07-18): the APK's RDT client releases a conn with
        # RDT_Destroy (NEVER RDT_Abort), whose wire packet — disasm libRDTAPIs.so @0x9180:
        # magic; `strh #0x0504` @off4 => type=0x04 CLOSE, ver=0x05; seqL/seqH=0; conn_id from the
        # conn struct — is a type-0x04 CLOSE with seq=0. Our legacy teardown fires FIN(type=0x03)
        # with seqL=seqH=0xFFFFFFFF: a type-AND-seq mismatch vs native. If the camera rejects that
        # malformed FIN, the conn releases only on its internal timeout (== the observed "conn_id
        # wedge / only pacing helps"). ON => send native's exact CLOSE instead. Default OFF =>
        # byte-identical to the proven path. A/B this live once the camera is healthy.
        # DEFAULT ON (2026-07-18b): wire A/B (single-client, back-to-back manifest pulls) proved it.
        # Baseline malformed FIN froze the conn_id (cids [13,14,14,14,14], 1/5 served — the wedge);
        # native CLOSE released it every pull (cids advance, no freeze). Flip=0 reverts to the legacy
        # FIN. Live AV path never uses RdtReceiver, so AV replay SHAs are unaffected (validator 36/0).
        self.native_close = _os.environ.get("CUBOAI_RDT_NATIVE_CLOSE", "1") == "1"
        # native-faithful OPEN (2026-07-18b): the APK client-INITIATES via RDT_Create, which sends
        # a 20-byte HELLO — disasm libRDTAPIs.so @0x9474: magic; `strh #0x0501` @off4 => type=0x01
        # HELLO, ver=0x05; seqL/seqH=0; NO payload; conn_id[17] = struct[0xa5], a CLIENT-LOCAL temp
        # id seeded from a process global (@0x1c018) that inits to 1 and increments per conn (wrap
        # 0xfe->1). It also RETRANSMITS the pending HELLO on a timer until the conn establishes
        # (struct[0xf]/[0x10]==3). Our legacy open sends ONE HELLO with a HARDCODED conn_id=1 then
        # goes passive (reacts to the camera's HELLOs, HELLO_ACKs them). The open PACKET bytes are
        # otherwise identical (type/ver/seq/payload) and the server assigns its own conn_id in the
        # reply + demuxes the HELLO by session-id, so the temp-id nonce is on-wire PARITY not a gate
        # per the disasm — but the client-initiated RETRANSMIT-until-established is a real behavioural
        # difference. ON => incrementing temp id + retransmit the client HELLO while starved. Default
        # OFF => byte-identical (conn_id=1, single kick). Independent of NATIVE_CLOSE (clean 2x2).
        # DEFAULT ON (2026-07-18b): wire A/B proved native OPEN (incrementing temp id + retransmit the
        # client HELLO until established) converts would-be-starves into serves — OPEN=1 cells were
        # 5/5 vs baseline 1/5, and served pulls show hello=5,6,7 THEN DATA (the retransmit drove
        # establishment where the passive path HELLO'd 113x and starved). Flip=0 reverts to the single
        # passive kick. Manifest/DVR-pull path only; live AV path untouched (validator 36/0).
        self.native_open = _os.environ.get("CUBOAI_RDT_NATIVE_OPEN", "1") == "1"
        if self.native_open:
            # replicate the native global: use the current value, then post-increment (wrap 0xfe->1)
            self.open_cid = RdtReceiver._client_temp_id
            RdtReceiver._client_temp_id = 1 if self.open_cid >= 0xFE else self.open_cid + 1
        else:
            self.open_cid = 1                # legacy hardcoded nonce (byte-identical default)
        # RDT-channel LINGER (2026-07-18c): the camera keeps SERVICING the RDT channel after we've
        # collected the file (trailing DATA + 0x0a control with climbing RDT counters) and WON'T
        # return to serving channel-0 IOCTLs (the next 0x910/DownloadFile) until that channel goes
        # quiet. recv() historically returned the instant it had the bytes, abandoning the channel
        # mid-flight -> the next same-session IOCTL stalls (no 0x911). The native lib's persistent
        # IOTC service thread keeps acking until the channel closes; we emulate that with a bounded
        # post-completion drain: keep reading + CACKing (+ answering keepalives) until the camera
        # stops sending RDT frames, THEN CLOSE. Lets consecutive DownloadFiles run on ONE session
        # like the app. INVESTIGATED + REVERTED (2026-07-18c, see WORKORDER log): draining the RDT
        # channel, servicing channel-0, FIN+CLOSE, and a reliable FIN->FIN_ACK handshake ALL failed to
        # unstick the same-session 2nd DownloadFile (ground truth: camera sends 0 channel-0 DATA during
        # RDT and never FIN_ACKs). Removed; the reconnect-per-pull path (works on Linux) stands.

    def _send_rdt(self, rdt_packet):
        s = self.inner._sock
        frame = build_rdt_frame(self.inner._R, self.mid, self.inner._seq,
                                self.channel, rdt_packet)
        self.inner._seq += 1
        s.sendto(frame, self.inner._cam)

    def _highest_contiguous(self):
        idx, n = 0, 0
        while idx in self.packets:
            n += 1; idx += 1
        return n - 1 if n else -1        # highest contiguous packet index, -1 if none

    def _collected_bytes(self):
        return sum(len(p) for p in self.packets.values())

    def _send_cack(self):
        hi = self._highest_contiguous()
        if hi < 0:
            return
        seqL = hi & 0xFFFFFFFF
        seqH = (hi >> 32) & 0xFFFFFFFF
        pkt = build_rdt_packet(RDT_CACK, seqL=seqL, seqH=seqH,
                               conn_id=(self.conn_id or 0))
        self._send_rdt(pkt)

    def _gaps(self, max_blocks=4):
        """Missing packet-index ranges (inclusive) strictly below the highest received index.
        Up to `max_blocks` (start,end) pairs — the SACK payload holds 4 x 16-B blocks."""
        if not self.packets:
            return []
        hi = max(self.packets)
        idx = self._highest_contiguous() + 1     # first missing index (0 if none contiguous)
        gaps, start = [], None
        while idx < hi and len(gaps) < max_blocks:
            if idx not in self.packets:
                if start is None:
                    start = idx
            elif start is not None:
                gaps.append((start, idx - 1)); start = None
            idx += 1
        if start is not None and len(gaps) < max_blocks:
            gaps.append((start, hi - 1))         # hi itself is present (it's the max)
        return gaps

    def _send_sack(self):
        """Selective-NAK the current hole(s) so the camera retransmits exactly those seqs.
        Wire format (matches native @0x89b8): 20-B hdr {type 0x45, seqL/seqH = cumulative
        expected} + 72-B payload {reserved u32@0, count u32@4, count x (start u64, end u64)}.
        Returns False (sends nothing) when there is no hole -> lossless pulls stay identical."""
        gaps = self._gaps()
        if not gaps:
            return False
        expected = self._highest_contiguous() + 1
        payload = bytearray(72)
        struct.pack_into("<I", payload, 0, 0)                 # reserved -> pkt[20]
        struct.pack_into("<I", payload, 4, len(gaps))         # block count -> pkt[24]
        off = 8
        for s, e in gaps:
            struct.pack_into("<Q", payload, off, s)           # start u64 -> pkt[28 + 16k]
            struct.pack_into("<Q", payload, off + 8, e)       # end   u64 -> pkt[36 + 16k]
            off += 16
        self._send_rdt(build_rdt_packet(
            RDT_SACK, seqL=expected & 0xFFFFFFFF, seqH=(expected >> 32) & 0xFFFFFFFF,
            conn_id=(self.conn_id or 0), payload=bytes(payload)))
        self.sacks_sent += 1
        return True

    def _close_all(self):
        """FIN/CLOSE EVERY conn_id the camera opened this pull (ghost-conn guard, ported from
        NativeScanSession._close_all 2026-07-19). The camera can open a SECOND RDT conn (seen as
        cid=40 on macOS) that we HELLO_ACK'd; closing only the DATA stream's conn_id leaves the other
        open → the camera HELLO-hammers it forever = ghost session that wedges the NEXT DownloadFile.
        This is the unattended SENSOR-poll path, so it must not rely on the camera never happening to
        open a 2nd conn. native_close selects native RDT_Destroy CLOSE(0x04,seq=0) [2x2 wire-proven:
        cids advance]; native_close=0 reverts to the legacy FIN(0x03,seq=0xFFFFFFFF)."""
        for pkt in rdt_close_packets(self.native_close, set(self.conn_ids) or {self.conn_id or 0}):
            try:
                self._send_rdt(pkt)
            except Exception:
                pass

    def _drain_until_quiet(self, s, cp, max_drain=1.5, quiet=0.3):
        """After closing, keep reading + CACKing trailing DATA and answering keepalives until the
        camera STOPS sending on the RDT channel (re-closing any conn — incl. a NEW one — that appears
        during the drain), so the channel closes cleanly and the next DownloadFile isn't wedged by a
        still-open conn. Bounded (<=max_drain; early-out after `quiet` seconds of silence)."""
        import time, select
        end = time.time() + max_drain
        last_activity = time.time()
        while time.time() < end:
            r, _, _ = select.select([s], [], [], 0.05)
            now = time.time()
            if not r:
                if now - last_activity > quiet:
                    break                              # camera quiet -> RDT closed cleanly
                continue
            try:
                raw, addr = s.recvfrom(4096)
            except (BlockingIOError, OSError):
                continue
            if cp.is_keepalive_probe(raw):
                try: s.sendto(cp.build_keepalive_reply(raw), addr)
                except OSError: pass
                continue
            try:
                dec = cp.inv_transcode(raw)
            except Exception:
                continue
            rp = parse_rdt_packet(dec)
            if rp is None:
                if len(dec) >= 29 and dec[28] in (0x09, 0x0A):
                    try: self.inner._send_ack()
                    except Exception: pass
                continue
            last_activity = now                        # camera still active on the RDT stream
            cid = rp["conn_id"]
            if cid and cid not in self.conn_ids:       # a new/second conn opened during teardown
                self.conn_ids.add(cid); self.conn_id = cid
            t = rp["type"]
            if t in (RDT_DATA, RDT_DATA_URGENT):
                idx = (rp["seqH"] << 32) | rp["seqL"]
                if idx not in self.packets:            # fold in any trailing DATA (harmless; helps)
                    self.packets[idx] = rp["payload"]
                self._send_cack()
            elif t == RDT_HELLO:
                self._send_rdt(build_rdt_packet(RDT_HELLO_ACK, conn_id=cid))  # ack then close it too
            self._close_all()                          # re-close all (incl. any conn newly seen)

    def recv(self):
        """Run the receive loop; return the reassembled file bytes (may be short on error)."""
        import time, select
        cp = self.cp
        s = self.inner._sock
        # Kick the handshake from our side too (client HELLO). native_open OFF => conn_id=1
        # (byte-identical); ON => the incrementing client temp id, matching RDT_Create's open.
        self._send_rdt(build_rdt_packet(RDT_HELLO, conn_id=self.open_cid))
        t0 = time.time(); last_ack = 0.0; last_cack = 0.0; last_sack = 0.0; last_hello = t0
        while time.time() - t0 < self.timeout:
            if self._collected_bytes() >= self.file_size and self.file_size > 0:
                self.stopped_reason = "complete"; break
            r, _, _ = select.select([s], [], [], 0.1)
            now = time.time()
            if now - last_ack > 0.2:            # IOTC-level liveness
                try: self.inner._send_ack()
                except Exception: pass
                last_ack = now
            if not r:
                # STALL: no DATA arriving. The old code went silent on the RDT stream here —
                # fatal, since recovery is receiver-driven. Re-drive it: (a) if we hold holes,
                # re-send the SACK so the camera resends the missing seqs; (b) if DATA never
                # started (mode-a start failure), re-kick the HELLO in case ours was lost.
                if self.sack_enabled and self.data_seen and now - last_sack > 0.1:
                    if self._send_sack():
                        last_sack = now
                if self.sack_enabled and self.data_seen == 0 and now - last_hello > 0.3:
                    self._send_rdt(build_rdt_packet(RDT_HELLO, conn_id=1)); last_hello = now
                # native_open: RDT_Create retransmits the pending client HELLO on a timer until the
                # conn establishes. Re-drive our client-initiated open (same temp id) while starved,
                # at a native-like cadence (~0.5s, gentler than the sack path's 0.3s). Independent of
                # sack_enabled; OFF => this branch never runs (default byte-identical).
                if self.native_open and self.data_seen == 0 and now - last_hello > 0.5:
                    self._send_rdt(build_rdt_packet(RDT_HELLO, conn_id=self.open_cid)); last_hello = now
                continue
            try:
                raw, addr = s.recvfrom(4096)
            except BlockingIOError:
                continue
            if cp.is_keepalive_probe(raw):
                try: s.sendto(cp.build_keepalive_reply(raw), addr)
                except OSError: pass
                continue
            try:
                dec = cp.inv_transcode(raw)
            except Exception:
                continue
            rp = parse_rdt_packet(dec)
            if rp is None:
                # non-RDT frame (IOTC ack/nak etc.) — ack the reliable stream
                if len(dec) >= 29 and dec[28] in (0x09, 0x0A):
                    try: self.inner._send_ack()
                    except Exception: pass
                continue
            if rp["conn_id"]:
                self.conn_id = rp["conn_id"]
                self.conn_ids.add(rp["conn_id"])   # track every conn (e.g. a 2nd cid the camera opens)
            t = rp["type"]
            if t == RDT_HELLO:
                self.hellos_seen += 1
                if self.first_hello_cid is None:
                    self.first_hello_cid = rp["conn_id"]
                self.hello_conn_ids.add(rp["conn_id"])
                if self.capture_hello and (len(self.hello_log) < 6 or self.hellos_seen % 20 == 0):
                    self.hello_log.append((self.hellos_seen,
                        {k: rp[k] for k in ("type", "ver", "length", "seqL", "seqH",
                                            "b16", "conn_id", "b18")},
                        rp["payload"].hex()))
                # reply HELLO-ACK echoing the camera's conn_id -> completes connect
                ack_pkt = build_rdt_packet(RDT_HELLO_ACK, conn_id=rp["conn_id"])
                if self.capture_hello and self.hello_ack_hex is None:
                    self.hello_ack_hex = ack_pkt.hex()
                self._send_rdt(ack_pkt)
            elif t == RDT_HELLO_ACK:
                pass                              # peer accepted our HELLO
            elif t in (RDT_DATA, RDT_DATA_URGENT):
                self.data_seen += 1
                if self.capture_hello and self.data_first is None:
                    self.data_first = {k: rp[k] for k in ("type", "ver", "length", "seqL",
                                                          "seqH", "b16", "conn_id", "b18")}
                idx = (rp["seqH"] << 32) | rp["seqL"]
                if idx not in self.packets:
                    self.packets[idx] = rp["payload"]
                else:
                    self.dup_data += 1        # already held => this is a retransmit
                if now - last_cack > 0.03:
                    self._send_cack(); last_cack = now
                # NAK any hole below the highest received seq (selective repeat).
                if self.sack_enabled and now - last_sack > 0.03:
                    if self._send_sack():
                        last_sack = now
            elif t in (RDT_FIN, RDT_CLOSE):
                self.stopped_reason = "fin"; break
            elif t == RDT_ABORT:
                self.stopped_reason = "abort"; break
            elif t == RDT_EXIT:
                self.stopped_reason = "exit"; break
        else:
            self.stopped_reason = self.stopped_reason or "timeout"
        # final ack, then CLOSE/FIN EVERY conn the camera opened (avoids leftover HELLO/DATA on that
        # channel), then DRAIN until the RDT stream goes quiet, then assemble. See _close_all/_drain.
        self._send_cack()
        self._close_all()
        self._drain_until_quiet(s, cp)
        blob = b"".join(self.packets[k] for k in sorted(self.packets))
        blob = blob[:self.file_size] if self.file_size > 0 else blob
        if self.file_size > 0 and len(blob) < self.file_size:
            # classify the failure (workorder Task 1b): data_seen==0 => mode (a) the transfer
            # NEVER STARTED (handshake/start failure); data_seen>0 => mode (b) started then
            # lost data unrecovered (recovery failure).
            self.stopped_reason = "never_started" if self.data_seen == 0 else "short_read"
        return blob


def pull_manifest(transport, dt_utc, timeout=20.0, retries=2, diag=None, capture_hello=False):
    """0x910 + RDT pull of one hour's manifest -> (records, DownloadFileResp). records=[] on
    error/short read. The camera won't serve the CURRENT (still-growing) hour's manifest over
    RDT (it sends only HELLOs, no DATA) — prefer COMPLETED hours. Retries the (occasionally
    flaky) RDT handshake `retries` times.

    Instrumentation (workorder Step 1, ADDITIVE): pass `diag` as a mutable dict to receive the
    TRUE per-pull outcome — {ioctl_ok, rdtChannel, file_size, stopped_reason, data_seen,
    hellos_seen, dup_data, got_bytes, attempts, elapsed}. stopped_reason distinguishes the two
    failure modes the CLI's old "short read / unavailable" catch-all conflated: 'ioctl_timeout'
    (0x910 never answered — camera IO send-window / socket) vs 'never_started' (0x910 fine,
    RDT sent HELLOs but no DATA) vs 'short_read' (DATA started, lost unrecovered). diag=None
    (default) → behaviour byte-identical."""
    import time as _t
    inner = getattr(transport, "_inner", transport)
    name = manifest_name(dt_utc)
    resp = None
    d = diag if isinstance(diag, dict) else None
    if d is not None:
        d.update(ioctl_ok=False, rdtChannel=None, file_size=0, stopped_reason=None,
                 data_seen=0, hellos_seen=0, dup_data=0, got_bytes=0, attempts=0, elapsed=0.0)
    t_start = _t.time()
    for attempt in range(retries + 1):
        if d is not None:
            d["attempts"] = attempt + 1
        try:
            rt, data = transport.ioctl(DOWNLOAD_FILE_REQ, build_download_req(name))
        except Exception:
            # 0x910 allocates an RDT channel (heavier than a plain GET) and can TIME OUT on a busy
            # camera or a high-latency (WiFi) link — e.g. the user's Mac saw "no response to IOCTL
            # 0x0910". Don't propagate (it used to crash the CLI); back off and retry, then give up
            # gracefully → ([], None). Callers treat that as "hour unavailable / unconfirmed", and
            # do_playback still ATTEMPTS the pull (playback does not hard-depend on the manifest).
            if d is not None:
                d["stopped_reason"] = "ioctl_timeout"; d["elapsed"] = _t.time() - t_start
            if attempt < retries:
                _t.sleep(0.6); continue
            return [], None
        resp = parse_download_resp(data)
        if d is not None and resp is not None:
            d.update(ioctl_ok=True, rdtChannel=resp.rdtChannel, file_size=resp.file_size)
        if resp is None or resp.result != 0 or resp.file_size <= 0:
            if d is not None:
                d["stopped_reason"] = "no_manifest"; d["elapsed"] = _t.time() - t_start
            return [], resp
        rcv = RdtReceiver(inner, resp.rdtChannel, resp.file_size, timeout=timeout,
                          capture_hello=capture_hello)
        blob = rcv.recv()
        if d is not None:
            d.update(stopped_reason=rcv.stopped_reason, data_seen=rcv.data_seen,
                     hellos_seen=rcv.hellos_seen, dup_data=rcv.dup_data,
                     got_bytes=len(blob), elapsed=_t.time() - t_start,
                     first_hello_cid=rcv.first_hello_cid,
                     hello_cids=sorted(rcv.hello_conn_ids),
                     latched_cid=rcv.conn_id)
            if capture_hello:
                d.update(hello_log=rcv.hello_log, hello_ack_hex=rcv.hello_ack_hex,
                         data_first=rcv.data_first)
        if len(blob) >= resp.file_size:
            if d is not None:
                d["raw_json"] = bytes(blob)   # diag: the RAW manifest bytes (for raw-key inspection)
            try:
                return parse_manifest(blob), resp
            except Exception:
                if d is not None:
                    d["stopped_reason"] = "parse_error"
                return [], resp
        _t.sleep(0.3)   # brief backoff before re-requesting
    return [], resp


class _ScanRdt:
    """Minimal SERVICED RDT receive assembler, driven by NativeScanSession's reader thread (it
    calls feed() per RDT packet). No socket loop of its own. Sends (HELLO_ACK/CACK) go through the
    service's locked sender. LAN manifests are tiny + low-loss, so plain in-order assembly + CACK
    is enough (no SACK/recovery — that lives in the standalone RdtReceiver)."""
    def __init__(self, svc, channel):
        self.svc = svc
        self.channel = channel
        self.packets = {}
        self.conn_id = None
        self.conn_ids = set()                  # ALL conn_ids the camera opened (must close every one)
        self.data_seen = 0
        self.hellos_seen = 0
        self.first_hello_cid = None
        self.done = threading.Event()          # set on FIN/CLOSE from the camera
        self._last_cack = 0.0

    def _hi(self):
        idx = 0
        while idx in self.packets:
            idx += 1
        return idx - 1

    def collected(self):
        return sum(len(p) for p in self.packets.values())

    def feed(self, rp):
        t = rp["type"]
        if rp["conn_id"]:
            self.conn_id = rp["conn_id"]
            self.conn_ids.add(rp["conn_id"])   # track every conn the camera opens (e.g. a 2nd cid=40)
        if t == RDT_HELLO:
            self.hellos_seen += 1
            if self.first_hello_cid is None:
                self.first_hello_cid = rp["conn_id"]
            self.svc._send_rdt(self.channel, build_rdt_packet(RDT_HELLO_ACK, conn_id=rp["conn_id"]))
        elif t in (RDT_DATA, RDT_DATA_URGENT):
            idx = (rp["seqH"] << 32) | rp["seqL"]
            if idx not in self.packets:
                self.packets[idx] = rp["payload"]
                self.data_seen += 1
            hi = self._hi()
            if hi >= 0:
                self.svc._send_rdt(self.channel, build_rdt_packet(
                    RDT_CACK, seqL=hi & 0xFFFFFFFF, seqH=(hi >> 32) & 0xFFFFFFFF,
                    conn_id=(self.conn_id or 0)))
        elif t in (RDT_FIN, RDT_CLOSE, RDT_EXIT, RDT_ABORT):
            self.done.set()


class NativeScanSession:
    """NATIVE-MATCH session service for a manifest scan. A single persistent reader thread owns the
    socket for the whole scan (the native IOTC service-thread analog): it continuously answers
    keepalives, ACKs the camera's reliable channel, NOTES its channel-0 DATA (so the send-window
    never stalls), routes IOCTL responses to waiters, and feeds RDT frames to the active pull. IOCTL
    requests + RDT HELLO/CACK/CLOSE are sent by the caller under the SAME lock (single seq owner).
    This removes the reader GAP our inline ioctl()/RdtReceiver had between operations, so consecutive
    DownloadFiles run on ONE session like the app — no per-file reconnect. ADDITIVE / opt-in; the live
    AV path and the standalone RdtReceiver are untouched.

    Usage:  svc = NativeScanSession(inner).start(); ...; svc.download_manifest(dt); ...; svc.close()
    """
    def __init__(self, inner, verbose=False):
        import cuboai_pure as cp
        self.cp = cp
        self.inner = inner
        self.sock = inner._sock
        self.mid = bytes(cp._AV_MID)
        self.verbose = verbose
        self._lock = threading.Lock()          # guards ALL sends + inner _seq/_relseq/_frmno/_data_ack
        self._stop = threading.Event()
        self._resp_ev = {}                     # resp_type -> Event
        self._resp = {}                        # resp_type -> payload bytes
        self._rdt = None                       # active _ScanRdt during a pull (else None)
        self._reader = threading.Thread(target=self._loop, name="rdt-scan-service", daemon=True)
        self.reconnects = 0            # count of camera-forced session resets during the scan
        self.rdt_frames_seen = 0       # diag: total RDT frames the reader parsed (any channel)
        # native_open temp-id source (shared with RdtReceiver's process counter)
        self._native_open = _os_env("CUBOAI_RDT_NATIVE_OPEN", "1") == "1"
        # B-2: honour CUBOAI_RDT_NATIVE_CLOSE here TOO — the pull teardown used to ignore it and
        # always emit the malformed legacy FIN on this (default) scan path. Shared with RdtReceiver.
        self._native_close = _os_env("CUBOAI_RDT_NATIVE_CLOSE", "1") == "1"

    def start(self):
        self.sock.setblocking(False)
        try: self.ports = [self.sock.getsockname()[1]]     # diag: source-port pattern across reconnects
        except Exception: self.ports = []
        self._reader.start()
        return self

    def close(self):
        self._stop.set()
        try: self._reader.join(1.5)
        except Exception: pass

    # Context-manager form (LOW-5) so the reader thread + socket ownership are ALWAYS torn
    # down, even when a pull raises mid-scan (was: an exception skipped the explicit close()).
    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- locked low-level sends (single seq owner) ----
    def _sendto(self, frame):
        with self._lock:
            try: self.sock.sendto(frame, self.inner._cam)
            except OSError: pass

    def _ack(self):
        with self._lock:
            try: self.inner._send_ack()
            except Exception: pass

    def _send_rdt(self, channel, rdt_packet):
        with self._lock:
            frame = build_rdt_frame(self.inner._R, self.mid, self.inner._seq, channel, rdt_packet)
            self.inner._seq += 1
            # RELIABLE send: the socket is non-blocking, so at teardown (send buffer full from the CACK
            # burst) sendto() can EWOULDBLOCK-DROP the RDT FIN/CLOSE -> the camera never sees the RDT
            # close, keeps retransmitting, and ghosts (macOS block). Retry instead of dropping.
            for _ in range(30):
                try:
                    self.sock.sendto(frame, self.inner._cam); return
                except (BlockingIOError, OSError):
                    time.sleep(0.003)
            # All 30 retries blocked: the RDT frame (often the FIN/CLOSE) was DROPPED. This is the
            # exact ghost-conn precursor — surface it instead of returning silently.
            if self.verbose:
                print(f"[scan] _send_rdt: send buffer stayed full for 30 retries "
                      f"(ch={channel}) — RDT frame DROPPED (possible ghost-conn)", file=sys.stderr)

    # ---- the persistent reader (sole socket recv) ----
    def _loop(self):
        cp = self.cp; s = self.sock
        last_ack = 0.0
        while not self._stop.is_set():
            now = time.time()
            if now - last_ack > 0.2:               # IOTC-level liveness (keeps the session serviced)
                self._ack(); last_ack = now
            r, _, _ = select.select([s], [], [], 0.05)
            if not r:
                continue
            try:
                raw, addr = s.recvfrom(4096)
            except (BlockingIOError, OSError):
                continue
            if cp.is_keepalive_probe(raw):
                try: self._sendto(cp.build_keepalive_reply(raw))
                except OSError: pass
                continue
            try:
                dec = cp.inv_transcode(raw)
            except Exception:
                continue
            if len(dec) < 30:
                continue
            self._dispatch(dec)

    def _dispatch(self, dec):
        rp = parse_rdt_packet(dec)
        if rp is not None:
            self.rdt_frames_seen += 1              # diag: RDT frames the reader parsed (routed or not)
        rdt = self._rdt
        if rdt is not None and rp is not None:
            rdt.feed(rp)                           # route RDT frames to the active pull
            return
        sub = dec[28]
        if sub == 0x0C and len(dec) >= 68:
            # channel-0 (IOCTL) reliable DATA: NOTE it (advance _data_ack -> window stays open) + ACK,
            # then hand the response to any IOCTL waiter.
            with self._lock:
                try: self.inner._note_cam_data(dec)
                except Exception: pass
            self._ack()
            io = struct.unpack_from("<H", dec, 64)[0]
            ev = self._resp_ev.get(io)
            if ev is not None and not ev.is_set():
                avlen = struct.unpack_from("<H", dec, 52)[0]
                end = min(len(dec), 68 + max(0, avlen - 4))
                self._resp[io] = bytes(dec[68:end])
                ev.set()
        elif sub in (0x09, 0x0A):
            self._ack()

    # ---- IOCTL through the service (request/response via the reader) ----
    def ioctl(self, type_code, payload, timeout=8.0):
        cp = self.cp; inner = self.inner
        resp_type = type_code | 1
        ev = threading.Event()
        self._resp_ev[resp_type] = ev
        self._resp.pop(resp_type, None)
        with self._lock:
            frmno = inner._frmno
            req_relseq = inner._relseq
            req = cp.build_ioctl_data(inner._R, inner._seq, req_relseq, frmno, type_code, payload)
            inner._seq += 1; inner._relseq += 1; inner._frmno += 1
            try: self.sock.sendto(req, inner._cam)
            except OSError: pass
        t0 = time.time(); last_tx = t0
        try:
            while time.time() - t0 < timeout:
                if ev.wait(0.05):
                    return resp_type, self._resp.pop(resp_type, b"")
                now = time.time()
                if now - last_tx > 0.4:            # retransmit (same relseq/frmno, fresh pkt seq)
                    with self._lock:
                        req = cp.build_ioctl_data(inner._R, inner._seq, req_relseq, frmno, type_code, payload)
                        inner._seq += 1
                        try: self.sock.sendto(req, inner._cam)
                        except OSError: pass
                    last_tx = now
            raise TimeoutError(f"no response to IOCTL 0x{type_code:04x}")
        finally:
            self._resp_ev.pop(resp_type, None)

    # ---- RDT pull through the service ----
    def _next_open_cid(self):
        if not self._native_open:
            return 1
        cid = RdtReceiver._client_temp_id
        RdtReceiver._client_temp_id = 1 if cid >= 0xFE else cid + 1
        return cid

    def rdt_pull(self, channel, file_size, timeout=12.0):
        asm = _ScanRdt(self, channel)
        self._rdt = asm
        try:
            cid = self._next_open_cid()
            self._send_rdt(channel, build_rdt_packet(RDT_HELLO, conn_id=cid))   # client-initiated open
            t0 = time.time(); last_hello = t0
            while time.time() - t0 < timeout:
                if asm.collected() >= file_size and file_size > 0:
                    break
                if asm.done.is_set():
                    break
                now = time.time()
                if asm.data_seen == 0 and now - last_hello > 0.5:               # native_open retransmit
                    self._send_rdt(channel, build_rdt_packet(RDT_HELLO, conn_id=cid)); last_hello = now
                time.sleep(0.02)
            # native RDT_Destroy teardown (CLOSE(0x04,seq=0) by default, via rdt_close_packets),
            # delivered RELIABLY, then DRAIN until the camera STOPS the RDT stream. If we return while
            # the camera is still sending (retransmitting the tail), the RDT stays open at the camera
            # -> ghost session that blocks the next DownloadFile (the macOS symptom). self._rdt stays
            # set so the reader keeps CACKing trailing DATA; resend the close while the camera is still
            # active; stop on quiet.
            def _close_all():
                # Close EVERY conn the camera opened. The camera opens a SECOND RDT conn (seen on
                # macOS as cid=40) that we HELLO_ACK'd; closing only the DATA stream (cid=1) leaves it
                # open -> the camera HELLO-hammers it forever = ghost session that blocks the next
                # DownloadFile. Closing all conn_ids (and any new one that appears during the drain)
                # makes the camera go quiet, like Linux. B-2: honour native_close (was hard-wired to
                # the malformed legacy FIN); rdt_close_packets is the shared source with RdtReceiver.
                for c in (set(asm.conn_ids) or {0}):
                    for pkt in rdt_close_packets(self._native_close, {c}):
                        self._send_rdt(channel, pkt)
            _close_all()
            _end = time.time() + 1.5
            _last = self.rdt_frames_seen; _quiet = time.time()
            while time.time() < _end:
                time.sleep(0.05)
                if self.rdt_frames_seen != _last:                 # camera still active on the RDT stream
                    _last = self.rdt_frames_seen; _quiet = time.time()
                    _close_all()                                  # re-close all (incl. any new conn_id)
                elif time.time() - _quiet > 0.3:
                    break                                          # camera quiet -> RDT closed cleanly
            blob = b"".join(asm.packets[k] for k in sorted(asm.packets))
            return blob[:file_size] if file_size > 0 else blob, asm
        finally:
            self._rdt = None

    def _reconnect(self):
        """The camera won't serve a 2nd DownloadFile on one session (PROVEN camera-side even with this
        persistent-reader native architecture — the most native-faithful client still stalls); only a
        fresh session resets it. Coordinate a reconnect with the reader thread: stop it, re-handshake
        inner inline (reader stopped => no socket race), re-point the new socket, restart the reader."""
        self._stop.set()
        try: self._reader.join(1.5)
        except Exception: pass
        try: self.inner.disconnect()
        except Exception: pass
        self.inner.connect()
        self.reconnects += 1
        self.sock = self.inner._sock
        try: self.sock.setblocking(False)
        except Exception: pass
        try: self.ports.append(self.sock.getsockname()[1])   # diag: did the OS reuse the port?
        except Exception: pass
        self._stop = threading.Event()
        self._resp_ev = {}; self._resp = {}; self._rdt = None
        self._reader = threading.Thread(target=self._loop, name="rdt-scan-service", daemon=True)
        self._reader.start()

    def download_manifest(self, dt_utc, timeout=12.0, diag=None, reconnect_on_stall=True, _retried=False):
        """0x910 + serviced RDT pull of one hour's manifest -> (records, DownloadFileResp), all via the
        persistent reader. The camera stalls the 2nd+ same-session DownloadFile (camera-side limit), so
        reconnect_on_stall re-establishes the session once on an ioctl_timeout (the fresh-session reset
        the camera requires) — native persistent-reader architecture + the camera-forced session reset."""
        name = manifest_name(dt_utc)
        d = diag if isinstance(diag, dict) else None
        if d is not None:
            d["attempts"] = 2 if _retried else 1     # 2 => one reader-coordinated reconnect
        t0 = time.time()
        # short DownloadFile-IOCTL timeout: the camera stalls the 2nd+ same-session 0x910 SILENTLY
        # (no 0x911), so detect that fast and reconnect rather than waiting out the full timeout.
        io_to = min(3.5, timeout)
        try:
            _rt, data = self.ioctl(DOWNLOAD_FILE_REQ, build_download_req(name), timeout=io_to)
        except TimeoutError:
            if reconnect_on_stall and not _retried:
                self._reconnect()
                return self.download_manifest(dt_utc, timeout=timeout, diag=diag,
                                              reconnect_on_stall=reconnect_on_stall, _retried=True)
            if d is not None:
                d.update(stopped_reason="ioctl_timeout", data_seen=0, got_bytes=0,
                         elapsed=time.time() - t0)
            return [], None
        resp = parse_download_resp(data)
        if resp is None or resp.result != 0 or resp.file_size <= 0:
            if d is not None:
                d.update(stopped_reason="no_manifest", rdtChannel=(resp.rdtChannel if resp else None),
                         file_size=(resp.file_size if resp else 0), data_seen=0,
                         got_bytes=0, elapsed=time.time() - t0)
            return [], resp
        blob, asm = self.rdt_pull(resp.rdtChannel, resp.file_size, timeout=timeout)
        if d is not None:
            d.update(rdtChannel=resp.rdtChannel, file_size=resp.file_size,
                     data_seen=asm.data_seen, hellos_seen=asm.hellos_seen,
                     first_hello_cid=asm.first_hello_cid, got_bytes=len(blob),
                     elapsed=time.time() - t0,
                     stopped_reason=("complete" if len(blob) >= resp.file_size
                                     else ("never_started" if asm.data_seen == 0 else "short_read")))
        if len(blob) >= resp.file_size:
            try:
                return parse_manifest(blob), resp
            except Exception:
                if d is not None: d["stopped_reason"] = "parse_error"
                return [], resp
        return [], resp


def _os_env(name, default):
    import os
    return os.environ.get(name, default)


def build_coverage(transport, hours_back=3, retention_s=RETENTION_72H_S, now_utc=None):
    """Pull the last `hours_back` hourly manifests into a CoverageModel (prefer COMPLETED
    hours; the current hour may short-read while it is being written)."""
    import datetime as _dt
    now_utc = now_utc or _dt.datetime.now(_dt.timezone.utc)
    cov = CoverageModel(retention_s=retention_s)
    for h in range(hours_back):
        recs, _ = pull_manifest(transport, now_utc - _dt.timedelta(hours=h))
        if recs:
            cov.add_manifest(recs)
    return cov

def _classify_au(unit):
    if unit[:4] == b"\x00\x00\x00\x01":
        return 'video'
    if len(unit) >= 2 and unit[0] == 0xFF and (unit[1] & 0xF6) == 0xF0:
        return 'audio'
    return None

class PlaybackReader:
    """ADDITIVE channel-demux reassembler for recorded DVR playback. Processes ONLY
    `[14]==channel` AV fragments into recorded access units, keeping its OWN fragment
    state — it never touches the live path / `_av_reader` / the live-channel frag-seq the
    SACK/retransmit machinery keys on. Simple in-order reassembly (recorded playback over
    LAN is low-loss and needs less aggressive recovery than live): group by msg-index
    `[56:58]`, order by frag-seq `[46:48]`, concatenate payloads `dec[64:64+avlen]`, emit a
    complete (contiguous-from-0) AU once `grace_msgs` newer messages have started. Parses
    the 24-B FRAMEINFO for ts_sec so callers can prove recorded (ts≈target) vs live."""
    _FI_SCAN = 256              # bytes at the AU tail to scan for the zero-padded FRAMEINFO
    _SACK_WINDOW = 384          # max outstanding frag window before abandoning an unfilled hole
    _HOLD_MAX = 48              # max AUs to HOLD an unproven-complete AU before force-emitting (see _emit)

    def __init__(self, channel, grace_msgs=3):
        import os as _os
        import cuboai_pure as cp
        self.cp = cp
        self.channel = channel
        self.grace = grace_msgs
        # WRAP FIX (audit C2, 2026-07-23) — THIRD instance of the "non-modular compare on a u16 wire
        # counter" class (after AV done_upto/H1 and _data_ack/CUBOAI_DATAACK_WRAP). The DVR msg-index
        # [56:48] and frag-seq [46:48] are u16 and WRAP at 65536; `self.hi`/`self.done_upto` were
        # unbounded Python ints compared RAW. At the msg-index wrap (~72 min of continuous playback at
        # ~15 AU/s) `idx` restarts near 0 while `hi` sits at 65535, so `idx > self.hi` never fires
        # again: `hi` freezes, `_emit`'s `while i <= self.hi` runs dry and the playback stream
        # SILENTLY DEAD-STALLS (exactly H1's failure mode, one file over). ON: lift each index into
        # done_upto's unbounded space (cp._unwrap_index, the H1 helper) and drop only genuinely-stale
        # (modularly BEHIND) indices; also make the per-AU fragment ordering/contiguity test modular
        # so an AU straddling the frag-seq wrap isn't misread as non-contiguous. `=0` reverts to the
        # exact legacy comparisons. Pre-wrap output is byte-identical (test_playback_wrap.py).
        self._wrapfix = _os.environ.get("CUBOAI_PB_WRAP", "1") != "0"
        self.msgs = {}          # msg-index -> {frag-seq: chunk}
        self.hi = -1            # highest msg-index seen (wrapfix: in done_upto's unbounded space)
        self.done_upto = -1     # highest msg-index emitted/skipped
        self.stale = 0          # frames dropped as modularly-behind duplicates (wrapfix only)
        self.frag_hi = None     # highest frag-seq [46:48] seen (for channel-N ACK C/D)
        self.frag_prev = 0xFFFF
        self.frames = 0         # channel-N fragments fed
        self.frames_video = 0   # video AUs emitted
        self.frames_audio = 0   # audio AUs emitted
        self.dropped = 0        # msg-indices dropped (incomplete / non-contiguous)
        # ── channel-N fragment ledger (for the optional missing-frag SACK) ──
        self.frag_received = set()   # abs frag-seqs [46:48] received in the live window
        self.frag_una = None         # contiguous edge: lowest frag-seq not yet received (holds at a hole)
        self.req_frags = set()       # frag-seqs we've SACK-requested (for the honor-rate metric)
        self.honored = 0             # requested frags that subsequently ARRIVED (armed-resend proof)
        self.sub2 = 0                # secondary-substream frags skipped (byte[29] & 0x08 — see feed())
        self.vdbg = []          # diagnostic: (unit_len, ks0, ksN, nfrags, fi_off, ts_sec) for 1st video AUs

    def feed(self, dec, out):
        """Feed one decoded IOTC frame; append (kind, unit, ts_sec) AUs to `out`."""
        if len(dec) < 68 or dec[28] != 0x0C:
            return
        if dec[14] != self.channel:
            return                                  # not our playback channel — ignore (live etc.)
        if dec[58:64] == b"\x00\x00\x00\x00\x00\x00":
            return                                  # reliable IO/control response, not an AV fragment
        # DVR channel N MULTIPLEXES two independent reliable streams under one [14]==N: the A/V
        # stream (byte[29] & 0x08 == 0 — HEVC + AAC share ONE dense frag-seq [46:48] counter, like
        # live channel 0) and a SEPARATE low-rate sub-stream (byte[29] & 0x08 set — DVR index/timing,
        # its OWN frag-seq counter in a far band ~5120+, no rendered media). Wire-proven on nat/pure
        # DVR pcaps: byte[29]&0x08 separates the two frag-seq bands with ZERO crossover. Folding the
        # second stream into the video ledger makes frag_hi LEAP ~1800 frags, so the whole inter-band
        # gap reads as phantom holes and the 0x09 SACK requests seqs the camera never sent (0 honored,
        # the ~11-drop GOP-cascade). Drop it from BOTH reassembly and the ledger (it carries nothing
        # we render), leaving the A/V frag-seq contiguous so hole-detection targets the REAL losses.
        if dec[29] & 0x08:
            self.sub2 += 1
            return
        idx = struct.unpack("<H", dec[56:58])[0]
        frag = struct.unpack("<H", dec[46:48])[0]
        avlen = struct.unpack("<H", dec[52:54])[0]
        chunk = bytes(dec[64:64 + max(0, avlen)])
        self.frames += 1
        if self.frag_hi is None or ((frag - self.frag_hi) & 0xFFFF) < 0x8000:
            self.frag_hi = frag                     # advance high-water (skip small backward jitter)
        # fragment ledger for the channel-N SACK: record receipt + advance the contiguous edge
        if frag in self.req_frags and frag not in self.frag_received:
            self.honored += 1        # a frag we asked the camera to RESEND actually arrived (armed)
        self.frag_received.add(frag)
        if self.frag_una is None:
            self.frag_una = frag                    # seed the edge at the first frag we see
        while self.frag_una in self.frag_received:  # slide past every filled slot
            self.frag_una = (self.frag_una + 1) & 0xFFFF
        # A permanently-lost frag would freeze the edge (and stall the camera's send-window)
        # forever. Cap the outstanding window: if the contiguous edge C=(una-1) falls a REAL
        # distance > _SACK_WINDOW behind the high-water, ABANDON the old hole (jump the edge
        # forward) — bounded, and recorded playback tolerates the rare dropped AU.
        # GUARD the modular subtraction: when we are fully caught up the edge sits AT frag_hi
        # (una == frag_hi+1), so (frag_hi - una) wraps to ~0xFFFF — that is a −1 lead, NOT a
        # 65k-deep backlog. Requiring the lag to be in the "behind" half (<= 0x8000) stops the
        # old unguarded check from firing the instant the stream caught up, which poisoned una to
        # frag_hi-WINDOW and every subsequent SACK C to a wrapped 65xxx value the camera could not
        # honor (wire-proven: SACKs carried C=65158 while D=7). Prune the ledger to the window.
        _lag = (self.frag_hi - ((self.frag_una - 1) & 0xFFFF)) & 0xFFFF
        if self._SACK_WINDOW < _lag <= 0x8000:
            self.frag_una = (self.frag_hi - self._SACK_WINDOW) & 0xFFFF
            while self.frag_una in self.frag_received:
                self.frag_una = (self.frag_una + 1) & 0xFFFF
        if len(self.frag_received) > 4096:
            self.frag_received = {f for f in self.frag_received
                                  if ((self.frag_hi - f) & 0xFFFF) <= 2048}
        if self.done_upto < 0:
            self.done_upto = idx - 1
        if self._wrapfix:
            # Lift the u16 wire index into done_upto's unbounded monotonic space. Pre-wrap this is
            # the identity for every FORWARD index, so behaviour is unchanged until the first wrap;
            # across it the space continues 65535 -> 65536 and the `> self.hi` / `i <= self.hi`
            # compares keep working. A modularly-BEHIND index (a duplicate/late frag for an AU we
            # already emitted or skipped) lifts to > +32768 and is dropped — legacy parked it in
            # `self.msgs` where nothing ever popped it (a slow leak), so dropping it changes no
            # output. Forward gaps up to 32767 AUs are still accepted, i.e. no new stall mode.
            _d = (idx - self.done_upto) & 0xFFFF
            if _d == 0 or _d > 0x8000:
                self.stale += 1
                return
            idx = self.done_upto + _d
        self.msgs.setdefault(idx, {})[frag] = chunk
        if idx > self.hi:
            self.hi = idx
        self._emit(out)

    def flush(self, out):
        self._emit(out, final=True)

    @staticmethod
    def _frag_sorted(fm):
        """Frag-seqs of ONE AU in wire order, wrap-safe (wrapfix): order by SIGNED modular offset
        from an arbitrary member, so an AU straddling the u16 frag-seq wrap (…65534, 65535, 0, 1…)
        orders — and tests contiguous — exactly as it would mid-range. An AU spans ~69 frags, far
        under the 0x8000 half-space, so the offset ordering is independent of which member seeds it.
        Pre-wrap this returns exactly `sorted(fm)`, so nothing changes below the wrap."""
        k0 = next(iter(fm))
        return sorted(fm, key=lambda k: ((k - k0 + 0x8000) & 0xFFFF) - 0x8000)

    def _emit(self, out, final=False):
        """Emit AUs as (kind, unit, info) where info is the parsed FRAMEINFO dict (or None).
        The dict is exactly what cuboai_pts.AVTimeline.video()/audio() consumes, so recorded AUs
        get PTS through the SAME timeline the live path uses (shared-base, drift-free A/V sync).

        RECOVERY HOLD (replaces the old fixed grace=3 window): emit AUs strictly in msg-index
        order, HOLDING an AU until the contiguous frag edge (frag_una) proves every one of its
        fragments has arrived — i.e. the edge has advanced to the FIRST fragment of the NEXT AU.
        Recorded frag-seqs [46:48] are GLOBALLY contiguous across AUs, so the edge reaching that
        boundary means AU i has no interior AND no tail hole left. DVR has ~0 permanent frag loss
        once resends are honored (wire-proven: video-band present=1.000), so an AU the old window
        emitted TRUNCATED before its late resend landed (→ decode error → GOP cascade) is now held
        the extra few ms and decodes clean. Bounded by _HOLD_MAX AUs (and forced on flush) so a
        genuinely-lost fragment can never stall the stream."""
        cp = self.cp
        i = self.done_upto + 1
        while i <= self.hi:
            # AU i is provably COMPLETE once the contiguous frag edge has reached the next AU's
            # first fragment (all of AU i's frags, interior + tail, are in). Locate that boundary.
            j = i + 1
            while j <= self.hi and j not in self.msgs:
                j += 1
            if j <= self.hi and j in self.msgs:
                nf = (self._frag_sorted(self.msgs[j])[0] if self._wrapfix else min(self.msgs[j]))
            else:
                nf = None
            edge_reached = (nf is not None and self.frag_una is not None
                            and ((self.frag_una - nf) & 0xFFFF) < 0x8000)
            expired = final or (self.hi - i) > self._HOLD_MAX
            if not (edge_reached or expired):
                break                            # HOLD AU i (and all after) for an in-flight resend
            fm = self.msgs.pop(i, None)
            if fm:
                ks = self._frag_sorted(fm) if self._wrapfix else sorted(fm)
                # complete AU: the fragment-seqs [46:48] are the GLOBAL frag counter (consecutive
                # but NOT starting at 0 per-AU), so completeness = CONTIGUOUS, any start (like the
                # live reader's (ks[-1]-ks[0]+1)==len(ks)). Concatenate in frag-seq order.
                # wrapfix: the span is measured modularly, so an AU straddling the frag-seq wrap
                # reads contiguous instead of spanning a phantom 65536 (legacy dropped that AU and
                # stuttered the hold for _HOLD_MAX AUs once per 65536 frags).
                _span = (((ks[-1] - ks[0]) & 0xFFFF) + 1 if self._wrapfix
                         else (ks[-1] - ks[0] + 1)) if ks else 0
                if ks and _span == len(ks):
                    unit = b"".join(fm[k] for k in ks)
                    kind = _classify_au(unit)
                    if kind == 'video':
                        # Recorded video AUs zero-pad the tail (no FRAMEINFO at [-24:] like live).
                        # SCAN the last _FI_SCAN bytes for the 24-B video FRAMEINFO (codec_id in the
                        # video range + plausible w/h); if found, parse it for PTS/keyframe and cut
                        # from there; else trim trailing zeros. Emit contiguous video AUs regardless
                        # (they decode; a missing-FRAMEINFO tail just loses this AU's timing).
                        info = None
                        _lo = max(0, len(unit) - self._FI_SCAN)
                        for j in range(len(unit) - cp._FRAMEINFO_LEN, _lo - 1, -1):
                            if cp._looks_like_frameinfo(unit[j:j + cp._FRAMEINFO_LEN]):
                                info = cp._parse_frameinfo(unit[j:j + cp._FRAMEINFO_LEN])
                                unit = unit[:j]; break
                        else:
                            unit = unit.rstrip(b"\x00")        # drop tail zero-padding
                        if len(self.vdbg) < 6:
                            self.vdbg.append((len(unit), ks[0], ks[-1], len(ks),
                                              info is not None, info.get('ts_sec') if info else None))
                        out.append(('video', unit, info)); self.frames_video += 1
                    elif kind == 'audio':
                        fl = cp._adts_frame_len(unit)
                        if fl and 7 <= fl and len(unit) >= fl + cp._FRAMEINFO_LEN \
                                and cp._looks_like_audio_frameinfo(unit[fl:fl + cp._FRAMEINFO_LEN]):
                            info = cp._parse_audio_frameinfo(unit[fl:fl + cp._FRAMEINFO_LEN])
                            out.append(('audio', unit[:fl], info)); self.frames_audio += 1
                        else:
                            self.dropped += 1
                else:
                    self.dropped += 1                          # non-contiguous -> incomplete, drop
            self.done_upto = i
            i += 1

    def compute_channel_sack(self, req_ts, req_interval, max_fid=64):
        """Missing-frag SACK for channel N (mirrors the live _compute_holes/_send_ack semantics):
        C = una (contiguous edge, holds at the first hole), D = high-water, sack = the MISSING
        frag-seqs in (una, high-water] not requested within req_interval. Returns (C, D, sack)
        with sack a list of ABSOLUTE frag-seqs (build_data_ack encodes each as (frag-C)); the
        camera RESENDS exactly those. `req_ts` is a per-hole {frag: last-request-time} dict the
        caller owns (so a still-missing hole is re-asked next round). A lone hole is padded to
        count>=2 (the camera reads a count-1 entry as a timestamp)."""
        import time as _t
        if self.frag_hi is None or self.frag_una is None:
            return 0xFFFF, 0xFFFF, None
        # C = highest CONTIGUOUS frag received (the low edge, == live's _frag_edge). frag_una is the
        # next-expected (one past the contiguous run), so C = frag_una-1. D = high-water. When fully
        # caught up (C==D) there are no holes -> a plain cumulative ACK.
        C = (self.frag_una - 1) & 0xFFFF
        D = self.frag_hi
        span = (D - C) & 0xFFFF
        if span == 0 or span > 0x8000:
            return C, D, None
        now = _t.time()
        holes = [(C + k) & 0xFFFF for k in range(1, span + 1)
                 if ((C + k) & 0xFFFF) not in self.frag_received][:max_fid]
        fresh = [h for h in holes if now - req_ts.get(h, 0.0) > req_interval]
        if not fresh:
            return C, D, None
        for h in fresh:
            req_ts[h] = now
        self.req_frags.update(fresh)                  # track for the honor-rate metric
        if len(fresh) == 1:
            fresh = [fresh[0], fresh[0]]              # pad lone hole to count>=2 (dup resends only it)
        # prune filled holes so req_ts can't grow unbounded
        if len(req_ts) > 2048:
            for h in [h for h in req_ts if h in self.frag_received]:
                req_ts.pop(h, None)
        return C, D, fresh

def build_channel_ack(inner, channel, C, D, sack=None, seq=None, relseq=None, ackord=None):
    """A data-channel ACK (sub 0x09) routed to `channel` via [14] (native ACKs the playback channel
    on [14]=N). Reuses build_data_ack (C/D advance the camera's send-window; `sack` = absolute
    missing frag-seqs the camera should RESEND) then overrides [14]. seq/relseq/ackord override the
    inner (channel-0) counters — the playback channel keeps its OWN reliable-frame state (native
    parity: nat.pcap shows per-channel seq/relseq/ackord with ZERO overlap)."""
    import cuboai_pure as cp
    fr = cp.build_data_ack(inner._R,
                           inner._seq if seq is None else seq,
                           inner._relseq if relseq is None else relseq,
                           inner._ack_ord if ackord is None else ackord,
                           C, D, sack=sack)
    p = bytearray(cp.inv_transcode(fr)); p[14] = channel & 0xFF
    return cp.transcode(bytes(p))


def build_channel_resend_b(inner, channel, ts, seq=None):
    """The 0x0b AVStatisticACK routed to `channel` via [14]. Carries THE ARMING clock-echo at
    [36:38] (ts = the session 0x0a ms-clock, advanced by elapsed wall time). 0x0b is UNRELIABLE
    ([32:34]=0) so it consumes only a per-channel seq, no relseq."""
    import cuboai_pure as cp
    fr = cp.build_resend_b(inner._R, inner._seq if seq is None else seq, 8, ts=ts)
    p = bytearray(cp.inv_transcode(fr)); p[14] = channel & 0xFF
    return cp.transcode(bytes(p))


def build_channel_resend_req(inner, channel, relseq, highwater=0, resend_timeout_ms=35, seq=None):
    """The 0x0a AVStatistic 'NAK' routed to `channel` via [14]. highwater=0 in selective mode (the
    0x09 SACK drives resends); paired with build_channel_resend_b it ARMS retransmit on the channel.
    seq overrides the inner counter (the playback channel has its own per-channel seq)."""
    import cuboai_pure as cp
    fr = cp.build_resend_req(inner._R, inner._seq if seq is None else seq, relseq, highwater=highwater,
                             resend_timeout_ms=resend_timeout_ms)
    p = bytearray(cp.inv_transcode(fr)); p[14] = channel & 0xFF
    return cp.transcode(bytes(p))

def open_playback_channel(inner, channel, n_subs=4):
    """Open the camera-assigned DVR playback channel N — mirrors the app's
    avClientStartEx(iotc_channel=N). NATIVE-STYLE framing (2026-07-16 capture): av-connects
    with **[14]=N** (the IOTC channel) and **[6:8]=sub-index 0..n_subs-1** (per-channel seq),
    NOT [6]=channel/[14]=0 (pure's old bug). Returns the wire frames (send/re-send to taste;
    native re-sends with an incrementing [6:8] until the camera grants)."""
    import cuboai_pure as cp
    nO = getattr(inner, "_nO", None)
    if nO is None:
        raise RuntimeError("session has no stashed _nO (need cuboai_pure connect() with the "
                           "playback storage patch)")
    tok = getattr(inner, "_av_token", None)
    frames = [cp.build_av_connect(None, nO, sub, inner.account, inner.password,
                                  token=tok, R=inner._R, iotc_channel=channel)
              for sub in range(n_subs)]
    for fr in frames:
        inner._sock.sendto(fr, inner._cam)
    return frames


# IPCAM_STOP (0x2ff): stop a live AV stream. Fire-and-forget (odd io_type, no response).
IPCAM_STOP = 0x2ff

def build_avstream_payload(channel: int = 0, stream_no: int = 4, bw: int = 1) -> bytes:
    """SMsgAVIoctrlAVStream {channel@0(LE i32), streamNo@4, bw@6} — the 0x2ff STOP body."""
    p = bytearray(8)
    struct.pack_into("<i", p, 0, channel)
    p[4] = stream_no & 0xFF
    p[6] = bw & 0xFF
    return bytes(p)


class PlaybackSession:
    """Gated LOCAL DVR 'rewind & watch' over an already-connected TUTKDirectSession.

    MUTUALLY EXCLUSIVE with live. start(target) stops the live VIDEO (0x2ff; live audio is left
    running but IGNORED — matching the app, which just stops READING channel 0) and starts
    recorded playback via 0x31a on the camera-assigned channel N. A DEDICATED reader thread — the
    SOLE socket reader while active, mirroring cuboai_pure._av_reader — drains the socket,
    keepalive-replies, feeds a PlaybackReader with ONLY [14]==N fragments, ACKs channel 0 (session
    liveness) + channel N (send-window, plus an optional missing-frag SACK resend request), and
    puts recorded (kind, unit, info) AUs on an output queue. seek() = STOP+START at a new target.
    close()/__exit__ ALWAYS restore the live video (NEVER leave the camera in playback with no live
    monitor). ADDITIVE / opt-in — the production live path never imports this module, so the live
    stdout stays byte-identical and the validator/replay SHAs are unaffected.

    Because ONE thread owns the socket at a time (live reader stopped during playback; the 0x31a
    request/response runs while the playback reader thread is stopped), the shared inner counters
    (_seq/_relseq/_frmno/_ack_ord) are never raced. Live and playback are mutually exclusive by
    construction, not by lock."""

    def __init__(self, transport, sack=True, verbose=False, log=None,
                 ack0_interval=0.1, ackN_interval=0.04, reopen_interval=0.15,
                 engage_tol_s=600, arm=True, nak_interval=0.19):
        import cuboai_pure as cp
        self.cp = cp
        self.transport = transport
        self.inner = getattr(transport, "_inner", transport)
        self.use_sack = sack
        self.verbose = verbose
        self._log = log or (lambda *a: None)
        self.ack0_interval = ack0_interval
        self.ackN_interval = ackN_interval
        self.reopen_interval = reopen_interval
        self.engage_tol_s = engage_tol_s
        self.channel = None            # assigned playback channel N
        self.target = None             # current requested epoch-seconds target
        self.reader = None             # PlaybackReader for channel N
        self._out = queue.Queue(maxsize=8000)
        self._stop_evt = threading.Event()
        self._thread = None
        self._live_stopped = False
        self._hole_req = {}            # {frag-seq: last-request-time} owned across ACK rounds
        # ── channel-N retransmit ARMING (S86 mechanism, ported per-channel) ──
        self.arm = arm                 # send the 0x0b/0x0a NAK pair on channel N (arms resend)
        self.nak_interval = nak_interval
        self._cam_clock_N = None       # session ms-clock from cam->host 0x0a [36:38] (any channel)
        self._cam_clock_ts_N = None    # local time when the clock was captured
        self._nak0a_seen = 0           # cam->host 0x0a frames seen on channel N (instrumentation)
        self._feed_errors = 0          # B-6: malformed frames that raised in reader.feed (skipped)
        # PER-CHANNEL reliable-frame state for the playback channel (native parity: nat.pcap shows
        # seq/relseq/ackord kept SEPARATELY per channel — sharing inner's breaks the camera's
        # per-channel reliable-stream tracking → SACK ignored). Reset per start()/seek().
        self._seq_N = 0; self._relseq_N = 0; self._ackord_N = 0
        self.stats = dict(started=0, seeks=0, aus_video=0, aus_audio=0, frames=0,
                          dropped=0, resend_req=0, honored=0, cam0a_seen=0, engaged=False)

    # ── low-level send helpers (fire-and-forget IOCTLs on the shared socket) ──
    def _send_ioctl(self, io_type, payload):
        inner = self.inner
        inner._sock.sendto(self.cp.build_ioctl_data(inner._R, inner._seq, inner._relseq,
                                                    inner._frmno, io_type, payload), inner._cam)
        inner._seq += 1; inner._relseq += 1; inner._frmno += 1

    def _stop_live_video(self):
        """IPCAM_STOP 0x2ff (channel 0) — stop the live VIDEO stream only. Live audio keeps
        flowing on channel 0 (we simply stop reading/emitting it). Idempotent-ish; safe to repeat."""
        self._send_ioctl(IPCAM_STOP, build_avstream_payload())
        self._live_stopped = True

    def _reset_live_frag_state(self):
        """Reset ONLY the live AV-fragment reassembly/recovery state — NOT the session sequence
        counters (_seq/_relseq/_frmno/_ack_ord), which MUST stay monotonic mid-session — so the
        live reader re-learns the current channel-0 fragment-seq after playback. Channel 0's
        frag-seq advanced while we weren't reading it (live audio kept flowing during playback), so
        a stale _frag_D would make the live ACK advertise a long-dead low edge → the camera's live
        send-window never advances and 0 live frames arrive (the GATE-C live_restored=False symptom).
        Mirrors the fragment-state subset of connect()'s idle reset."""
        i = self.inner
        for attr, val in (('_frag_D', None), ('_frag_C', 0xFFFF), ('_frag_edge', None),
                          ('_frag_edge_acked', 0xFFFF), ('_got_first', False), ('_ts_ref', None)):
            if hasattr(i, attr):
                setattr(i, attr, val)
        for attr in ('_frag_received',):
            if hasattr(i, attr):
                setattr(i, attr, set())
        for attr in ('_frag_gap_ts', '_hole_req_ts', '_hole_first_req'):
            if hasattr(i, attr):
                setattr(i, attr, {})

    def restore_live(self):
        """Re-issue the live VIDEO_START sequence so the camera resumes live video on channel 0.
        ALWAYS run on close — the camera must never be left in playback with no live monitor. Resets
        the live fragment state first so the reader re-syncs to the (advanced) channel-0 frag-seq."""
        inner = self.inner
        self._reset_live_frag_state()
        start = list(inner._VIDEO_START) + [inner._VIDEO_START_MID, inner._VIDEO_START_LATE]
        for io_type, pl in start:
            self._send_ioctl(io_type, pl); time.sleep(0.02)
        self._live_stopped = False

    def _playrecord(self, command, target, disable_timecontrol=0):
        """0x31a request/response (run with the reader thread STOPPED so nothing races recvfrom).
        Returns the assigned channel N (>=0) on START, else the raw result (<0 on error)."""
        if command == PLAY_CMD_START:
            body = build_playrecord_start(target, disable_timecontrol=disable_timecontrol)
        else:
            body = build_playrecord_stop(target, disable_timecontrol=disable_timecontrol)
        _rt, data = self.transport.ioctl(PLAYRECORD_REQ, body)
        pr = parse_playrecord_resp(data)
        return pr.result if pr is not None else None

    def _drain_socket(self, secs):
        """Consume + discard all pending datagrams for `secs` (keepalive-replying) so the socket is
        QUIET before a 0x31a request — otherwise transport.ioctl can read a residual playback DATA
        frame or a stale 0x31b instead of the fresh response (the seek 'N=0' collision bug)."""
        s = self.inner._sock; cp = self.cp; t0 = time.time()
        while time.time() - t0 < secs:
            r, _, _ = select.select([s], [], [], 0.05)
            if not r:
                continue
            while True:
                try: raw, addr = s.recvfrom(8192)
                except (BlockingIOError, OSError): break
                if cp.is_keepalive_probe(raw):
                    try: s.sendto(cp.build_keepalive_reply(raw), addr)
                    except OSError: pass

    def _start_playback_ioctl(self, target, disable_timecontrol, tries=8, drain_s=0.5, gap_s=0.6):
        """Drain the socket (so transport.ioctl reads the FRESH 0x31b, not a residual playback DATA
        frame or a stale STOP response — the seek 'N=0' collision), then 0x31a START. A valid
        playback channel is >=1: channel 0 is the live channel, and a stale-frame misread shows up
        as 0 (or None/negative).

        The camera also returns **-1 for a target that is too FRESH** — it is still finalizing the
        just-recorded footage into the playable store (proven by the user's live camera: 5-min target
        works first try; 3-min target succeeded on the 3rd retry; 1-min failed in 3). So retry the
        SAME target PATIENTLY (~8 tries over several seconds) — the footage becomes servable within a
        few seconds — before the caller falls back to an older target."""
        N = None
        for i in range(tries):
            self._drain_socket(drain_s)
            N = self._playrecord(PLAY_CMD_START, target, disable_timecontrol)
            if N is not None and N >= 1:
                return N
            self._log(f"START refused (result={N}); retry {i + 1}/{tries} "
                      "(camera may still be finalizing fresh footage)")
            try: self._playrecord(PLAY_CMD_STOP, target)
            except Exception: pass
            time.sleep(gap_s)
        return N

    # ── lifecycle ──
    def start(self, target, disable_timecontrol=0):
        """Begin recorded playback at epoch-seconds `target`. Stops live video, issues 0x31a START,
        opens channel N, and spins up the dedicated reader thread. Returns N (>=0) or raises."""
        if self._thread is not None:
            raise RuntimeError("PlaybackSession already active — call seek()/stop_playback() first")
        if not self._live_stopped:
            self._stop_live_video(); time.sleep(0.4)
        N = self._start_playback_ioctl(target, disable_timecontrol)
        if N is None or N < 1:                    # channel 0 is LIVE — never run playback on it
            raise RuntimeError(f"0x31a START rejected (result={N}) for target={target}")
        self.channel = N; self.target = target
        self.reader = PlaybackReader(N)
        self._hole_req = {}
        # fresh per-channel reliable state for channel N (start seq past the av-connect sub-indices
        # 0..3 that open_playback_channel sends, so our first ACK doesn't collide with a sub).
        self._seq_N = 4; self._relseq_N = 0; self._ackord_N = 0
        self._cam_clock_N = None; self._cam_clock_ts_N = None
        try:                                   # drop any AUs left from a prior target (defensive;
            while True: self._out.get_nowait()  # seek() drains too — keeps a reused session clean)
        except queue.Empty:
            pass
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._reader_loop, name="pb-reader", daemon=True)
        self._thread.start()
        self.stats['started'] += 1
        self._log(f"playback START target={target} -> channel N={N}")
        return N

    def _reader_loop(self):
        cp = self.cp; inner = self.inner; s = inner._sock
        reader = self.reader; N = self.channel
        last_open = 0.0; last_ack0 = 0.0; last_ackN = 0.0; last_nak = 0.0
        frag_prev = 0xFFFF; engaged = False
        while not self._stop_evt.is_set():
            now = time.time()
            if not engaged and now - last_open > self.reopen_interval:
                try: open_playback_channel(inner, N)      # av-connects [14]=N until the camera grants
                except Exception: pass
                last_open = now
            if now - last_ack0 > self.ack0_interval:
                try: inner._send_ack()                    # channel-0 session/reliable-IO liveness
                except Exception: pass
                last_ack0 = now
            if reader.frag_hi is not None and now - last_ackN > self.ackN_interval:
                if self.use_sack:
                    C, D, sack = reader.compute_channel_sack(self._hole_req, 0.15)
                    if sack: self.stats['resend_req'] += len(sack)
                else:
                    C, D, sack = frag_prev, reader.frag_hi, None
                try:                              # channel-N ACK uses channel N's OWN counters
                    s.sendto(build_channel_ack(inner, N, C, D, sack=sack, seq=self._seq_N,
                             relseq=self._relseq_N, ackord=self._ackord_N), inner._cam)
                    self._seq_N += 1; self._relseq_N += 1; self._ackord_N += 1; frag_prev = D
                except Exception: pass
                last_ackN = now
            # ARM retransmit: echo the session ms-clock in the 0x0b/0x0a pair (THE arming
            # discriminator, S86). NATIVE PARITY (nat.pcap): the full pair runs on BOTH channel 0
            # (still carrying live audio, inner's counters) AND channel N (its OWN counters). Without
            # per-channel reliable state + arming the 0x09 SACK is ignored (~0% honor).
            if self.arm and self._cam_clock_N is not None and now - last_nak > self.nak_interval:
                ts_b = (self._cam_clock_N + int((now - self._cam_clock_ts_N) * 1000)) & 0xFFFF
                try:                               # channel 0 — inner's (channel-0) counters
                    s.sendto(build_channel_resend_b(inner, 0, ts_b), inner._cam); inner._seq += 1
                    s.sendto(build_channel_resend_req(inner, 0, inner._relseq, highwater=0),
                             inner._cam)
                    inner._seq += 1; inner._relseq += 1
                except Exception: pass
                try:                               # channel N — its OWN per-channel counters
                    s.sendto(build_channel_resend_b(inner, N, ts_b, seq=self._seq_N), inner._cam)
                    self._seq_N += 1
                    s.sendto(build_channel_resend_req(inner, N, self._relseq_N, highwater=0,
                             seq=self._seq_N), inner._cam)
                    self._seq_N += 1; self._relseq_N += 1
                except Exception: pass
                last_nak = now
            r, _, _ = select.select([s], [], [], 0.02)
            if not r:
                continue
            drained = []
            while True:
                try: raw, addr = s.recvfrom(8192)
                except (BlockingIOError, OSError): break
                if cp.is_keepalive_probe(raw):
                    try: s.sendto(cp.build_keepalive_reply(raw), addr)
                    except OSError: pass
                    continue
                try: dec = cp.inv_transcode(raw)
                except Exception: continue
                # capture the camera's SESSION ms-clock from its cam->host 0x0a [36:38] so the
                # NAK-pairs above can echo it (arming). NATIVE PARITY (nat.pcap): the 0x0a clock is
                # ONE session-wide monotonic clock sent on BOTH channels, and native echoes the
                # latest value from EITHER — so capture from ANY 0x0a, not just channel N.
                if len(dec) >= 38 and dec[28] == 0x0A:
                    self._cam_clock_N = struct.unpack_from('<H', dec, 36)[0]
                    self._cam_clock_ts_N = time.time()
                    if dec[14] == N:
                        self._nak0a_seen += 1
                # B-6: reader.feed parses AU/ADTS headers; a malformed frame that raises must NOT
                # kill this daemon thread (that death is silent — read() just times out with no log,
                # leaving the camera in playback with no reader). Skip the bad frame, keep going.
                try:
                    reader.feed(dec, drained)
                except Exception as e:
                    self._feed_errors += 1
                    if self._feed_errors <= 5:
                        self._log(f"reader.feed raised on a frame "
                                  f"({type(e).__name__}: {e}) — skipping, playback continues")
            for au in drained:
                if (not engaged and au[0] == 'video' and au[2] and au[2].get('ts_sec')
                        and (self.target is None
                             or abs(au[2]['ts_sec'] - self.target) <= self.engage_tol_s)):
                    engaged = True; self.stats['engaged'] = True   # recorded video is flowing -> stop reflooding
                try: self._out.put_nowait(au)
                except queue.Full: pass
        # final flush of any complete-but-held AUs, then publish counters
        try:
            drained = []; reader.flush(drained)
            for au in drained:
                try: self._out.put_nowait(au)
                except queue.Full: pass
        except Exception: pass
        self.stats.update(frames=reader.frames, aus_video=reader.frames_video,
                          aus_audio=reader.frames_audio, dropped=reader.dropped,
                          honored=reader.honored, cam0a_seen=self._nak0a_seen,
                          req_frags=len(reader.req_frags), sub2=reader.sub2,
                          feed_errors=self._feed_errors)

    def _stop_reader(self):
        if self._thread is not None:
            self._stop_evt.set()
            self._thread.join(timeout=3.0)
            self._thread = None

    def read(self, timeout=0.5):
        """Pop one recorded AU (kind, unit, info) or None on timeout. info is the parsed FRAMEINFO
        dict (feed straight to cuboai_pts.AVTimeline.video()/audio()) or None (interpolate)."""
        try:
            return self._out.get(timeout=timeout)
        except queue.Empty:
            return None

    def __iter__(self):
        while self._thread is not None:
            au = self.read()
            if au is not None:
                yield au

    def seek(self, new_target, disable_timecontrol=0):
        """Jump to another in-range target = STOP current playback + START the new one (the app has
        no scrub/FF/speed — only seek). The reader thread is stopped across the 0x31a exchange so
        nothing races the socket, then a fresh PlaybackReader/thread starts on the new channel."""
        self._stop_reader()
        try: self._playrecord(PLAY_CMD_STOP, self.target)
        except Exception: pass
        # drain any stale AUs queued from the previous target
        try:
            while True: self._out.get_nowait()
        except queue.Empty:
            pass
        self._thread = None
        # START (with socket drain + channel validation) directly — live is already stopped, so we
        # must NOT go through start()'s _stop_live_video path again.
        N = self._start_playback_ioctl(new_target, disable_timecontrol)
        if N is None or N < 1:                    # channel 0 is LIVE — never run playback on it
            raise RuntimeError(f"seek 0x31a START rejected (result={N}) for target={new_target}")
        self.channel = N; self.target = new_target
        self.reader = PlaybackReader(N); self._hole_req = {}
        self._seq_N = 4; self._relseq_N = 0; self._ackord_N = 0
        self._cam_clock_N = None; self._cam_clock_ts_N = None
        self._stop_evt.clear()
        self._thread = threading.Thread(target=self._reader_loop, name="pb-reader", daemon=True)
        self._thread.start()
        self.stats['seeks'] += 1
        self._log(f"playback SEEK target={new_target} -> channel N={N}")
        return N

    def stop_playback(self):
        """Stop the recorded stream (0x31a STOP) and the reader thread, WITHOUT restoring live."""
        self._stop_reader()
        try: self._playrecord(PLAY_CMD_STOP, self.target)
        except Exception: pass

    def close(self, restore_live=True):
        """Stop playback and (by default) RESTORE live video. Idempotent; always safe to call."""
        try:
            self.stop_playback()
        finally:
            if restore_live:
                try: self.restore_live()
                except Exception as e: self._log(f"restore_live failed: {e!r}")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close(restore_live=True)
        return False


def mux_playback_stream(pbsess, writer, duration=None, idle_timeout=3.0,
                        on_stats=None, log=None, raw_video_writer=None,
                        record_seconds=None, stop_flag=None):
    """Drain recorded AUs from an ACTIVE PlaybackSession, assign PTS via cuboai_pts.AVTimeline
    (shared-base, drift-free A/V — the SAME timeline the live streamer uses), mux to MPEG-TS via
    cuboai_mpegts.TSMuxer(audio_codec='aac'), and write TS bytes to `writer` (a binary stream — the
    SAME container go2rtc/the live consumer plays). Stops on the FIRST of: `record_seconds` of
    RECORDED-timestamp span played (the "duration of footage" the caller asked for — there is no
    FF/pause/speed in the protocol, so this is span of FRAMEINFO ts_sec, not wall time); `duration` s
    of WALL time (a safety cap); `idle_timeout` s with no AU (end-of-range / EOS); or `stop_flag`
    set (external interrupt, e.g. a SIGINT handler). Returns a stats dict. The caller owns
    pbsess.start()/close() (so live is always restored)."""
    import cuboai_pts, cuboai_mpegts
    def _nal_kf(au):                                    # same NAL keyframe test as the live streamer
        return (len(au) >= 5 and au[:4] == b'\x00\x00\x00\x01'
                and ((au[4] >> 1) & 0x3f) in (32, 33, 34, 19, 20, 21))
    tl = cuboai_pts.AVTimeline()
    mux = cuboai_mpegts.TSMuxer(codec='hevc', audio_codec='aac')
    _log = log or (lambda *a: None)
    t0 = time.time(); last_au = t0; nv = na = 0; kf = 0
    v_ts_n = 0                                         # count of recorded video AUs carrying a ts_sec
    v_lo = v_hi = None                                 # running recorded-ts span (min/max + record_seconds)
    while True:
        if stop_flag is not None and stop_flag.is_set():
            break
        if duration is not None and time.time() - t0 >= duration:
            break
        au = pbsess.read(timeout=0.5)
        if au is None:
            if time.time() - last_au > idle_timeout:
                break                                  # no recorded AUs for idle_timeout -> EOS
            continue
        last_au = time.time()
        kind, unit, info = au
        now = int(time.time() * 1000)
        try:
            if kind == 'video':
                p = tl.video(info, nal_keyframe=_nal_kf(unit))
                writer.write(mux.mux_au(unit, p['pts_90k'], keyframe=p['keyframe'], now_ms=now))
                if raw_video_writer is not None:
                    raw_video_writer.write(unit)       # diagnostic tee: raw Annex-B AUs
                nv += 1
                if p['keyframe']: kf += 1
                if info and info.get('ts_sec'):
                    ts = info['ts_sec']; v_ts_n += 1
                    v_lo = ts if v_lo is None else min(v_lo, ts)
                    v_hi = ts if v_hi is None else max(v_hi, ts)
            elif kind == 'audio':
                p = tl.audio(info)
                writer.write(mux.mux_audio_au(unit, p['pts_90k'], now_ms=now))
                na += 1
        except Exception as e:
            _log(f"mux error: {e!r}")
        if record_seconds is not None and v_lo is not None and (v_hi - v_lo) >= record_seconds:
            break                                      # played the requested span of RECORDED footage
        if on_stats and (nv + na) % 200 == 0:
            on_stats(dict(video=nv, audio=na))
    return dict(video=nv, audio=na, keyframes=kf, seconds=time.time() - t0,
                v_ts_min=v_lo, v_ts_max=v_hi, v_ts_count=v_ts_n)


# ── offline self-test ─────────────────────────────────────────────────────────────
def _selftest() -> None:
    # DownloadFileReq round-trip-ish check
    name = "20260713_18_status.json"
    req = build_download_req(name)
    assert len(req) == _DL_REQ_SIZE
    assert struct.unpack_from("<i", req, 4)[0] == 1          # file_type
    assert req[8:8 + len(name)] == name.encode()
    # DownloadFileResp parse (synthesize a resp: id=0,result=0,rdt=2,file_size=10765,ftype=1,name)
    resp = bytearray(88)
    struct.pack_into("<i", resp, 4, 0)
    struct.pack_into("<i", resp, 8, 2)
    struct.pack_into("<i", resp, 12, 10765)
    struct.pack_into("<i", resp, 16, 1)
    resp[20:20 + len(name)] = name.encode()
    r = parse_download_resp(bytes(resp))
    assert r and r.rdtChannel == 2 and r.file_size == 10765 and r.file_name == name

    # STimeDay: 2026-07-13 18:30:00 UTC
    ts = int(datetime.datetime(2026, 7, 13, 18, 30, 0, tzinfo=datetime.timezone.utc).timestamp())
    st = build_stimeday(ts)
    assert struct.unpack_from("<h", st, 0)[0] == 2026
    assert st[2] == 7 and st[3] == 13 and st[5] == 18 and st[6] == 30 and st[7] == 0
    pr = build_playrecord_start(ts)
    assert len(pr) == _PLAYREC_SIZE
    assert struct.unpack_from("<i", pr, 4)[0] == PLAY_CMD_START
    assert pr[12:20] == st
    stop = build_playrecord_stop(ts)
    assert struct.unpack_from("<i", stop, 4)[0] == PLAY_CMD_STOP

    # PlayRecordResp parse: result repurposed as channel
    prr = bytearray(12); struct.pack_into("<i", prr, 4, 3)
    assert parse_playrecord_resp(bytes(prr)).result == 3

    # Manifest parse + coverage (synthetic full hour = 60 per-minute records)
    base = int(datetime.datetime(2026, 7, 13, 18, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
    s_log = [{"ts": base + 60 * i, "te": 22.5, "hu": 40.0, "mo": (2 if i == 5 else 0),
              "na": 0, "bp": 1, "be": 0} for i in range(60)]
    recs = parse_manifest(json.dumps({"s_log": s_log}))
    assert len(recs) == 60
    cov = CoverageModel(retention_s=RETENTION_72H_S)
    assert cov.add_manifest(recs) == 60
    assert cov.count == 60
    lo, hi = cov.span(); assert lo == base and hi == base + 59 * 60
    assert cov.has_footage(base + 5 * 60 + 3)          # within a covered minute
    assert cov.nearest(base + 5 * 60 + 3) == base + 5 * 60
    assert not cov.has_footage(base + 200 * 60)        # far outside
    # minute-5 has a motion event
    m5 = cov._minutes[base + 5 * 60]
    assert m5.flags.get("mo") == 2
    # RDT packet + frame builders round-trip through parse_rdt_packet
    hello = build_rdt_packet(RDT_HELLO, conn_id=0x1f)
    assert hello[:4] == RDT_MAGIC and hello[4] == RDT_HELLO and hello[5] == RDT_VER
    assert hello[17] == 0x1f and len(hello) == 20
    dpkt = build_rdt_packet(RDT_DATA, seqL=3, seqH=0, conn_id=0x1f, payload=b"hello-bytes")
    fr = build_rdt_frame(0x0e41, bytes.fromhex("aabbccddeeff"), 5, 1, dpkt)
    import cuboai_pure as cp
    dec = cp.inv_transcode(fr)
    assert dec[8:12] == _RDT_FRAME_TYPE and dec[14] == 1          # frame type + channel
    rp = parse_rdt_packet(dec)
    assert rp and rp["type"] == RDT_DATA and rp["seqL"] == 3 and rp["conn_id"] == 0x1f
    assert rp["payload"] == b"hello-bytes"
    print("cuboai_playback self-test: PASS "
          f"(req={len(req)}B, playrec={len(pr)}B, coverage minutes={cov.count}, span={hi-lo}s, "
          f"rdt frame+parse OK)")

if __name__ == "__main__":
    _selftest()
