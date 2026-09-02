// Plain-node unit tests for the time-series skips series helpers
// (static/js/chart-utils.js). renderTimeSeriesChart itself needs a real canvas,
// so the two decisions the series turns on are extracted as pure functions and
// pinned here. No test framework - run with: node tests/test_chart_skip_series.js
const assert = require('assert');
const {
  maxSkipsIn, skipAxisMax, timeSeriesHasNothingToDraw, timeSeriesLegendItems, legendHtml,
  escapeHtml, PALETTE, TIME_SERIES_PLAY_COLOR_INDEX, TIME_SERIES_SKIP_COLOR_INDEX,
  GRID_LINE_COUNT,
} = require('../static/js/chart-utils.js');

// The grid drawYAxisGrid draws and charts.js sizes its axes for - read off the
// export rather than restated, so this cannot drift from the chart.
const GRID_LINES = GRID_LINE_COUNT;

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

// --- skip axis scale -------------------------------------------------------
// The skip bars get their own labelled axis on the right. Its top has to be a
// clean multiple of the grid line count or the labels repeat: 3 skips split
// into 4 steps reads 0 / 1 / 2 / 2 / 3 once each line is rounded to whole plays.

run('the skip axis tops out at the next whole multiple of the grid', () => {
  assert.strictEqual(skipAxisMax(1, GRID_LINES), 4);
  assert.strictEqual(skipAxisMax(3, GRID_LINES), 4);
  assert.strictEqual(skipAxisMax(4, GRID_LINES), 4);
  assert.strictEqual(skipAxisMax(5, GRID_LINES), 8);
  assert.strictEqual(skipAxisMax(26, GRID_LINES), 28);
});

run('every grid line of the skip axis lands on a whole skip count', () => {
  [1, 2, 3, 7, 13, 26, 99].forEach((maxSkips) => {
    const axisMax = skipAxisMax(maxSkips, GRID_LINES);
    for (let i = 0; i <= GRID_LINES; i++) {
      const value = axisMax * i / GRID_LINES;
      assert.strictEqual(value, Math.round(value), `line ${i} of max ${maxSkips} is ${value}`);
    }
  });
});

run('the skip axis always reaches the tallest bar', () => {
  // Bars are scaled against this max, so a max below the tallest count would
  // draw it past the top of the plot.
  [1, 2, 3, 5, 26, 100].forEach((maxSkips) => {
    assert.ok(skipAxisMax(maxSkips, GRID_LINES) >= maxSkips, `max ${maxSkips}`);
  });
});

run('no skips means no axis to scale', () => {
  // Guarded by hasSkips at the call site; must not invent a 0..4 axis for a
  // series that is never drawn - and must never return 0 as a divisor when it is.
  assert.strictEqual(skipAxisMax(0, GRID_LINES), 0);
  assert.strictEqual(skipAxisMax(null, GRID_LINES), 0);
  assert.strictEqual(skipAxisMax(undefined, GRID_LINES), 0);
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

// --- legend ----------------------------------------------------------------
// The skip bars are a second, narrower bar drawn on their own count scale, and
// nothing on screen said so: the detail pages showed two differently coloured
// bars per bucket with one axis and no key at all.

run('a chart with skip bars names both series and the axis each is read against', () => {
  const items = timeSeriesLegendItems(true);
  assert.strictEqual(items.length, 2);
  assert.strictEqual(items[0].name, 'Listening time (left axis)');
  // Was "(own scale)" while the skip counts had no labels of their own; they
  // are now drawn against a labelled right-hand axis, so the key points at it.
  assert.strictEqual(items[1].name, 'Skips (right axis)');
});

run('the legend swatches are the colours the bars are actually drawn in', () => {
  // Bar colour and swatch colour must come from one place, or a palette change
  // silently relabels the chart.
  const items = timeSeriesLegendItems(true);
  assert.strictEqual(items[0].color, PALETTE[TIME_SERIES_PLAY_COLOR_INDEX]);
  assert.strictEqual(items[1].color, PALETTE[TIME_SERIES_SKIP_COLOR_INDEX]);
  assert.notStrictEqual(items[0].color, items[1].color);
});

run('no skip bars drawn means no legend', () => {
  // A single-series chart needs no key, and claiming a skips series that is not
  // on screen (the aggregate pages, or a range with no skips in it) would be
  // worse than none. Empty list, so the caller renders nothing.
  assert.deepStrictEqual(timeSeriesLegendItems(false), []);
  assert.deepStrictEqual(timeSeriesLegendItems(), []);
});

run('an empty item list renders no legend markup', () => {
  assert.strictEqual(legendHtml([]), '');
  assert.strictEqual(legendHtml(null), '');
});

run('legendHtml renders a swatch and a name per item', () => {
  const html = legendHtml([{ name: 'Skips (own scale)', color: '#ABCDEF' }]);
  assert.ok(html.includes('chart-legend-swatch'));
  assert.ok(html.includes('background:#ABCDEF'));
  assert.ok(html.includes('Skips (own scale)'));
});

run('legendHtml escapes the series name', () => {
  // Series names reach this from user-visible data (renderMultiLineChart passes
  // artist names straight through), so the shared builder must escape.
  const html = legendHtml([{ name: '<img src=x onerror=1>', color: '#000000' }]);
  assert.ok(!html.includes('<img'));
  assert.ok(html.includes('&lt;img'));
});

// --- escapeHtml ------------------------------------------------------------
// Was a DOM textContent/innerHTML round-trip, which made everything built on
// it (legendHtml above, the tooltip bodies) untestable under plain node.

run('escapeHtml neutralises markup-significant characters', () => {
  assert.strictEqual(escapeHtml('<b>'), '&lt;b&gt;');
  assert.strictEqual(escapeHtml('Simon & Garfunkel'), 'Simon &amp; Garfunkel');
  assert.strictEqual(escapeHtml('say "hi"'), 'say &quot;hi&quot;');
  assert.strictEqual(escapeHtml("it's"), 'it&#39;s');
});

run('escapeHtml escapes the ampersand first, not what it just produced', () => {
  // A naive sequential replace turns "<" into "&lt;" and then mangles its "&".
  assert.strictEqual(escapeHtml('&lt;'), '&amp;lt;');
});

run('escapeHtml leaves plain text alone', () => {
  assert.strictEqual(escapeHtml('Bohemian Rhapsody'), 'Bohemian Rhapsody');
});

run('escapeHtml of nothing is an empty string', () => {
  // The DOM version rendered null/undefined as '' - callers rely on that.
  assert.strictEqual(escapeHtml(null), '');
  assert.strictEqual(escapeHtml(undefined), '');
  assert.strictEqual(escapeHtml(''), '');
});

run('escapeHtml coerces non-strings like textContent did', () => {
  assert.strictEqual(escapeHtml(42), '42');
});

console.log('All chart skip-series tests passed.');
