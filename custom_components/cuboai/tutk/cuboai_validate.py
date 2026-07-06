#!/usr/bin/env python3
"""
cuboai_validate.py — CuboAI camera validation and control tool.

Defaults to the pure-Python session — no native library, and the library is NOT auto-discovered.
Pass --lib (or set CUBOAI_LIB) to explicitly opt into the native TUTK backend.

Capture commands run under the same production A/V profile as cuboai_stream_video (FRAMEINFO
strip + loss recovery), so the playable outputs (--snapshot, --record) are clean by default.
Add --raw to grab the unprocessed Annex-B bitstream (trailers present, no recovery) for inspection.

Usage:
    python3 cuboai_validate.py --uid YOUR_20CHAR_UID_HERE \\
                               --account admin@YOUR_ACCOUNT \\
                               --password YOUR_PASSWORD \\
                               --camera-ip 192.0.2.10 --record clip.mp4
                               [--lib /path/to/libIOTCAPIs_ALL.so]   # native opt-in

Capture (playable by default; --raw = unprocessed bitstream):
    --snapshot FILE          Save a JPEG snapshot (one keyframe → PyAV → JPEG)
    --record FILE            Record muxed audio+video to a playable .mp4 (camera-clock A/V sync)
    --record-video FILE      Record the raw HEVC video element to FILE
    --record-audio FILE      Record the AAC-ADTS audio element to FILE (e.g. audio.aac)
    --record-av BASE         Record both elements raw, separate: BASE.hevc + BASE.aac
    --stream-video           Stream HEVC to stdout (pipe to: ffplay -f hevc -i -)
    --stream-audio           Stream raw AAC-ADTS to stdout
    --duration SECS          Capture duration (default 10)
    --raw                    Unprocessed bitstream (no FRAMEINFO strip / no recovery)
    --talk FILE              Send audio to the camera speaker (native; pure uplink experimental)

Control:
    --night-light on|off / --brightness 0-100 / --volume 0-100 / --timer repeat|30min|60min /
    --play NAME / --stop / --sleep-mode on|off / --list-songs.  See --help for the full SET
    command group (night-vision, cry/cough detection, sleep-safety, comfort range, …).
    --no-status              Skip the status read (status and AV streaming coexist)

Environment: CUBOAI_LIB, CUBOAI_UID, CUBOAI_ACCOUNT, CUBOAI_PASSWORD, CUBOAI_CAMERA_IP
"""
from __future__ import annotations
import argparse
import os
import platform
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cuboai_session import get_session   # auto-selects TUTKSession (--lib) or PureSession
from cuboai_stream_video import apply_env_profile   # shared production/raw A/V env profile
from cuboai_messages import (
    build_get_hw_control,
    build_get_light_style,
    build_get_lullaby_vol_duration,
    build_set_night_light,
    build_set_light_style_brightness,
    build_set_lullaby_play,
    build_set_lullaby_stop,
    build_set_lullaby_vol_duration,
    build_get_cry_detect,
    build_get_sleep_safety_status,
    build_get_sleep_safety_setting,
    build_get_sleep_mode,
    build_set_sleep_mode,
    build_get_cough_setting,
    build_get_connected_users,
    HWControl,
    LightStyle,
    LullabyVolDuration,
    LullabySchedule,
    IOTYPE_USER_GET_HW_CONTROL_RESP,
    IOTYPE_USER_GET_LIGHT_STYLE_RESP,
    IOTYPE_USER_GET_LULLABY_VOL_DURATION_RESP,
    IOTYPE_USER_GET_LULLABY_SCHEDULE_RESP,
    IOTYPE_USER_GET_STATUS_LIGHT_ON_OFF_REQ,
    IOTYPE_USER_GET_STATUS_LIGHT_ON_OFF_RESP,
    IOTYPE_USER_GET_UPDATE_INFO_REQ,
    IOTYPE_USER_GET_UPDATE_INFO_RESP,
    LULLABY_TIMER_REPEAT,
    LULLABY_TIMER_30MIN,
    LULLABY_TIMER_60MIN,
    LULLABY_CATALOG,
    get_song_name,
    GET_METHODS,
)


def find_song(query: str):
    q = query.lower().strip()
    for uuid, (key, name, category) in LULLABY_CATALOG.items():
        if q in name.lower() or q in key.lower():
            return uuid, name
    return None


# ── Status output ────────────────────────────────────────────────
# Every GET method (cuboai_messages.GET_METHODS) is read into one dict, then a
# clean human-readable status card is composed from the decoded fields, grouping
# related items and cross-referencing where one feature spans two responses
# (e.g. the lullaby song comes from get_lullaby but the volume from
# get_lullaby_schedule). See cuboai_messages for the per-field wire decoding.

_W = 16  # label column width


def _onoff(v):
    return 'ON' if v else ('OFF' if v is not None else '—')


def _read_all(sess) -> dict:
    """Read every GET method into {name: parsed_dict}. Failed/empty reads → None."""
    out = {}
    for name, (builder, resp_type, parser) in GET_METHODS.items():
        try:
            tc, data = sess.ioctl(*builder())
            out[name] = parser(data) if data else None
        except Exception:
            out[name] = None
    return out


def _row(lines: list, label: str, value) -> None:
    if value is None or value == '':
        return
    lines.append(f"    {(label + ':'):<{_W}} {value}")


