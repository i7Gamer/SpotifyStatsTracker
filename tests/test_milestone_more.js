// Plain-node unit test for the Milestones "Show N more" chunked-reveal state
// logic (static/js/milestone-more.js). No test framework/dependency - run with:
//   node tests/test_milestone_more.js
const assert = require('assert');
const { milestoneRevealState } = require('../static/js/milestone-more.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

const CHUNK = 5;

run('a single milestone leaves nothing to reveal and hides the button', () => {
  assert.deepStrictEqual(milestoneRevealState(1, 1, CHUNK), {
    visible: 1, moreHidden: true, label: '',
  });
});

run('the first reveal offers a full chunk', () => {
  assert.deepStrictEqual(milestoneRevealState(1, 12, CHUNK), {
    visible: 1, moreHidden: false, label: 'Show 5 more',
  });
});

run('mid-way through, full chunks are still offered', () => {
  assert.deepStrictEqual(milestoneRevealState(6, 12, CHUNK), {
    visible: 6, moreHidden: false, label: 'Show 5 more',
  });
});

run('the final chunk is labelled with the true remainder, not the chunk size', () => {
  assert.deepStrictEqual(milestoneRevealState(11, 12, CHUNK), {
    visible: 11, moreHidden: false, label: 'Show 1 more',
  });
});

run('once everything is visible the button disappears', () => {
  assert.deepStrictEqual(milestoneRevealState(12, 12, CHUNK), {
    visible: 12, moreHidden: true, label: '',
  });
});

run('overshooting the total clamps to it and hides the button', () => {
  assert.deepStrictEqual(milestoneRevealState(16, 12, CHUNK), {
    visible: 12, moreHidden: true, label: '',
  });
});

run('a sub-chunk remainder is fully revealed by a single click', () => {
  // 3 total, 1 shown -> "Show 2 more" -> one click (1 + chunk) reveals the rest.
  assert.deepStrictEqual(milestoneRevealState(1, 3, CHUNK), {
    visible: 1, moreHidden: false, label: 'Show 2 more',
  });
  assert.deepStrictEqual(milestoneRevealState(1 + CHUNK, 3, CHUNK), {
    visible: 3, moreHidden: true, label: '',
  });
});

console.log('All milestone-more tests passed.');
