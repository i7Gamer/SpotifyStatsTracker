// Plain-node unit test for the Top Songs/Artists/Albums two-phase loader's
// pure logic (static/js/top-list.js): which query params a filter change sets
// vs deletes, and whether a settled response still belongs to the newest load.
// No test framework/dependency - run with:
//   node tests/test_top_list.js
//
// These three pages share one module, and the no-preview rule means a
// regression here would otherwise reach the browser unchecked.
const assert = require('assert');
const {
  isCurrentTopListLoad,
  applyTopListFilterParams,
  applyTopListTagParam,
  applyTopListFullPlaysParam,
} = require('../static/js/top-list.js');

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

const BASE_STATE = {
  interval: 'week', sortBy: 'plays', searchQuery: '',
  startDate: '', endDate: '', forceCustom: false,
};

function state(overrides) {
  return Object.assign({}, BASE_STATE, overrides);
}

// --- stale-response guard ---------------------------------------------------

run('a response from the active load is applied', () => {
  const controller = { id: 1 };
  assert.strictEqual(isCurrentTopListLoad({ controller }, controller), true);
});

run('a response from a superseded load is discarded', () => {
  const oldController = { id: 1 };
  const newController = { id: 2 };
  assert.strictEqual(isCurrentTopListLoad({ controller: newController }, oldController), false);
});

run('a response arriving after every load finished is discarded', () => {
  assert.strictEqual(isCurrentTopListLoad(null, { id: 1 }), false);
});

// --- filter params ----------------------------------------------------------

run('a named interval sets it and clears any custom dates', () => {
  const result = applyTopListFilterParams(
    params('interval=custom&startDate=2020-01-01&endDate=2020-01-02'),
    state({ interval: 'month' }));
  assert.strictEqual(result.get('interval'), 'month');
  assert.strictEqual(result.get('startDate'), null);
  assert.strictEqual(result.get('endDate'), null);
});

run('an empty interval means All Time and drops the param entirely', () => {
  const result = applyTopListFilterParams(params('interval=week'), state({ interval: '' }));
  assert.strictEqual(result.get('interval'), null);
});

run('the custom interval carries its dates', () => {
  const result = applyTopListFilterParams(params(''), state({
    interval: 'custom', startDate: '2026-01-01', endDate: '2026-02-01',
  }));
  assert.strictEqual(result.get('interval'), 'custom');
  assert.strictEqual(result.get('startDate'), '2026-01-01');
  assert.strictEqual(result.get('endDate'), '2026-02-01');
});

run('forceCustom switches to a custom range even from a named interval', () => {
  const result = applyTopListFilterParams(params('interval=week'), state({
    interval: 'week', forceCustom: true, startDate: '2026-01-01', endDate: '2026-02-01',
  }));
  assert.strictEqual(result.get('interval'), 'custom');
  assert.strictEqual(result.get('startDate'), '2026-01-01');
});

run('a search query is set, and a blank one is removed', () => {
  const withQuery = applyTopListFilterParams(params(''), state({ searchQuery: 'daft' }));
  assert.strictEqual(withQuery.get('q'), 'daft');

  const cleared = applyTopListFilterParams(params('q=daft'), state({ searchQuery: '' }));
  assert.strictEqual(cleared.get('q'), null);
});

run('a whitespace-only query counts as blank', () => {
  const result = applyTopListFilterParams(params('q=daft'), state({ searchQuery: '   ' }));
  assert.strictEqual(result.get('q'), null);
});

run('sortBy is always written', () => {
  const result = applyTopListFilterParams(params(''), state({ sortBy: 'totalTimeListened' }));
  assert.strictEqual(result.get('sortBy'), 'totalTimeListened');
});

run('any filter change returns to page 1', () => {
  const result = applyTopListFilterParams(params('page=7'), state({}));
  assert.strictEqual(result.get('page'), null);
});

run('an unrelated param is preserved', () => {
  const result = applyTopListFilterParams(params('tag=chill&page=3'), state({}));
  assert.strictEqual(result.get('tag'), 'chill');
});

// --- tag + full-plays params ------------------------------------------------

run('selecting a tag sets it and resets paging', () => {
  const result = applyTopListTagParam(params('page=4'), 'chill');
  assert.strictEqual(result.get('tag'), 'chill');
  assert.strictEqual(result.get('page'), null);
});

run('clearing the tag removes the param', () => {
  const result = applyTopListTagParam(params('tag=chill'), '');
  assert.strictEqual(result.get('tag'), null);
});

run('the full-plays toggle writes both states explicitly', () => {
  assert.strictEqual(applyTopListFullPlaysParam(params(''), true).get('fullOnly'), '1');
  assert.strictEqual(applyTopListFullPlaysParam(params('fullOnly=1'), false).get('fullOnly'), '0');
});

run('toggling full-plays resets paging', () => {
  const result = applyTopListFullPlaysParam(params('page=2'), true);
  assert.strictEqual(result.get('page'), null);
});

console.log('\nAll top-list tests passed.');