def _render_status(d: dict) -> str:
    """Compose the clean status card from the decoded GET responses (d)."""
    G = lambda name, key, default=None: (d.get(name) or {}).get(key, default)
    L = []
    bar = "  " + "═" * 44
    L.append("")
    L.append(bar)
    L.append("    📷 CuboAI Camera Status")
    L.append(bar)

    # ── Sensors ──────────────────────────────────────────────────
    temp = G('get_hw_control', 'temp_c')
    humid = G('get_hw_control', 'humidity_pct')
    wifi = G('get_hw_control', 'wifi_strength')
    ssid = G('get_hw_control', 'ssid') or G('get_wifi', 'ssid')
    t_lo, t_hi = G('get_hw_policy', 'temp_low_c'), G('get_hw_policy', 'temp_high_c')
    h_lo, h_hi = G('get_hw_policy', 'humi_low_pct'), G('get_hw_policy', 'humi_high_pct')
    sec = []
    if temp is not None:
        note = f"  (comfort {t_lo}–{t_hi}°C)" if t_lo is not None else ""
        _row(sec, "Temperature", f"{temp:.1f}°C{note}")
    if humid is not None:
        note = f"  (comfort {h_lo}–{h_hi}%)" if h_lo is not None else ""
        _row(sec, "Humidity", f"{round(humid)}%{note}")
    if wifi is not None:
        _row(sec, "WiFi", f"{wifi}%" + (f"  ({ssid})" if ssid else ""))
    if sec:
        L.append("\n  🌡️  Sensors"); L += sec

    # ── Lighting ─────────────────────────────────────────────────
    nl_on = G('get_hw_control', 'night_light_on')
    bright = G('get_light_style', 'brightness')
    nv = G('get_hw_control', 'night_vision')
    led = G('get_hw_control', 'status_light_on')
    sec = []
    if nl_on is not None:
        _row(sec, "Night light", _onoff(nl_on))
    if bright is not None:
        _row(sec, "Brightness", f"{bright}%")
    nls = (d.get('get_light_style') or {}).get('night_light')
    if nls and any(nls.get(c) for c in ('r', 'g', 'b')):
        _row(sec, "Light colour",
             f"#{(nls.get('r') or 0):02x}{(nls.get('g') or 0):02x}{(nls.get('b') or 0):02x}"
             f"  bri {nls.get('brightness')}  pattern {nls.get('pattern_id')}  [RGB unverified]")
    _row(sec, "Night vision", nv)
    if led is not None:
        _row(sec, "Status LED", _onoff(led))
    flip = G('get_hw_control', 'video_flip')
    if flip is not None:
        _row(sec, "Flip screen", _onoff(flip))
    if sec:
        L.append("\n  💡 Lighting"); L += sec

    # ── Comfort Range (temperature/humidity comfort-alert thresholds) ──
    hp = d.get('get_hw_policy') or {}
    sec = []
    if hp.get('temp_low_c') is not None:
        state = "alerts if outside" if hp.get('temp_alert') else "alert off"
        _row(sec, "Temperature", f"{hp['temp_low_c']}–{hp['temp_high_c']}°C  ({state})")
    if hp.get('humi_low_pct') is not None:
        state = "alerts if outside" if hp.get('humi_alert') else "alert off"
        _row(sec, "Humidity", f"{hp['humi_low_pct']}–{hp['humi_high_pct']}%   ({state})")
    if sec:
        L.append("\n  🌡️  Comfort Range"); L += sec

    # ── Audio ────────────────────────────────────────────────────
    song = G('get_lullaby', 'current_sound')
    playing = G('get_lullaby', 'is_playing')
    vol = G('get_lullaby_schedule', 'volume')
    timer = G('get_lullaby_schedule', 'timer')
    sec = []
    if song:
        state = "▶ playing" if playing else "⏹ stopped"
        v = f"  🔊 {vol}%" if vol is not None else ""
        _row(sec, "Playing", f"{song}  {state}{v}")
        if timer:
            _row(sec, "Timer", timer)
    scheds = (d.get('get_lullaby_schedules') or {}).get('schedules') or []
    for s in scheds[:3]:
        dm = s.get('days_mask', 0)
        days = "Mon–Sun" if (dm & 0x7f) == 0x7f else f"days 0x{dm:02x}"
        dur = s.get('duration_sec', 0); dh, dmin = dur // 3600, (dur % 3600) // 60
        ai = " +AI-autoplay" if s.get('ai_autoplay') else ""
        _row(sec, "Schedule",
             f"{s.get('sound') or s.get('name')} @ {s.get('start_hour', 0):02d}:"
             f"{s.get('start_minute', 0):02d}  {days}  for {dh}h{dmin:02d}m{ai}")
    if not scheds:
        sa = d.get('get_lullaby_schedule_action') or {}
        if sa.get('has_schedule') is not None:
            _row(sec, "Schedule", "configured" if sa.get('has_schedule') else "none scheduled")
    if sec:
        L.append("\n  🎵 Audio"); L += sec

    # ── Detection ────────────────────────────────────────────────
    sec = []
    cry = d.get('get_cry_detection')
    if cry is not None:
        v = "enabled" if cry.get('enabled') else "disabled"
        if cry.get('enabled'):
            if cry.get('sensitivity_label'):
                v += f"  (sensitivity: {cry['sensitivity_label']})"
            extra = []
            if cry.get('ai_enabled') is not None:
                extra.append(f"AI {'on' if cry['ai_enabled'] else 'off'}")
            if cry.get('dnn_confidence'):
                extra.append(f"conf≥{cry['dnn_confidence']:.2f}")
            if cry.get('hit_percentage'):
                extra.append(f"hit≥{cry['hit_percentage']:.0f}%")
            if cry.get('audio_filter_enable') is not None:
                extra.append(f"filter {'on' if cry['audio_filter_enable'] else 'off'}")
            if extra:
                v += "  [" + ", ".join(extra) + "]"
        _row(sec, "Cry", v)
    cough = d.get('get_cough_detection')
    if cough is not None:
        v = "enabled" if cough.get('enabled') else "disabled"
        if cough.get('enabled') and cough.get('mode_desc'):
            v += f"  ({cough['mode_desc']})"
        _row(sec, "Cough", v)
    danger = d.get('get_danger_zone')
    if danger is not None:
        if danger.get('configured'):
            nm = f": {danger['name']}" if danger.get('name') else ""
            _row(sec, "Danger zone", f"set{nm}")
        else:
            _row(sec, "Danger zone", "not set")
    dz = d.get('get_detection_zone_v2')
    if dz is not None:
        if dz.get('configured'):
            # normalized bounding box: the rectangle of the frame that is watched
            w = (dz['x_max'] - dz['x_min']) * 100
            h = (dz['y_max'] - dz['y_min']) * 100
            _row(sec, "Motion zone",
                 f"{w:.0f}%×{h:.0f}% box  "
                 f"(left {dz['x_min']*100:.0f}% → right {dz['x_max']*100:.0f}%, "
                 f"top {dz['y_min']*100:.0f}% → bottom {dz['y_max']*100:.0f}%)")
        else:
            _row(sec, "Motion zone", "full frame")
    ac = d.get('get_auto_capture')
    if ac is not None:
        _row(sec, "Auto capture", ac.get('desc'))
    if sec:
        L.append("\n  🔍 Detection"); L += sec

    # ── Sleep & Safety ───────────────────────────────────────────
    sm = G('get_sleep_mode', 'enabled')
    ss = d.get('get_sleep_safety_setting') or {}
    baby = ss.get('baby_presence_alert')
    sec = []
    if sm is not None:
        _row(sec, "Sleep mode", _onoff(sm) + (" (feed suspended)" if sm else ""))
    if ss.get('mode') is not None:
        _row(sec, "Sleep alerts", ss.get('mode_desc'))
    if baby is not None:
        _row(sec, "Baby presence", _onoff(baby))
    if sec:
        L.append("\n  😴 Sleep & Safety"); L += sec

    # ── Smart Accessories ────────────────────────────────────────
    stc = d.get('get_smart_temp_config')
    sti = d.get('get_smart_temp_info')
    mat = d.get('get_mat_info')
    sec = []
    if stc is not None and stc.get('enabled'):
        _row(sec, "Fever alert", f"high {stc.get('high_temp_c')}°C  low {stc.get('low_temp_c')}°C")
    elif stc is not None:
        _row(sec, "Fever alert", "disabled")
    if sti is not None:
        _row(sec, "Thermometer",
             f"{sti.get('temp_c')}°C  (battery {sti.get('battery')}%)" if sti.get('paired')
             else "not paired")
    if mat is not None:
        _row(sec, "Breathing mat",
             f"{mat.get('bpm')} bpm  ({mat.get('detect_state')})" if mat.get('connected')
             else "not connected")
    if sec:
        L.append("\n  🍼 Smart Accessories"); L += sec

    # ── Network ──────────────────────────────────────────────────
    wf = d.get('get_wifi') or {}
    sec = []
    _row(sec, "WiFi SSID", wf.get('ssid') or ssid)
    _row(sec, "IP address", wf.get('ip'))
    _row(sec, "Camera MAC", wf.get('mac'))
    # Connected-AP radio metrics (new parse_wifi keys @0x94..0xa4 — present only when the
    # response is long enough). DISTINCT from the WiFi % above (get_hw_control.wifi_strength,
    # a 0-100 quality percent). 0 = this firmware did not populate the field (confirm live).
    if 'strength' in wf:
        _row(sec, "Radio (AP)",
             f"RSSI={wf.get('strength')} dBm  quality={wf.get('quality')}  "
             f"noise={wf.get('noise')} dBm  ch {wf.get('channel')} ({wf.get('frequency')} MHz)")
    if sec:
        L.append("\n  📡 Network"); L += sec

    # ── Stream ───────────────────────────────────────────────────
    mp = d.get('get_media_profiles')
    sec = []
    if mp is not None and mp.get('width'):
        _row(sec, "Resolution", f"{mp['width']}×{mp['height']} @ {mp['fps']} fps")
        _row(sec, "Codec", mp.get('codec'))
        if mp.get('bitrate_kbps'):
            _row(sec, "Bitrate", f"~{mp['bitrate_kbps']/1000:.1f} Mbps (HD)")
        if mp.get('gop'):
            _row(sec, "Keyframe", f"every {mp['gop']} frames")
    if sec:
        L.append("\n  📺 Stream"); L += sec

    # ── Session stats (camera-side telemetry — 0x0934, undocumented) ──
    # The camera's own view of this session: connection mode + NAT, per-stream
    # frame/keyframe counts, its resend-FIFO pressure (resendBufferUsage) and
    # send-error counters. A health channel to cross-check our own loss/recovery.
    # Always surfaced (degrades to "n/a") so it's clear it was queried. NOTE: on this
    # firmware the VIDEO frm_count reads 0 (a session_id/index quirk) while audio advances —
    # a 0 here is NORMAL, not "broken"; empty error rings are expected.
    ssx = d.get('get_session_stats')
    sec = []
    if ssx and ssx.get('mode'):
        natv = ssx.get('nat')
        _row(sec, "Connection", f"{ssx.get('mode')}" + (f"  (NAT {natv})" if natv else "  (NAT 0)"))
        vstat = ssx.get('video') or {}
        astat = ssx.get('audio') or {}
        vp = [f"frames={vstat.get('frm_count', 0)}"]
        if vstat.get('key_frm_count') is not None:
            vp.append(f"keyframes={vstat.get('key_frm_count')}")
        if vstat.get('resendBufferUsage') is not None:
            vp.append(f"resendBuf={vstat.get('resendBufferUsage')}")
        if vstat.get('send_err_count') is not None:
            vp.append(f"send_err={vstat.get('send_err_count')}")
        errs = [str(e.get('code')) for e in (vstat.get('errors') or []) if e.get('code')]
        if errs:
            vp.append("errors=[" + ",".join(errs[:6]) + "]")
        _row(sec, "Video", "  ".join(vp))
        ap = [f"frames={astat.get('frm_count', 0)}"]
        if astat.get('send_err_count') is not None:
            ap.append(f"send_err={astat.get('send_err_count')}")
        _row(sec, "Audio", "  ".join(ap))
        if not (vstat.get('frm_count') or astat.get('frm_count')):
            _row(sec, "(note)", "per-session counters — richer mid-stream (see --benchmark)")
    else:
        _row(sec, "Session stats", "n/a (no response)")
    L.append("\n  📊 Session stats"); L += sec

    # ── Users ────────────────────────────────────────────────────
    # Prefer get_user_list (0x0946, JSON {"users":[…]}); fall back to the documented
    # get_connected_users (0x099a, which returns count 0 on this firmware).
    # Always surfaced (degrades to "n/a"). Prefer the JSON user list; fall back to 0x099a.
    ul = d.get('get_user_list') or {}
    users = sorted(set(ul.get('users') or []))
    cu = d.get('get_connected_users') or {}
    L.append("\n  👥 Users")
    if users:
        _row(L, "Connected", f"{len(users)}: {', '.join(users)}")
    elif cu.get('count'):
        accs = cu.get('accounts') or []
        _row(L, "Connected", f"{cu.get('count')}" + (f": {', '.join(accs)}" if accs else ""))
    else:
        _row(L, "Connected", "n/a")

    # ── System ───────────────────────────────────────────────────
    fw = (G('get_hw_control', 'firmware')
          or G('get_lightweight_status', 'firmware')
          or G('check_firmware_update', 'current_version'))
    upd = G('check_firmware_update', 'update_available')
    events = G('get_event_list', 'count')
    sec = []
    if fw:
        tag = "✅ up to date" if upd is False else (
            f"⬆ update → {G('check_firmware_update','latest_version')}" if upd else "")
        _row(sec, "Firmware", f"{fw}  {tag}".rstrip())
    if events is not None:
        _row(sec, "Recent events", str(events))
    fs = d.get('get_feature_support') or {}
    flags = fs.get('flags')
    if flags:
        _row(sec, "Capabilities", f"{sum(1 for f in flags if f)}/{len(flags)} feature flags set "
                                  "(0x1316 map; ordering not decoded)")
    if sec:
        L.append("\n  ℹ️  System"); L += sec

    L.append("")
    L.append(bar)
    return "\n".join(L)


