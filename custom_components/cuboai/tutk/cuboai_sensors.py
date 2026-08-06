#!/usr/bin/env python3
"""
cuboai_sensors.py — the library's public sensor API.

Two calls, meant for an HA (or any) integration to consume directly instead of scraping CLI
output:

    get_live_sensors(sess)     -> LiveSensors      instant GET reads, no meaningful lag
    get_history_sensors(sess)  -> HistorySensors | HistoryWindow   s_log DVR history, ~1 min lag

Every value either function returns is wrapped in a `Reading`: value + source ('live'/'history')
+ measurement timestamp + age-at-call-time + availability + unit. This is deliberate — a history
reading must be structurally impossible to render as if it were live. A HA integrator mapping
`.value` onto an entity still sees `.age_s` sitting right there on the same object; ignoring it
is an active choice, not something a `# NOTE: this is lagged` docstring comment can enforce.

Graceful degradation: any single field or pull that fails falls back to its own last-known-good
value (age grows from ITS true timestamp, `stale=True`) rather than raising or going silently
None/zero. `get_history_sensors` also paces its own camera RDT pulls internally (the
CUBOAI_LIST_PACE_S / conn_id-release rhythm) — callers never need to know about that.

Read-only / firmware-owned fields (do not build a setter or a HA switch for these — see
SENSORS.md): `LiveSensors.status_light`, `LiveSensors.baby_presence_alert_configured`. Both are
values the camera happily *reports*, but SET_STATUS_LIGHT / the baby-presence-alert flag inside
SET_SLEEP_SAFETY are accepted (result=0) and then silently not applied by this firmware.

Never infer on/off state from a SET response — SET_NIGHT_LIGHT_ON_OFF_RESP is 12 zero bytes
({id, result, reserved}, no state echo; wire-proven 2026-07-25, F14). The only correct pattern is
what this module does: read the state back via a GET.
"""
from __future__ import annotations

import datetime as _dt
import time as _time
from dataclasses import dataclass
from typing import Any, Optional

import cuboai_messages as _msg
import cuboai_playback as _pb
from cuboai_stream_video import _env_float

_PACE_ENV = "CUBOAI_LIST_PACE_S"
_DEFAULT_MIN_PULL_INTERVAL_S = 5.0   # floor between get_history_sensors() network pulls per mode

_HIST_BP_LABELS = {1: "in crib", 2: "not in crib"}
_MOTION_LABELS = {0: "still", 1: "moving"}


def _motion_label(mo) -> Optional[str]:
    if mo is None:
        return None
    return _MOTION_LABELS.get(mo, f"strong ({mo})")


def _privacy_label(pr) -> Optional[str]:
    if pr is None:
        return None
    return "sleep/privacy mode" if pr == 1 else "recording"


def _wellbeing_note(bw) -> Optional[str]:
    if bw is None:
        return None
    base = {0: "out of crib (caregiver?)", 1: "flagged active"}.get(bw, "not flagged")
    return f"{base} — opaque firmware activity bit; app only uses it to tint the timeline tick"


def _now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


# ── Reading: the one metadata-carrying value type both public calls return ───────────────────

@dataclass(frozen=True)
class Reading:
    """One value plus enough metadata that it can't be mistaken for fresher than it is.

    age_s is computed once, at the moment the enclosing get_live_sensors()/get_history_sensors()
    call returns it — re-derive it yourself if you hold a Reading and consult it later.
    """
    value: Any
    source: str                         # 'live' | 'history'
    ts_utc: Optional[_dt.datetime]      # when the value was true (device or fetch time); None if never obtained
    age_s: Optional[float]              # seconds between ts_utc and call time; None if never obtained
    available: bool                     # do we have a real value at all (fresh or cached-stale)
    unit: Optional[str] = None
    stale: bool = False                 # True: a fresh pull failed/was paced-out; this is a cached last-good value
    note: Optional[str] = None

    @property
    def ts_local(self) -> Optional[_dt.datetime]:
        return self.ts_utc.astimezone() if self.ts_utc is not None else None


