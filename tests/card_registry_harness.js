const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync(process.argv[2], 'utf8');
const scenario = process.argv[3];
const names = ['cuboai-camera-card-editor', 'cuboai-camera-card', 'cuboai-timeline-card'];

class Registry {
  constructor({ rejectUsedConstructors = false, usedConstructors = new Set() } = {}) {
    this.entries = new Map();
    this.attempts = [];
    this.failures = new Map();
    this.rejectUsedConstructors = rejectUsedConstructors;
    this.usedConstructors = usedConstructors;
    this.ready = new Promise((resolve, reject) => { this.resolve = resolve; this.reject = reject; });
  }
  get(name) { return this.entries.get(name); }
  define(name, cls) {
    this.attempts.push([name, cls]);
    if (this.failures.has(name)) throw this.failures.get(name);
    if (this.entries.has(name) || [...this.entries.values()].includes(cls)) throw new Error('duplicate definition');
    if (this.rejectUsedConstructors && this.usedConstructors.has(cls)) throw new Error('constructor already used');
    this.entries.set(name, cls);
    this.usedConstructors.add(cls);
  }
  whenDefined(name) {
    assert.equal(name, 'home-assistant');
    return this.ready;
  }
}

function environment() {
  let now = 0;
  const timers = [];
  const logs = { info: [], warn: [], error: [] };
  const registry = new Registry();
  const unrelated = class OtherElement {};
  registry.define('other-element', unrelated);
  const sandbox = {
    HTMLElement: class {}, customElements: registry,
    console: {
      info: (...args) => logs.info.push(args),
      warn: (...args) => logs.warn.push(args),
      error: (...args) => logs.error.push(args),
    },
    setTimeout: (fn, delay) => { timers.push({ fn, at: now + delay }); return timers.length; },
  };
  sandbox.window = sandbox;
  const context = vm.createContext(sandbox);
  let loads = 0;
  function load() {
    // Separate lexical scopes model distinct module/resource URLs without changing the bundle.
    return vm.runInContext(`(function() {\n${source}\nreturn [CuboAICameraCardEditor, CuboAICameraCard, CuboAITimelineCard];\n})()`, context,
      { filename: `https://ha.example/local/cuboai-card.js?v=${++loads}` });
  }
  function advance(ms) {
    const end = now + ms;
    while (true) {
      const index = timers.findIndex(timer => timer.at <= end);
      if (index < 0) break;
      const [timer] = timers.splice(index, 1);
      now = timer.at;
      timer.fn();
    }
    now = end;
  }
  function metadata() {
    assert.equal(sandbox.customCards.filter(c => c.type === names[1]).length, 1);
    assert.equal(sandbox.customCards.filter(c => c.type === names[2]).length, 1);
  }
  return { sandbox, registry, unrelated, timers, logs, load, advance, metadata };
}

const flush = async () => { for (let i = 0; i < 5; i++) await Promise.resolve(); };
const assertEntries = (registry, constructors) => names.forEach((name, i) => assert.equal(registry.get(name), constructors[i], name));
const assertLogged = (records, name, error) => assert(records.some(args => args[0].includes(name) && (!error || args.includes(error))), `missing log for ${name}`);
const assertSubclassEntries = (registry, constructors) => names.forEach((name, i) => {
  assert.equal(Object.getPrototypeOf(registry.get(name)), constructors[i], name);
});

