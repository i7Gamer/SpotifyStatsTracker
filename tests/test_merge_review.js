// Plain-node unit test for the merge review page's main-version picker
// (static/js/merge-review.js). No test framework/dependency - run with:
//   node tests/test_merge_review.js
const assert = require('assert');
// `document` is deliberately left undefined here, so requiring the file does
// not run its own initMergeReview() against a DOM that does not exist.
const { mergeReviewRowStates, applyMergeReviewGroup } = require('../static/js/merge-review.js');

const ORIGINAL = 'A'.repeat(22);
const REMASTER = 'B'.repeat(22);
const DELUXE = 'C'.repeat(22);

// Enough of an element to answer the four questions the wiring asks. Each row
// holds TWO canonical inputs - the merge form's and the reject form's, since
// a "no" records what it was ruled against - which is the whole reason this
// is exercised against a tree rather than only through the pure function.
function makeRow(trackId, isMain) {
  const badge = { hidden: !isMain };
  const actions = { hidden: isMain };
  const canonicals = [{ value: '' }, { value: '' }];
  return {
    dataset: { trackId },
    badge,
    actions,
    canonicals,
    querySelector(selector) {
      if (selector === '[data-merge-main-badge]') return badge;
      if (selector === '[data-merge-actions]') return actions;
      return null;
    },
    querySelectorAll(selector) {
      return selector === 'input[name="canonical"]' ? canonicals : [];
    },
  };
}

function makeGroup(rows, checkedId) {
  return {
    querySelector(selector) {
      if (selector === '[data-merge-main-radio]:checked') {
        return checkedId ? { value: checkedId } : null;
      }
      return null;
    },
    querySelectorAll(selector) {
      return selector === '[data-merge-release]' ? rows : [];
    },
  };
}

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('the picked release is the main one and every other row aims at it', () => {
  assert.deepStrictEqual(
    mergeReviewRowStates([ORIGINAL, REMASTER, DELUXE], ORIGINAL),
    [
      { trackId: ORIGINAL, isMain: true, canonical: ORIGINAL },
      { trackId: REMASTER, isMain: false, canonical: ORIGINAL },
      { trackId: DELUXE, isMain: false, canonical: ORIGINAL },
    ],
  );
});

run('picking another release re-aims every row, the old main included', () => {
  const states = mergeReviewRowStates([ORIGINAL, REMASTER, DELUXE], REMASTER);

  assert.deepStrictEqual(states.map((state) => state.isMain), [false, true, false]);
  // the point of the control: the release that WAS being kept is now an
  // ordinary member, and its merge form has to post the new canonical
  assert.deepStrictEqual(states.map((state) => state.canonical),
    [REMASTER, REMASTER, REMASTER]);
});

run('a selection naming no row falls back to the first - the server suggestion', () => {
  // Nothing on the page can produce this; it is the shape a stale radio value
  // or a hand-edited DOM would, and silently merging into an id that is not
  // in the group is the one outcome worth ruling out.
  const states = mergeReviewRowStates([ORIGINAL, REMASTER], 'Z'.repeat(22));

  assert.deepStrictEqual(states.map((state) => state.isMain), [true, false]);
  assert.strictEqual(states[1].canonical, ORIGINAL);
});

run('no selection at all also falls back to the first', () => {
  assert.strictEqual(mergeReviewRowStates([ORIGINAL, REMASTER], null)[0].isMain, true);
});

run('an empty group produces no rows instead of throwing', () => {
  assert.deepStrictEqual(mergeReviewRowStates([], null), []);
});

run('applying a pick moves the badge and the verdict buttons', () => {
  const rows = [makeRow(ORIGINAL, true), makeRow(REMASTER, false)];

  applyMergeReviewGroup(makeGroup(rows, REMASTER));

  assert.deepStrictEqual(rows.map((row) => row.badge.hidden), [true, false]);
  assert.deepStrictEqual(rows.map((row) => row.actions.hidden), [false, true]);
});

run('applying a pick re-aims EVERY canonical field, both forms in each row', () => {
  // The reject form grew one of these when a "no" started recording what it
  // was ruled against. A single-element lookup here left it on the release
  // the server rendered, so an overruled pick wrote the wrong counterpart -
  // silently, since nothing on screen shows a hidden field.
  const rows = [makeRow(ORIGINAL, true), makeRow(REMASTER, false), makeRow(DELUXE, false)];

  applyMergeReviewGroup(makeGroup(rows, DELUXE));

  for (const row of rows) {
    assert.deepStrictEqual(row.canonicals.map((input) => input.value), [DELUXE, DELUXE]);
  }
});

run('a group whose radios were all somehow cleared falls back to the first row', () => {
  const rows = [makeRow(ORIGINAL, true), makeRow(REMASTER, false)];

  applyMergeReviewGroup(makeGroup(rows, null));

  assert.strictEqual(rows[0].badge.hidden, false);
  assert.strictEqual(rows[1].canonicals[0].value, ORIGINAL);
});

console.log('All merge-review tests passed.');