# ── per-transport cache (graceful degradation + pacing state) ────────────────────────────────
# Attached to the session/transport object itself (it's the natural lifetime scope: one cache
# per connection). Callers that pass an explicit `cache={}` get an isolated, inspectable one
# instead — this is how the offline tests exercise the degradation path without a real camera.

def _cache_of(sess, cache: Optional[dict]) -> dict:
    if cache is not None:
        return cache
    c = getattr(sess, "_cuboai_sensor_cache", None)
    if c is None:
        c = {}
        try:
            setattr(sess, "_cuboai_sensor_cache", c)
        except Exception:
            pass   # duck-typed/frozen transport: still correct, just not persisted across calls
    return c


def _live_value(cache, key, value, *, unit=None, now=None, note=None) -> Reading:
    """A live field: fresh if `value is not None` (updates the cache); else fall back to the
    last-known-good value for `key` (age grows from its real timestamp, stale=True); else
    unavailable. Never raises, never fabricates a value."""
    now = now or _now()
    if value is not None:
        cache[key] = (value, now)
        return Reading(value=value, source="live", ts_utc=now, age_s=0.0, available=True,
                        unit=unit, note=note)
    hit = cache.get(key)
    if hit is None:
        return Reading(value=None, source="live", ts_utc=None, age_s=None, available=False,
                        unit=unit, note=note or "no successful reading yet")
    cval, cts = hit
    age = max((now - cts).total_seconds(), 0.0)
    return Reading(value=cval, source="live", ts_utc=cts, age_s=age, available=True, unit=unit,
                    stale=True, note=note or "last-good value; most recent read failed")


def _pace_allows(cache, gate_key, min_interval) -> bool:
    """Rate-gate a network pull: True (and marks the gate) if `min_interval` seconds have
    elapsed since the last pull attempt under this gate_key; False if it's too soon (caller
    should fall back to cached data with a grown age instead of hitting the camera again)."""
    last = cache.get(gate_key)
    now_m = _time.monotonic()
    if last is not None and (now_m - last) < min_interval:
        return False
    cache[gate_key] = now_m
    return True


# ── LIVE sensors ───────────────────────────────────────────────────────────────────────────────

def _sweep_gets(sess) -> dict:
    """Read every GET method into {name: parsed_dict}; failed/empty reads -> None. The single
    wire-reading sweep shared by get_live_sensors() and the CLI's raw-response debug views —
    there is exactly one place that iterates cuboai_messages.GET_METHODS."""
    out = {}
    for name, (builder, resp_type, parser) in _msg.GET_METHODS.items():
        try:
            tc, data = sess.ioctl(*builder())
            out[name] = parser(data) if data else None
        except Exception:
            out[name] = None
    return out


@dataclass(frozen=True)
class LiveSensors:
    temperature_c: Reading
    humidity_pct: Reading
    wifi: Reading                              # dict: quality_pct, ssid, ip, mac, rssi_dbm, noise_dbm, channel, frequency_mhz, radio_quality
    sleep_mode: Reading                        # bool — privacy/sleep mode (feed suspended when True)
    night_light: Reading                       # dict: on (bool), brightness (0-100)
    status_light: Reading                      # bool — READ-ONLY telemetry; SET is a no-op on this firmware
    firmware: Reading                          # dict: version, update_available, latest_version
    detection_config: Reading                  # dict: cry{...}, cough{...}, sleep_safety_enabled
    baby_presence_alert_configured: Reading    # bool — READ-ONLY; accepted but not enforced by firmware
    sleep_safety_status: Reading               # dict: status, active, remaining_time, duration
    feature_bitmap: Reading                    # tuple of per-feature flags (ordering not reversed)
    fetched_at: _dt.datetime