def print_status(sess) -> None:
    """Read every GET method and print the clean CuboAI status card."""
    print(_render_status(_read_all(sess)))


def take_snapshot(sess, path: str) -> None:
    print("📸 Taking snapshot...", flush=True)
    try:
        path = sess.save_snapshot(os.path.expanduser(path), timeout_sec=20.0)
        print(f"   ✅ Saved JPEG: {path} ({os.path.getsize(path)//1024} KB)")
    except ImportError:
        print("❌ Snapshot requires PyAV: pip install av")
    except TimeoutError as e:
        print(f"   ❌ {e}")
    except Exception as e:
        print(f"   ❌ Snapshot failed: {e}")


# ── WiFi-placement / performance benchmark ───────────────────────────────────
# Read-only: streams (so the engine's loss/recovery counters advance) while polling
# the camera's WiFi signal + 0x0934 session-stats at a modest cadence, printing one
# metrics block per interval and a comparison summary on exit. Client-side counters are
# free (get_stats reads in-memory ints); the camera GETs are INJECTED onto the engine's
# reader thread (get_during_stream) so they never race the AV socket.
#
# RSSI note (confirmed from parse_wifi/parse_hw_control): the camera reports WiFi as a
# 0-100 quality PERCENT (get_hw_control.wifi_strength); get_wifi carries SSID/IP/MAC but
# NO dBm RSSI field. So signal% leads, and client-side loss% is the placement proxy the
# brief calls for (lower loss% = better placement).

def _color(s, c, enable):
    if not enable:
        return s
    codes = {'g': '\033[32m', 'y': '\033[33m', 'r': '\033[31m'}
    return codes.get(c, '') + s + '\033[0m'


def _sig_band(pct):
    """(label, colour) for a WiFi quality percent. green ≥70, yellow 40-69, red <40."""
    if pct is None:
        return 'n/a', 'y'
    if pct >= 70:
        return f'{pct}%', 'g'
    if pct >= 40:
        return f'{pct}%', 'y'
    return f'{pct}%', 'r'


def _loss_band(p):
    """colour for an interval loss% — green <1%, yellow 1-5%, red >5%."""
    return 'g' if p < 1.0 else ('y' if p <= 5.0 else 'r')


