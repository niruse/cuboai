"""Alerts are not states, and the timeline card was only able to draw states.

Every lane the card had was built from recorder history: a row named an entity
and a state, and the card shaded wherever that state held. That works for
``baby_present``, motion, noise and camera uptime, and it cannot work for the
CuboAI app's Cry/Cough/Caregiver series, because those are point events. They
have a moment, not a duration, they are never written to the recorder as
states, and they reach Home Assistant as a LIST on an attribute of
``sensor.cuboai_..._last_alert_...``.

Three consequences shape what is guarded below:

- a point event has ``to == from``, and ``_spans`` ends with
  ``filter((sp) => sp.to > sp.from)``. Pushed through the existing producer,
  every alert would be silently discarded. Events get their own pure producer.
- the data is an ATTRIBUTE. ``_load`` asks the recorder with
  ``no_attributes: true``, and the recorder refuses to store attributes over
  16 KiB in the first place, so no history call can ever return it. An events
  row must therefore make no history call at all -- and must keep drawing when
  the history call it never made fails.
- the throttle in ``set hass`` returns before ``_render``. History does not
  care, since the recorder is a minute coarse anyway; an alert that lands is in
  ``hass.states`` immediately and would still sit invisible for up to a minute.

The alert type is deliberately not enumerated anywhere. Only
``CUBO_ALERT_TEMPERATURE`` has been observed on this camera, the types are
free-form strings from the API, and nothing between the API and the attribute
filters them -- so the lanes are driven by whatever arrives, with ``match_type``
available to split one type into its own lane once Cry and Cough appear.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CARD = Path(__file__).parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js"


def _card_code():
    """The card without comment lines.

    Same reason as in test_dvr_in_card.py: the comments here quote the traps
    they explain, and a guard that reads its own explanation guards nothing.
    """
    return "\n".join(
        line for line in CARD.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("//")
    )


def _node():
    return shutil.which("node")


requires_node = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(tmp_path, harness):
    (tmp_path / "card.js").write_text(CARD.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "h.js").write_text(harness, encoding="utf-8")
    proc = subprocess.run(
        [_node(), str(tmp_path / "h.js"), str(tmp_path / "card.js")],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout)


# =============================================================================
# Static guards
# =============================================================================


class TestTheHistoryLanesAreUntouched:
    def test_the_span_producer_still_drops_zero_width_intervals(self):
        """The reason events needed their own producer in the first place. If
        this line ever goes, the negative-coverage bug test_dvr_in_card.py
        pins comes back -- events must not be the excuse to remove it."""
        assert "return spans.filter((sp) => sp.to > sp.from);" in _card_code()

    def test_events_rows_are_kept_out_of_the_recorder_query(self):
        code = _card_code()
        assert "this._rows.filter((r) => !this._eventsAttr(r)).map((r) => r.entity)" in code
        # And the query itself still asks for no attributes, which is the other
        # half of why the alert list can never arrive through history.
        assert "no_attributes: true" in code

    def test_the_dvr_class_is_not_involved(self):
        """The scrub bar shares this file and shares nothing else."""
        code = CARD.read_text(encoding="utf-8")
        assert code.index("class CuboAICameraCard") < code.index("class CuboAITimelineCard")
        dvr = code[code.index("class CuboAICameraCard") : code.index("class CuboAITimelineCard")]
        for name in ("_eventsAttr", "_events(", "_eventList", "tl-mark"):
            assert name not in dvr, name


class TestTheLaneIsHonestAboutWhatItHolds:
    def test_no_alert_type_is_hardcoded(self):
        """Only one type has ever been seen. A built-in list of three would
        draw nothing for a fourth and would be wrong about the three."""
        code = _card_code()
        for guess in ("CUBO_ALERT_CRY", "CUBO_ALERT_COUGH", "CUBO_ALERT_CAREGIVER"):
            assert guess not in code, guess
        # The only literal type text is the prefix that gets stripped for display.
        assert code.count("CUBO_ALERT_") == 1

    def test_the_count_does_not_claim_to_be_the_whole_night(self):
        """alerts_count defaults to 5 and hours_back to 12, so a 14-hour Night
        window cannot show its first two hours however quiet they were. A bare
        "3" in the legend reads as "three alerts last night"."""
        code = _card_code()
        assert "recent alert" in code
        assert "Only the most recent alerts the integration keeps are available." in code

    def test_the_marker_is_a_fixed_pixel_width(self):
        """A percentage width scales with the window: a mark sized to be
        tappable across 12 hours is a slab across 1 hour and a hairline across
        a week."""
        code = _card_code()
        # The whole declaration, not just "width: 9px" -- .tl-dot in the legend
        # is 9px too, and a guard that matched it passed while the mark was a
        # percentage.
        assert ".tl-mark { position: absolute; top: 1px; bottom: 1px; width: 9px;" in code

    def test_a_photo_that_no_longer_exists_leaves_no_broken_image(self):
        """max_saved_photos is 10 while alerts_count goes to 50, so older
        alerts routinely hold the path of a deleted jpg."""
        code = _card_code()
        assert 'img.addEventListener("error", () => img.remove());' in code


class TestConfig:
    def test_an_events_row_without_an_entity_is_refused(self):
        """There is no history reply to come back empty, so the failure mode is
        a blank lane with no explanation, forever."""
        code = _card_code()
        assert "cuboai-timeline-card: an 'events' row needs an 'entity'" in code


# =============================================================================
# The producer, executed in Node against realistic payloads
# =============================================================================

# Everything time-related is computed inside the harness with the local Date
# constructor, so the assertions hold in any timezone. The window is a night in
# August deliberately: no DST transition falls inside it anywhere.
_EVENTS_HARNESS = """
const fs = require('fs');
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => undefined, define: () => {} };
globalThis.window = globalThis;
globalThis.document = { createElement: () => ({ style:{}, appendChild(){}, setAttribute(){}, addEventListener(){} }) };
new Function(fs.readFileSync(process.argv[2], 'utf8') + ';globalThis.__TL = CuboAITimelineCard;')();
const card = Object.create(globalThis.__TL.prototype);