def get_live_sensors(sess, *, cache: Optional[dict] = None) -> LiveSensors:
    """Instant GET reads: temperature/humidity/WiFi/lighting/firmware/detection-config/
    sleep-safety-status/feature-bitmap. No RDT, no DVR pull — age_s reflects only this call's
    own round trip, not a meaningful lag. A single GET failing (camera busy, one dropped ioctl)
    degrades that one field to its last-known-good value (grown age, stale=True) rather than
    going silently None — never an exception.
    """
    d = _sweep_gets(sess)
    now = _now()
    cache = _cache_of(sess, cache)

    hw = d.get('get_hw_control') or {}
    ls = d.get('get_light_style') or {}
    wf = d.get('get_wifi') or {}
    nl = d.get('get_night_light') or {}
    fw_check = d.get('check_firmware_update') or {}
    cry = d.get('get_cry_detection') or {}
    cough = d.get('get_cough_detection') or {}
    ss_setting = d.get('get_sleep_safety_setting') or {}
    ss_live = d.get('get_sleep_safety') or {}
    feat = d.get('get_feature_support') or {}
    sm = d.get('get_sleep_mode') or {}

    wifi_val = None
    if hw or wf:
        wifi_val = {
            'quality_pct':    hw.get('wifi_strength'),
            'ssid':           hw.get('ssid') or wf.get('ssid'),
            'ip':             wf.get('ip'),
            'mac':            wf.get('mac'),
            'rssi_dbm':       wf.get('strength'),
            'noise_dbm':      wf.get('noise'),
            'channel':        wf.get('channel'),
            'frequency_mhz':  wf.get('frequency'),
            'radio_quality':  wf.get('quality'),
        }

    nl_on = hw.get('night_light_on')
    if nl_on is None:
        nl_on = nl.get('on')
    night_light_val = None
    if nl_on is not None or ls.get('brightness') is not None:
        night_light_val = {'on': nl_on, 'brightness': ls.get('brightness')}

    fw_ver = hw.get('firmware') or fw_check.get('current_version')
    firmware_val = None
    if fw_ver is not None or fw_check:
        firmware_val = {
            'version':           fw_ver,
            'update_available':  fw_check.get('update_available'),
            'latest_version':    fw_check.get('latest_version'),
        }

    detection_val = None
    if cry or cough or ss_setting:
        detection_val = {
            'cry': {
                'enabled':            cry.get('enabled'),
                'ai_enabled':         cry.get('ai_enabled'),
                'sensitivity':        cry.get('sensitivity'),
                'sensitivity_label':  cry.get('sensitivity_label'),
            },
            'cough': {
                'enabled':            cough.get('enabled'),
                'mode':               cough.get('mode'),
                'sensitivity':        cough.get('sensitivity'),
                'sensitivity_label':  cough.get('sensitivity_label'),
            },
            'sleep_safety_enabled': (bool(ss_setting.get('safety_alert'))
                                      if ss_setting.get('safety_alert') is not None else None),
        }

    ss_status_val = None
    if ss_live:
        ss_status_val = {
            'status':          ss_live.get('status'),
            'active':          ss_live.get('active'),
            'remaining_time':  ss_live.get('remaining_time'),
            'duration':        ss_live.get('duration'),
        }

    feature_val = tuple(feat['flags']) if feat.get('flags') is not None else None

    return LiveSensors(
        temperature_c=_live_value(cache, 'temperature_c', hw.get('temp_c'), unit='°C', now=now),
        humidity_pct=_live_value(cache, 'humidity_pct', hw.get('humidity_pct'), unit='%', now=now),
        wifi=_live_value(cache, 'wifi', wifi_val, now=now),
        sleep_mode=_live_value(cache, 'sleep_mode', sm.get('enabled'), now=now),
        night_light=_live_value(cache, 'night_light', night_light_val, now=now),
        status_light=_live_value(
            cache, 'status_light', hw.get('status_light_on'), now=now,
            note="read-only: SET_STATUS_LIGHT is accepted but ignored by this firmware"),
        firmware=_live_value(cache, 'firmware', firmware_val, now=now),
        detection_config=_live_value(cache, 'detection_config', detection_val, now=now),
        baby_presence_alert_configured=_live_value(
            cache, 'baby_presence_alert_configured', ss_setting.get('baby_presence_alert'), now=now,
            note="read-only: accepted (result=0) but silently not applied by this firmware — "
                 "do not expose as a switch (F-verified 2026-07-25)"),
        sleep_safety_status=_live_value(cache, 'sleep_safety_status', ss_status_val, now=now),
        feature_bitmap=_live_value(cache, 'feature_bitmap', feature_val, now=now),
        fetched_at=now,
    )


