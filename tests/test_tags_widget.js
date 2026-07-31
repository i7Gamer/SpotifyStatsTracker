// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// Plain-node unit test for the tag widget's response handling
// (static/js/tags.js). No test framework/dependency - run with:
//   node tests/test_tags_widget.js
//
// Pins the fix for silently-swallowed /api/tags failures: the old handlers
// did `if (data.success) { ... }` with no else, so a 401 after session
// expiry, a 400 on a rejected tag, or the admin kill switch's 404 HTML all
// left the widget looking untouched with nothing logged or shown.
const assert = require('assert');
const { tagUpdateOutcome } = require('../static/js/tags.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

const FALLBACK = "Couldn't update tags. Please try again.";

run('a successful update applies the returned tags', () => {
  assert.deepStrictEqual(
    tagUpdateOutcome(true, { success: true, tags: ['workout', 'focus'] }, FALLBACK),
    { apply: true, tags: ['workout', 'focus'] });
});

run('a success body without tags still applies (as empty)', () => {
  assert.deepStrictEqual(
    tagUpdateOutcome(true, { success: true }, FALLBACK),
    { apply: true, tags: [] });
});

run("a 400's server message is what the user sees", () => {
  assert.deepStrictEqual(
    tagUpdateOutcome(false, { error: 'Tag is empty after normalization' }, FALLBACK),
    { apply: false, message: 'Tag is empty after normalization' });
});

run('a JSON error body without a message falls back', () => {
  assert.deepStrictEqual(
    tagUpdateOutcome(false, {}, FALLBACK),
    { apply: false, message: FALLBACK });
});

run("a non-JSON body (the kill switch's 404 HTML page) falls back", () => {
  //< res.json() rejected, so the handler passes data=null
  assert.deepStrictEqual(
    tagUpdateOutcome(false, null, FALLBACK),
    { apply: false, message: FALLBACK });
});

run('a 200 that is not success:true is an error, not a silent no-op', () => {
  assert.deepStrictEqual(
    tagUpdateOutcome(true, { error: 'Failed to add tag' }, FALLBACK),
    { apply: false, message: 'Failed to add tag' });
});

console.log('All tags-widget tests passed.');