// 19:00 -> 07:00, the Night window, as local wall-clock time.
const start = new Date(2026, 7, 7, 19, 0, 0, 0);
const end   = new Date(2026, 7, 8, 7, 0, 0, 0);
const span  = end.getTime() - start.getTime();
const at = (h, m) => new Date(2026, 7, h < 19 ? 8 : 7, h, m || 0, 0, 0);
const frac = (t) => Math.round(((t - start.getTime()) / span) * 10000) / 10000;
const secs = (d) => Math.floor(d.getTime() / 1000);

// The shape sensor.cuboai_last_alert_<baby> publishes: unix SECONDS in `ts`,
// an already-local /local/... image path, a stable id.
const tsAlert = (id, type, when, image) => ({
  id, device_id: 'DEV1', type, ts: secs(when),
  created: '2026-08-07', image: image === undefined ? '/local/cuboai_images/DEV1_' + id + '.jpg' : image,
  params: { temperature: 27.4 }, profile: null, region: null,
});
// The shape sensor.cuboai_<baby>_cuboai_session_history_<baby> publishes: an
// ISO string in `time`, no id, no ts, the same path under a different key.
const isoAlert = (type, iso, url) => ({ type, time: iso, image_url: url === undefined ? null : url });

const out = {};
const marks = (row, list, s, e) => card._events(row, list, s || start, e || end).map((m) => ({
  frac: frac(m.t), type: m.type, image: m.image, id: m.id === undefined ? null : m.id,
}));

// ── the ts-seconds shape ────────────────────────────────────────────────────
out.ts_shape = marks({ events: 'alerts' }, [
  tsAlert('A3', 'CUBO_ALERT_TEMPERATURE', at(4, 0)),          // newest first,
  tsAlert('A2', 'CUBO_ALERT_CRY', at(1, 0), null),            // as the API sorts
  tsAlert('A1', 'CUBO_ALERT_TEMPERATURE', at(22, 0)),
]);

// ── the ISO shape ───────────────────────────────────────────────────────────
out.iso_shape = marks({ events: 'alerts' }, [
  isoAlert('CUBO_ALERT_TEMPERATURE', '2026-08-08T04:00:00', '/local/cuboai_images/x.jpg'),
  isoAlert('CUBO_ALERT_CRY', '2026-08-07T22:00:00'),
]);

// A bare date parses as UTC midnight per spec while every other form parses as
// local. Read as local midnight it lands on the card's own 00:00 gridline.
out.date_only = (() => {
  const s = new Date(2026, 7, 7, 12, 0, 0), e = new Date(2026, 7, 9, 12, 0, 0);
  const m = card._events({ events: 'alerts' }, [isoAlert('CUBO_ALERT_TEMPERATURE', '2026-08-08')], s, e);
  return { n: m.length, isLocalMidnight: m.length === 1 && m[0].t === new Date(2026, 7, 8, 0, 0, 0).getTime() };
})();

