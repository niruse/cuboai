#!/usr/bin/env python3
"""
cuboai_stream_video.py — HEVC video stream for go2rtc (CuboAI camera, pure Python).

Connects to a CuboAI camera and writes a continuous media stream to stdout, designed to be used
as an 'exec' source in go2rtc.

DEFAULT (production): MPEG-TS carrying per-AU PTS from the camera FRAMEINFO, with the FRAMEINFO
trailer stripped, selective-repeat loss recovery, and clean-GOP gating — the proven stack that
plays in MSE/HLS/WebRTC. `--raw` reverts to the original byte-for-byte HEVC Annex-B passthrough
(the byte-identical regression anchor). `--output-format annexb` keeps Annex-B but still strips
the trailer. Audio (AAC) is available, gated behind CUBOAI_MUX_AUDIO (default off).

The pure-Python transport runs a background reader that keeps the camera's send-window open even
if go2rtc's pipe back-pressures, so the stream does not stall.

Usage in go2rtc config (go2rtc.yaml):
    streams:
      cuboai_video:
        - exec:python3 /path/to/cuboai_stream_video.py#{killsignal=SIGTERM}

Environment variables (required):
    CUBOAI_UID        Device UID (license_id from the REST API)
    CUBOAI_ACCOUNT    dev_admin_id  (e.g. admin@YOUR_DEVICE_HEX)
    CUBOAI_PASSWORD   dev_admin_pwd
    CUBOAI_CAMERA_IP  LAN IP of camera (optional but recommended for LAN)
    CUBOAI_LIB        Path to libIOTCAPIs_ALL.so (optional, auto-detected)

Why exec source?
    go2rtc's 'exec' source runs a subprocess and reads its stdout as a
    media stream. This avoids the need for a full RTSP server and lets
    go2rtc handle all the WebRTC/HLS/RTSP re-streaming to Home Assistant.

Video format:
    Raw HEVC (H.265) Annex B bytestream, no container.
    Each frame starts with 00 00 00 01 (start code).
    Keyframes start with 00 00 00 01 40 (VPS NAL unit, type 32).
    Camera streams ~10-12 fps with a keyframe roughly every 3 s (P-frames between).
    Resolution: 2560x1440 on the test camera (depends on firmware/setting).

Known limitations:
    - Resolution cannot be changed via IOCTL (camera ignores it).
    - Frame rate is fixed by camera firmware.
    - Audio (AAC, interleaved on the same channel) is muxed into the TS only when
      CUBOAI_MUX_AUDIO=1; go2rtc transcodes it to Opus for the WebRTC leg.

See also:
    cuboai_stream_audio.py — standalone audio stream
"""

import argparse
import os
import sys

# ── Locate our modules ────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cuboai_session import get_session   # auto: PureSession (no --lib) or TUTKSession

def _env_float(name: str, default: float) -> float:
    """Parse a CUBOAI_* env var as float, falling back to `default` on empty or
    non-numeric input (a bare float(os.environ[...]) raises on garbage).

    Vendored alongside cuboai_sensors.py, which imports it from this module.
    """
    try:
        raw = os.environ.get(name, '')
        return float(raw) if raw else float(default)
    except (TypeError, ValueError):
        return float(default)



