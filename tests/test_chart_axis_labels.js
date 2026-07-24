// Plain-node unit test for ChartUtils.formatAxisLabel (static/js/chart-utils.js).
// Regression guard for the review finding where a blanket slice(0,7) collapsed
// every day/week bucket label in a month to the same "YYYY-MM" string.
// No test framework - run with: node tests/test_chart_axis_labels.js
const assert = require('assert');
const ChartUtils = require('../static/js/chart-utils.js');
const { formatAxisLabel } = ChartUtils;

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('day/week buckets keep month-day (year dropped, not collapsed)', () => {
  assert.strictEqual(formatAxisLabel('2026-07-18'), '07-18');
  assert.strictEqual(formatAxisLabel('2026-07-25'), '07-25');
});

run('two day buckets in the same month stay distinct', () => {
  assert.notStrictEqual(formatAxisLabel('2026-07-18'), formatAxisLabel('2026-07-25'));
});

run('month buckets are kept whole', () => {
  assert.strictEqual(formatAxisLabel('2026-07'), '2026-07');
  assert.strictEqual(formatAxisLabel('2025-12'), '2025-12');
});

run('hour buckets keep month-day-hour', () => {
  assert.strictEqual(formatAxisLabel('2026-07-18 14:00'), '07-18 14:00');
});

run('non-date labels pass through unchanged', () => {
  assert.strictEqual(formatAxisLabel('Rock'), 'Rock');
  assert.strictEqual(formatAxisLabel(''), '');
});

run('non-string input is returned as-is', () => {
  assert.strictEqual(formatAxisLabel(null), null);
  assert.strictEqual(formatAxisLabel(undefined), undefined);
});

console.log('All chart-axis-label tests passed.');
