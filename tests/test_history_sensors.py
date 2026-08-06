"""Tests for the camera DVR history sensors.

The contract these lock down is not "does a value appear" but "can a stale
value ever be presented as current". This is a baby monitor: a `baby_present`
reading from 40 minutes ago rendered as now is the failure mode the upstream
sensor API is explicitly built to make impossible, so the age has to survive
the trip from the library, through the coordinator payload, to the entity.
"""
from __future__ import annotations

import datetime as dt
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
TUTK = REPO / "custom_components" / "cuboai" / "tutk"


# ── the payload flattener (coordinator side) ──────────────────────────────


def _load_history_payload():
    """Import _history_payload from coordinator.py without dragging in HA.

    coordinator.py imports homeassistant at module level, so the function is
    extracted and exec'd on its own — the same scaffolding pattern the other
    issue-scoped tests here use.
    """
    src = (REPO / "custom_components" / "cuboai" / "coordinator.py").read_text(encoding="utf-8")
    start = src.index("_HISTORY_FIELDS")
    end = src.index("def _fetch_local_data")
    module = types.ModuleType("hist")
    exec(compile(src[start:end], "coordinator_extract", "exec"), module.__dict__)
    return module._history_payload


class _Reading:
    def __init__(self, value, age_s, available=True, stale=False, note=None, unit=None, ts=None):
        self.value = value
        self.age_s = age_s
        self.available = available
        self.stale = stale
        self.note = note
        self.unit = unit
        self.ts_utc = ts


class _Hist:
    def __init__(self, **fields):
        self.stale = fields.pop("stale", False)
        self.fetched_at = dt.datetime(2026, 8, 6, 12, 0, 0, tzinfo=dt.timezone.utc)
        for name in ("baby_present", "noise", "motion", "wellbeing",
                     "baby_event", "privacy", "temperature_c", "humidity_pct"):
            setattr(self, name, fields.get(name, _Reading(None, None, available=False)))


def test_payload_keeps_age_and_staleness_with_every_value():
    payload = _load_history_payload()(
        _Hist(baby_present=_Reading(1, 62.0, note="in crib",
                                    ts=dt.datetime(2026, 8, 6, 11, 59, tzinfo=dt.timezone.utc)))
    )
    bp = payload["baby_present"]
    # The value must never travel alone.
    assert bp["value"] == 1
    assert bp["age_s"] == 62.0
    assert bp["available"] is True
    assert bp["stale"] is False
    assert bp["note"] == "in crib"
    assert bp["ts_utc"].startswith("2026-08-06T11:59")


def test_payload_marks_a_stale_pull():
    payload = _load_history_payload()(_Hist(stale=True))
    assert payload["stale"] is True


# ── the entity (staleness gating) ─────────────────────────────────────────


class _FakeCoordinator:
    def __init__(self, history):
        self.data = {"cameras": {"DEV1": {"local": {"history": history}}}}
        self.history_sensors_enabled = True


def _make_sensor(history, monkeypatch):
    """Build CuboHistorySensor with HA's CoordinatorEntity stubbed out."""
    import importlib

    ha = types.ModuleType("homeassistant")
    comp = types.ModuleType("homeassistant.components")
    sens = types.ModuleType("homeassistant.components.sensor")

    class _SensorEntity:
        pass

    sens.SensorEntity = _SensorEntity
    sens.SensorDeviceClass = types.SimpleNamespace(
        TEMPERATURE="temperature", HUMIDITY="humidity", TIMESTAMP="timestamp", ENUM="enum"
    )
    const = types.ModuleType("homeassistant.const")
    const.EntityCategory = types.SimpleNamespace(DIAGNOSTIC="diagnostic", CONFIG="config")
    helpers = types.ModuleType("homeassistant.helpers")
    upd = types.ModuleType("homeassistant.helpers.update_coordinator")

    class _CoordinatorEntity:
        def __init__(self, coordinator):
            self.coordinator = coordinator

    upd.CoordinatorEntity = _CoordinatorEntity

    for name, mod in (
        ("homeassistant", ha), ("homeassistant.components", comp),
        ("homeassistant.components.sensor", sens), ("homeassistant.const", const),
        ("homeassistant.helpers", helpers),
        ("homeassistant.helpers.update_coordinator", upd),
    ):
        monkeypatch.setitem(sys.modules, name, mod)

    pkg = types.ModuleType("cuboai_pkg")
    pkg.__path__ = [str(REPO / "custom_components" / "cuboai")]
    monkeypatch.setitem(sys.modules, "cuboai_pkg", pkg)
    constmod = types.ModuleType("cuboai_pkg.const")
    constmod.DOMAIN = "cuboai"
    monkeypatch.setitem(sys.modules, "cuboai_pkg.const", constmod)

    spec = importlib.util.spec_from_file_location(
        "cuboai_pkg.sensor", REPO / "custom_components" / "cuboai" / "sensor.py"
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "cuboai_pkg.sensor", module)
    spec.loader.exec_module(module)
    return module.CuboHistorySensor(
        _FakeCoordinator(history), "DEV1", "Mia", "baby_present", "Baby Present", None
    )


def test_fresh_reading_is_available_and_uses_the_note(monkeypatch):
    s = _make_sensor(
        {"baby_present": {"value": 1, "age_s": 65.0, "available": True,
                          "stale": False, "note": "in crib", "ts_utc": None}},
        monkeypatch,
    )
    assert s.available is True
    assert s.native_value == "in crib"
    assert s.extra_state_attributes["age_seconds"] == 65.0


def test_a_forty_minute_old_reading_is_not_shown_as_current(monkeypatch):
    """The core guarantee: too old means unavailable, not a stale-looking value."""
    s = _make_sensor(
        {"baby_present": {"value": 1, "age_s": 40 * 60, "available": True,
                          "stale": True, "note": "in crib", "ts_utc": None}},
        monkeypatch,
    )
    assert s.available is False


def test_unknown_age_is_treated_as_untrustworthy(monkeypatch):
    """An age we cannot read is not the same as a fresh one."""
    s = _make_sensor(
        {"baby_present": {"value": 1, "age_s": None, "available": True,
                          "stale": False, "note": "in crib", "ts_utc": None}},
        monkeypatch,
    )
    assert s.available is False


def test_never_obtained_reading_is_unavailable(monkeypatch):
    s = _make_sensor({"baby_present": {"value": None, "age_s": None, "available": False,
                                       "stale": False, "note": None, "ts_utc": None}}, monkeypatch)
    assert s.available is False


def test_missing_history_block_does_not_raise(monkeypatch):
    s = _make_sensor({}, monkeypatch)
    assert s.available is False
    assert s.native_value is None


def test_opaque_fields_are_not_exposed_as_entities(monkeypatch):
    """`wellbeing`/`baby_event` are documented upstream as an opaque UI-tint bit
    and an effectively-never-firing flag — exposing them would invent meaning."""
    import importlib

    s = _make_sensor({}, monkeypatch)
    module = sys.modules["cuboai_pkg.sensor"]
    fields = {f for f, _, _ in module.HISTORY_SENSORS}
    assert fields == {"baby_present", "noise", "motion", "privacy"}
