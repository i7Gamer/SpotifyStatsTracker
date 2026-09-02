// Plain-node unit test for ChartUtils.bindRepaint (static/js/chart-utils.js).
// Run with: node tests/test_chart_repaint.js
//
// The two repaints that are not swaps - a window resize and a cross-tab theme
// change - used to be a 12-line block copied verbatim into charts.js and
// genres.js. Now that the block lives once, its two timings are pinned once,
// here, against the real library; the page harnesses (test_charts_render.js,
// test_genres_page.js) only pin that each page binds its own renderer.
//
// Both timings are behaviour, not decoration: without the debounce a drag
// resize repaints nine charts per pixel, and a theme repaint that runs before
// the new CSS variables have landed reads the OLD colours - which is the bug
// the wait exists for.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'chart-utils.js');

function loadChartUtils() {
  const calls = { listeners: {}, timeouts: [], cleared: [] };
  global.window = {
    addEventListener(type, fn) { calls.listeners[type] = fn; },
  };
  global.setTimeout = function (fn, ms) {
    calls.timeouts.push({ fn, ms });
    return calls.timeouts.length;
  };
  global.clearTimeout = function (id) { calls.cleared.push(id); };

  delete require.cache[require.resolve(SCRIPT)];
  calls.ChartUtils = require(SCRIPT);
  return calls;
}

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('a resize is coalesced into one debounced repaint of the given renderer', () => {
  const page = loadChartUtils();
  let repaints = 0;
  page.ChartUtils.bindRepaint(() => { repaints += 1; });

  page.listeners.resize();
  page.listeners.resize();

  assert.deepStrictEqual(page.timeouts.map(t => t.ms), [150, 150]);
  //< the first timer is cancelled by the second resize, so only one fires
  assert.deepStrictEqual(page.cleared, [undefined, 1]);
  page.timeouts[1].fn();
  assert.strictEqual(repaints, 1);
});

run('a theme change waits for the new CSS variables before repainting', () => {
  const page = loadChartUtils();
  let repaints = 0;
  page.ChartUtils.bindRepaint(() => { repaints += 1; });

  page.listeners.themechange();

  assert.deepStrictEqual(page.timeouts.map(t => t.ms), [50]);
  page.timeouts[0].fn();
  assert.strictEqual(repaints, 1);
});

console.log('all chart repaint tests passed');
