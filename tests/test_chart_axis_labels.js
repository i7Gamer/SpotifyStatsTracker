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

// The alignment half of the same function. Only the LAST label is right-
// aligned, so it ends at the plot's right edge instead of overflowing it. With
// a single bucket that label is also the first: it sits at x = paddingLeft,
// the left edge, and right-aligning it draws the text backwards off the canvas.
// One bucket is what a brand-new account's daily trend has on day one, which
// is the worst moment to show them a clipped chart.
function alignmentsFor(labelCount) {
  const alignments = [];
  const ctx = {
    set textAlign(value) { alignments.push(value); },
    get textAlign() { return alignments[alignments.length - 1]; },
    textBaseline: '', fillStyle: '', font: '',
    fillText() {},
  };
  const labels = Array.from({ length: labelCount }, (_, i) => `2026-07-${10 + i}`);
  //< plotWidth wide enough that every label is drawn (no sparse stepping)
  ChartUtils.drawSparseXLabels(ctx, labels, 40, 1000, 200, 16, i => 40 + i * 20, 10);
  return alignments;
}

run('a single-bucket chart centres its only label instead of clipping it', () => {
  assert.deepStrictEqual(alignmentsFor(1), ['center']);
});

run('the last of several labels is still right-aligned', () => {
  const alignments = alignmentsFor(4);
  assert.strictEqual(alignments[alignments.length - 1], 'right',
    'the rightmost label must end at the plot edge, not overflow it');
});

run('the labels before the last are centred', () => {
  const alignments = alignmentsFor(4);
  assert.deepStrictEqual(alignments.slice(0, -1), ['center', 'center', 'center']);
});

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