async function run() {
  const e = environment();
  const initialDefine = e.registry.define;
  if (scenario === 'startup') {
    const classes = e.load();
    assertEntries(e.registry, classes);
    assert.equal(e.registry.get('other-element'), e.unrelated);
    assert.equal(e.registry.define, initialDefine);
    const another = class AnotherElement {};
    e.registry.define('another-element', another);
    assert.equal(e.registry.get('another-element'), another);
    e.metadata();
    e.registry.resolve(); await flush();
    e.advance(15000);
    assertEntries(e.registry, classes);
    assert.equal(e.registry.attempts.length, 5);
    assert.equal(e.logs.error.length, 0);
  } else if (scenario === 'ready_after_swap') {
    const classes = e.load();
    const next = new Registry();
    const nextDefine = next.define;
    const other = class Other {};
    next.define('other-element', other);
    e.sandbox.customElements = next;
    e.registry.resolve(); await flush();
    assertEntries(next, classes);
    names.forEach(name => assertLogged(e.logs.info, name));
    assert.equal(next.get('other-element'), other);
    assert.equal(next.define, nextDefine);
    assert.equal(e.registry.define, initialDefine);
    const attempts = next.attempts.length;
    e.advance(15000);
    assert.equal(next.attempts.length, attempts);
    e.metadata();
  } else if (scenario === 'second_swap_after_recovery') {
    const classes = e.load();
    const second = new Registry(); e.sandbox.customElements = second;
    e.registry.resolve(); await flush();
    assertEntries(second, classes);
    const third = new Registry(); e.sandbox.customElements = third;
    e.advance(5000);
    assertEntries(third, classes);
    assertEntries(second, classes);
    e.metadata();
  } else if (scenario === 'ready_before_swap' || scenario === 'no_readiness') {
    const classes = e.load();
    if (scenario === 'ready_before_swap') { e.registry.resolve(); await flush(); }
    const next = new Registry(); e.sandbox.customElements = next;
    e.advance(499);
    names.forEach(name => assert.equal(next.get(name), undefined));
    e.advance(1);
    assertEntries(next, classes);
    names.forEach(name => assertLogged(e.logs.info, name));
    e.metadata();
  } else if (scenario === 'late_swap_before_last_recheck') {
    const classes = e.load();
    e.registry.resolve(); await flush();
    e.advance(6000);
    const next = new Registry(); e.sandbox.customElements = next;
    e.advance(8999);
    names.forEach(name => assert.equal(next.get(name), undefined));
    e.advance(1);
    assertEntries(next, classes);
    names.forEach(name => assertLogged(e.logs.info, name));
  } else if (scenario === 'constructor_fallback') {
    const classes = e.load();
    const next = new Registry({ rejectUsedConstructors: true, usedConstructors: new Set(classes) });
    e.sandbox.customElements = next;
    e.registry.resolve(); await flush();
    assertSubclassEntries(next, classes);
    names.forEach(name => {
      assertLogged(e.logs.info, name);
    });
    assert.equal(e.logs.warn.length, 0);
    assert.equal(e.logs.error.length, 0);
  } else if (scenario === 'existing' || scenario === 'partial') {
    const old = names.map(() => class Existing {});
    const count = scenario === 'existing' ? names.length : 2;
    for (let i = 0; i < count; i++) e.registry.define(names[i], old[i]);
    const metadata = { type: names[1], name: 'external' };
    e.sandbox.customCards = [metadata, { type: 'other-card' }];
    const classes = e.load();
    assertEntries(e.registry, classes.map((cls, i) => i < count ? old[i] : cls));
    const next = new Registry();
    next.define(names[1], old[1]);
    e.sandbox.customElements = next;
    e.registry.resolve(); await flush();
    assertEntries(next, [classes[0], old[1], classes[2]]);
    e.advance(15000);
    assert.equal(e.sandbox.customCards[0], metadata);
    assert.equal(e.sandbox.customCards[1].type, 'other-card');
    e.metadata();
  } else if (scenario === 'repeat') {
    const first = e.load();
    const second = e.load();
    assertEntries(e.registry, first);
    assert.notEqual(first[0], second[0]);
    const next = new Registry(); e.sandbox.customElements = next;
    e.registry.resolve(); await flush();
    assertEntries(next, first);
    const third = e.load();
    assert.notEqual(first[0], third[0]);
    assertEntries(next, first);
    next.resolve(); await flush();
    const attempts = next.attempts.length;
    e.advance(15000);
    assert.equal(next.attempts.length, attempts);
    assertEntries(next, first);
    e.metadata();
  } else if (scenario === 'repeat_after_swap_before_ready') {
    const first = e.load();
    const next = new Registry(); e.sandbox.customElements = next;
    const second = e.load();
    assertEntries(next, second);
    e.registry.resolve(); await flush();
    assertEntries(next, second);
    next.resolve(); await flush();
    e.advance(15000);
    assertEntries(e.registry, first);
    assertEntries(next, second);
    e.metadata();
  } else if (scenario === 'too_late') {
    e.load(); e.registry.resolve(); await flush(); e.advance(15000);
    const next = new Registry(); e.sandbox.customElements = next;
    e.advance(60000); await flush();
    names.forEach(name => assert.equal(next.get(name), undefined));
    assert.equal(e.timers.length, 0);
  } else if (scenario.startsWith('sync_error:')) {
    const name = scenario.slice('sync_error:'.length);
    const error = new Error('sync define failure');
    e.registry.failures.set(name, error);
    // A failure on the first load is logged, never thrown: a throw would stop
    // the file and leave the cards defined after this one undefined.
    const classes = e.load();
    assert.equal(e.registry.get(name), undefined);
    assertLogged(e.logs.error, name, error);
    names.forEach((other, i) => { if (other !== name) assert.equal(e.registry.get(other), classes[i], other); });
    e.metadata();
    // ...and a later recheck registers it once the registry accepts it.
    e.registry.failures.delete(name);
    e.advance(500);
    assert.equal(e.registry.get(name), classes[names.indexOf(name)]);
  } else if (scenario.startsWith('async_error:')) {
    const name = scenario.slice('async_error:'.length);
    const classes = e.load();
    const next = new Registry(); e.sandbox.customElements = next;
    const error = new Error('async define failure'); next.failures.set(name, error);
    e.registry.resolve(); await flush();
    assert.equal(next.get(name), undefined);
    assertLogged(e.logs.error, name, error);
    next.failures.delete(name);
    e.advance(5000);
    assertEntries(next, classes);
    assertLogged(e.logs.info, name);
  } else if (scenario === 'timer_error_retry') {
    const classes = e.load();
    const next = new Registry(); e.sandbox.customElements = next;
    const errors = names.map(name => new Error(`timer failure ${name}`));
    names.forEach((name, i) => next.failures.set(name, errors[i]));
    e.advance(5000);
    names.forEach((name, i) => { assert.equal(next.get(name), undefined); assertLogged(e.logs.error, name, errors[i]); });
    next.failures.clear();
    e.registry.resolve(); await flush();
    assertEntries(next, classes);
  } else if (scenario === 'readiness_error') {
    const classes = e.load();
    const next = new Registry(); e.sandbox.customElements = next;
    const error = new Error('readiness failure');
    e.registry.reject(error); await flush();
    names.forEach(name => assertLogged(e.logs.error, name, error));
    e.advance(5000);
    assertEntries(next, classes);
  } else {
    throw new Error(`unknown scenario ${scenario}`);
  }
}

run().catch(error => { console.error(error); process.exitCode = 1; });
