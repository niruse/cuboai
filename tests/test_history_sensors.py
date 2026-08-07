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
        self.fetched_at = dt.datetime(2026, 8, 6, 12, 0, 0, tzinfo=dt.UTC)
        for name in (
            "baby_present",
            "noise",
            "motion",
            "wellbeing",
            "baby_event",
            "privacy",
            "temperature_c",
            "humidity_pct",
        ):
            setattr(self, name, fields.get(name, _Reading(None, None, available=False)))


def test_payload_keeps_age_and_staleness_with_every_value():
    payload = _load_history_payload()(
        _Hist(baby_present=_Reading(1, 62.0, note="in crib", ts=dt.datetime(2026, 8, 6, 11, 59, tzinfo=dt.UTC)))
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


def _make_sensor(history, monkeypatch, field="baby_present", labelled=True):
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
        ("homeassistant", ha),
        ("homeassistant.components", comp),
        ("homeassistant.components.sensor", sens),
        ("homeassistant.const", const),
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
    return module.CuboHistorySensor(_FakeCoordinator(history), "DEV1", "Mia", field, "Baby Present", None, labelled)


def test_fresh_reading_is_available_and_uses_the_note(monkeypatch):
    s = _make_sensor(
        {
            "baby_present": {
                "value": 1,
                "age_s": 65.0,
                "available": True,
                "stale": False,
                "note": "in crib",
                "ts_utc": None,
            }
        },
        monkeypatch,
    )
    assert s.available is True
    assert s.native_value == "in crib"
    assert s.extra_state_attributes["age_seconds"] == 65.0


def test_a_forty_minute_old_reading_is_not_shown_as_current(monkeypatch):
    """The core guarantee: too old means unavailable, not a stale-looking value."""
    s = _make_sensor(
        {
            "baby_present": {
                "value": 1,
                "age_s": 40 * 60,
                "available": True,
                "stale": True,
                "note": "in crib",
                "ts_utc": None,
            }
        },
        monkeypatch,
    )
    assert s.available is False


def test_unknown_age_is_treated_as_untrustworthy(monkeypatch):
    """An age we cannot read is not the same as a fresh one."""
    s = _make_sensor(
        {
            "baby_present": {
                "value": 1,
                "age_s": None,
                "available": True,
                "stale": False,
                "note": "in crib",
                "ts_utc": None,
            }
        },
        monkeypatch,
    )
    assert s.available is False


def test_never_obtained_reading_is_unavailable(monkeypatch):
    s = _make_sensor(
        {
            "baby_present": {
                "value": None,
                "age_s": None,
                "available": False,
                "stale": False,
                "note": None,
                "ts_utc": None,
            }
        },
        monkeypatch,
    )
    assert s.available is False


def test_missing_history_block_does_not_raise(monkeypatch):
    s = _make_sensor({}, monkeypatch)
    assert s.available is False
    assert s.native_value is None


def test_only_the_never_firing_field_stays_unexposed(monkeypatch):
    """`baby_event` is documented upstream as effectively never firing, and a
    sensor that never changes is noise.

    `wellbeing` used to be excluded alongside it, on the reasoning that
    exposing an opaque bit invents meaning. That was backwards. It is the only
    field that plausibly corresponds to the app's "Caregiver visit" series, and
    leaving it out guaranteed it could never be checked against a night when
    someone knew they went in. It is recorded now, with its uncertainty kept in
    the value -- upstream's own phrase for 0 is "out of crib (caregiver?)",
    question mark included, and the state keeps it. Inventing meaning would be
    labelling it "Caregiver visit"; recording it is what makes that claim
    testable later.
    """

    _make_sensor({}, monkeypatch)
    module = sys.modules["cuboai_pkg.sensor"]
    fields = {f for f, _, _, _ in module.HISTORY_SENSORS}
    assert fields == {"baby_present", "noise", "motion", "privacy", "wellbeing"}
    assert "baby_event" not in fields


def test_a_sentence_note_is_shortened_for_the_state(monkeypatch):
    """wellbeing's note is a full sentence about firmware tinting. A state has
    a 255-character limit and is read at a glance, so only the phrase before
    the em dash becomes the state; the whole thing stays in attributes."""
    long_note = "out of crib (caregiver?) — opaque firmware activity bit; app only uses it to tint"
    s = _make_sensor(
        {"wellbeing": {"value": 0, "age_s": 30.0, "available": True,
                       "stale": False, "note": long_note, "ts_utc": None}},
        monkeypatch, field="wellbeing",
    )
    assert s.native_value == "out of crib (caregiver?)"
    assert s.extra_state_attributes["note"] == long_note
    assert s.extra_state_attributes["raw_value"] == 0


def test_unlabelled_baby_present_reports_its_number(monkeypatch):
    """Reversal of an earlier decision in this file, on evidence.

    bp=0 has no phrase because the library's map only covers 1 "in crib" and
    2 "not in crib". This used to report unknown, on the reasoning that a bare
    `0` reads like "no baby" when the camera had given no interpretable
    answer. Twenty-four hours of real history says otherwise: while the house
    was empty the field sat at 0 continuously, so 0 is the camera answering,
    not declining to.

    Unknown was the worse of the two. It is indistinguishable from a broken
    sensor, it cannot be charted, and it cannot mark up a timeline -- which is
    what this reading is recorded for. The number is reported; `note` still
    wins whenever the library has a phrase.
    """
    s = _make_sensor(
        {"baby_present": {"value": 0, "age_s": 55.0, "available": True, "stale": False, "note": None, "ts_utc": None}},
        monkeypatch,
    )
    assert s.available is True
    assert s.native_value == 0
    assert s.extra_state_attributes["raw_value"] == 0


def test_a_phrase_still_wins_over_the_number(monkeypatch):
    """1 and 2 are mapped, and "in crib" is far more use than "1"."""
    s = _make_sensor(
        {
            "baby_present": {
                "value": 1,
                "age_s": 55.0,
                "available": True,
                "stale": False,
                "note": "in crib",
                "ts_utc": None,
            }
        },
        monkeypatch,
    )
    assert s.native_value == "in crib"
    assert s.extra_state_attributes["raw_value"] == 1


def test_a_stale_reading_is_still_withheld(monkeypatch):
    """Showing the number is not the same as vouching for it: an old "in crib"
    presented as current is the failure this sensor was built to avoid."""
    s = _make_sensor(
        {"baby_present": {"value": 0, "age_s": 3600.0, "available": True, "stale": True, "note": None, "ts_utc": None}},
        monkeypatch,
    )
    assert s.available is False


def test_numeric_field_still_shows_its_number(monkeypatch):
    """Noise is a 0-100 measurement: the number IS the meaning, so no phrase
    is expected and the value must not be suppressed."""
    s = _make_sensor(
        {"noise": {"value": 25, "age_s": 55.0, "available": True, "stale": False, "note": None, "ts_utc": None}},
        monkeypatch,
        field="noise",
        labelled=False,
    )
    assert s.native_value == 25
