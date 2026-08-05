// Plain-node unit test for the two pure helpers left in
// static/js/history-page.js once htmx owns the /history request layer.
//
// The tag-filter param surgery this file used to cover is gone: htmx serializes
// the filter form, so "which params does a tag change set vs delete" is now
// which controls exist, and tests/test_history_htmx.py covers the resulting
// request end to end. What stayed is the logic htmx has no opinion about.
//
// No test framework/dependency - run with:
//   node tests/test_history_page.js
const assert = require('assert');
const {
  historyRangeProblem,
  pruneEmptyParams,
  RANGE_OK,
  RANGE_INCOMPLETE,
  RANGE_INVERTED,
} = require('../static/js/history-page.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

// --- historyRangeProblem -----------------------------------------------------

run('a named interval never needs dates', () => {
  assert.strictEqual(historyRangeProblem('week', '', ''), RANGE_OK);
  assert.strictEqual(historyRangeProblem('all time', '', ''), RANGE_OK);
});

run('a named interval ignores dates left over from a custom range', () => {
  // The controls keep their values while disabled, so a stale pair must not
  // make an otherwise fine "Last Week" look broken.
  assert.strictEqual(historyRangeProblem('week', '2026-05-01', '2026-01-01'), RANGE_OK);
});

run('a complete, ordered custom range is fine', () => {
  assert.strictEqual(historyRangeProblem('custom', '2026-01-01', '2026-05-01'), RANGE_OK);
});

run('the same day at both ends is a valid range, not an inversion', () => {
  assert.strictEqual(historyRangeProblem('custom', '2026-01-01', '2026-01-01'), RANGE_OK);
});

run('a half-entered custom range is incomplete rather than wrong', () => {
  // What every keystroke of typing a date looks like: no error shown, but no
  // request sent either.
  assert.strictEqual(historyRangeProblem('custom', '2026-01-01', ''), RANGE_INCOMPLETE);
  assert.strictEqual(historyRangeProblem('custom', '', '2026-01-01'), RANGE_INCOMPLETE);
  assert.strictEqual(historyRangeProblem('custom', '', ''), RANGE_INCOMPLETE);
});

run('start after end is reported as inverted', () => {
  assert.strictEqual(historyRangeProblem('custom', '2026-05-01', '2026-01-01'), RANGE_INVERTED);
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

run('a whitespace-only search is a real value, not an empty one', () => {
  // Only the empty string is "untouched"; trimming here would silently disagree
  // with what the server receives and searches on.
  const result = pruneEmptyParams({ q: ' ' });
  assert.strictEqual(result.q, ' ');
});

run('"0" is kept - it is falsy but it is a value', () => {
  const result = pruneEmptyParams({ page: '0' });
  assert.strictEqual(result.page, '0');
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

console.log('All history page tests passed.');
