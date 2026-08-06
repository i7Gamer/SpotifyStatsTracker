// Plain-node unit test for the shared cover-art sweep (static/js/chrome-common.js).
// Uses a tiny hand-rolled DOM stub (no jsdom dependency) - the sweep only needs
// querySelectorAll and classList. Run with: node tests/test_chrome_common.js
//
// What this pins is the HANDOFF, not the sweep: markLoadedImages takes a root
// ELEMENT, and every registration has to hand it one. Wiring it straight into
// addEventListener('DOMContentLoaded', markLoadedImages) hands it an Event
// instead, and every dashboard load threw
//   TypeError: (root || document).querySelectorAll is not a function
// with the whole cached-cover sweep never running. Nothing in the pytest suite
// executes these files, which is why this lives here.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'chrome-common.js');

function makeImg(complete) {
  const classes = [];
  return { complete, classes, classList: { add(name) { classes.push(name); } } };
}

// Loads the script fresh against a new stub. The module cache would otherwise
// hand back the first load's side effects, and readyState is read at load time.
function loadScript(readyState, images) {
  const listeners = {};
  const sweptRoots = [];
  global.window = { addEventListener() {} };
  global.document = {
    readyState,
    addEventListener(type, fn) { (listeners[type] = listeners[type] || []).push(fn); },
    querySelectorAll(selector) { sweptRoots.push({ root: 'document', selector }); return images; },
    getElementById() { return null; },   //< no #scroll-to-top in this stub
  };
  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  return { listeners, sweptRoots };
}

function run(name, fn) {
  try { fn(); console.log(`ok - ${name}`); }
  catch (err) { console.error(`FAIL - ${name}`); throw err; }
}

run('a cached cover already loaded at DOMContentLoaded gets the fade class', () => {
  const cached = makeImg(true);
  const { listeners } = loadScript('loading', [cached]);

  const onReady = listeners['DOMContentLoaded'];
  assert.ok(onReady && onReady.length, 'the sweep is deferred to DOMContentLoaded');
  //< the argument a real listener receives, and the whole point of this test:
  //  an Event has no querySelectorAll
  onReady.forEach(fn => fn({ type: 'DOMContentLoaded' }));

  assert.deepStrictEqual(cached.classes, ['loaded']);
});

run('a cover still in flight is left to the load listener', () => {
  const pending = makeImg(false);
  const { listeners } = loadScript('loading', [pending]);

  listeners['DOMContentLoaded'].forEach(fn => fn({ type: 'DOMContentLoaded' }));

  assert.deepStrictEqual(pending.classes, []);
});

run('a script that loads after parsing sweeps immediately instead', () => {
  const cached = makeImg(true);
  const { listeners } = loadScript('interactive', [cached]);

  assert.deepStrictEqual(cached.classes, ['loaded']);
  assert.strictEqual(listeners['DOMContentLoaded'], undefined);
});

run('an htmx swap sweeps the swapped element, not the whole document', () => {
  const swapped = makeImg(true);
  const elsewhere = makeImg(true);
  const { listeners, sweptRoots } = loadScript('interactive', [elsewhere]);
  const before = sweptRoots.length;

  listeners['htmx:afterSwap'].forEach(fn => fn({
    target: { querySelectorAll() { return [swapped]; } },
  }));

  assert.deepStrictEqual(swapped.classes, ['loaded']);
  assert.strictEqual(sweptRoots.length, before, 'the document was not re-swept');
});

console.log('all chrome-common tests passed');
