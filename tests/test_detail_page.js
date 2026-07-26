// Plain-node unit test for the detail pages' deferred-body URL builder
// (static/js/detail-page.js). No test framework/dependency - run with:
//   node tests/test_detail_page.js
const assert = require('assert');
const { detailBodyUrl, detailChartData, DETAIL_BODY_AJAX } = require('../static/js/detail-page.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('the marker is the one the routes branch on', () => {
  // routes/charts.py's DETAIL_BODY_AJAX. Deliberately neither of the two
  // narrower refetches' values ('true' for the bucket series, 'list' for the
  // play log), which the routes test first.
  assert.strictEqual(DETAIL_BODY_AJAX, 'page');
});

run('detailBodyUrl marks a bare page URL', () => {
  assert.strictEqual(detailBodyUrl('/song/t1', ''), '/song/t1?ajax=page');
});

run('detailBodyUrl keeps the params the visitor asked for', () => {
  // A shared/bookmarked link carries its bucket, sort, page and tab, and the
  // deferred body is what renders all four.
  assert.strictEqual(
    detailBodyUrl('/artist/a1', '?groupBy=month&view=history&sort=oldest&page=2'),
    '/artist/a1?groupBy=month&view=history&sort=oldest&page=2&ajax=page',
  );
});

run('detailBodyUrl overwrites a narrower mode rather than duplicating it', () => {
  // Landing on a URL that still carries ?ajax=list must not ask for the play
  // log when what is missing is the whole body.
  assert.strictEqual(detailBodyUrl('/album/alb1', '?ajax=list'), '/album/alb1?ajax=page');
  assert.strictEqual(detailBodyUrl('/album/alb1', '?ajax=true'), '/album/alb1?ajax=page');
});

run('the detail pages are the only ones that draw the skips series', () => {
  // The series belongs to one item's chart, where it answers "does this get
  // skipped" and keeps a skip-only track's page from rendering blank. The
  // aggregate pages never set the flag: there the skip bar sat on its own
  // unlabelled count scale next to a time axis, so a bucket with 26 skips
  // could out-tower one with 566 plays.
  assert.strictEqual(detailChartData({}).showSkips, true);
});

run('detailChartData carries both chart series through', () => {
  const data = detailChartData({ timeSeries: [{ label: '2026-07' }], heatmap: [[1]] });
  assert.deepStrictEqual(data.timeSeries, [{ label: '2026-07' }]);
  assert.deepStrictEqual(data.heatmap, [[1]]);
});

run('an artist/album payload without a heatmap stays without one', () => {
  // renderAllCharts skips a canvas that isn't on the page - the key must not
  // arrive as anything but undefined.
  assert.strictEqual(detailChartData({ timeSeries: [] }).heatmap, undefined);
});

console.log('All detail-page tests passed.');