# ── HISTORY (s_log DVR) sensors ────────────────────────────────────────────────────────────────
# s_log is a LOCAL per-minute detection/presence history pulled from the on-camera DVR manifest
# over RDT (cuboai_playback.pull_manifest). bp is app/footage-confirmed ground truth; na/mo/bw/
# be/pr are footage-cross-checked. ni/nm/se/ve are real firmware fields the official app never
# surfaces and are intentionally NOT exposed here (see SENSORS.md) — don't add them as entities.

_HIST_CACHE_LATEST = "history_latest_record"
_HIST_CACHE_LATEST_GATE = "history_latest_pull_gate"
_HIST_CACHE_WINDOW = "history_window_records"
_HIST_CACHE_WINDOW_GATE = "history_window_pull_gate"


def _pull_latest_record(sess, hours_back, timeout):
    """Freshest retrievable s_log record: try the growing hour first (serves w/ ~1 min lag),
    then fall back through completed hours on failure. Returns (MinuteRecord, hour_dt) or
    (None, None). Never raises — retries=0: a starved conn_id is best left to the NEXT poll
    cycle, not hammered now."""
    now = _now()
    for h in range(hours_back):
        dt = now - _dt.timedelta(hours=h)
        try:
            recs, _resp = _pb.pull_manifest(sess, dt, timeout=timeout, retries=0)
        except Exception:
            recs = None
        if recs:
            return recs[-1], dt
    return None, None


def _pull_window_records(sess, hours_back, timeout):
    """Merge up to `hours_back` hours of manifest into a minute-deduped record list (oldest
    first). Paced at CUBOAI_LIST_PACE_S between hours (native conn_id-release parity). Never
    raises — a hard failure per hour just contributes nothing to the merge."""
    now = _now()
    pace = _env_float(_PACE_ENV, 0.5)
    merged = {}
    for h in range(max(int(hours_back), 1)):
        if h:
            _time.sleep(pace)
        dt = now - _dt.timedelta(hours=h)
        try:
            recs, _resp = _pb.pull_manifest(sess, dt, timeout=timeout, retries=0)
        except Exception:
            recs = None
        for r in (recs or []):
            merged[int(r.ts) - (int(r.ts) % 60)] = r
    return [merged[k] for k in sorted(merged)]


@dataclass(frozen=True)
class HistorySensors:
    """The single freshest retrievable s_log reading (the HA-sensor case). Every field's ts_utc
    is the DEVICE's own per-minute timestamp (not the fetch time) — age_s is the true staleness,
    always >= 0 and typically 30-90s. `stale=True` means this pull failed/was paced-out and every
    field below is a cached last-good value with a grown age (never a fabricated live-looking
    value, never an exception)."""
    baby_present: Reading    # bp: label 'in crib'/'not in crib' — app/footage-confirmed ground truth
    noise: Reading           # na: 0-100 per-minute average; >=60 elevated
    motion: Reading          # mo: 0 still / 1 moving / else 'strong (N)' (footage: 'strong' never observed)
    wellbeing: Reading       # bw: opaque firmware activity bit; app's only use is a UI tint, not a real signal
    baby_event: Reading      # be: baby-event flag (rare; unfired across 72h in the reference capture)
    privacy: Reading         # pr: 1 = sleep/privacy mode active, else recording
    temperature_c: Reading
    humidity_pct: Reading
    fetched_at: _dt.datetime
    stale: bool