// An explicit offset is an absolute moment and must be honoured as one.
out.offset_form = (() => {
  const iso = '2026-08-07T22:00:00+03:00';
  const s = new Date(Date.parse(iso) - 3600e3), e = new Date(Date.parse(iso) + 3600e3);
  const m = card._events({ events: 'alerts' }, [isoAlert('CUBO_ALERT_CRY', iso)], s, e);
  return { n: m.length, exact: m.length === 1 && m[0].t === Date.parse(iso) };
})();

// ── the window ──────────────────────────────────────────────────────────────
out.outside_window = marks({ events: 'alerts' }, [
  tsAlert('BEFORE', 'CUBO_ALERT_TEMPERATURE', new Date(start.getTime() - 1)),
  tsAlert('EDGE_START', 'CUBO_ALERT_TEMPERATURE', start),
  tsAlert('INSIDE', 'CUBO_ALERT_TEMPERATURE', at(1, 0)),
  tsAlert('EDGE_END', 'CUBO_ALERT_TEMPERATURE', end),
  tsAlert('AFTER', 'CUBO_ALERT_TEMPERATURE', new Date(end.getTime() + 3600e3)),
]).map((m) => m.id);

// ── junk ────────────────────────────────────────────────────────────────────
out.unusable = marks({ events: 'alerts' }, [
  { id: 'N', type: 'CUBO_ALERT_TEMPERATURE', ts: null },      // the key exists, holding null
  { id: 'U', type: 'CUBO_ALERT_TEMPERATURE' },                // no ts at all
  { id: 'S', type: 'CUBO_ALERT_TEMPERATURE', ts: 'not a number' },
  { id: 'Z', type: 'CUBO_ALERT_TEMPERATURE', ts: 0 },
  { id: 'B', type: 'CUBO_ALERT_TEMPERATURE', time: 'sometime last night' },
  null,
  'a string',
  tsAlert('OK', 'CUBO_ALERT_TEMPERATURE', at(2, 0)),
]).map((m) => m.id);

// ── the same alert twice ────────────────────────────────────────────────────
out.duplicates = marks({ events: 'alerts' }, [
  tsAlert('A1', 'CUBO_ALERT_TEMPERATURE', at(2, 0)),
  tsAlert('A1', 'CUBO_ALERT_TEMPERATURE', at(2, 0)),
]).length;

// ── one type per lane ───────────────────────────────────────────────────────
const mixed = [
  tsAlert('C1', 'CUBO_ALERT_CRY', at(23, 0)),
  tsAlert('T1', 'CUBO_ALERT_TEMPERATURE', at(2, 0)),
  tsAlert('K1', 'CUBO_ALERT_COUGH', at(3, 0)),
];
out.all_types = marks({ events: 'alerts' }, mixed).map((m) => m.id);
out.one_type = marks({ events: 'alerts', match_type: 'CUBO_ALERT_CRY' }, mixed).map((m) => m.id);
out.two_types = marks({ events: 'alerts', match_type: ['CUBO_ALERT_CRY', 'CUBO_ALERT_COUGH'] }, mixed).map((m) => m.id);
out.unknown_type = marks({ events: 'alerts', match_type: 'CUBO_ALERT_NOT_SEEN_YET' }, mixed).length;
out.untyped_is_kept = marks({ events: 'alerts' }, [{ id: 'X', ts: secs(at(2, 0)) }]).map((m) => m.type);

// ── nothing to draw is not an error ─────────────────────────────────────────
out.empty = {
  emptyList: marks({ events: 'alerts' }, []).length,
  notAList: marks({ events: 'alerts' }, { alerts: 'nope' }).length,
  undefinedList: marks({ events: 'alerts' }, undefined).length,
  nullList: marks({ events: 'alerts' }, null).length,
};