def run_benchmark(transport, interval=2.0, cap=None, csv_path=None):
    """Read-only WiFi-placement/perf benchmark (see the block comment above)."""
    import cuboai_pure as cp
    import threading
    import csv as _csv
    if not hasattr(transport, 'get_stats'):
        print("❌ --benchmark requires the pure-Python backend (omit --lib / CUBOAI_LIB).")
        return
    color = sys.stdout.isatty() and not csv_path
    print(f"\n📶 WiFi-placement benchmark — sampling every {interval:g}s"
          + (f" for {cap:g}s" if cap else " (Ctrl-C to stop)"), flush=True)
    print("   signal% = camera WiFi quality (no dBm RSSI on this camera); "
          "loss% = client-side placement proxy\n", flush=True)

    # Background consumer: drain av_frames so the engine's reader thread stays alive and
    # the counters advance. Frames are discarded — observe-only, no media is written.
    stop = threading.Event()

    def _drain():
        # duration=None so the stream stays alive for the whole benchmark (incl. the final
        # sample); the main loop ends it via `stop` (checked each queue tick) at the cap.
        try:
            for _ in transport.av_frames(duration=None):
                if stop.is_set():
                    break
        except Exception:
            pass

    th = threading.Thread(target=_drain, daemon=True)
    th.start()

    csvf = csvw = None
    if csv_path:
        csvf = open(os.path.expanduser(csv_path), 'w', newline='')
        csvw = _csv.writer(csvf)
        csvw.writerow(['t', 'elapsed_s', 'wifi_pct', 'ssid', 'loss_pct', 'recovery_pct',
                       'fps', 'bitrate_kbps', 'resend_buffer', 'send_err', 'mode', 'nat',
                       'video_frames', 'audio_frames', 'gap_now', 'kf_incomplete'])

    # Let the reader come up + a little video flow before the first sample (also keeps
    # get_during_stream on the inject path rather than the pre-stream direct-ioctl path).
    time.sleep(min(interval, 1.0))

    t0 = time.time()
    prev = first_snap = None
    sig_samples, loss_samples, fps_samples = [], [], []
    nsamples = 0
    try:
        while True:
            tick = time.time()
            hw = transport.get_during_stream('get_hw_control', timeout=1.5) or {}
            ssx = transport.get_during_stream('get_session_stats', timeout=1.5) or {}
            cur = transport.get_stats()
            if first_snap is None:
                first_snap = cur
            d = cp.stats_delta(prev, cur)
            prev = cur
            elapsed = tick - t0

            wifi = hw.get('wifi_strength')
            ssid = hw.get('ssid')
            vstat = ssx.get('video') or {}
            astat = ssx.get('audio') or {}
            rbu = vstat.get('resendBufferUsage')
            serr = vstat.get('send_err_count')
            mode = ssx.get('mode')
            nat = ssx.get('nat')

            sig_s, sig_c = _sig_band(wifi)
            loss_s = f"{d['loss_pct']:4.1f}%"
            ssid_s = f"({ssid})  " if ssid else ""
            # loss% is the per-interval signal (responsive to placement); recovery% is shown
            # cumulative (a per-interval ratio of two counters is noisy / can exceed 100%).
            line = (f"  t={elapsed:5.1f}s  WiFi {_color(sig_s.ljust(5), sig_c, color)} {ssid_s}"
                    f"loss {_color(loss_s, _loss_band(d['loss_pct']), color)}  "
                    f"recov {cur['recovery_pct']:5.1f}%  fps {d['fps']:4.1f}  "
                    f"{d['bitrate_kbps'] / 1000.0:4.1f}Mbps")
            if mode:
                line += f"  [{mode}{('/NAT' + str(nat)) if nat else ''}]"
            if rbu:
                line += f"  rbuf {rbu}"
            if serr:
                line += f"  serr {serr}"
            print(line, flush=True)

            if wifi is not None:
                sig_samples.append(wifi)
            loss_samples.append(d['loss_pct'])
            fps_samples.append(d['fps'])
            nsamples += 1
            if csvw:
                csvw.writerow([f"{tick:.3f}", f"{elapsed:.1f}", wifi, ssid,
                               f"{d['loss_pct']:.2f}", f"{d['recovery_pct']:.1f}",
                               f"{d['fps']:.1f}", f"{d['bitrate_kbps']:.0f}", rbu, serr,
                               mode, nat, vstat.get('frm_count'), astat.get('frm_count'),
                               cur['gap_now'], d['kf_incomplete']])
                csvf.flush()

            if cap and elapsed >= cap:
                break
            dt = interval - (time.time() - tick)        # the GETs already used some of it
            if dt > 0:
                time.sleep(dt)
    except KeyboardInterrupt:
        print("\n  (interrupted)", flush=True)
    finally:
        stop.set()
        th.join(timeout=2.0)         # M4: don't leave the drain thread attached to the live camera
        if csvf:
            csvf.close()

    # ── summary (compare location A vs B at a glance) ──
    total = time.time() - t0
    span = cp.stats_delta(first_snap, transport.get_stats()) if first_snap else {}

    def _avg(xs):
        return sum(xs) / len(xs) if xs else 0.0

    print("\n  " + "─" * 50)
    print("  📶 Benchmark summary")
    print(f"     Duration        {total:.0f}s  ({nsamples} samples)")
    if sig_samples:
        print(f"     WiFi signal     avg {_avg(sig_samples):.0f}%   "
              f"min {min(sig_samples)}%   max {max(sig_samples)}%")
    else:
        print("     WiFi signal     n/a (camera reports no signal field)")
    print(f"     Loss            avg {_avg(loss_samples):.2f}%")
    print(f"     Recovery        {span.get('recovery_pct', 100.0):.1f}%   "
          f"({span.get('recovery_events', 0)} of {span.get('frags_lost', 0)} lost fragments recovered)")
    print(f"     Frame rate      avg {_avg(fps_samples):.1f} fps")
    print(f"     Video AUs       {span.get('au_video', 0)}  "
          f"(incomplete {span.get('au_incomplete', 0)}, "
          f"keyframe-incomplete {span.get('kf_incomplete', 0)})")
    if csv_path:
        print(f"     CSV             {os.path.expanduser(csv_path)}")
    print("  " + "─" * 50)
    print("  Compare locations by WiFi% (higher is better) and loss% (lower is better).",
          flush=True)


def _clamp_env_knobs():
    """Range-clamp the numeric CUBOAI_* tuning env vars (startup only, stderr only) so a zero/absurd
    override can't divide-by-zero or break recovery. Non-numeric -> default; out-of-range -> clamp."""
    KNOBS = [
        ('CUBOAI_GAP_DEPTH_CAP', 1, 10000, 200, False),
        ('CUBOAI_RECOVERY_HOLD', 0, 2000, 24, False),
        ('CUBOAI_LONE_SKIP_ROUNDS', 0, 2000, 20, False),
        ('CUBOAI_KF_HOLD', 0, 2000, 40, False),
        ('CUBOAI_GAP_HOLD_MS', 1, 600000, None, False),
        ('CUBOAI_VERBOSE_INTERVAL', 0.1, 3600, 5.0, True),
        ('CUBOAI_VERBOSE_CAMERA_STATS_INTERVAL', 1, 86400, None, False),
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


def _validate_startup(args, uid, account, password, camera_ip):
    """Startup-only input validation: stderr only, never the per-frame hot path or stdout. Hard-fails
    (exit 2) on malformed required input with a clear message; clamps out-of-range env knobs."""
    import re as _re
    errs = []
    if not camera_ip:
        errs.append("--camera-ip (or CUBOAI_CAMERA_IP) is required — the pure backend connects "
                    "directly to the camera (no LAN broadcast discovery). The IP comes from the REST API.")
    elif _re.fullmatch(r'[\d.]+', camera_ip):
        octs = camera_ip.split('.')
        if not (len(octs) == 4 and all(o.isdigit() and 0 <= int(o) <= 255 for o in octs)):
            errs.append(f"--camera-ip {camera_ip!r} is not a valid IPv4 address.")
    elif not _re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.\-]{0,253}', camera_ip):
        errs.append(f"--camera-ip {camera_ip!r} is not a valid IPv4 address or hostname.")
    miss = [n for n, v in (('uid', uid), ('account', account), ('password', password)) if not v]
    if miss:
        errs.append(f"missing credential(s): {', '.join(miss)} "
                    "(pass --uid/--account/--password or CUBOAI_UID/ACCOUNT/PASSWORD).")
    elif not _re.fullmatch(r'[A-Za-z0-9]{16,24}', uid):
        errs.append(f"--uid {uid!r} looks malformed (expected ~20 alphanumeric characters).")
    for name, val in (('--brightness', getattr(args, 'brightness', None)),
                      ('--volume', getattr(args, 'volume', None)),
                      ('--mic-volume', getattr(args, 'mic_volume', None)),
                      ('--speaker-volume', getattr(args, 'speaker_volume', None))):
        if val is not None and not (0 <= val <= 100):
            errs.append(f"{name} {val} out of range [0,100].")
    if getattr(args, 'duration', None) is not None and args.duration <= 0:
        errs.append(f"--duration must be > 0 (got {args.duration}).")
    v = getattr(args, 'benchmark_interval', None)
    if v is not None and v <= 0:
        errs.append(f"--benchmark-interval must be > 0 (got {v}).")
    for opt in ('record', 'snapshot', 'record_video', 'record_audio', 'record_av'):
        p = getattr(args, opt, None)
        if p:
            d = os.path.dirname(os.path.abspath(os.path.expanduser(p))) or '.'
            if not (os.path.isdir(d) and os.access(d, os.W_OK)):
                errs.append(f"--{opt.replace('_', '-')} {p!r}: directory {d!r} is not writable.")
    if errs:
        for e in errs:
            print("Error: " + e, file=sys.stderr)
        sys.exit(2)
    _clamp_env_knobs()


