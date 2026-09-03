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

// --- flash (UT-4d, revised 2026-09-03 review L3) ------------------------------
// flash() writes the visible label on the button and the announced text into a
// hidden live region beside it. role="status" used to go on the BUTTON and was
// never taken off, so from the first click the control stayed exposed as a
// status region rather than a button, for the life of the page - and a node
// that becomes a live region and gets its text in the same tick is ignored by
// several screen readers anyway.

function makeRegion() {
  return {
    //< what _share_link_panel.html renders: the region arrives already a
    //  live region, which is the whole point of not creating it on click
    textContent: '', className: 'visually-hidden',
    _attrs: { 'data-copy-status': '', role: 'status', 'aria-live': 'polite' },
    setAttribute(name, value) { this._attrs[name] = value; },
    hasAttribute(name) { return this._attrs[name] !== undefined; },
  };
}

function makeButton(initialText, options) {
  const button = {
    textContent: initialText, dataset: {}, _attrs: {},
    setAttribute(name, value) { this._attrs[name] = value; },
    nextElementSibling: null, nextSibling: null, parentNode: null,
  };
  if (options && options.withRegion) {
    button.nextElementSibling = makeRegion();
  }
  if (options && options.inDom) {
    const inserted = [];
    button.parentNode = { inserted, insertBefore(node) { inserted.push(node); } };
  }
  return button;
}

run('the flash text is announced through the region beside the button', () => {
  const button = makeButton('Copy', { withRegion: true });

  flash(button, 'Copied!');

  assert.strictEqual(button.nextElementSibling.textContent, 'Copied!',
                     'the pre-existing live region is what carries the announcement');
  assert.strictEqual(button.textContent, 'Copied!', 'and the button still shows it');

  clearTimeout(button._copyTimer);   //< don't hold the process open for the restore timer
});

run('the button is never turned into a status region', () => {
  const button = makeButton('Copy', { withRegion: true });

  flash(button, 'Copied!');

  assert.strictEqual(button._attrs.role, undefined,
                     'a Copy button must stay a button after it has been clicked');

  clearTimeout(button._copyTimer);
});

run('restoring the label empties the region but keeps it', () => {
  const button = makeButton('Copy', { withRegion: true });
  const region = button.nextElementSibling;
  const realSetTimeout = global.setTimeout;
  let restore = null;
  global.setTimeout = (fn) => { restore = fn; return 0; };

  try {
    flash(button, 'Copied!');
    restore();
  } finally {
    global.setTimeout = realSetTimeout;
  }

  assert.strictEqual(button.textContent, 'Copy');
  assert.strictEqual(region.textContent, '',
                     'the region outlives the message so the NEXT copy reads as a change');
  assert.strictEqual(region._attrs.role, 'status', 'and stays a live region');
});

run('a button with no region beside it gets one, once', () => {
  //< the fallback for any caller that does not render its own
  const button = makeButton('Copy', { inDom: true });
  //< a BARE element, the way document.createElement hands one over: the
  //  attributes below are the ones liveRegionFor is expected to set itself
  global.document = { createElement: () => ({
    textContent: '', className: '', _attrs: {},
    setAttribute(name, value) { this._attrs[name] = value; },
    hasAttribute(name) { return this._attrs[name] !== undefined; },
  }) };

  try {
    flash(button, 'Copied!');
    clearTimeout(button._copyTimer);
    flash(button, 'Copy failed');
    clearTimeout(button._copyTimer);
  } finally {
    delete global.document;
  }

  assert.strictEqual(button.parentNode.inserted.length, 1,
                     'a second click must reuse the region, not stack another');
  const region = button.parentNode.inserted[0];
  assert.strictEqual(region._attrs.role, 'status');
  assert.strictEqual(region._attrs['aria-live'], 'polite');
  assert.strictEqual(region.textContent, 'Copy failed');
});

run('a detached button still flashes rather than throwing', () => {
  //< no parent, no sibling: the visible half must survive on its own
  const button = makeButton('Copy');

  flash(button, 'Copied!');

  assert.strictEqual(button.textContent, 'Copied!');
  clearTimeout(button._copyTimer);
});

console.log('all copy-link tests passed');