// ── reading it off hass.states ──────────────────────────────────────────────
const row = { entity: 'sensor.cuboai_last_alert_mia', events: 'alerts' };
const withStates = (states) => { card._hass = states === null ? null : { states }; return card._eventList(row).length; };
out.from_states = {
  present: withStates({ [row.entity]: { state: 'CUBO_ALERT_TEMPERATURE', attributes: { alerts: [tsAlert('A1', 'CUBO_ALERT_TEMPERATURE', at(2, 0))] } } }),
  emptyAttr: withStates({ [row.entity]: { state: 'No alerts', attributes: { alerts: [] } } }),
  noAttr: withStates({ [row.entity]: { state: 'No alerts', attributes: {} } }),
  attrNotAList: withStates({ [row.entity]: { state: 'x', attributes: { alerts: 'nope' } } }),
  unavailable: withStates({ [row.entity]: { state: 'unavailable', attributes: {} } }),
  unavailableHoldingAList: withStates({ [row.entity]: { state: 'unavailable', attributes: { alerts: [tsAlert('A1', 'CUBO_ALERT_TEMPERATURE', at(2, 0))] } } }),
  unknown: withStates({ [row.entity]: { state: 'unknown', attributes: { alerts: [tsAlert('A1', 'CUBO_ALERT_TEMPERATURE', at(2, 0))] } } }),
  entityMissing: withStates({}),
  noHass: withStates(null),
};

// ── the attribute name is configurable, and it is what makes a row an events row
out.kinds = {
  named: card._eventsAttr({ events: 'alerts' }),
  shorthand: card._eventsAttr({ events: true }),
  other: card._eventsAttr({ events: 'incidents' }),
  historyRow: card._eventsAttr({ entity: 'sensor.x', match: 'in crib' }),
  emptyString: card._eventsAttr({ events: '' }),
  falseFlag: card._eventsAttr({ events: false }),
  nothing: card._eventsAttr(null),
};

// ── the moment itself, before any window is applied ─────────────────────────
// Number(null) is 0, a perfectly finite number, so "no timestamp" turns into
// "midnight on 1 January 1970" unless it is rejected here. The window filter
// happens to hide that today; a card given a window that contains the epoch,
// or an axis clamped to its data, would plot it.
const known = (a) => { const t = card._eventTime(a); return Number.isNaN(t) ? 'unknown' : t; };
out.times = {
  nullTs: known({ ts: null, time: '' }),
  missingTs: known({ type: 'x' }),
  emptyTs: known({ ts: '' }),
  zeroTs: known({ ts: 0 }),
  negativeTs: known({ ts: -1 }),
  stringTs: known({ ts: 'soon' }),
  seconds: known({ ts: 1770000000 }) === 1770000000000,
  notAnObject: known('nope'),
};

out.labels = ['CUBO_ALERT_TEMPERATURE', 'CUBO_ALERT_CRY', 'SOMETHING_NEW', undefined].map((t) => card._eventLabel(t));

