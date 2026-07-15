// Plain-node unit test for the back-button referrer resolution logic
// (static/js/back-button.js). No test framework/dependency - run with:
//   node tests/test_back_button.js
const assert = require('assert');
const { resolveBackTarget } = require('../static/js/back-button.js');

const ORIGIN = 'http://localhost:5000';

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('empty referrer keeps server-rendered default', () => {
  assert.strictEqual(resolveBackTarget('', ORIGIN), null);
});

run('cross-origin referrer keeps server-rendered default', () => {
  assert.strictEqual(resolveBackTarget('https://google.com/search?q=x', ORIGIN), null);
});

run('unparseable referrer keeps server-rendered default', () => {
  assert.strictEqual(resolveBackTarget('not a url', ORIGIN), null);
});

run('dashboard root referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/`, ORIGIN), { label: '← Back to Dashboard' });
});

run('wrapped referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/wrapped`, ORIGIN), { label: '← Back to Wrapped' });
});

run('top-songs referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/top-songs`, ORIGIN), { label: '← Back to Top Songs' });
});

run('top-albums referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/top-albums`, ORIGIN), { label: '← Back to Top Albums' });
});

run('top-artists referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/top-artists`, ORIGIN), { label: '← Back to Top Artists' });
});

run('song detail referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/song/abc123`, ORIGIN), { label: '← Back to Song' });
});

run('album detail referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/album/abc123`, ORIGIN), { label: '← Back to Album' });
});

run('artist detail referrer', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/artist/abc123`, ORIGIN), { label: '← Back to Artist' });
});

run('unrecognized same-origin path still allows history.back(), but keeps default label', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/charts`, ORIGIN), { label: null });
});

run('regression: dashboard search query containing a reserved word is not misread as that page', () => {
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/?q=wrapped`, ORIGIN), { label: '← Back to Dashboard' });
  assert.deepStrictEqual(resolveBackTarget(`${ORIGIN}/?q=top-albums&page=2`, ORIGIN), { label: '← Back to Dashboard' });
});

console.log('All back-button tests passed.');
