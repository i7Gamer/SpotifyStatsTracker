// Plain-node unit test for the merge review page's main-version picker
// (static/js/merge-review.js). No test framework/dependency - run with:
//   node tests/test_merge_review.js
const assert = require('assert');
const { mergeReviewRowStates } = require('../static/js/merge-review.js');

const ORIGINAL = 'A'.repeat(22);
const REMASTER = 'B'.repeat(22);
const DELUXE = 'C'.repeat(22);

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

console.log('All merge-review tests passed.');
