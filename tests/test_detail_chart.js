// Plain-node unit test for the detail pages' Trend-buckets URL builders
// (static/js/detail-chart.js). No test framework/dependency - run with:
//   node tests/test_detail_chart.js
const assert = require('assert');
const { detailDataUrl, detailPageUrl } = require('../static/js/detail-chart.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('detailDataUrl adds ajax=true', () => {
  assert.strictEqual(detailDataUrl('/song/t1', ''), '/song/t1?ajax=true');
});

run('detailDataUrl preserves existing params and adds ajax=true', () => {
  assert.strictEqual(
    detailDataUrl('/artist/a1', '?groupBy=month'),
    '/artist/a1?groupBy=month&ajax=true',
  );
});

run('detailDataUrl overwrites a stale ajax value rather than duplicating it', () => {
  assert.strictEqual(detailDataUrl('/album/x', '?ajax=false'), '/album/x?ajax=true');
});

run('detailPageUrl sets groupBy for an explicit bucket', () => {
  assert.strictEqual(detailPageUrl('/song/t1', '', 'day'), '/song/t1?groupBy=day');
  assert.strictEqual(detailPageUrl('/song/t1', '', 'week'), '/song/t1?groupBy=week');
  assert.strictEqual(detailPageUrl('/song/t1', '', 'month'), '/song/t1?groupBy=month');
});

run('detailPageUrl drops groupBy entirely for Auto (empty value)', () => {
  assert.strictEqual(detailPageUrl('/song/t1', '?groupBy=month', ''), '/song/t1');
});

run('detailPageUrl strips ajax from the pushed page URL', () => {
  assert.strictEqual(detailPageUrl('/song/t1', '?ajax=true', 'week'), '/song/t1?groupBy=week');
});

run('detailPageUrl preserves unrelated params', () => {
  assert.strictEqual(
    detailPageUrl('/artist/a1', '?foo=bar', 'week'),
    '/artist/a1?foo=bar&groupBy=week',
  );
});

console.log('All detail-chart tests passed.');
