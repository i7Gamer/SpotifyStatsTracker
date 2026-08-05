// Plain-node unit test for the detail pages' chart-data shaping
// (static/js/detail-page.js). No test framework/dependency - run with:
//   node tests/test_detail_page.js
//
// The URL builder this file used to test is gone: htmx asks for the deferred
// body from the hx-get the route prints on the shell's placeholder, so there is
// no client-side URL assembly left. What survives is the one thing the server
// deliberately does NOT send - see below.
const assert = require('assert');
const { detailChartData } = require('../static/js/detail-page.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('the detail pages are the only ones that draw the skips series', () => {
  // The series belongs to one item's chart, where it answers "does this get
  // skipped" and keeps a skip-only track's page from rendering blank. The
  // aggregate pages never set the flag: there the skip bar sat on its own
  // unlabelled count scale next to a time axis, so a bucket with 26 skips
  // could out-tower one with 566 plays.
  //
  // It stays client-side rather than riding in the data island because it is
  // about how charts.js DRAWS, not about what the server measured.
  assert.strictEqual(detailChartData({}).showSkips, true);
});

run('detailChartData carries both chart series through', () => {
  const data = detailChartData({ timeSeries: [{ label: '2026-07' }], heatmap: [[1]] });
  assert.deepStrictEqual(data.timeSeries, [{ label: '2026-07' }]);
  assert.deepStrictEqual(data.heatmap, [[1]]);
});

run('an artist/album island without a heatmap stays without one', () => {
  // renderAllCharts skips a canvas that isn't on the page - the key must not
  // arrive as anything but undefined.
  assert.strictEqual(detailChartData({ timeSeries: [] }).heatmap, undefined);
});

console.log('All detail-page tests passed.');
