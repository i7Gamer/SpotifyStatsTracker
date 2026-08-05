// Plain-node unit test for static/js/htmx-filters.js - the filter logic shared
// by every htmx-driven list page (/history and the three Top lists).
//
// These assertions started life in tests/test_history_page.js and moved here
// with the code when the Top lists became the second caller: the point of one
// shared helper is that both pages agree on "is this date range worth a
// request", so the test that pins that answer belongs to the helper, not to a
// page.
//
// No test framework/dependency - run with:
//   node tests/test_htmx_filters.js
const assert = require('assert');
const {
  rangeProblem,
  pruneEmptyParams,
  isNativeModifierClick,
  RANGE_OK,
  RANGE_INCOMPLETE,
  RANGE_INVERTED,
} = require('../static/js/htmx-filters.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

// --- rangeProblem ------------------------------------------------------------

run('a named interval never needs dates', () => {
  assert.strictEqual(rangeProblem('week', '', ''), RANGE_OK);
  assert.strictEqual(rangeProblem('all time', '', ''), RANGE_OK);
});

run('the Top lists\' empty-string All Time is a named interval too', () => {
  // /history spells All Time "all time"; the Top pages spell it "". Both must
  // reach the same answer, or one of the two pages blocks every request.
  assert.strictEqual(rangeProblem('', '', ''), RANGE_OK);
});

run('a named interval ignores dates left over from a custom range', () => {
  // The controls keep their values while disabled, so a stale pair must not
  // make an otherwise fine "Last Week" look broken.
  assert.strictEqual(rangeProblem('week', '2026-05-01', '2026-01-01'), RANGE_OK);
});

run('a complete, ordered custom range is fine', () => {
  assert.strictEqual(rangeProblem('custom', '2026-01-01', '2026-05-01'), RANGE_OK);
});

run('the same day at both ends is a valid range, not an inversion', () => {
  assert.strictEqual(rangeProblem('custom', '2026-01-01', '2026-01-01'), RANGE_OK);
});

run('a half-entered custom range is incomplete rather than wrong', () => {
  // What every keystroke of typing a date looks like: no error shown, but no
  // request sent either.
  assert.strictEqual(rangeProblem('custom', '2026-01-01', ''), RANGE_INCOMPLETE);
  assert.strictEqual(rangeProblem('custom', '', '2026-01-01'), RANGE_INCOMPLETE);
  assert.strictEqual(rangeProblem('custom', '', ''), RANGE_INCOMPLETE);
});

run('start after end is reported as inverted', () => {
  assert.strictEqual(rangeProblem('custom', '2026-05-01', '2026-01-01'), RANGE_INVERTED);
});

run('incomplete and inverted are distinguishable', () => {
  // Both block the request; only one gets an error message, so collapsing them
  // to a single falsy "not ok" would either shout at every keystroke or stay
  // silent on a genuinely wrong range.
  assert.notStrictEqual(RANGE_INCOMPLETE, RANGE_INVERTED);
  assert.notStrictEqual(RANGE_INCOMPLETE, RANGE_OK);
  assert.notStrictEqual(RANGE_INVERTED, RANGE_OK);
});

// --- pruneEmptyParams --------------------------------------------------------

run('untouched controls are dropped from the request', () => {
  const result = pruneEmptyParams({ q: '', interval: 'week', tag: '', sort: '' });
  assert.deepStrictEqual(Object.keys(result).sort(), ['interval']);
});

run('a set value survives', () => {
  const result = pruneEmptyParams({ q: 'radiohead', interval: 'week', tag: 'chill' });
  assert.strictEqual(result.q, 'radiohead');
  assert.strictEqual(result.tag, 'chill');
  assert.strictEqual(result.interval, 'week');
});

run('a whitespace-only search counts as untouched and is dropped', () => {
  // Relocated from the retired tests/test_top_list.js, which pinned this for
  // applyTopListFilterParams: both pages' hand-written param code tested
  // `searchQuery.trim()`, so letting spaces through here would be a regression
  // - `?q=%20%20` in the address bar and a LIKE '% %' across all history.
  assert.deepStrictEqual(pruneEmptyParams({ q: '   ' }), {});
  assert.deepStrictEqual(pruneEmptyParams({ q: '\t\n' }), {});
});

run('a real query keeps the spaces the user typed around it', () => {
  // Blank-ness decides whether to send it; it does not rewrite the value.
  const result = pruneEmptyParams({ q: ' daft punk ' });
  assert.strictEqual(result.q, ' daft punk ');
});

run('"0" is kept - it is falsy but it is a value', () => {
  // The Top lists' "Full plays only" opt-out is exactly this: fullOnly=0 must
  // reach the route, because its absence means the OPPOSITE (the default is on).
  const result = pruneEmptyParams({ fullOnly: '0' });
  assert.strictEqual(result.fullOnly, '0');
});

run('the object is mutated in place, as htmx requires', () => {
  // htmx reads evt.detail.parameters back off the same object after the event,
  // so returning a fresh copy would prune nothing.
  const params = { q: '', interval: 'week' };
  const returned = pruneEmptyParams(params);
  assert.strictEqual(returned, params);
  assert.strictEqual('q' in params, false);
});

run('an already-clean object is left alone', () => {
  const result = pruneEmptyParams({ interval: 'week' });
  assert.deepStrictEqual(result, { interval: 'week' });
});

// --- isNativeModifierClick ---------------------------------------------------
// The regression this exists for: every delegated pagination handler hx-boost
// replaced exempted all four modifiers, and htmx's boost exempts only two.

run('shift-click and alt-click belong to the browser', () => {
  // htmx would otherwise swallow these - shift opens a new window, alt
  // downloads, and boost has no opinion about either.
  assert.strictEqual(isNativeModifierClick({ shiftKey: true }), true);
  assert.strictEqual(isNativeModifierClick({ altKey: true }), true);
});

run('an unmodified click is htmx\'s to handle', () => {
  assert.strictEqual(isNativeModifierClick({}), false);
});

run('ctrl and meta are not claimed here - htmx already passes those through', () => {
  // Listing them would read like this function is what makes new-tab work,
  // and someone would later "fix" htmx's own check by deleting it.
  assert.strictEqual(isNativeModifierClick({ ctrlKey: true }), false);
  assert.strictEqual(isNativeModifierClick({ metaKey: true }), false);
});

run('a missing event is not a modifier click', () => {
  assert.strictEqual(isNativeModifierClick(null), false);
  assert.strictEqual(isNativeModifierClick(undefined), false);
});

console.log('All htmx filter tests passed.');
