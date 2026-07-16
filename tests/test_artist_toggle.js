// Plain-node unit test for the artist-list "+N more" toggle state logic
// (static/js/artist-toggle.js). No test framework/dependency - run with:
//   node tests/test_artist_toggle.js
const assert = require('assert');
const { nextArtistToggleState } = require('../static/js/artist-toggle.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('expanding reveals the overflow and relabels to "Show less"', () => {
  assert.deepStrictEqual(nextArtistToggleState(false, 3), {
    expanded: true,
    label: 'Show less',
    overflowHidden: false,
  });
});

run('collapsing hides the overflow and restores the "+N more" label', () => {
  assert.deepStrictEqual(nextArtistToggleState(true, 3), {
    expanded: false,
    label: '+3 more',
    overflowHidden: true,
  });
});

run('collapsed label reflects the actual hidden count', () => {
  assert.strictEqual(nextArtistToggleState(true, 2).label, '+2 more');
  assert.strictEqual(nextArtistToggleState(true, 12).label, '+12 more');
});

run('toggling twice returns to the initial state', () => {
  const once = nextArtistToggleState(false, 4);
  const twice = nextArtistToggleState(once.expanded, 4);
  assert.deepStrictEqual(twice, {
    expanded: false,
    label: '+4 more',
    overflowHidden: true,
  });
});

console.log('All artist-toggle tests passed.');