console.log(JSON.stringify(out));
"""


@pytest.fixture(scope="module")
def got(tmp_path_factory):
    """One Node run for the whole producer suite."""
    return _run(tmp_path_factory.mktemp("events"), _EVENTS_HARNESS)


@requires_node
class TestTheProducer:
    def test_ts_seconds_land_at_the_right_fraction_of_the_window(self, got):
        """`ts` is unix SECONDS. Treated as milliseconds the whole lane sits at
        the far left of 1970; multiplied twice it sits past the year 3000. Both
        are off-axis and neither raises anything."""
        assert [m["frac"] for m in got["ts_shape"]] == [0.25, 0.5, 0.75]
        # Sorted by time: the attribute arrives newest first, the axis is not.
        assert [m["id"] for m in got["ts_shape"]] == ["A1", "A2", "A3"]

    def test_the_iso_shape_lands_on_the_same_fractions(self, got):
        assert [m["frac"] for m in got["iso_shape"]] == [0.25, 0.75]
        assert [m["type"] for m in got["iso_shape"]] == ["CUBO_ALERT_CRY", "CUBO_ALERT_TEMPERATURE"]

    def test_a_bare_date_is_read_as_local_midnight(self, got):
        """Date.parse("2026-08-08") is UTC midnight by spec, while
        Date.parse("2026-08-08T00:00") is local. Every other time in this card
        is local -- setHours, getHours, toLocaleTimeString -- so a bare date
        left as UTC drifts a lane by up to 14 hours with no error anywhere."""
        assert got["date_only"] == {"n": 1, "isLocalMidnight": True}

    def test_an_explicit_offset_is_an_absolute_moment(self, got):
        assert got["offset_form"] == {"n": 1, "exact": True}

    def test_only_alerts_inside_the_window_are_drawn(self, got):
        """Half-open, exactly like the span producer: the start belongs to the
        window and the end does not, so the last alert of one night cannot also
        appear as the first alert of the next."""
        assert got["outside_window"] == ["EDGE_START", "INSIDE"]

    def test_an_alert_whose_moment_is_unknowable_is_dropped_not_plotted(self, got):
        """`ts` has no default in the coordinator, so the key exists holding
        None whenever the API omits it. Number(null) is 0 -- a mark at the
        epoch, which after the window filter is simply gone, and Number
        (undefined) is NaN, which is `left: NaN%`."""
        assert got["unusable"] == ["OK"]

    def test_an_alert_republished_on_the_next_poll_is_one_mark(self, got):
        assert got["duplicates"] == 1

    def test_a_lane_can_be_narrowed_to_one_type_or_left_open(self, got):
        assert got["all_types"] == ["C1", "T1", "K1"]
        assert got["one_type"] == ["C1"]
        assert got["two_types"] == ["C1", "K1"]
        assert got["unknown_type"] == 0
        # A type nobody has seen yet still draws on an unfiltered lane. That is
        # the whole reason the lane is not driven by a list of known types.
        assert got["untyped_is_kept"] == ["unknown"]

    def test_nothing_to_draw_is_an_empty_lane_not_a_throw(self, got):
        assert got["empty"] == {"emptyList": 0, "notAList": 0, "undefinedList": 0, "nullList": 0}

    def test_a_missing_moment_is_unknown_rather_than_the_epoch(self, got):
        """`ts` has no default in the coordinator, and Number(null) is 0 -- a
        finite number, and a real moment in 1970. The window filter hides it
        today; a Summary card windowed differently, or an axis ever clamped to
        its data, would put a mark at the far left of the night."""
        assert got["times"] == {
            "nullTs": "unknown",
            "missingTs": "unknown",
            "emptyTs": "unknown",
            "zeroTs": "unknown",
            "negativeTs": "unknown",
            "stringTs": "unknown",
            "seconds": True,
            "notAnObject": "unknown",
        }

    def test_the_list_is_read_off_hass_states_and_survives_every_absence(self, got):
        """A refresh whose alert fetch throws resets the attribute to [] rather
        than keeping the previous list, so marks legitimately vanish between
        polls. Empty is never an error.

        `unavailable` holding a list is the same rule the history lanes already
        follow: a sensor reporting no data is "no idea", never a reading.
        """
        assert got["from_states"] == {
            "present": 1,
            "emptyAttr": 0,
            "noAttr": 0,
            "attrNotAList": 0,
            "unavailable": 0,
            "unavailableHoldingAList": 0,
            "unknown": 0,
            "entityMissing": 0,
            "noHass": 0,
        }

    def test_what_makes_a_row_an_events_row(self, got):
        assert got["kinds"] == {
            "named": "alerts",
            "shorthand": "alerts",
            "other": "incidents",
            "historyRow": None,
            "emptyString": None,
            "falseFlag": None,
            "nothing": None,
        }

    def test_the_type_is_readable_on_a_phone(self, got):
        assert got["labels"] == ["Temperature", "Cry", "Something new", "Unknown"]


# =============================================================================
# The lane on screen
# =============================================================================

_RENDER_HARNESS = """
const fs = require('fs');

// Enough DOM to render into and then read back. textContent aggregates
// descendants so a whole subtree can be asserted on as text, and setting it
// clears children -- which is what _render relies on to redraw.
class El {
  constructor(tag) {
    this.tag = tag; this.style = {}; this.children = []; this._text = '';
    this.className = ''; this.attrs = {}; this.handlers = {};
    this.classList = {
      add: (c) => { this.className = (this.className + ' ' + c).trim(); },
      remove: (c) => { this.className = this.className.split(/\\s+/).filter((x) => x && x !== c).join(' '); },
      contains: (c) => this.className.split(/\\s+/).includes(c),
    };
  }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(''); }
  set textContent(v) { this.children = []; this._text = String(v); }
  appendChild(c) { c.parent = this; this.children.push(c); return c; }
  setAttribute(k, v) { this.attrs[k] = v; }
  addEventListener(k, f) { (this.handlers[k] = this.handlers[k] || []).push(f); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter((c) => c !== this); }
  fire(k, ev) { for (const f of this.handlers[k] || []) f(ev || { stopPropagation() {} }); }
}
globalThis.HTMLElement = class {};
globalThis.customElements = { get: () => undefined, define: () => {} };
globalThis.window = globalThis;
globalThis.document = { createElement: (t) => new El(t) };
new Function(fs.readFileSync(process.argv[2], 'utf8') + ';globalThis.__TL = CuboAITimelineCard;')();