# ── Production env profile ─────────────────────────────────────────────────
# The proven streaming stack — MPEG-TS container + FRAMEINFO strip + selective-repeat loss
# recovery + clean-GOP — is the default so the engine works out of the box. Applied via
# os.environ.setdefault() BEFORE get_session() (the engine reads these at construction); every
# value stays overridable by an explicit env var, and --raw forces the passthrough profile.
#
# IMPORTANT: cuboai_pure.py keeps every one of these gates OFF by default — it is a neutral
# library shared by many tools (cli, validate, snapshot). The PRODUCTION profile is a property
# of THIS streaming entry point, not of the engine. So "no flags here" == production, while the
# engine on its own stays vanilla.  (CUBOAI_SELECTIVE_ACK / CUBOAI_GRACE_SCALE /
# CUBOAI_ECHO_CAMCLOCK already default ON inside the engine, so they are not repeated here.)
PRODUCTION_ENV = {
    'CUBOAI_OUTPUT_FORMAT':    'mpegts',   # MPEG-TS w/ per-AU PTS (MSE/HLS play along currentTime)
    'CUBOAI_STRIP_FRAMEINFO':  '1',        # drop the 24B trailer (HW decoders choke on it → black)
    'CUBOAI_NODROP':           '1',        # in-order never-skip seal (no POC-gap on a refs=1 stream)
    'CUBOAI_LONE_HOLE':        '1',        # pad a count-1 SACK to count≥2 so the camera resends it
    'CUBOAI_TRUNCATE_PARTIAL': '1',        # emit a clean prefix slice, never a bridged garbage AU
    'CUBOAI_GAP_DEPTH_CAP':    '200',      # let una hold past a ~69-frag keyframe burst before jumping
    'CUBOAI_LONE_SKIP_ROUNDS': '20',       # padded-request rounds before giving up on a lone hole
    'CUBOAI_RECOVERY_HOLD':    '24',       # hold a present-incomplete AU ~1s to catch a late resend
    'CUBOAI_CLEAN_GOP':        '1',        # mpegts path: emit only complete AUs, resync at IDR
    'CUBOAI_MUX_AUDIO':        '1',        # mux interleaved AAC into the TS (combined A/V by default)
    # Mid-stream stall recovery (issue #105). The camera session can go silent for good
    # roughly once an hour; without these the producer sits on an empty queue forever and is
    # only replaced when the last consumer gives up. The engine ends its AV generator after
    # STALL_S of total silence (no AU dequeued AND no fragment received) or when a fresh
    # session has streamed nothing for FIRST_AU_S; _reconnecting_frames then re-establishes
    # the session on the SAME timeline/muxer. Budget: detect ≤4.2 s + reconnect ~1-3 s must
    # stay under an NVR's ~10-15 s patience and go2rtc's ffmpeg 20 s RTSP read timeout.
    'CUBOAI_STALL_S':          '4',
    'CUBOAI_OUTPUT_STALL_S':   '6',        # no complete AU out for 6 s even while fragments arrive
    'CUBOAI_FIRST_AU_S':       '15',
    # The other way a live stream dies while the camera keeps talking: a loss burst damages
    # every keyframe for a while, clean_gop sits "awaiting IDR" dropping everything, and the
    # consumer starves although fragments still flow (observed: 21 s, NVR reset at ~10 s).
    # The engine cannot see that — only the muxer knows nothing decodable has gone out — so
    # after DESYNC_S of that it asks the wrapper for a reconnect: a fresh session opens on an
    # IDR and resets the wedged receive window.
    'CUBOAI_DESYNC_S':         '8',
}

# --raw / --passthrough: the historical byte-for-byte Annex-B passthrough. Forces the OUTPUT
# transform fully OFF (annexb container, no FRAMEINFO strip, no recovery/seal gates) so the
# emitted bytes equal the original pre-recovery engine output. This is the re-anchored
# byte-identical baseline — the regression guard for the core transport.
RAW_ENV = {
    'CUBOAI_OUTPUT_FORMAT':    'annexb',
    'CUBOAI_STRIP_FRAMEINFO':  '0',
    'CUBOAI_NODROP':           '0',
    'CUBOAI_LONE_HOLE':        '0',
    'CUBOAI_TRUNCATE_PARTIAL': '0',
    'CUBOAI_KF_GRACE':         '0',
    'CUBOAI_GRACE_SCALE':      '0',
    'CUBOAI_SELECTIVE_ACK':    '0',
    'CUBOAI_MUX_AUDIO':        '0',
    'CUBOAI_STALL_S':          '0',        # the anchor never ends its generator on silence
    'CUBOAI_OUTPUT_STALL_S':   '0',
    'CUBOAI_FIRST_AU_S':       '0',
    'CUBOAI_DESYNC_S':         '0',
}


def apply_env_profile(raw: bool) -> str:
    """Install the env profile and return the resolved output format.

    raw=True  → hard-force the passthrough profile (a stray production env in the shell can NOT
                defeat --raw; the byte-identical anchor must be reproducible).
    raw=False → setdefault the production profile (each value still overridable by an explicit
                env var). The engine reads these at construction, so this MUST run before
                get_session().
    """
    if raw:
        for k, v in RAW_ENV.items():
            os.environ[k] = v
        return 'annexb'
    for k, v in PRODUCTION_ENV.items():
        os.environ.setdefault(k, v)
    return (os.environ.get('CUBOAI_OUTPUT_FORMAT') or 'mpegts').lower()


