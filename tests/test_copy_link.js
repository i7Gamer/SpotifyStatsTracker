// Plain-node unit test for the copy-link JS helper (static/js/copy-link.js).
// The DOM wiring (Clipboard API / execCommand fallback / button flash) needs a
// browser, but the feedback-text decision is a pure function we can check here.
// Run with: node tests/test_copy_link.js
const assert = require('assert');
const { copyFeedbackText, COPIED_TEXT, FAILED_TEXT } = require('../static/js/copy-link.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

run('reports success as "Copied!"', () => {
  assert.strictEqual(copyFeedbackText(true), COPIED_TEXT);
  assert.strictEqual(COPIED_TEXT, 'Copied!');
});

run('reports failure as "Copy failed"', () => {
  assert.strictEqual(copyFeedbackText(false), FAILED_TEXT);
  assert.strictEqual(FAILED_TEXT, 'Copy failed');
});

console.log('all copy-link tests passed');
