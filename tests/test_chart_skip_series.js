// Plain-node unit tests for the time-series skips series helpers
// (static/js/chart-utils.js). renderTimeSeriesChart itself needs a real canvas,
// so the two decisions the series turns on are extracted as pure functions and
// pinned here. No test framework - run with: node tests/test_chart_skip_series.js
const assert = require('assert');
const { maxSkipsIn, timeSeriesHasNothingToDraw } = require('../static/js/chart-utils.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

const LISTENED = { label: '2026-07-01', totalTimeListened: 60000, plays: 1, skips: 0 };
const SKIPPED = { label: '2026-07-02', totalTimeListened: 0, plays: 0, skips: 3 };
const NOTHING = { label: '2026-07-03', totalTimeListened: 0, plays: 0, skips: 0 };

run('maxSkipsIn returns the highest skip count', () => {
  assert.strictEqual(maxSkipsIn([LISTENED, SKIPPED, NOTHING]), 3);
});

run('maxSkipsIn is 0 when no bucket has skips', () => {
  assert.strictEqual(maxSkipsIn([LISTENED, NOTHING]), 0);
});

run('maxSkipsIn tolerates buckets predating the skips key', () => {
  // Cached/older payloads have no `skips` at all - must not produce NaN, which
  // would make every bar height NaN and blank the chart.
  assert.strictEqual(maxSkipsIn([{ totalTimeListened: 10, plays: 1 }]), 0);
  assert.strictEqual(maxSkipsIn([]), 0);
  assert.strictEqual(maxSkipsIn(null), 0);
});

run('a skip-only track is NOT treated as empty where skips are drawn', () => {
  // The reported bug: zero listening time in every bucket used to read as
  // "no data" on a page whose whole point is the skips.
  assert.strictEqual(timeSeriesHasNothingToDraw([SKIPPED], true), false);
});

run('a skips-only range IS empty where skips are not drawn', () => {
  // The aggregate pages (/charts, /wrapped) leave the series off, so there is
  // genuinely nothing on the time axis - drawing a row of zero-height bars
  // instead of the empty state would say less, not more.
  assert.strictEqual(timeSeriesHasNothingToDraw([SKIPPED], false), true);
});

run('the skips series is off unless a page asks for it', () => {
  // Anything that does not opt in gets the pre-skips behaviour, so a new
  // caller can never inherit the two-units-one-axis chart by accident.
  assert.strictEqual(timeSeriesHasNothingToDraw([SKIPPED]), true);
});

run('buckets with real listening are not empty either way', () => {
  assert.strictEqual(timeSeriesHasNothingToDraw([LISTENED], true), false);
  assert.strictEqual(timeSeriesHasNothingToDraw([LISTENED], false), false);
  assert.strictEqual(timeSeriesHasNothingToDraw([LISTENED, SKIPPED], true), false);
  assert.strictEqual(timeSeriesHasNothingToDraw([LISTENED, SKIPPED], false), false);
});

run('genuinely empty buckets still report empty', () => {
  assert.strictEqual(timeSeriesHasNothingToDraw([NOTHING, NOTHING], true), true);
  assert.strictEqual(timeSeriesHasNothingToDraw([], true), true);
  assert.strictEqual(timeSeriesHasNothingToDraw(null, true), true);
});

run('a bucket list with no skips key behaves as before', () => {
  assert.strictEqual(timeSeriesHasNothingToDraw([{ totalTimeListened: 0, plays: 0 }], true), true);
  assert.strictEqual(timeSeriesHasNothingToDraw([{ totalTimeListened: 5, plays: 1 }], true), false);
});

console.log('All chart skip-series tests passed.');