def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _verbose_loop(sess, interval, camera_stats, stop):
    """Periodic stream-health to STDERR ONLY (stdout is the media pipe — never touched).

    Runs on a daemon thread reading the engine's read-only get_stats() snapshot (lock-free,
    no socket I/O) and pairing successive snapshots through cuboai_pure.stats_delta for the
    interval fps/bitrate/loss/recovery — the same metric set the benchmark prints.
    With camera_stats it also folds the camera 0x0934 session-stats at a slower cadence
    (injected on the reader thread via get_during_stream, so it never races the AV socket).
    Decoupled from the engine's own verbose (_vlog prints to stdout) so media stays clean.
    """
    import time as _t
    try:
        import cuboai_pure as cp
    except Exception as e:                  # pragma: no cover - import shape varies by install
        _stderr(f"[health] DISABLED — cannot import the engine module: {e!r}")
        return
    if not hasattr(sess, 'get_stats'):
        _stderr("[health] verbose stats need the pure-Python backend — disabled.")
        return
    prev = None
    t0 = _t.time()
    tick = 0
    errors = 0
    while not stop.wait(interval):          # first line after `interval` s; exits when stopped
        # ONE try around the WHOLE body. Everything below used to run bare, so a single
        # exception — a renamed stats key, a None where a number was expected — killed this
        # thread for the life of the producer. The census simply stopped, the `verbose ON`
        # banner stayed as the last word on the subject, and the only diagnostic that can
        # explain a wedged stream was gone with no trace in the log anyone would grep for
        # (issue #105: a reporter soaked for weeks on a 549 MB log holding 21 banners and
        # zero census lines). A monitor that can die silently is worse than no monitor: it
        # reads as "nothing to report".
        try:
            cur = sess.get_stats()
            d = cp.stats_delta(prev, cur)
            prev = cur
            tsv = cur['ts_valid'] + cur['ts_garbage']
            gpct = (100.0 * cur['ts_garbage'] / tsv) if tsv else 0.0
            line = (f"[health t={_t.time() - t0:.0f}s] fps {d['fps']:.1f} "
                    f"{d['bitrate_kbps'] / 1000.0:.1f}Mbps | loss {d['loss_pct']:.1f}% "
                    f"recov {cur['recovery_pct']:.0f}% (req {d['resend_req']} rec {d['recovery_events']}) | "
                    f"gap {cur['gap_now']} (max {cur['gap_max']}, capjmp {cur['gap_cap_jumps']}) | "
                    f"incAU {d['au_incomplete']} kf {d['kf_incomplete']}/{d['kf_total']} | "
                    f"ts garbage {gpct:.0f}% regress {cur['ts_regress']}")
            # Recovery visibility: a path that quietly heals must never read as healthy. Print
            # only when something happened, so a clean session's line is unchanged and any
            # occurrence stands out. (Same rule as the playback engine's health line.)
            _rec = (cur.get('reconnects', 0), cur.get('stalls', 0), cur.get('first_au_timeouts', 0),
                    cur.get('keepalive_err', 0))
            if any(_rec):
                line += (f" | RECOVERED reconn {_rec[0]} (fail {cur.get('reconnect_fail', 0)}, "
                         f"{cur.get('reconnect_s', 0)}s) stalls {_rec[1]} first_au {_rec[2]} "
                         f"kaerr {_rec[3]}")
            # Never poll the camera through a session that is mid-reconnect: the inject slot
            # would fall back to an ioctl() with its OWN disconnect/connect on this thread.
            if camera_stats and tick % 6 == 0 and getattr(sess, 'connected', True):   # slow cadence
                try:
                    ss = sess.get_during_stream('get_session_stats', timeout=1.5) or {}
                    vs = ss.get('video') or {}
                    if ss.get('mode'):
                        line += f" | cam {ss['mode']}"
                        if vs.get('resendBufferUsage'):
                            line += f" rbuf {vs['resendBufferUsage']}"
                        if vs.get('send_err_count'):
                            line += f" serr {vs['send_err_count']}"
                except Exception:
                    pass
            tick += 1
            if errors:
                line += f" | (census recovered after {errors} failed tick(s))"
                errors = 0
            _stderr(line)
        except Exception as e:
            # Say so, on a backoff, and keep going: a transient blip must not cost the rest
            # of the session's visibility, and a permanent fault must be greppable. The word
            # "health" is in the line so the census grep everyone is told to run finds it.
            errors += 1
            if errors in (1, 10, 100) or errors % 1000 == 0:
                import traceback
                _stderr(f"[health] census tick FAILED ({errors}x, still running): {e!r}")
                if errors == 1:
                    _stderr(traceback.format_exc().strip())


