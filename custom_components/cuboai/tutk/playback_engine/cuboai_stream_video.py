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

STATUS: Working at ~10–12 fps. The pure-Python transport's background reader keeps the camera's
send-window open even if go2rtc's pipe back-pressures, so the stream does not stall (session-14
C/D windowed-ACK fix).

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
    cuboai_stream_audio.py — standalone audio stream (legacy)
    LIBRARY_SETUP.md       — native library notes (the deployment runs pure Python)
"""

import argparse
import os
import sys

# ── Locate our modules ────────────────────────────────────────────────────
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cuboai_session import get_session   # auto: PureSession (no --lib) or TUTKSession


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
}


def _env_float(name: str, default: float) -> float:
    """Parse a CUBOAI_* env var as float, falling back to `default` on empty/non-numeric input
    (B-8/R3 — the previous bare float(os.environ[...]) raised an uncaught ValueError on garbage)."""
    try:
        raw = os.environ.get(name, '')
        return float(raw) if raw else float(default)
    except (TypeError, ValueError):
        return float(default)


def _clamp_env_knobs():
    """Range-clamp the numeric CUBOAI_* tuning env vars (startup only, stderr only) so a zero/absurd
    override can't divide-by-zero or break recovery. Non-numeric -> default; out-of-range -> clamp.

    Lives in the shared streamer module (B-8) so BOTH the production streamer (apply_env_profile)
    and the validate CLI sanitise identically — the streamer used to skip clamping entirely, so a
    bad CUBOAI_* only got caught under `cuboai_validate`, never on the go2rtc exec path."""
    KNOBS = [  # (env, lo, hi, default-or-None, is_float)
        ('CUBOAI_GAP_DEPTH_CAP', 1, 10000, 200, False),
        ('CUBOAI_RECOVERY_HOLD', 0, 2000, 24, False),
        ('CUBOAI_LONE_SKIP_ROUNDS', 0, 2000, 20, False),
        ('CUBOAI_KF_HOLD', 0, 2000, 40, False),
        ('CUBOAI_GAP_HOLD_MS', 1, 600000, None, False),
        ('CUBOAI_VERBOSE_INTERVAL', 0.1, 3600, 5.0, True),
        ('CUBOAI_VERBOSE_CAMERA_STATS_INTERVAL', 1, 86400, None, False),
        ('CUBOAI_LIST_PACE_S', 0, 3600, 0.5, True),   # B-8/R3: also parsed with a bare float()
    ]
    for env, lo, hi, dflt, isf in KNOBS:
        raw = os.environ.get(env)
        if not raw:
            continue
        try:
            v = float(raw) if isf else int(raw)
        except ValueError:
            if dflt is not None:
                os.environ[env] = str(dflt)
                print(f"Warning: {env}={raw!r} is not a number; using default {dflt}.", file=sys.stderr)
            continue
        cv = min(hi, max(lo, v))
        if cv != v:
            os.environ[env] = str(cv if isf else int(cv))
            print(f"Warning: {env}={raw} out of range [{lo},{hi}]; clamped to {cv}.", file=sys.stderr)


def apply_env_profile(raw: bool) -> str:
    """Install the env profile and return the resolved output format.

    raw=True  → hard-force the passthrough profile (a stray production env in the shell can NOT
                defeat --raw; the byte-identical anchor must be reproducible).
    raw=False → setdefault the production profile (each value still overridable by an explicit
                env var). The engine reads these at construction, so this MUST run before
                get_session().
    """
    _clamp_env_knobs()          # B-8: sanitise numeric CUBOAI_* on the streamer path too (was
                                # validate-only) — a bad knob must never reach get_session().
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
    interval fps/bitrate/loss/recovery — the same Task-1 metric set the benchmark prints.
    With camera_stats it also folds the camera 0x0934 session-stats at a slower cadence
    (injected on the reader thread via get_during_stream, so it never races the AV socket).
    Decoupled from the engine's own verbose (_vlog prints to stdout) so media stays clean.
    """
    import time as _t
    import cuboai_pure as cp
    if not hasattr(sess, 'get_stats'):
        _stderr("[health] verbose stats need the pure-Python backend — disabled.")
        return
    prev = None
    t0 = _t.time()
    tick = 0
    while not stop.wait(interval):          # first line after `interval` s; exits when stopped
        try:
            cur = sess.get_stats()
        except Exception:
            continue
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
        # SILENT-RECOVERY VISIBILITY (audit 2026-07-23). ioctl()'s auto-reconnect masked the
        # ghost-conn bug for weeks on Linux — a path that quietly recovers must never read as
        # healthy. Print the send side + the recoveries ONLY when non-zero, so a clean session's
        # line is unchanged and any occurrence stands out. stderr only (stdout is the media pipe).
        _rec = (cur.get('reconnects', 0), cur.get('ioctl_retries', 0),
                cur.get('tx_ioctl_retx', 0), cur.get('tx_err', 0), cur.get('keepalive_err', 0))
        if any(_rec):
            line += (f" | RECOVERED reconn {_rec[0]} (fail {cur.get('reconnect_fail', 0)}, "
                     f"{cur.get('reconnect_s', 0)}s) ioctl-retry {_rec[1]} ioctl-retx {_rec[2]} "
                     f"txerr {_rec[3]} kaerr {_rec[4]}")
        if camera_stats and tick % 6 == 0:          # slow cadence (~6× the interval)
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
        _stderr(line)


