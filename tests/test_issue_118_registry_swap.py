"""Issue #118 (follow-up): cards "don't exist" although cuboai-card.js ran.

Home Assistant's frontend replaces window.customElements with a scoped-registry
polyfill while it boots (home-assistant/frontend#52960). If the card file runs
before that swap, its elements are defined in the native registry, which the
polyfill never consults, so the dashboard shows "Custom element doesn't exist"
until the file is imported again by hand. cuboaiDefine() checks again through
whatever registry is current and defines the tag there when it is missing.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

CARD = Path(__file__).parent.parent / "custom_components" / "cuboai" / "www" / "cuboai-card.js"

_HARNESS = """
const fs = require('fs');
globalThis.HTMLElement = class {};
globalThis.window = globalThis;
globalThis.document = { createElement: () => ({ style: {} }) };
const warnings = [];
console.warn = (...a) => warnings.push(a.join(' '));

// A registry like the browser's: one tag per name, one tag per constructor.
function makeRegistry(name, parent) {
  const tags = new Map();
  const used = new Set();
  const waiting = new Map();
  const reg = {
    name, tags, defines: 0, attempts: 0,
    get: (tag) => tags.get(tag),
    define(tag, cls) {
      reg.attempts += 1;
      if (tags.has(tag)) throw new Error(`${name}: ${tag} already defined`);
      // The polyfill hands every definition to the native registry too; that
      // refuses a constructor it already holds.
      if (parent) parent.claim(cls);
      else if (used.has(cls)) throw new Error('constructor already used');
      used.add(cls); tags.set(tag, cls); reg.defines += 1;
      (waiting.get(tag) || []).forEach((r) => r(cls));
    },
    claim(cls) { if (used.has(cls)) throw new Error('constructor already used'); used.add(cls); },
    whenDefined(tag) {
      if (tags.has(tag)) return Promise.resolve(tags.get(tag));
      return new Promise((r) => { waiting.set(tag, [...(waiting.get(tag) || []), r]); });
    },
  };
  return reg;
}

let timers = [];
globalThis.setTimeout = (fn, ms) => { timers.push({ fn, ms }); return timers.length; };
globalThis.clearTimeout = () => {};
const runTimers = () => { const due = timers; timers = []; due.forEach((t) => t.fn && t.fn()); };
const settle = () => new Promise((r) => setImmediate(r));

const TAGS = ['cuboai-camera-card', 'cuboai-camera-card-editor', 'cuboai-timeline-card'];
const src = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];

(async () => {
  const native = makeRegistry('native');
  globalThis.customElements = native;
  const nativeDefine = native.define;
  new Function(src)();
  const out = { nativeHas: TAGS.map((t) => !!native.get(t)), defineUntouched: native.define === nativeDefine };

  if (scenario === 'swap') {
    // HA installs the polyfill after our file ran, then defines its shell.
    const poly = makeRegistry('polyfill', native);
    globalThis.customElements = poly;
    out.polyBefore = TAGS.map((t) => !!poly.get(t));
    poly.define('home-assistant', class {});
    await settle();
    // HA waits 2 s for an element; the first re-check must come well before that.
    out.firstRecheckMs = Math.min(...timers.map((t) => t.ms));
    runTimers(); await settle();
    out.afterTimers = TAGS.map((t) => !!poly.get(t));
    out.polyDefines = poly.defines;
    out.subclassOk = TAGS.every((t) => typeof poly.get(t) === 'function');
  } else if (scenario === 'shell-never') {
    // No home-assistant element ever: the timers alone must catch the swap.
    const poly = makeRegistry('polyfill', native);
    globalThis.customElements = poly;
    runTimers(); await settle();
    out.afterTimers = TAGS.map((t) => !!poly.get(t));
  } else {
    // Normal load: nothing swaps. Re-checks must not define anything again.
    native.define('home-assistant', class {});
    await settle(); runTimers(); await settle();
    out.nativeDefines = native.defines;
    out.nativeAttempts = native.attempts;
  }
  out.warnings = warnings;
  console.log(JSON.stringify(out));
})();
"""


def _run(tmp_path, scenario):
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    harness = tmp_path / "h.js"
    harness.write_text(_HARNESS, encoding="utf-8")
    res = subprocess.run([node, str(harness), str(CARD), scenario], capture_output=True, text=True, timeout=30)
    assert res.returncode == 0, res.stderr
    return json.loads(res.stdout.strip().splitlines()[-1])


def test_defined_natively_on_normal_load(tmp_path):
    out = _run(tmp_path, "normal")
    assert out["nativeHas"] == [True, True, True]
    # three cards + home-assistant, nothing defined twice, nothing logged
    assert out["nativeDefines"] == 4
    assert out["nativeAttempts"] == 4  # re-checks see the tag and leave it alone
    assert out["warnings"] == []
    assert out["defineUntouched"] is True  # #86: never wrap customElements.define


def test_redefined_in_polyfill_registry_after_swap(tmp_path):
    out = _run(tmp_path, "swap")
    assert out["polyBefore"] == [False, False, False]  # the bug: invisible after the swap
    assert out["afterTimers"] == [True, True, True]
    assert out["firstRecheckMs"] < 2000  # inside HA's 2 s whenDefined timeout
    assert out["polyDefines"] == 4
    assert out["subclassOk"] is True
    assert out["warnings"] == []


def test_timers_catch_swap_without_shell(tmp_path):
    out = _run(tmp_path, "shell-never")
    assert out["afterTimers"] == [True, True, True]