def mux_timed_stream(frames_timed, emit, *, clean_gop=True, mux_audio=False, log=_stderr,
                     tap=None, audio_tap=None, control=None, desync_reconnect_s=0.0):
    """Mux a (kind, data, frameinfo) access-unit stream into MPEG-TS, writing each AU's TS bytes via
    emit(bytes). Single source of truth for the live mpegts path AND the validator — replaying a
    recorded fixture through it tests the real muxer, not a copy.

    Video PTS comes from the camera FRAMEINFO through PTSClock (interpolated when a trailer is
    absent); clean_gop drops incomplete VIDEO AUs until the next clean IDR so MSE/HLS never sees a
    broken GOP.

    mux_audio (CUBOAI_MUX_AUDIO): when True, add a second AAC ES — audio AUs get a PTS from the
    camera audio ts via AudioTimeline through a PTSClock that SHARES video's base (A/V sync; NOT a
    free-running cadence), and are interleaved on the audio PID in arrival (≈PTS) order. clean_gop
    never gates audio (audio has no GOP). When False the output is video-only and BYTE-IDENTICAL to
    the pre-audio muxer (one-ES PMT, no audio PID, the original now_ms PSI cadence).

    tap/audio_tap, if given, are called per muxed video/audio AU with (pts_90k, keyframe, pts_ms);
    the live path passes None so a 24/7 stream retains no per-AU state. Returns the PTSClock stats.

    control/desync_reconnect_s (live path only): when clean_gop has been "awaiting IDR" for
    desync_reconnect_s seconds — nothing decodable went out although AUs keep arriving, i.e. a
    loss burst keeps damaging keyframes — set control.force so _reconnecting_frames tears the
    session down and brings it back (a fresh session opens on an IDR). 0 = off (the validator).
    """
    import time as _t
    from cuboai_mpegts import TSMuxer
    from cuboai_pts import AVTimeline
    from cuboai_pure import detect_video_codec

    def _nal_kf(au):
        return (len(au) >= 5 and au[:4] == b'\x00\x00\x00\x01'
                and ((au[4] >> 1) & 0x3f) in (32, 33, 34, 19, 20, 21))

    # AVTimeline is the single source of truth for shared-base A/V PTS (also used by
    # cuboai_pure.record_video) so the live stream and a saved .mp4 stay in lockstep. Its audio
    # clock is created but only fed when mux_audio → the audio-off path is byte-identical.
    avc = AVTimeline()
    mux = None; _warned = False
    synced = not clean_gop; _cg_drop = 0
    psi_now = [0]                                # monotonic PSI cadence clock (audio path only)
    t_desync = None                              # when clean_gop last lost a decodable GOP (None = synced)

    def _desync_at(t):
        # Track the desync clock here AND publish the deadline to the wrapper's control, so the
        # engine's idle loop can end the session on time even if no further AU ever arrives.
        nonlocal t_desync
        t_desync = t
        if control is not None and desync_reconnect_s > 0:
            control.deadline = (t + desync_reconnect_s) if t is not None else None

    _desync_at(_t.time() if clean_gop else None)  # awaiting the first IDR counts too

    if clean_gop:
        log("[clean_gop] ON — emitting only complete AUs, resync at IDR after any hole")
    if mux_audio:
        log("[mux_audio] ON — interleaving AAC audio (shared-base PTS) on a second TS PID")
    for kind, data, fi in frames_timed:
        if kind == 'reconnect':
            # Boundary from _reconnecting_frames: the camera session was re-established. The
            # timeline (avc) and muxer keep going — PTS resumes on the same base so consumers
            # see a real gap, not a reset, and continuity counters / PID identity survive.
            # Just wait for the next IDR so the first post-gap AU is decodable and carries RAI.
            synced = not clean_gop; _cg_drop = 0
            _desync_at(_t.time() if clean_gop else None)  # the new session gets a full window
            avc.mark_discontinuity()
            log("[reconnect] session re-established — same timeline, awaiting IDR")
            continue
        if (control is not None and desync_reconnect_s > 0 and not synced
                and t_desync is not None and _t.time() - t_desync >= desync_reconnect_s):
            # Decode stall: AUs are arriving (so the engine's silence gate stays quiet — rightly)
            # but none has been decodable for this long. Ask for a reconnect; the wrapper acts
            # on it at the next yield. Reset the clock so this fires once per window.
            log(f"[clean_gop] no decodable video for {_t.time() - t_desync:.1f}s "
                f"(awaiting IDR, dropped {_cg_drop} AUs) — requesting reconnect")
            control.force = True
            _desync_at(_t.time())
        if kind == 'audio':
            if not mux_audio or mux is None:         # need the muxer (built on first video AU)
                continue
            ta = avc.audio(fi)                       # shared-base; lost trailer → AAC-cadence interp
            now = max(psi_now[0], int(ta['pts_ms'])); psi_now[0] = now
            emit(mux.mux_audio_au(data, ta['pts_90k'], now_ms=now))
            if audio_tap is not None:
                audio_tap(ta['pts_90k'], ta['keyframe'], ta['pts_ms'])
            continue
        if kind != 'video':
            continue
        if mux is None:
            # The PMT stream_type is written once, from the FIRST video AU — a wrong guess
            # poisons the whole producer: an H264 stream declared as HEVC parses to no
            # parameter sets, so go2rtc registers NO video track at all and every video
            # consumer dies ("codecs not matched: audio:AAC, audio:OPUS" on frame.jpeg,
            # "finding first packet" timeout on the RTSP leg — issue #85). When this AU has
            # no FRAMEINFO (mid-GOP join / incomplete), sniff the codec from its NAL
            # headers; if even that is indecisive (P-frame-only AU), WAIT for the next
            # video AU instead of defaulting.
            codec = (fi or {}).get('codec') or detect_video_codec(data, default=None)
            if not codec:
                continue
            if not fi:
                log(f"[mpegts] first video AU has no FRAMEINFO — codec sniffed from NAL headers: {codec}")
            mux = TSMuxer(codec=codec, audio_codec=('aac' if mux_audio else None))
            log(f"[mpegts] muxing {codec}{'+aac' if mux_audio else ''} → MPEG-TS with FRAMEINFO PTS "
                f"(stream_type=0x{mux.stream_type:02x})")
        if clean_gop:
            if fi is None:                           # incomplete AU → poison the GOP tail
                if synced:
                    log("[clean_gop] hole → desync, awaiting IDR")
                    _desync_at(_t.time())
                synced = False; _cg_drop += 1
                continue
            if not synced:
                if fi.get('is_keyframe'):             # clean IDR → resume a fresh decodable GOP
                    synced = True
                    _desync_at(None)                   # clears the wrapper's deadline too
                    log(f"[clean_gop] resync at IDR (dropped {_cg_drop} AUs)")
                    _cg_drop = 0                       # per-event count (it used to accumulate for life)
                else:
                    _cg_drop += 1
                    continue
        if fi is None and not _warned:
            log("[mpegts] AU without FRAMEINFO (strip off/incomplete) — interpolating PTS")
            _warned = True
        # only a VALID ts seeds the shared base (the ts_valid gate lives inside AVTimeline) →
        # audio-off byte-identical with the pre-refactor inline PTSClock.
        t = avc.video(fi, nal_keyframe=_nal_kf(data))
        # video-only path keeps the ORIGINAL now_ms (byte-identical); audio path uses a monotonic
        # PSI clock so interleaved A/V now_ms can't make the PAT/PMT cadence regress.
        now = int(t['pts_ms'])
        if mux_audio:
            now = max(psi_now[0], now); psi_now[0] = now
        emit(mux.mux_au(data, t['pts_90k'], keyframe=t['keyframe'], now_ms=now))
        if tap is not None:
            tap(t['pts_90k'], t['keyframe'], t['pts_ms'])
    return avc.stats()