const start = new Date(2026, 7, 7, 19, 0, 0, 0);
const end   = new Date(2026, 7, 8, 7, 0, 0, 0);
const secs  = (h, m) => Math.floor(new Date(2026, 7, h < 19 ? 8 : 7, h, m || 0, 0, 0).getTime() / 1000);
const ALERT_ENTITY = 'sensor.cuboai_last_alert_mia';
const alert = (id, type, h, image) => ({
  id, device_id: 'DEV1', type, ts: secs(h), created: '2026-08-07',
  image: image === undefined ? '/local/cuboai_images/DEV1_' + id + '.jpg' : image, params: {},
});
const eventsRow = { entity: ALERT_ENTITY, label: 'Alerts', icon: 'mdi:bell-ring', events: 'alerts', color: '#ff453a' };
const historyRow = { entity: 'sensor.cuboai_mia_cuboai_baby_present_mia', label: 'In crib', match: 'in crib', color: '#2a9d8f' };

const statesWith = (list) => ({ [ALERT_ENTITY]: { state: list.length ? list[0].type : 'No alerts', attributes: { alerts: list } } });

function build(rows, states, config) {
  const card = Object.create(globalThis.__TL.prototype);
  card.appendChild = function (c) { this.root = c; };
  card.dispatchEvent = function () {};
  card.setConfig(Object.assign({ rows }, config || {}));
  card._hass = { states: states || {} };
  return card;
}
const flat = (el, out) => { out = out || []; out.push(el); for (const c of el.children) flat(c, out); return out; };
const byClass = (card, cls) => flat(card._body).filter((e) => e.className.split(/\\s+/).includes(cls));
const text = (card) => card._body.textContent;

const out = {};

// ── a night with three alerts ───────────────────────────────────────────────
{
  const list = [alert('A3', 'CUBO_ALERT_TEMPERATURE', 4), alert('A2', 'CUBO_ALERT_CRY', 1, null), alert('A1', 'CUBO_ALERT_TEMPERATURE', 22)];
  const card = build([historyRow, eventsRow], statesWith(list));
  card._render({ [historyRow.entity]: [{ lu: secs(20), s: 'in crib' }, { lu: secs(2), s: 'out' }] }, start, end, null, end);
  const marks = byClass(card, 'tl-mark');
  out.night = {
    marks: marks.length,
    lefts: marks.map((m) => Math.round(parseFloat(m.style.left) * 100) / 100),
    widths: marks.map((m) => m.style.width),
    colours: [...new Set(marks.map((m) => m.style.background))],
    spans: byClass(card, 'tl-seg').length,
    legend: byClass(card, 'tl-pc').map((p) => p.textContent),
    note: text(card).includes('Nothing recorded in this window.'),
  };
  // Tapping one writes its detail line and shows its photo. Marks are in time
  // order, so [0] is the 22:00 temperature alert and [1] the 01:00 cry with no
  // photo behind it.
  const detail = card._detail;
  marks[0].fire('click');
  out.tapped = { text: detail.textContent, images: flat(detail).filter((e) => e.tag === 'img').map((e) => e.attrs.src || e.src) };
  // The one with no photo must not claim one, and must clear the previous img.
  marks[1].fire('click');
  out.tapped_no_photo = { text: detail.textContent, images: flat(detail).filter((e) => e.tag === 'img').length };
  // A pruned jpg 404s; the img removes itself rather than leaving a broken icon.
  marks[0].fire('click');
  const img = flat(detail).find((e) => e.tag === 'img');
  img.fire('error');
  out.broken_photo_images = flat(detail).filter((e) => e.tag === 'img').length;
}

// ── an empty, missing or unavailable alert sensor ───────────────────────────
for (const [name, states] of [
  ['emptyList', statesWith([])],
  ['entityMissing', {}],
  ['unavailable', { [ALERT_ENTITY]: { state: 'unavailable', attributes: {} } }],
]) {
  const card = build([eventsRow], states);
  card._render({}, start, end, null, end);
  out[name] = {
    marks: byClass(card, 'tl-mark').length,
    lanes: byClass(card, 'tl-track').length,
    legend: byClass(card, 'tl-pc').map((p) => p.textContent),
    note: text(card).includes('Nothing recorded in this window.'),
  };
}

// ── one alert, so the legend is not pluralised wrongly ──────────────────────
{
  const card = build([eventsRow], statesWith([alert('A1', 'CUBO_ALERT_TEMPERATURE', 22)]));
  card._render({}, start, end, null, end);
  out.one = { legend: byClass(card, 'tl-pc').map((p) => p.textContent), note: text(card).includes('Nothing recorded') };
}

