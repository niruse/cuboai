"""Tests for the history carry-forward path (DVR-poll flakiness fix).

The observed failure: the DVR pull fails on roughly every other poll, and each
failure produced a full unavailable gap on every history sensor (~50% dead air
on the dashboard timelines) — because the library's own last-good cache lived
on a session object that every poll rebuilt. The contract locked down here:

- `history_sensors_from_cache` re-serves the last pulled record with an age
  RECOMPUTED at call time and `stale=True` — never a frozen age.
- The age keeps growing, so the entity's 15-minute freshness gate still
  expires carried data: a transient failure is bridged, a dead camera is not.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TUTK = REPO / "custom_components" / "cuboai" / "tutk"


def _load_sensors_module(monkeypatch):
    """Import cuboai_sensors.py by file location.

    It uses the tutk scripts' FLAT import style (`import cuboai_messages`),
    so its sibling deps are stubbed — none of their attributes are touched by
    the code paths under test (no network pull happens from a cache hit).
    """
    stream_video = types.ModuleType("cuboai_stream_video")
    stream_video._env_float = lambda key, default: default
    for name, mod in (
        ("cuboai_messages", types.ModuleType("cuboai_messages")),
        ("cuboai_playback", types.ModuleType("cuboai_playback")),
        ("cuboai_stream_video", stream_video),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    spec = importlib.util.spec_from_file_location("cuboai_sensors_under_test", TUTK / "cuboai_sensors.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "cuboai_sensors_under_test", module)
    spec.loader.exec_module(module)
    return module


class _Record:
    """Shape-compatible stand-in for a MinuteRecord (ts / flags / temp / humidity)."""

    def __init__(self, ts_utc: dt.datetime, flags: dict, temp=22.5, humidity=48):
        self.ts = int(ts_utc.timestamp())
        self.flags = flags
        self.temp = temp
        self.humidity = humidity


def _cache_with_record(module, age_seconds: float) -> dict:
    ts = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=age_seconds)
    rec = _Record(ts, {"bp": 2, "na": 25, "mo": 0, "bw": 1, "be": 0, "pr": 0})
    return {module._HIST_CACHE_LATEST: rec}


def test_empty_cache_yields_none(monkeypatch):
    m = _load_sensors_module(monkeypatch)
    assert m.history_sensors_from_cache(None) is None
    assert m.history_sensors_from_cache({}) is None


def test_cached_record_is_reaged_and_marked_stale(monkeypatch):
    m = _load_sensors_module(monkeypatch)
    hist = m.history_sensors_from_cache(_cache_with_record(m, age_seconds=600))

    assert hist is not None
    assert hist.stale is True
    # The reading itself is served as a real value...
    assert hist.baby_present.available is True
    assert hist.baby_present.value == 2
    # ...with its age recomputed from the record's own timestamp, not frozen.
    assert 595 <= hist.baby_present.age_s <= 660
    assert hist.baby_present.stale is True


def test_carried_age_keeps_growing_past_the_freshness_cutoff(monkeypatch):
    # A record 20 minutes old must come back with age > 900s, so the entity's
    # MAX_AGE_S gate (15 min) expires it — carried data cannot outlive a real
    # outage and masquerade as live.
    m = _load_sensors_module(monkeypatch)
    hist = m.history_sensors_from_cache(_cache_with_record(m, age_seconds=1200))
    assert hist.baby_present.age_s > 900


def test_an_older_fallback_pull_never_beats_the_cached_record(monkeypatch):
    # A transient growing-hour failure makes the pull fall back to the tail of
    # a COMPLETED hour — a "successful" pull of a record older than the one
    # already cached (>15 min at :15+ past the hour), which expired the entity
    # for one cycle every few minutes. Newest record must win: served AND kept.
    m = _load_sensors_module(monkeypatch)
    now = dt.datetime.now(dt.UTC)
    fresh = _Record(now - dt.timedelta(seconds=90), {"bp": 1, "na": 20, "mo": 0, "bw": 1, "be": 0, "pr": 0})
    old_fallback = _Record(now - dt.timedelta(seconds=1300), {"bp": 2, "na": 30, "mo": 0, "bw": 1, "be": 0, "pr": 0})

    cache = {m._HIST_CACHE_LATEST: fresh}
    monkeypatch.setattr(m, "_pace_allows", lambda *a: True)
    monkeypatch.setattr(m, "_pull_latest_record", lambda *a, **k: (old_fallback, None))

    hist = m.get_history_sensors(object(), cache=cache)
    # Served: the newer cached record (marked stale — it wasn't from this pull)
    assert hist.baby_present.value == 1
    assert hist.baby_present.age_s < 900
    assert hist.stale is True
    # Kept: the cache still holds the newer record, not the old fallback.
    assert cache[m._HIST_CACHE_LATEST] is fresh


def test_a_newer_pull_still_wins_and_updates_the_cache(monkeypatch):
    m = _load_sensors_module(monkeypatch)
    now = dt.datetime.now(dt.UTC)
    older = _Record(now - dt.timedelta(seconds=600), {"bp": 2, "na": 25, "mo": 0, "bw": 1, "be": 0, "pr": 0})
    newer = _Record(now - dt.timedelta(seconds=30), {"bp": 1, "na": 20, "mo": 0, "bw": 1, "be": 0, "pr": 0})

    cache = {m._HIST_CACHE_LATEST: older}
    monkeypatch.setattr(m, "_pace_allows", lambda *a: True)
    monkeypatch.setattr(m, "_pull_latest_record", lambda *a, **k: (newer, None))

    hist = m.get_history_sensors(object(), cache=cache)
    assert hist.baby_present.value == 1
    assert hist.stale is False
    assert cache[m._HIST_CACHE_LATEST] is newer


def test_reaged_payload_flattens_with_the_grown_age(monkeypatch):
    # End-to-end through the coordinator's flattener: the payload the entities
    # read carries the recomputed age and the stale flag.
    m = _load_sensors_module(monkeypatch)

    src = (REPO / "custom_components" / "cuboai" / "coordinator.py").read_text(encoding="utf-8")
    start = src.index("_HISTORY_FIELDS")
    end = src.index("def _fetch_local_data")
    flat = types.ModuleType("hist_flat")
    exec(compile(src[start:end], "coordinator_extract", "exec"), flat.__dict__)

    payload = flat._history_payload(m.history_sensors_from_cache(_cache_with_record(m, age_seconds=300)))
    assert payload["stale"] is True
    assert payload["baby_present"]["available"] is True
    assert 295 <= payload["baby_present"]["age_s"] <= 360
    assert payload["baby_present"]["stale"] is True