class _ReconnectControl:
    """Muxer → wrapper signalling (same thread).

    force     — the muxer saw an AU arrive past the decode deadline: reconnect at the next yield.
    deadline  — wall time after which the stream counts as decode-stalled (None = synced). The
                engine's idle loop polls it through stop_when, so a stall is caught within ~0.2 s
                of the deadline even when NO AU arrives to trigger the per-item check (during a
                loss storm items are sparse — observed: a 12.1 s detection against an 8 s window).
    """
    __slots__ = ('force', 'deadline', 'n_forced')

    def __init__(self):
        self.force = False
        self.deadline = None
        self.n_forced = 0

    def due(self):
        import time as _t
        return self.deadline is not None and _t.time() >= self.deadline


class StreamExhausted(RuntimeError):
    """The camera could not be brought back after repeated reconnects; the producer should exit
    so go2rtc respawns a fresh process (the last-resort rung of the recovery ladder)."""


def _reconnecting_frames(sess, log=_stderr, *, max_fail=3, connect_timeout=5.0, settle=0.3,
                         backoff=(1.0, 2.0, 4.0), control=None):
    """Yield (kind, data, fi) from successive camera sessions, reconnecting IN PLACE between them.

    The engine's AV generator ends on its own when the camera goes silent (CUBOAI_STALL_S) or a
    session never streams (CUBOAI_FIRST_AU_S). Here that is turned into recovery: log why it
    ended, re-establish the session on the SAME engine object (so the health thread and the
    cumulative counters stay attached), yield one ('reconnect', None, None) boundary so the muxer
    resyncs on the same timeline, and carry on. Exiting the process instead would restart PTS at
    0 into a muxer that cannot flag a discontinuity — fatal for an fMP4/MSE consumer.

    Failure accounting: a failed handshake OR a session that yielded nothing counts as one
    consecutive failure; any session that delivered an AU resets the count. After `max_fail`
    consecutive failures raise StreamExhausted. Between failed handshakes wait `backoff[k]`.
    All of this runs on the muxer's thread, never inside the engine's reader thread.
    """
    import time as _t
    fails = 0
    attempt = 0
    stop_when = control.due if control is not None else None
    while True:
        got = 0
        forced = False
        t_sess0 = _t.time()
        inner = sess.av_frames_timed(stop_when=stop_when)
        for item in inner:
            got += 1
            yield item
            if control is not None and control.force:
                # The muxer saw nothing decodable for too long (see mux_timed_stream). Close the
                # engine generator here — GeneratorExit runs its `finally`, which joins the
                # reader — then fall into the same reconnect path a silence would take.
                control.force = False
                control.n_forced += 1
                forced = True
                inner.close()
                break
        # ── the session ended (on its own, or because the muxer asked) ────────────────
        fails = 0 if got > 0 else fails + 1
        info = getattr(sess, 'last_stall_info', None) or {}
        if info.get('kind') == 'external':        # the engine's idle loop hit the decode deadline
            forced = True
        if forced and control is not None:
            control.deadline = None
            control.force = False
        try:
            st = sess.get_stats() or {}
        except Exception:
            st = {}
        attempt += 1
        tail = (f"session {(_t.time() - t_sess0):.0f}s, delivered {got} AUs, "
                f"kaerr {st.get('keepalive_err', 0)}, stalls {st.get('stalls', 0)}")
        if forced:
            log(f"[stall] desync: nothing decodable past the {'idle' if info.get('kind') == 'external' else 'muxer'} "
                f"deadline ({tail}) -> reconnect #{attempt}")
        else:
            log(f"[stall] {info.get('kind') or 'ended'}: no AU for {info.get('since_s', 0.0):.1f}s "
                f"({tail}, last frag {info.get('last_frag_age')}s, last pkt {info.get('last_pkt_age')}s, "
                f"cam_clock {info.get('cam_clock_age')}s) -> reconnect #{attempt}")
        if fails >= max_fail:
            raise StreamExhausted(f"{fails} consecutive sessions delivered nothing")
        # ── reconnect, with backoff on a failed handshake ─────────────────────────────
        while True:
            t0 = _t.time()
            try:
                ok = bool(sess.reconnect(connect_timeout, settle))
            except Exception as e:
                log(f"[reconnect] error: {e}")
                ok = False
            if ok:
                log(f"[reconnect] ok #{attempt} in {(_t.time() - t0):.1f}s")
                break
            fails += 1
            log(f"[reconnect] FAILED #{attempt} ({fails}/{max_fail} consecutive)")
            if fails >= max_fail:
                raise StreamExhausted(f"{fails} consecutive reconnect failures")
            _t.sleep(backoff[min(fails - 1, len(backoff) - 1)])
            attempt += 1
        yield ('reconnect', None, None)