// ── the recorder failed ─────────────────────────────────────────────────────
{
  const card = build([historyRow, eventsRow], statesWith([alert('A1', 'CUBO_ALERT_TEMPERATURE', 22)]));
  card._render(null, start, end, 'History unavailable', end);
  out.history_broken = { marks: byClass(card, 'tl-mark').length, note: text(card).includes('History unavailable') };
}
{
  // Unchanged for a card with no events row: the error and nothing else.
  const card = build([historyRow], {});
  card._render(null, start, end, 'History unavailable', end);
  out.history_broken_no_events = { legend: byClass(card, 'tl-pc').length, note: text(card).includes('History unavailable') };
}

// ── no websocket call for an events-only card ───────────────────────────────
const calls = [];
// _load computes its own window from the clock, so this alert is placed
// relative to now rather than on the fixed night above.
const anHourAgo = { id: 'A1', type: 'CUBO_ALERT_TEMPERATURE', ts: Math.floor((Date.now() - 3600e3) / 1000), image: null };
async function loads() {
  const only = build([eventsRow], statesWith([anHourAgo]), { hours: 12 });
  only._hass.callWS = (msg) => { calls.push(msg); return Promise.resolve({}); };
  await only._load();
  const eventsOnly = { calls: calls.length, marks: byClass(only, 'tl-mark').length };

  calls.length = 0;
  const mixed = build([historyRow, eventsRow], statesWith([anHourAgo]), { hours: 12 });
  mixed._hass.callWS = (msg) => { calls.push(msg); return Promise.resolve({}); };
  await mixed._load();
  return { eventsOnly, mixed: { calls: calls.length, ids: calls[0] ? calls[0].entity_ids : null } };
}

// ── a new alert repaints inside the fetch throttle ──────────────────────────
function repaint() {
  const list = [alert('A1', 'CUBO_ALERT_TEMPERATURE', 22)];
  const card = build([eventsRow], statesWith(list), { hours: 12 });
  card._hass.callWS = () => Promise.resolve({});
  card._render({}, start, end, null, end);
  card._fetchedAt = Date.now();
  card._eventSig = card._eventSignature();
  const before = byClass(card, 'tl-mark').length;
  // A second alert arrives 40 seconds into the 60-second throttle.
  const grown = [alert('A2', 'CUBO_ALERT_CRY', 23), ...list];
  card.hass = { states: statesWith(grown), callWS: () => Promise.resolve({}) };
  const after = byClass(card, 'tl-mark').length;
  // A tick with nothing new must not rebuild the card under the user's finger.
  card._body.appendChild(Object.assign(new El('div'), { className: 'sentinel' }));
  card.hass = { states: statesWith(grown), callWS: () => Promise.resolve({}) };
  return { before, after, keptWhenUnchanged: byClass(card, 'sentinel').length };
}

// ── config ──────────────────────────────────────────────────────────────────
function config() {
  const bad = (rows) => { try { build(rows, {}); return null; } catch (e) { return e.message; } };
  return {
    noEntity: bad([{ label: 'Alerts', events: 'alerts' }]),
    historyRowUnchecked: bad([{ label: 'x', match: 'y' }]),
    size: build([historyRow, eventsRow], {}).getCardSize(),
  };
}