def _wrap_history_reading(value, ts, age, stale, *, unit=None, note=None) -> Reading:
    return Reading(value=value, source='history', ts_utc=ts, age_s=age, available=True,
                    unit=unit, stale=stale, note=note)


def _history_sensors_from_fields(*, bp, na, mo, bw, be, pr, temp, hum, ts, age, stale, fetched_at):
    """The one place that maps raw s_log values to labelled Readings — shared by the record path
    (a fresh/cached pull) and the point path (deriving a HistorySensors from an already-fetched
    HistoryWindow point, no extra network round trip)."""
    return HistorySensors(
        baby_present=_wrap_history_reading(bp, ts, age, stale, note=_HIST_BP_LABELS.get(bp)),
        noise=_wrap_history_reading(na, ts, age, stale, unit='0-100',
                                     note='elevated (>=60)' if (na is not None and na >= 60) else None),
        motion=_wrap_history_reading(mo, ts, age, stale, note=_motion_label(mo)),
        wellbeing=_wrap_history_reading(bw, ts, age, stale, note=_wellbeing_note(bw)),
        baby_event=_wrap_history_reading(be, ts, age, stale),
        privacy=_wrap_history_reading(pr, ts, age, stale, note=_privacy_label(pr)),
        temperature_c=_wrap_history_reading(temp, ts, age, stale, unit='°C'),
        humidity_pct=_wrap_history_reading(hum, ts, age, stale, unit='%'),
        fetched_at=fetched_at, stale=stale,
    )


def _history_sensors_from_record(rec, now, *, stale) -> HistorySensors:
    ts = _dt.datetime.fromtimestamp(int(rec.ts), _dt.timezone.utc)
    age = max((now - ts).total_seconds(), 0.0)
    f = rec.flags or {}
    return _history_sensors_from_fields(
        bp=f.get('bp'), na=f.get('na'), mo=f.get('mo'), bw=f.get('bw'), be=f.get('be'),
        pr=f.get('pr'), temp=rec.temp, hum=rec.humidity, ts=ts, age=age, stale=stale, fetched_at=now)


def history_sensors_from_point(point: "HistoryPoint", *, stale: bool = False) -> HistorySensors:
    """Adapt one already-fetched HistoryWindow point into a Reading-wrapped HistorySensors,
    without a second network pull. Handy for a caller that already pulled a window (e.g. for a
    chart) and wants the same 'latest reading' shape for its newest point instead of triggering
    a second, redundant camera pull."""
    return _history_sensors_from_fields(
        bp=point.baby_present, na=point.noise, mo=point.motion, bw=point.wellbeing,
        be=point.baby_event, pr=point.privacy, temp=point.temperature_c, hum=point.humidity_pct,
        ts=point.ts_utc, age=point.age_s, stale=stale, fetched_at=_now())


def _history_sensors_unavailable(now) -> HistorySensors:
    def U():
        return Reading(value=None, source='history', ts_utc=None, age_s=None, available=False,
                        note='no manifest retrievable yet')
    return HistorySensors(baby_present=U(), noise=U(), motion=U(), wellbeing=U(), baby_event=U(),
                           privacy=U(), temperature_c=U(), humidity_pct=U(), fetched_at=now, stale=True)


@dataclass(frozen=True)
class HistoryPoint:
    """One minute of s_log — bare values (the enclosing HistoryWindow carries the collection-
    level staleness/availability; wrapping every field of every point in a Reading would be
    pure overhead for a charting window). ni/nm/se/ve are intentionally NOT included — see
    module docstring / SENSORS.md."""
    ts_utc: _dt.datetime
    age_s: float
    baby_present: Optional[int]
    noise: Optional[int]
    motion: Optional[int]
    wellbeing: Optional[int]
    baby_event: Optional[int]
    privacy: Optional[int]
    temperature_c: Optional[float]
    humidity_pct: Optional[float]