def _install_sigterm_handler() -> None:
    """Route SIGTERM into the KeyboardInterrupt path so `finally: sess.disconnect()` runs.

    go2rtc stops an exec: source with SIGTERM (killsignal=SIGTERM in the stream recipe). Python's
    DEFAULT SIGTERM action terminates the process WITHOUT unwinding, so the disconnect — the 3x
    build_close session-stop burst — was skipped on every normal stream stop, and the camera held
    that session slot until its own alive-timeout. Over hours of consumer churn that is a slow
    leak of camera slots; for an in-process reconnect it is a slot the camera still thinks is
    live. SIGINT is deliberately left alone: its default handler already raises
    KeyboardInterrupt and unwinds cleanly. The handler is one-shot (restores SIG_DFL before
    raising) so a second, impatient SIGTERM hard-kills instead of re-entering teardown.
    """
    import signal as _signal

    def _term_handler(_signum, _frame):
        _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)   # 2nd SIGTERM → hard kill
        raise KeyboardInterrupt                             # → except/finally → disconnect()

    try:
        _signal.signal(_signal.SIGTERM, _term_handler)
    except (ValueError, OSError):
        pass   # not the main thread / unsupported platform — leave the default (best-effort)


def main() -> None:
    # ── Configuration from CLI args (falling back to environment) ─────────
    ap = argparse.ArgumentParser(description="CuboAI raw HEVC video stream → stdout")
    ap.add_argument('--uid');       ap.add_argument('--account')
    ap.add_argument('--password');  ap.add_argument('--camera-ip')
    ap.add_argument('--lib', help='Path to libIOTCAPIs_ALL.so (omit → pure Python, the default)')
    ap.add_argument('--defer-start', action='store_true',
                    help='Re-enable the ~5s native startup defer (wire-fidelity). Default now starts '
                         'the stream immediately (the production behaviour).')
    ap.add_argument('--no-defer-start', action='store_true', help=argparse.SUPPRESS)  # back-compat no-op
    ap.add_argument('--output-format', choices=('annexb', 'mpegts'),
                    help='Output container. mpegts (default) = MPEG-TS carrying per-frame PTS from '
                         'the camera FRAMEINFO (MSE/HLS play along currentTime; auto-strips the '
                         'FRAMEINFO trailer). annexb = raw HEVC Annex-B (no timestamps).')
    ap.add_argument('--raw', '--passthrough', dest='raw', action='store_true',
                    help='Historical byte-for-byte Annex-B passthrough: forces --output-format '
                         'annexb and turns every FRAMEINFO-strip / loss-recovery / seal gate OFF. '
                         'The byte-identical regression anchor (overrides all of the above).')
    ap.add_argument('-v', '--verbose', action='store_true',
                    help='Print periodic stream-health metrics (loss%%, recovery, fps, bitrate, gaps, '
                         'PTS health) to STDERR only — stdout stays the media stream. Also CUBOAI_VERBOSE=1.')
    ap.add_argument('--verbose-interval', type=float, default=None, metavar='SECS',
                    help='Seconds between verbose health lines (default 5; or CUBOAI_VERBOSE_INTERVAL).')
    ap.add_argument('--verbose-camera-stats', action='store_true',
                    help='Also fold the camera 0x0934 session-stats into verbose output at a slower '
                         'cadence (injects a read onto the reader thread). Also CUBOAI_VERBOSE_CAMERA_STATS=1.')
    args = ap.parse_args()

    uid       = args.uid       or os.environ.get('CUBOAI_UID')
    account   = args.account   or os.environ.get('CUBOAI_ACCOUNT')
    password  = args.password  or os.environ.get('CUBOAI_PASSWORD')
    camera_ip = args.camera_ip or os.environ.get('CUBOAI_CAMERA_IP')
    lib_path  = args.lib       or os.environ.get('CUBOAI_LIB')

    # ── defer-start ───────────────────────────────────────────────────────
    # Default: start the stream immediately (the deployed/production behaviour — the wrapper always
    # passed --no-defer-start). --defer-start (or CUBOAI_DEFER_START) re-enables the ~5s native
    # startup defer for wire-fidelity. _defer=False starts fast; _defer=None follows full_fidelity
    # (~5s defer). --no-defer-start stays an accepted no-op so the existing wrapper keeps working.
    defer  = args.defer_start or os.environ.get('CUBOAI_DEFER_START', '0') != '0'
    _defer = None if defer else False

    # ── env profile (production setdefaults, or --raw passthrough) ─────────
    # Must run before get_session(): the engine reads its gate env vars at construction. An
    # explicit --output-format wins over the production default; --raw overrides everything.
    if args.raw and args.output_format and args.output_format != 'annexb':
        print("Error: --raw forces --output-format annexb (drop the conflicting --output-format).",
              file=sys.stderr)
        sys.exit(1)
    if args.output_format and not args.raw:
        os.environ['CUBOAI_OUTPUT_FORMAT'] = args.output_format
    output_format = apply_env_profile(args.raw)
    if args.raw:
        print("[raw] passthrough — annexb, FRAMEINFO strip + all recovery/seal gates OFF "
              "(byte-identical anchor)", file=sys.stderr, flush=True)

    if not all([uid, account, password]):
        print(
            "Error: --uid/--account/--password (or CUBOAI_UID/ACCOUNT/PASSWORD) required.",
            file=sys.stderr
        )
        sys.exit(1)

    # ── Connect (pure Python unless --lib/CUBOAI_LIB given) ───────────────
    # _defer (computed above): False = start immediately (default); None = follow full_fidelity
    # (~5s native defer). get_session forwards both kwargs; the native backend ignores them.
    # auto_discover_lib=False: pure is the GUARANTEED default for the deployment — only an EXPLICIT
    # --lib/CUBOAI_LIB selects native; a stray lib in ~ or a standard path can't override it.
    sess = get_session(uid, account, password, lib_path=lib_path, camera_ip=camera_ip,
                       defer_stream_start=_defer, defer_video_start_late=_defer,
                       auto_discover_lib=False)

    try:
        sess.connect()
    except Exception as e:
        print(f"Connection failed: {e}", file=sys.stderr)
        sys.exit(1)

    # ── verbose health (STDERR only — stdout stays the media pipe) ────────
    # Decoupled from the engine's own connect/stream trace (which prints to stdout): this
    # reads the read-only get_stats() snapshot on a daemon thread and writes only to stderr.
    verbose = args.verbose or os.environ.get('CUBOAI_VERBOSE', '0') != '0'
    _v_stop = None
    if verbose:
        import threading as _threading
        v_interval = (args.verbose_interval
                      or float(os.environ.get('CUBOAI_VERBOSE_INTERVAL', '') or 5.0))
        v_camera = (args.verbose_camera_stats
                    or os.environ.get('CUBOAI_VERBOSE_CAMERA_STATS', '0') != '0')
        _v_stop = _threading.Event()
        _threading.Thread(target=_verbose_loop, args=(sess, v_interval, v_camera, _v_stop),
                          daemon=True).start()
        _stderr(f"[health] verbose ON — metrics to stderr every {v_interval:g}s"
                + (" + camera session-stats" if v_camera else ""))

    # ── Stream video frames to stdout ────────────────────────────────────
    # go2rtc reads from stdout and handles the re-streaming.
    # We write raw HEVC Annex B frames — go2rtc detects the format from
    # the 00 00 00 01 start codes.
    import time as _time
    stdout = sys.stdout.buffer
    # Optional per-video-AU emit-timestamp trace (latency/jitter harness). Gated; when
    # CUBOAI_EMIT_TS_FILE is unset this is a no-op and the stream is byte-identical.
    _etsf = None
    _ets = os.environ.get('CUBOAI_EMIT_TS_FILE')
    if _ets:
        _etsf = open(_ets, 'w', buffering=1)

    def _emit(data):
        if _etsf is not None:
            _etsf.write(f"{_time.time():.6f}\n")
        stdout.write(data)
        stdout.flush()

    _install_sigterm_handler()
    rc = 0
    try:
        if output_format == 'mpegts':
            # MPEG-TS path: carry per-AU PTS from the camera FRAMEINFO so MSE/HLS play along
            # currentTime without underrun (the PTS, not arrival timing, drives the timeline).
            # clean-GOP (default on) drops incomplete AUs until the next clean keyframe.
            clean_gop = os.environ.get('CUBOAI_CLEAN_GOP', '1') != '0'
            mux_audio = os.environ.get('CUBOAI_MUX_AUDIO', '0') != '0'
            # Mid-stream stall recovery: the engine ends its generator on camera silence
            # (CUBOAI_STALL_S, production default 4 s); reconnect in place on the same
            # timeline/muxer rather than dying and restarting PTS from 0. CUBOAI_RECONNECT_MAX=0
            # disables the wrapper (the generator then simply ends and the process exits).
            max_fail = int(_env_float('CUBOAI_RECONNECT_MAX', 3))
            control = None
            desync_s = 0.0
            if max_fail > 0:
                control = _ReconnectControl()
                desync_s = _env_float('CUBOAI_DESYNC_S', 0.0)
                frames = _reconnecting_frames(
                    sess, _stderr, max_fail=max_fail,
                    connect_timeout=_env_float('CUBOAI_RECONNECT_TIMEOUT_S', 5.0),
                    control=control)
            else:
                frames = sess.av_frames_timed()
            mux_timed_stream(frames, _emit, clean_gop=clean_gop, mux_audio=mux_audio,
                             control=control, desync_reconnect_s=desync_s)
        else:
            for frame_type, data in sess.av_frames():
                if frame_type == 'video':
                    _emit(data)
    except (BrokenPipeError, KeyboardInterrupt):
        # go2rtc closed the pipe (stream stopped) — clean exit
        pass
    except StreamExhausted as e:
        # Last rung of the ladder: in-process recovery gave up. Exit NON-ZERO after the
        # normal teardown so go2rtc respawns a fresh producer on the next consumer.
        _stderr(f"[reconnect] giving up ({e}) — exiting so go2rtc can respawn the producer")
        rc = 1
    except Exception as e:
        print(f"Stream error: {e}", file=sys.stderr)
    finally:
        if _v_stop is not None:
            _v_stop.set()
        if _etsf is not None:        # M3: close the gated emit-trace file (was leaked for process life)
            _etsf.close()
        sess.disconnect()
    if rc:
        sys.exit(rc)


if __name__ == '__main__':
    # Standalone process: allow the broadcast-redirect shim to re-exec us with
    # LD_PRELOAD (blocked when this module is imported by a host application).
    import os as _os
    _os.environ.setdefault('CUBOAI_ALLOW_REEXEC', '1')
    main()