loads().then((l) => {
  out.load = l;
  out.repaint = repaint();
  out.config = config();
  console.log(JSON.stringify(out));
});
"""


@pytest.fixture(scope="module")
def drawn(tmp_path_factory):
    """One Node run for the whole rendering suite."""
    return _run(tmp_path_factory.mktemp("render"), _RENDER_HARNESS)


@requires_node
class TestTheLaneOnScreen:
    def test_marks_are_drawn_at_their_moment_beside_the_history_lanes(self, drawn):
        night = drawn["night"]
        assert night["marks"] == 3
        assert night["lefts"] == [25.0, 50.0, 75.0]
        # Fixed pixel width, set by the stylesheet -- not an inline percentage
        # that would scale with the window.
        assert night["widths"] == [None, None, None] or all(w is None for w in night["widths"])
        assert night["colours"] == ["#ff453a"]
        # The history lane still drew: adding events must not cost the card
        # what it already did.
        assert night["spans"] == 1

    def test_the_legend_counts_marks_where_a_history_lane_shows_a_share(self, drawn):
        """A percentage of a zero-duration series is 0% however many fired, so
        a night full of alerts read as a lane where nothing happened."""
        assert drawn["night"]["legend"] == ["50%", "3 recent alerts"]
        assert drawn["one"]["legend"] == ["1 recent alert"]

    def test_a_night_whose_only_content_is_alerts_is_not_called_empty(self, drawn):
        assert drawn["night"]["note"] is False
        assert drawn["one"]["note"] is False

    def test_tapping_a_mark_says_what_it_was_and_when(self, drawn):
        tapped = drawn["tapped"]
        assert "Alerts" in tapped["text"]
        assert "Temperature" in tapped["text"]
        assert "photo" in tapped["text"]
        assert tapped["images"] == ["/local/cuboai_images/DEV1_A1.jpg"]

    def test_an_alert_with_no_photo_neither_claims_one_nor_shows_the_last_one(self, drawn):
        """`image` is None whenever download_images is off -- and in the
        session-history sensor the key is present holding None, so this is a
        truthiness test rather than a comparison against ""."""
        assert "photo" not in drawn["tapped_no_photo"]["text"]
        assert "Cry" in drawn["tapped_no_photo"]["text"]
        assert drawn["tapped_no_photo"]["images"] == 0

    def test_a_deleted_photo_leaves_no_broken_image(self, drawn):
        assert drawn["broken_photo_images"] == 0

    def test_a_quiet_sensor_renders_an_empty_lane(self, drawn):
        for name in ("emptyList", "unavailable"):
            case = drawn[name]
            assert case["marks"] == 0, name
            # The lane is still there. A row that disappears reads as a card
            # that has broken, not as a quiet night.
            assert case["lanes"] == 1, name
            assert case["legend"] == ["0 recent alerts"], name
            assert case["note"] is True, name

    def test_a_missing_entity_says_so_instead_of_reading_as_quiet(self, drawn):
        """A typo in the entity id returns an empty list exactly as a quiet
        night does, so the lane drew "0 recent alerts" forever and nothing
        anywhere said the sensor did not exist. That shipped once already: the
        dashboard named a doubled-prefix id that no install has."""
        case = drawn["entityMissing"]
        assert case["marks"] == 0
        assert case["lanes"] == 1
        assert case["legend"] == ["sensor not found"]
        assert case["note"] is True

    def test_a_failed_history_call_does_not_blank_the_alert_lane(self, drawn):
        """The alert lane never asked the recorder anything."""
        assert drawn["history_broken"] == {"marks": 1, "note": True}

    def test_a_card_with_no_events_row_still_shows_only_the_error(self, drawn):
        assert drawn["history_broken_no_events"] == {"legend": 0, "note": True}

    def test_an_events_only_card_makes_no_history_call(self, drawn):
        assert drawn["load"]["eventsOnly"] == {"calls": 0, "marks": 1}

    def test_a_mixed_card_asks_only_about_its_history_rows(self, drawn):
        assert drawn["load"]["mixed"]["calls"] == 1
        assert drawn["load"]["mixed"]["ids"] == ["sensor.cuboai_mia_cuboai_baby_present_mia"]

    def test_a_new_alert_appears_without_waiting_out_the_fetch_throttle(self, drawn):
        """The throttle exists to spare the recorder. Alerts do not come from
        the recorder, and an alert that fires at 02:00 and shows up at 02:01 is
        a monitor nobody trusts."""
        assert drawn["repaint"] == {"before": 1, "after": 2, "keptWhenUnchanged": 1}

    def test_config(self, drawn):
        assert drawn["config"]["noEntity"] == "cuboai-timeline-card: an 'events' row needs an 'entity'"
        # History rows have shipped unvalidated; tightening them here would
        # throw on dashboards that already work.
        assert drawn["config"]["historyRowUnchecked"] is None
        assert drawn["config"]["size"] == 4


def test_a_recorder_failure_is_not_reported_as_a_percentage():
    """Source-level guard, because the Node harness only inspects the alert
    lane's legend and not the history lanes'.

    When an events row is present the card keeps drawing after a recorder
    error, so the history lanes have no data at all -- and `0%` is a claim
    about the night rather than an admission of not knowing. On the Nighttime
    view that read `In crib 0%, Motion 0%` for a window the card had just said
    it could not read. `_spans` already refuses that conflation for
    `unavailable`; the legend must not undo it one line later.
    """
    card = (Path(__file__).parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js").read_text(
        encoding="utf-8"
    )
    code = chr(10).join(line for line in card.splitlines() if not line.strip().startswith("//"))
    assert "const historyFailed = Boolean(error);" in code
    assert "} else if (historyFailed) {" in code
    assert 'pc.textContent = "—";' in code