def main():
    parser = argparse.ArgumentParser(
        description="CuboAI camera validation and control",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    conn = parser.add_argument_group("Connection")
    conn.add_argument('--lib',       metavar='PATH', help='Path to libIOTCAPIs_ALL.so')
    conn.add_argument('--uid',       metavar='UID',  help='Device UID (license_id)')
    conn.add_argument('--account',   metavar='STR',  help='dev_admin_id')
    conn.add_argument('--password',  metavar='STR',  help='dev_admin_pwd')
    conn.add_argument('--camera-ip', metavar='IP',   help='Camera LAN IP (enables broadcast redirect on Linux)')
    conn.add_argument('--channels',  metavar='DIGITS', default=None,
                      help='(pure backend) AV channels to open as single digits, e.g. "0123", "01", "1"; '
                           'default (omitted) = ch0..39, the full av-connect channel set')
    conn.add_argument('-v', '--verbose', action='store_true',
                      help='(pure backend) print a connect/stream trace (channels, grant, ACK/gap state)')
    # defer-start (pure backend) — same naming/default as cuboai_stream_video. By DEFAULT video
    # starts fast (0x0300+0x01FF up front, first frame in ~0.5-2 s) so short captures don't race a
    # ~5 s window. --defer-start re-enables the deliberate ~5 s native startup defer for wire-
    # fidelity. The wire-fidelity behaviour (ACK timestamp / NAK cadence / SACK list) stays ON
    # either way — it affects resend efficiency, never whether AV works.
    conn.add_argument('--defer-start', action='store_true',
                      help='(pure backend) re-enable the ~5 s native startup defer (wire-fidelity); '
                           'default starts video immediately. Also via CUBOAI_DEFER_START=1.')
    conn.add_argument('--no-defer-start', action='store_true', help=argparse.SUPPRESS)  # back-compat no-op

    parser.add_argument('--snapshot',    metavar='FILE',              help='Save JPEG snapshot to FILE')
    parser.add_argument('--record',       metavar='FILE',              help='Record muxed audio+video to a playable .mp4')
    parser.add_argument('--record-video', metavar='FILE',              help='Record the raw HEVC video element to file')
    parser.add_argument('--duration',    type=float, default=10.0,    metavar='SECS')
    parser.add_argument('--stream-video', action='store_true',        help='Stream HEVC video to stdout (pipe to ffplay -f hevc -i -)')
    parser.add_argument('--talk',          metavar='FILE',              help='Send audio file to camera speaker')
    parser.add_argument('--record-audio',  metavar='FILE',              help='Record AAC audio to file (e.g. audio.aac)')
    parser.add_argument('--record-av',     metavar='BASE',              help='Record both streams: BASE.hevc + BASE.aac')
    parser.add_argument('--stream-audio',  action='store_true',         help='Stream raw AAC-ADTS to stdout')
    parser.add_argument('--raw', '--passthrough', dest='raw', action='store_true',
                        help='Capture the unprocessed Annex-B bitstream (no FRAMEINFO strip / no '
                             'recovery) for inspection. Default = the production profile (clean, playable).')
    parser.add_argument('--night-light', choices=['on','off'],        help='Night light on/off')
    parser.add_argument('--brightness',  type=int,   metavar='0-100', help='Night light brightness %%')
    parser.add_argument('--volume',      type=int,   metavar='0-100', help='Lullaby volume %%')
    parser.add_argument('--timer',       choices=['repeat','30min','60min'])
    parser.add_argument('--play',        metavar='NAME',              help='Play lullaby by name')
    parser.add_argument('--stop',        action='store_true',         help='Stop lullaby')
    parser.add_argument('--sleep-mode',  choices=['on','off'],        help='Sleep/privacy mode')
    # ── additional SET commands ────────────────────────────────────────────
    setg = parser.add_argument_group("SET commands (new)")
    setg.add_argument('--night-vision', choices=['auto','on','off'],
                      help='Night-vision/IR mode (SET_HW_CONTROL; accepted but firmware-managed on this device)')
    setg.add_argument('--status-light', choices=['on','off'],
                      help='Camera-body status LED (accepted but firmware-managed on this device)')
    setg.add_argument('--video-flip',   choices=['on','off'],
                      help='Vertical image flip (SET_HW_CONTROL)')
    setg.add_argument('--mic-volume',     type=int, metavar='N',
                      help='Mic level via SET_HW_CONTROL (firmware-managed on this device)')
    setg.add_argument('--speaker-volume', type=int, metavar='N',
                      help='Speaker level via SET_HW_CONTROL (firmware-managed on this device)')
    setg.add_argument('--cry-detection',  choices=['on','off'], help='Cry detection on/off')
    setg.add_argument('--cry-sensitivity',choices=['low','medium','high'],
                      help='Cry detection sensitivity (low/medium/high → wire 3/2/1)')
    setg.add_argument('--cough-detection',choices=['on','off'], help='Cough detection on/off')
    setg.add_argument('--cough-mode',     choices=['always','in-crib'],
                      help='Cough alert mode: always alert vs only when baby is in crib')
    setg.add_argument('--cough-sensitivity', choices=['low','medium','high'],
                      help='Cough detection sensitivity (low/medium/high → wire 3/2/1)')
    setg.add_argument('--flip-screen',    choices=['on','off'],
                      help='Vertical image flip (alias of --video-flip; SET_HW_CONTROL video_v_flip)')
    setg.add_argument('--sleep-alerts',   choices=['covered-only','covered-and-rollover','off'],
                      help='Sleep-safety mode: covered-face-only vs covered-face+rollover (or off)')
    setg.add_argument('--safety-alert',        choices=['on','off'], help='Sleep-safety: safety/rollover alert (low-level)')
    setg.add_argument('--cover-alert',         choices=['on','off'], help='Sleep-safety: cover alert (low-level)')
    setg.add_argument('--baby-presence-alert', choices=['on','off'], help='Sleep-safety: baby presence alert')
    setg.add_argument('--danger-zone-alert',   choices=['on','off'],
                      help='Danger-zone alert on/off (toggles roi.enable; full polygon needs the region grid, not wired)')
    setg.add_argument('--comfort-temp-low',  type=int, metavar='C',   help='Comfort range: low temperature (°C)')
    setg.add_argument('--comfort-temp-high', type=int, metavar='C',   help='Comfort range: high temperature (°C)')
    setg.add_argument('--comfort-humi-low',  type=int, metavar='PCT', help='Comfort range: low humidity (%%)')
    setg.add_argument('--comfort-humi-high', type=int, metavar='PCT', help='Comfort range: high humidity (%%)')
    setg.add_argument('--auto-capture', choices=['off','motion','schedule','both'],
                      help='Auto event-snapshot mode (off/motion/schedule/both)')
    setg.add_argument('--schedule-volume', type=int, metavar='0-100',
                      help='Lullaby schedule volume (the volume GET_LULLABY_SCHEDULE reports)')
    setg.add_argument('--temp-alert', choices=['on','off'], help='Environment: temperature comfort alert on/off')
    setg.add_argument('--temp-low',   type=int, metavar='C', help='Environment: low temperature threshold (C)')
    setg.add_argument('--temp-high',  type=int, metavar='C', help='Environment: high temperature threshold (C)')
    setg.add_argument('--humi-alert', choices=['on','off'], help='Environment: humidity comfort alert on/off')
    setg.add_argument('--humi-low',   type=int, metavar='PCT', help='Environment: low humidity threshold (pct)')
    setg.add_argument('--humi-high',  type=int, metavar='PCT', help='Environment: high humidity threshold (pct)')
    parser.add_argument('--list-songs',  action='store_true',         help='List all songs')
    parser.add_argument('--no-status',   action='store_true',         help='Skip the status read (status and AV streaming coexist)')

    # ── WiFi-placement / performance benchmark (read-only) ─────────────────
    bench = parser.add_argument_group("Benchmark (WiFi placement & performance)")
    bench.add_argument('--benchmark', nargs='?', type=float, const=0.0, default=None, metavar='SECS',
                       help='Stream + print a metrics block every --benchmark-interval seconds '
                            '(WiFi signal%%, client loss%%, recovery, fps, bitrate, camera resend-buffer), '
                            'then a summary on exit. Optional SECS bounds the run (default: until Ctrl-C). '
                            'Read-only; pure-Python backend only.')
    bench.add_argument('--benchmark-interval', type=float, default=2.0, metavar='SECS',
                       help='Seconds between benchmark metric blocks (default 2).')
    bench.add_argument('--benchmark-csv', metavar='FILE',
                       help='Append each benchmark sample as a CSV row to FILE (for comparing locations).')

    args = parser.parse_args()

    if args.list_songs:
        print("\nAvailable lullaby songs:")
        cur_cat = None
        for uuid, (key, name, category) in LULLABY_CATALOG.items():
            if category != cur_cat:
                cur_cat = category
                print(f"\n  {category.upper()}")
            print(f"    {name:<42} {uuid}")
        print()
        return

    # ── Broadcast redirect shim (Linux LAN) ─────────────────────
    camera_ip = getattr(args, 'camera_ip', None) or os.environ.get('CUBOAI_CAMERA_IP')
    # The broadcast-redirect shim is ONLY needed for the native TUTK library (it
    # broadcasts discovery). Pure Python unicasts straight to camera_ip, so skip the
    # shim+execve entirely in pure mode (the execve also breaks stdout piping).
    # Native is now an EXPLICIT opt-in (--lib / CUBOAI_LIB) — the library is never
    # auto-discovered (matches the pure-by-default get_session call below).
    _will_use_native = bool(args.lib or os.environ.get('CUBOAI_LIB'))
    if camera_ip and platform.system() == 'Linux' and _will_use_native:
        shim_path = '/tmp/cuboai_redirect.so'
        shim_src  = '/tmp/cuboai_redirect.c'
        if not os.path.exists(shim_path):
            import subprocess, textwrap
            c_src = textwrap.dedent("""
                #define _GNU_SOURCE
                #include <dlfcn.h>
                #include <string.h>
                #include <sys/socket.h>
                #include <netinet/in.h>
                typedef ssize_t (*sendto_t)(int,const void*,size_t,int,const struct sockaddr*,socklen_t);
                static sendto_t real_sendto=NULL;
                static unsigned char cam_ip[4]={0,0,0,0};
                void set_camera_ip(unsigned char a,unsigned char b,unsigned char c,unsigned char d){cam_ip[0]=a;cam_ip[1]=b;cam_ip[2]=c;cam_ip[3]=d;}
                ssize_t sendto(int fd,const void*buf,size_t len,int flags,const struct sockaddr*addr,socklen_t al){
                    if(!real_sendto)real_sendto=dlsym(RTLD_NEXT,"sendto");
                    if(addr&&addr->sa_family==AF_INET6&&len==88&&cam_ip[0]){
                        struct sockaddr_in6*s6=(struct sockaddr_in6*)addr;
                        if(ntohs(s6->sin6_port)==32761&&s6->sin6_addr.s6_addr[15]==255){
                            struct sockaddr_in6 c=*s6;
                            memset(c.sin6_addr.s6_addr,0,10);c.sin6_addr.s6_addr[10]=0xff;c.sin6_addr.s6_addr[11]=0xff;
                            memcpy(c.sin6_addr.s6_addr+12,cam_ip,4);
                            return real_sendto(fd,buf,len,flags,(struct sockaddr*)&c,al);
                        }
                    }
                    return real_sendto(fd,buf,len,flags,addr,al);
                }
            """)
            with open(shim_src, 'w') as f:
                f.write(c_src)
            subprocess.run(['gcc','-shared','-fPIC','-O2','-o',shim_path,shim_src,'-ldl'],
                           capture_output=True)

        if os.path.exists(shim_path):
            if shim_path not in os.environ.get('LD_PRELOAD', ''):
                import sys as _sys
                env = os.environ.copy()
                env['LD_PRELOAD'] = (shim_path + ':' + env.get('LD_PRELOAD','')).strip(':')
                env['CUBOAI_CAMERA_IP'] = camera_ip
                os.execve(_sys.executable, [_sys.executable] + _sys.argv, env)
            else:
                import ctypes, importlib
                shim = ctypes.CDLL(shim_path)
                shim.set_camera_ip.argtypes = [ctypes.c_ubyte] * 4
                shim.set_camera_ip(*map(int, camera_ip.split('.')))
                import cuboai_tutk
                importlib.reload(cuboai_tutk)
                global TUTKSession
                TUTKSession = cuboai_tutk.TUTKSession

    # ── Connection params ────────────────────────────────────────
    lib_path = args.lib      or os.environ.get('CUBOAI_LIB')
    uid      = args.uid      or os.environ.get('CUBOAI_UID')
    account  = args.account  or os.environ.get('CUBOAI_ACCOUNT')
    password = args.password or os.environ.get('CUBOAI_PASSWORD')
    _validate_startup(args, uid, account, password, camera_ip)   # startup-only; stderr; exits on bad input

    missing = [k for k,v in [('--uid',uid),('--account',account),('--password',password)] if not v]
    if missing:
        print(f"❌ Missing: {', '.join(missing)}")
        print("   Set via args or env: CUBOAI_UID, CUBOAI_ACCOUNT, CUBOAI_PASSWORD")
        sys.exit(1)

    if args.brightness is not None and not 0 <= args.brightness <= 100:
        parser.error("--brightness must be 0-100")
    if args.volume is not None and not 0 <= args.volume <= 100:
        parser.error("--volume must be 0-100")

    # ── Connect ──────────────────────────────────────────────────
    channels = [int(c) for c in args.channels] if args.channels else None   # AV channels (None = all)
    # defer-start: same naming/default as cuboai_stream_video. Default = start fast (_defer=False, so
    # 0x0300+0x01FF go up front and the first frame lands in ~0.5-2 s — short captures don't race a
    # ~5 s window). --defer-start (or CUBOAI_DEFER_START) re-enables the ~5 s native defer (_defer=None
    # → follow full_fidelity). full_fidelity (ACK ts / NAK cadence / SACK) stays ON either way.
    defer  = args.defer_start or os.environ.get('CUBOAI_DEFER_START', '0') != '0'
    _defer = None if defer else False
    # Install the same A/V env profile cuboai_stream_video ships: default = production (FRAMEINFO
    # strip + loss recovery, so --snapshot/--record are clean & playable); --raw = the
    # unprocessed Annex-B passthrough. MUST run before get_session() (the engine reads the gates at
    # construction). Explicit env vars still win in the default branch; --raw hard-forces.
    apply_env_profile(args.raw)
    transport = get_session(uid, account, password, lib_path=lib_path, camera_ip=camera_ip,
                            channels=channels, verbose=args.verbose,
                            auto_discover_lib=False,   # pure by default; --lib/CUBOAI_LIB = native opt-in
                            defer_stream_start=_defer, defer_video_start_late=_defer)
    is_pure = type(transport).__name__ == 'PureSession'
    print(f"\nConnecting to {uid}...", flush=True)
    try:
        transport.connect()
        if is_pure:
            print("✅ Connected (pure Python)", flush=True)
            if transport.session_hdr:
                print(f"   session_hdr: {transport.session_hdr.hex()}")
            print(flush=True)
        else:
            print("✅ Connected\n", flush=True)
    except Exception as e:
        print(f"❌ Connection failed: {e}")
        sys.exit(1)

    # Both backends now follow the same feature flow — PureSession implements
    # ioctl/snapshot/av_frames/audio_frames in pure Python (only --talk differs).
    try:
        # ── Benchmark (its own read-only mode; skips the rest) ───────────────
        if args.benchmark is not None:
            run_benchmark(transport, interval=args.benchmark_interval,
                          cap=(args.benchmark or None), csv_path=args.benchmark_csv)
            return

        if not args.no_status:
            print_status(transport)

        # ── Snapshot ─────────────────────────────────────────────
        if args.snapshot:
            take_snapshot(transport, args.snapshot)

        # ── Talk ─────────────────────────────────────────────────
        if args.talk:
            print(f"\n🎤 Sending audio to camera: {args.talk}", flush=True)
            try:
                transport.send_audio_file(args.talk)
                print("   ✅ Audio sent")
            except FileNotFoundError as e:
                print(f"   ❌ {e}")
            except RuntimeError as e:
                print(f"   ❌ {e}")
            except ImportError as e:
                # Surface the ACTUAL missing module rather than always blaming PyAV
                # (e.g. the stdlib `audioop` removal in Python 3.13 used to show here).
                print(f"   ❌ Talkback dependency import failed: {e}  "
                      f"(transcode needs PyAV: pip install av)")

        # ── Record video element (raw HEVC) ──────────────────────
        if args.record_video:
            path = os.path.expanduser(args.record_video)
            print(f"🎥 Recording {args.duration}s raw HEVC video element to {path}...", flush=True)
            count = 0
            with open(path, 'wb') as f:
                for ftype, data in transport.av_frames(duration=args.duration):
                    if ftype == 'video':
                        f.write(data)
                        count += 1
            print(f"   ✅ {count} video frames ({os.path.getsize(path)//1024} KB)")

        # ── Record audio (uses av_frames to avoid wasting video) ────
        if args.record_audio:
            path = os.path.expanduser(args.record_audio)
            print(f"🎤 Recording {args.duration}s AAC audio to {path}...", flush=True)
            a_count = 0
            with open(path, 'wb') as fa:
                for ftype, data in transport.av_frames(duration=args.duration):
                    if ftype == 'audio':
                        fa.write(data)
                        a_count += 1
            print(f"   ✅ {a_count} audio frames ({os.path.getsize(path)//1024} KB)")

        # ── Record AV (combined video + audio) ───────────────────
        if args.record_av:
            base = os.path.expanduser(args.record_av).rstrip('/')
            if os.path.isdir(base):
                print(f"❌ --record-av needs a base filename, not a directory.")
                print(f"   Example: --record-av /tmp/clip  (produces /tmp/clip.hevc + /tmp/clip.aac)")
            else:
                vpath = base + '.hevc'
                apath = base + '.aac'
                print(f"🎥 Recording {args.duration}s AV to {vpath} + {apath}...", flush=True)
                v_count = a_count = 0
                with open(vpath, 'wb') as fv, open(apath, 'wb') as fa:
                    for ftype, data in transport.av_frames(duration=args.duration):
                        if ftype == 'video':
                            fv.write(data); v_count += 1
                        else:
                            fa.write(data); a_count += 1
                print(f"   ✅ {v_count} video frames ({os.path.getsize(vpath)//1024} KB)")
                print(f"   ✅ {a_count} audio frames ({os.path.getsize(apath)//1024} KB)")

        # ── Stream audio ──────────────────────────────────────────
        if args.stream_audio:
            print("🎤 Streaming AAC-ADTS to stdout", flush=True)
            import sys as _sys
            for frame in transport.audio_frames():
                _sys.stdout.buffer.write(frame)
                _sys.stdout.buffer.flush()

        # ── Stream ───────────────────────────────────────────────
        if args.stream_video:
            print("🎥 Streaming HEVC video to stdout — pipe to: ffplay -f hevc -i -", flush=True)
            import sys as _sys
            for frame in transport.video_frames():
                _sys.stdout.buffer.write(frame)
                _sys.stdout.buffer.flush()

        # ── Record (muxed audio+video .mp4) ──────────────────────
        if args.record:
            path = os.path.expanduser(args.record)
            print(f"🎥 Recording {args.duration}s muxed audio+video to {path}...", flush=True)
            try:
                transport.record_video(path, duration_sec=args.duration)
                print(f"   ✅ Saved: {path} ({os.path.getsize(path)//1024} KB)")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Night light ──────────────────────────────────────────
        if args.night_light:
            on = args.night_light == 'on'
            print(f"\n💡 Night light → {'ON' if on else 'OFF'}...", flush=True)
            try:
                transport.ioctl(*build_set_night_light(on))
                print("   ✅ Done")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Brightness ───────────────────────────────────────────
        if args.brightness is not None:
            print(f"\n💡 Brightness → {args.brightness}%...", flush=True)
            try:
                transport.ioctl(*build_set_light_style_brightness(args.brightness))
                print("   ✅ Done")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Volume / timer ───────────────────────────────────────
        if args.volume is not None or args.timer is not None:
            print(f"\n🔊 Updating lullaby settings...", flush=True)
            try:
                tc, data = transport.ioctl(2440, b'\x00' * 132)
                cur_vol, cur_timer = 50, LULLABY_TIMER_REPEAT
                if tc == IOTYPE_USER_GET_LULLABY_SCHEDULE_RESP and len(data) >= 16:
                    sched = LullabySchedule.parse(data)
                    cur_vol, cur_timer = sched.volume, sched.timer_mode
                new_vol = args.volume if args.volume is not None else cur_vol
                timer_map = {'repeat': LULLABY_TIMER_REPEAT,
                             '30min':  LULLABY_TIMER_30MIN,
                             '60min':  LULLABY_TIMER_60MIN}
                new_timer = timer_map.get(args.timer, cur_timer) if args.timer else cur_timer
                transport.ioctl(*build_set_lullaby_vol_duration(new_vol, new_timer))
                t_name = {LULLABY_TIMER_REPEAT:'repeat',
                          LULLABY_TIMER_30MIN:'30min',
                          LULLABY_TIMER_60MIN:'60min'}.get(new_timer, '?')
                print(f"   ✅ Volume={new_vol}%  Timer={t_name}")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Play ─────────────────────────────────────────────────
        if args.play:
            result = find_song(args.play)
            if not result:
                print(f"\n❌ No song matching '{args.play}' — use --list-songs")
            else:
                uuid, name = result
                print(f"\n🎵 Playing: {name}...", flush=True)
                try:
                    transport.ioctl(*build_set_lullaby_play(uuid))
                    print(f"   ✅ Now playing: {name}")
                except Exception as e:
                    print(f"   ❌ Failed: {e}")

        # ── Stop ─────────────────────────────────────────────────
        if args.stop:
            print(f"\n⏹  Stopping lullaby...", flush=True)
            try:
                tc, data = transport.ioctl(*build_get_lullaby_vol_duration())
                uuid = ""
                if tc == IOTYPE_USER_GET_LULLABY_VOL_DURATION_RESP and len(data) >= 20:
                    lv = LullabyVolDuration.parse(data)
                    uuid = lv.current_song_uuid
                transport.ioctl(*build_set_lullaby_stop(uuid))
                print("   ✅ Stopped")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Sleep mode ───────────────────────────────────────────
        if args.sleep_mode:
            on = args.sleep_mode == 'on'
            print(f"\n😴 Sleep mode → {'ON' if on else 'OFF'}...", flush=True)
            try:
                transport.ioctl(*build_set_sleep_mode(on))
                print("   ✅ Done")
            except Exception as e:
                print(f"   ❌ Failed: {e}")

        # ── Night vision / status light / video flip / volumes (SET_HW_CONTROL) ──
        if args.night_vision:
            print(f"\n🌙 Night vision → {args.night_vision}...", flush=True)
            try:    transport.set_night_vision(args.night_vision); print("   ✅ Sent (firmware-managed; may not change)")
            except Exception as e: print(f"   ❌ Failed: {e}")
        if args.status_light:
            on = args.status_light == 'on'
            print(f"\n🔆 Status LED → {'ON' if on else 'OFF'}...", flush=True)
            try:    transport.set_status_light(on); print("   ✅ Sent (firmware-managed; may not change)")
            except Exception as e: print(f"   ❌ Failed: {e}")
        _flip = args.flip_screen or args.video_flip
        if _flip:
            on = _flip == 'on'
            print(f"\n🔄 Flip screen → {'ON' if on else 'OFF'}...", flush=True)
            try:    transport.set_video_flip(on); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")
        if args.mic_volume is not None:
            print(f"\n🎙  Mic level → {args.mic_volume}...", flush=True)
            try:    transport.set_mic_volume(args.mic_volume); print("   ✅ Sent (firmware-managed; may not change)")
            except Exception as e: print(f"   ❌ Failed: {e}")
        if args.speaker_volume is not None:
            print(f"\n📢 Speaker level → {args.speaker_volume}...", flush=True)
            try:    transport.set_speaker_volume(args.speaker_volume); print("   ✅ Sent (firmware-managed; may not change)")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Cry / cough detection ────────────────────────────────
        # sensitivity labels map INVERTED to the wire: low=3, medium=2, high=1.
        _SENS = {'low': 3, 'medium': 2, 'high': 1}
        if args.cry_detection or args.cry_sensitivity is not None:
            cur = transport.get_cry_detection()
            on  = (args.cry_detection == 'on') if args.cry_detection else cur.get('enabled', True)
            sens = _SENS[args.cry_sensitivity] if args.cry_sensitivity else cur.get('sensitivity', 2)
            slab = {1:'High',2:'Medium',3:'Low'}.get(sens, sens)
            print(f"\n👶 Cry detection → {'ON' if on else 'OFF'} sensitivity={slab}...", flush=True)
            try:    transport.set_cry_detection(enabled=on, sensitivity=sens); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")
        if args.cough_detection or args.cough_mode or args.cough_sensitivity:
            on = (args.cough_detection == 'on') if args.cough_detection else None
            in_crib = {'always': False, 'in-crib': True}.get(args.cough_mode) if args.cough_mode else None
            sens = _SENS[args.cough_sensitivity] if args.cough_sensitivity else None
            mode_txt = f" mode={args.cough_mode}" if args.cough_mode else ""
            print(f"\n🤧 Cough detection → {args.cough_detection or 'unchanged'}{mode_txt}...", flush=True)
            try:    transport.set_cough_detection(enabled=on, in_crib=in_crib, sensitivity=sens); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Sleep-safety mode (high-level: mutually-exclusive radio) ──
        # safety_alert=1,cover=0 → "Covered Face + Rollover"; cover=1,safety=0 →
        # "Covered Face Only"; both 0 → off.
        if args.sleep_alerts:
            sa, ca = {'covered-and-rollover': (1, 0),
                      'covered-only':         (0, 1),
                      'off':                  (0, 0)}[args.sleep_alerts]
            print(f"\n🛡  Sleep alerts → {args.sleep_alerts} (safety={sa} cover={ca})...", flush=True)
            try:    transport.set_sleep_safety_setting(safety_alert=sa, cover_alert=ca); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Sleep-safety alerts (low-level individual flags, read-modify-write) ──
        if args.safety_alert or args.cover_alert or args.baby_presence_alert:
            cur = transport.get_sleep_safety_setting()
            def _b(flag, key): return (flag == 'on') if flag else bool(cur.get(key))
            sa = _b(args.safety_alert, 'safety_alert')
            ca = _b(args.cover_alert, 'cover_alert')
            bp = _b(args.baby_presence_alert, 'baby_presence_alert')
            se = int(cur.get('sensitivity') or 0)
            print(f"\n🛡  Sleep-safety → safety={sa} cover={ca} baby_presence={bp}...", flush=True)
            try:    transport.set_sleep_safety(int(sa), int(ca), se, int(bp)); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Danger-zone alert on/off (toggles roi.enable, the app's switch path) ──
        if args.danger_zone_alert:
            on = args.danger_zone_alert == 'on'
            print(f"\n⛔ Danger-zone alert → {'ON' if on else 'OFF'}...", flush=True)
            try:    transport.set_danger_zone(enable=1 if on else 0); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Auto event-snapshot mode ─────────────────────────────
        if args.auto_capture:
            mode = {'off': 0, 'motion': 1, 'schedule': 2, 'both': 3}[args.auto_capture]
            print(f"\n📸 Auto-capture → {args.auto_capture} (mode {mode})...", flush=True)
            try:    transport.set_auto_capture(mode); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Lullaby schedule volume ──────────────────────────────
        if args.schedule_volume is not None:
            print(f"\n🔊 Lullaby schedule volume → {args.schedule_volume}...", flush=True)
            try:    transport.set_lullaby_schedule(volume=args.schedule_volume); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

        # ── Environment / comfort-range thresholds (read-modify-write) ──
        # --comfort-* are the friendly aliases of --temp-*/--humi-*.
        _t_lo = args.comfort_temp_low  if args.comfort_temp_low  is not None else args.temp_low
        _t_hi = args.comfort_temp_high if args.comfort_temp_high is not None else args.temp_high
        _h_lo = args.comfort_humi_low  if args.comfort_humi_low  is not None else args.humi_low
        _h_hi = args.comfort_humi_high if args.comfort_humi_high is not None else args.humi_high
        if any(v is not None for v in (args.temp_alert, _t_lo, _t_hi,
                                       args.humi_alert, _h_lo, _h_hi)):
            kw = {}
            if args.temp_alert: kw['temp_alert'] = 1 if args.temp_alert == 'on' else 0
            if args.humi_alert: kw['humi_alert'] = 1 if args.humi_alert == 'on' else 0
            if _t_lo is not None: kw['temp_low']  = _t_lo
            if _t_hi is not None: kw['temp_high'] = _t_hi
            if _h_lo is not None: kw['humi_low']  = _h_lo
            if _h_hi is not None: kw['humi_high'] = _h_hi
            print(f"\n🌡  Comfort range → {kw}...", flush=True)
            try:    transport.set_environment_alert(**kw); print("   ✅ Done")
            except Exception as e: print(f"   ❌ Failed: {e}")

    finally:
        transport.disconnect()
        print("\nDisconnected.")


if __name__ == '__main__':
    # Standalone process: allow the broadcast-redirect shim to re-exec us with
    # LD_PRELOAD (blocked when this module is imported by a host application).
    import os as _os
    _os.environ.setdefault('CUBOAI_ALLOW_REEXEC', '1')
    main()