def mux_timed_stream(frames_timed, emit, *, clean_gop=True, mux_audio=False, log=_stderr,
                     tap=None, audio_tap=None):
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
    """
    from cuboai_pts import AVTimeline
    from cuboai_mpegts import TSMuxer

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

    if clean_gop:
        log("[clean_gop] ON — emitting only complete AUs, resync at IDR after any hole")
    if mux_audio:
        log("[mux_audio] ON — interleaving AAC audio (shared-base PTS) on a second TS PID")
    for kind, data, fi in frames_timed:
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
            codec = (fi or {}).get('codec', 'hevc')
            mux = TSMuxer(codec=codec, audio_codec=('aac' if mux_audio else None))
            log(f"[mpegts] muxing {codec}{'+aac' if mux_audio else ''} → MPEG-TS with FRAMEINFO PTS "
                f"(stream_type=0x{mux.stream_type:02x})")
        if clean_gop:
            if fi is None:                           # incomplete AU → poison the GOP tail
                if synced:
                    log("[clean_gop] hole → desync, awaiting IDR")
                synced = False; _cg_drop += 1
                continue
            if not synced:
                if fi.get('is_keyframe'):             # clean IDR → resume a fresh decodable GOP
                    synced = True
                    log(f"[clean_gop] resync at IDR (dropped {_cg_drop} AUs)")
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
        v_interval = args.verbose_interval or _env_float('CUBOAI_VERBOSE_INTERVAL', 5.0)
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
    # Optional per-video-AU emit-timestamp trace (overnight #2 latency/jitter harness). Gated;
    # when CUBOAI_EMIT_TS_FILE is unset this is a no-op and the stream is byte-identical.
    _etsf = None
    _ets = os.environ.get('CUBOAI_EMIT_TS_FILE')
    if _ets:
        _etsf = open(_ets, 'w', buffering=1)

    def _emit(data):
        if _etsf is not None:
            _etsf.write(f"{_time.time():.6f}\n")
        stdout.write(data)
        stdout.flush()

    # ── SIGTERM → clean teardown (B-1) ────────────────────────────────────
    # go2rtc stops an exec: source with SIGTERM (see the recipe above, killsignal=SIGTERM).
    # Python's DEFAULT SIGTERM action terminates the process WITHOUT unwinding, so the
    # `finally: sess.disconnect()` below — the 3x build_close session-stop burst — is skipped
    # on every normal stream stop. Route SIGTERM into the SAME path SIGINT already takes:
    # raise KeyboardInterrupt in the main thread so the `except (BrokenPipeError,
    # KeyboardInterrupt)` + `finally` below run disconnect() exactly as they do for Ctrl-C.
    #
    # SIGINT is DELIBERATELY LEFT ALONE — Python's default handler already raises
    # KeyboardInterrupt and unwinds cleanly (offline-proven: 3x build_close, ~30 ms), so
    # touching it could only regress the working path. This is why the fix is just a SIGTERM
    # alias, with none of the SIGALRM-watchdog / os._exit machinery that hung SIGINT in the
    # first attempt. The handler is one-shot (restores SIG_DFL before raising) so a second,
    # impatient SIGTERM hard-kills instead of re-entering teardown.
    import signal as _signal

    def _term_handler(_signum, _frame):
        _signal.signal(_signal.SIGTERM, _signal.SIG_DFL)   # 2nd SIGTERM → hard kill
        raise KeyboardInterrupt                             # → except/finally → disconnect()

    try:
        _signal.signal(_signal.SIGTERM, _term_handler)
    except (ValueError, OSError):
        pass   # not the main thread / unsupported platform — leave the default (best-effort)

    try:
        if output_format == 'mpegts':
            # MPEG-TS path (Part C): carry per-frame PTS from the camera FRAMEINFO so MSE/HLS play
            # along currentTime without underrun. PACE is not used here — the PTS, not arrival
            # timing, drives the timeline. The muxing loop lives in mux_timed_stream() (shared with
            # the validator). clean-GOP (default on) drops incomplete AUs until the next clean IDR.
            clean_gop = os.environ.get('CUBOAI_CLEAN_GOP', '1') != '0'
            mux_audio = os.environ.get('CUBOAI_MUX_AUDIO', '0') != '0'
            mux_timed_stream(sess.av_frames_timed(), _emit, clean_gop=clean_gop, mux_audio=mux_audio)
        else:
            for frame_type, data in sess.av_frames():
                if frame_type == 'video':
                    _emit(data)
    except (BrokenPipeError, KeyboardInterrupt):
        # go2rtc closed the pipe (stream stopped) — clean exit
        pass
    except Exception as e:
        print(f"Stream error: {e}", file=sys.stderr)
    finally:
        if _v_stop is not None:
            _v_stop.set()
        if _etsf is not None:        # M3: close the gated emit-trace file (was leaked for process life)
            _etsf.close()
        sess.disconnect()


if __name__ == '__main__':
    main()