def _record_to_point(rec, now) -> HistoryPoint:
    ts = _dt.datetime.fromtimestamp(int(rec.ts), _dt.timezone.utc)
    age = max((now - ts).total_seconds(), 0.0)
    f = rec.flags or {}
    return HistoryPoint(
        ts_utc=ts, age_s=age,
        baby_present=f.get('bp'), noise=f.get('na'), motion=f.get('mo'),
        wellbeing=f.get('bw'), baby_event=f.get('be'), privacy=f.get('pr'),
        temperature_c=rec.temp, humidity_pct=rec.humidity,
    )


@dataclass(frozen=True)
class HistoryWindow:
    """A charting/statistics window of history points, merged + minute-deduped across up to
    `hours_requested` hours. `available`/`stale` describe the PULL, not any one point — a failed
    pull returns the last-good window (if any) with every point's age_s recomputed against
    `fetched_at`, `stale=True` — never an exception, never a silently-empty window presented as
    fresh."""
    points: list   # list[HistoryPoint], oldest first
    start_utc: Optional[_dt.datetime]
    end_utc: Optional[_dt.datetime]
    fetched_at: _dt.datetime
    available: bool
    stale: bool
    hours_requested: int
    note: Optional[str] = None


def _history_window_from_records(recs, now, hours_back, *, stale) -> HistoryWindow:
    points = [_record_to_point(r, now) for r in recs]
    return HistoryWindow(
        points=points,
        start_utc=points[0].ts_utc if points else None,
        end_utc=points[-1].ts_utc if points else None,
        fetched_at=now, available=bool(points), stale=stale, hours_requested=hours_back,
    )


def get_history_sensors(sess, *, hours_back=3, window=False, window_hours=1, timeout=10,
                         min_pull_interval_s=_DEFAULT_MIN_PULL_INTERVAL_S,
                         cache: Optional[dict] = None):
    """The s_log DVR history — ~1 min lag (the growing hour serves after the fact, over RDT).

    window=False (default): the single freshest retrievable per-minute reading -> HistorySensors.
        Every field is Reading-wrapped (source='history') so staleness stays structural; searches
        back up to `hours_back` completed hours if the growing hour's pull fails.
    window=True: a merged, minute-deduped window across `window_hours` -> HistoryWindow, for
        charting/statistics (mirrors cuboai_validate's --history-chart data).

    Pacing + graceful degradation live HERE, not in the caller: repeated calls inside
    `min_pull_interval_s` reuse the last pull (age just grows, no extra camera load); a failed
    pull returns the last-good reading/window with a grown age and `stale=True` — never an
    exception, never a silent zero. The caller never needs to know about CUBOAI_LIST_PACE_S,
    conn_id release timing, or the growing-hour behaviour.
    """
    cache = _cache_of(sess, cache)
    now = _now()

    if window:
        if _pace_allows(cache, _HIST_CACHE_WINDOW_GATE, min_pull_interval_s):
            recs = _pull_window_records(sess, window_hours, timeout)
            if recs:
                cache[_HIST_CACHE_WINDOW] = (recs, window_hours)
                return _history_window_from_records(recs, now, window_hours, stale=False)
        cached = cache.get(_HIST_CACHE_WINDOW)
        if cached is None:
            return HistoryWindow(points=[], start_utc=None, end_utc=None, fetched_at=now,
                                  available=False, stale=True, hours_requested=window_hours,
                                  note='no manifest retrievable yet')
        recs, req_hours = cached
        return _history_window_from_records(recs, now, req_hours, stale=True)

    if _pace_allows(cache, _HIST_CACHE_LATEST_GATE, min_pull_interval_s):
        rec, _hr = _pull_latest_record(sess, hours_back, timeout)
        if rec is not None:
            cache[_HIST_CACHE_LATEST] = rec
            return _history_sensors_from_record(rec, now, stale=False)
    cached = cache.get(_HIST_CACHE_LATEST)
    if cached is None:
        return _history_sensors_unavailable(now)
    return _history_sensors_from_record(cached, now, stale=True)
