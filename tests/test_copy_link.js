// Plain-node unit test for the copy-link JS helper (static/js/copy-link.js).
// The DOM wiring (Clipboard API / execCommand fallback / button flash) needs a
// browser, but the feedback-text decision is a pure function we can check here.
// Run with: node tests/test_copy_link.js
const assert = require('assert');
const { copyFeedbackText, flash, COPIED_TEXT, FAILED_TEXT } = require('../static/js/copy-link.js');

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

// --- flash (UT-4d) ------------------------------------------------------------
// flash() only ever touches the button object it's handed - no document/window
// reads - so it's testable in plain node without a DOM stub.

function makeButton(initialText) {
  return {
    textContent: initialText, dataset: {}, _attrs: {},
    setAttribute(name, value) { this._attrs[name] = value; },
  };
}

run('the flash text is announced: the button gets role="status"', () => {
  const button = makeButton('Copy');

  flash(button, 'Copied!');

  assert.strictEqual(button._attrs.role, 'status',
                     'the button doubles as the flash element - a silent label swap is not enough');
  assert.strictEqual(button.textContent, 'Copied!');

  clearTimeout(button._copyTimer);   //< don't hold the process open for the restore timer
});

console.log('all copy-link tests passed');
