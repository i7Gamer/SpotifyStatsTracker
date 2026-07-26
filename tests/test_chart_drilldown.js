// Plain-node unit tests for the time-series bar click-through URL
// (static/js/chart-utils.js). The click handler itself needs a real canvas, so
// the URL it navigates to is a pure function pinned here. No test framework -
// run with: node tests/test_chart_drilldown.js
const assert = require('assert');
const { bucketDrilldownUrl } = require('../static/js/chart-utils.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('a bucket click lands on the play-history list', () => {
  // The reported bug: this used to point at '/', which stopped scoping
  // anything once the play list moved off the dashboard onto /history.
  assert.strictEqual(
    bucketDrilldownUrl('2026-07-01', '2026-07-07'),
    '/history?interval=custom&startDate=2026-07-01&endDate=2026-07-07');
});

run('the target is /history, not the dashboard', () => {
  const url = bucketDrilldownUrl('2026-01-01', '2026-01-31');
  assert.ok(url.startsWith('/history?'), `expected a /history link, got ${url}`);
});

run('range values are url-encoded', () => {
  // Values are server-stamped "YYYY-MM-DD" strings today, but the handler must
  // not build an injectable query string if that ever changes.
  assert.strictEqual(
    bucketDrilldownUrl('2026-07-01&x=1', '2026-07-07'),
    '/history?interval=custom&startDate=2026-07-01%26x%3D1&endDate=2026-07-07');
});

console.log('All chart drilldown tests passed.');
