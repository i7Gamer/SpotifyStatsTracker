// Plain-node unit test for the /history page's pure tag-filter logic
// (static/js/history-page.js): which query params a Tag filter change sets vs
// deletes. Mirrors tests/test_top_list.js for the Top pages' identical
// applyTopListTagParam. No test framework/dependency - run with:
//   node tests/test_history_page.js
const assert = require('assert');
const { applyHistoryTagParam } = require('../static/js/history-page.js');

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

function params(query) {
  return new URLSearchParams(query);
}

run('picking a tag sets it and resets to page 1', () => {
  const result = applyHistoryTagParam(params('page=3'), 'chill');
  assert.strictEqual(result.get('tag'), 'chill');
  assert.strictEqual(result.has('page'), false);
});

run('clearing back to All Tags removes the param', () => {
  const result = applyHistoryTagParam(params('tag=chill&page=2'), '');
  assert.strictEqual(result.has('tag'), false);
  assert.strictEqual(result.has('page'), false);
});

run('an unrelated param survives untouched', () => {
  const result = applyHistoryTagParam(params('q=foo&sort=oldest'), 'workout');
  assert.strictEqual(result.get('tag'), 'workout');
  assert.strictEqual(result.get('q'), 'foo');
  assert.strictEqual(result.get('sort'), 'oldest');
});

console.log('All history page tests passed.');
