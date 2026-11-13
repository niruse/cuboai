"""Newest-record-wins must not pin the history cache when the camera clock steps back.

Newest-record-wins (v2.6.2) keeps the cached record whenever a pulled one is older, which is
right for a completed-hour fallback. But if the camera clock steps BACK (NTP fix after a reboot,
DST), every new record is "older" than the cached one, so the cache stayed pinned for the whole
step (2 h reproduced) with age clamped to 0, and no freshness cutoff could expire it.

The difference is progress: a fallback returns the same tail record on every retry, while a
stepped clock keeps advancing at clock rate. An older record that has advanced by no more than
the time elapsed between pulls (+2 min slack) now replaces the cache.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TUTK = REPO / "custom_components" / "cuboai" / "tutk"
T = 1_790_000_000


def _load(monkeypatch):
    stream_video = types.ModuleType("cuboai_stream_video")
    stream_video._env_float = lambda key, default: default
    for name, mod in (
        ("cuboai_messages", types.ModuleType("cuboai_messages")),
        ("cuboai_playback", types.ModuleType("cuboai_playback")),
        ("cuboai_stream_video", stream_video),
    ):
        monkeypatch.setitem(sys.modules, name, mod)
    spec = importlib.util.spec_from_file_location("cuboai_sensors_clock_step", TUTK / "cuboai_sensors.py")
    m = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "cuboai_sensors_clock_step", m)
    spec.loader.exec_module(m)
    return m


class _Rig:
    def __init__(self, m, monkeypatch):
        self.m, self.clock, self.queue, self.cache = m, [T], [], {}
        monkeypatch.setattr(m, "_now", lambda: dt.datetime.fromtimestamp(self.clock[0], dt.UTC))
        monkeypatch.setattr(m, "_pace_allows", lambda *a: True)
        monkeypatch.setattr(m, "_pull_latest_record", lambda *a, **k: (self.queue.pop(0), None))

    def pull(self, at, ts, bp):
        self.clock[0] = at
        self.queue.append(types.SimpleNamespace(ts=ts, flags={"bp": bp, "na": 10}, temp=21.0, humidity=40.0))
        return self.m.get_history_sensors(object(), cache=self.cache)

    def cached_ts(self):
        return int(self.cache[self.m._HIST_CACHE_LATEST].ts)


def test_an_advancing_older_record_replaces_a_pinned_cache(monkeypatch):
    r = _Rig(_load(monkeypatch), monkeypatch)
    r.pull(T, T + 7200, 1)  # one record stamped 2 h ahead
    r.pull(T + 60, T + 30, 0)  # clock correct again: first "older" record
    h = r.pull(T + 120, T + 90, 0)  # ...and it advances: the camera moved on
    assert int(h.baby_present.ts_utc.timestamp()) == T + 90
    assert h.stale is False
    assert r.cached_ts() == T + 90


def test_a_repeated_fallback_still_serves_the_cached_record(monkeypatch):
    r = _Rig(_load(monkeypatch), monkeypatch)
    r.pull(T, T, 1)
    for k in range(4):  # the same completed-hour tail, retried
        h = r.pull(T + 65 * (k + 1), T - 1200, 0)
    assert int(h.baby_present.ts_utc.timestamp()) == T and h.stale is True
    assert r.cached_ts() == T


def test_a_fallback_hopping_between_hours_is_not_progress(monkeypatch):
    r = _Rig(_load(monkeypatch), monkeypatch)
    r.pull(T, T, 1)
    r.pull(T + 65, T - 5000, 0)  # H-2 tail
    h = r.pull(T + 130, T - 1400, 0)  # H-1 tail: ~3600 s jump in 65 s
    assert int(h.baby_present.ts_utc.timestamp()) == T and h.stale is True
