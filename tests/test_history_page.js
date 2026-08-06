// Plain-node unit test for the /history browser logic (static/js/history-page.js).
// Run with: node tests/test_history_page.js
//
// The sibling of tests/test_top_list_page.js: these two files are near-twins by
// design (both are what survived the htmx migration), and they are tested
// separately rather than through a shared harness precisely because a change
// applied to one and not the other is the failure worth catching.
//
// What is unique to this page is the Date sort toggle. It has no natural form
// control - it is a button driving a hidden field - so all three of "flip the
// value", "relabel the button" and "tell the form to re-fire" are hand-written,
// and a toggle that flips the value without re-firing looks exactly like a
// toggle that works until you notice the list never changed.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'history-page.js');
const RANGE_OK = null;                //< see tests/test_top_list_page.js - null, not a string
const RANGE_INCOMPLETE = 'incomplete';

function makeField(value) {
  return { value: value === undefined ? '' : value, textContent: '' };
}

function makeForm() {
  const dispatched = [];
  return { dispatched, dispatchEvent(evt) { dispatched.push(evt.type); return true; } };
}

function loadHistory(options) {
  options = options || {};
  const calls = {
    replaced: [], pushed: [], ajax: [], syncedRanges: [], shownErrors: [],
    pruned: [], swapFailure: null, bodyListeners: {},
  };
  const elements = options.elements || {};

  global.window = {
    location: { pathname: '/history', search: options.search || '' },
    history: {
      replaceState(state, title, url) { calls.replaced.push(url); },
      pushState(state, title, url) { calls.pushed.push(url); },
    },
  };
  global.document = {
    getElementById(id) { return elements[id] || null; },
    body: { addEventListener(type, fn) { calls.bodyListeners[type] = fn; } },
  };
  global.htmx = { ajax(method, url, opts) { calls.ajax.push({ method, url, opts }); } };
  global.HtmxFilters = {
    RANGE_OK,
    syncCustomRange(containerId) { calls.syncedRanges.push(containerId); },
    rangeProblemFromDom() { return options.rangeProblem === undefined ? RANGE_OK : options.rangeProblem; },
    showRangeError(problem) { calls.shownErrors.push(problem); },
    pruneEmptyParams(parameters) { calls.pruned.push(parameters); },
    onSwapFailure(targetId, retry) { calls.swapFailure = { targetId, retry }; },
  };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  return calls;
}

function sortElements(currentValue) {
  const field = makeField(currentValue);
  const button = makeField();
  const form = makeForm();
  return {
    elements: { historySortValue: field, historySort: button, historyFilters: form },
    field, button, form,
  };
}

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ---------------------------------------------------------- the sort toggle

run('the default (newest first) flips to oldest and relabels the button', () => {
  const dom = sortElements('');
  const page = loadHistory({ elements: dom.elements });

  page.window.updateHistorySort();

  assert.strictEqual(dom.field.value, 'oldest');
  assert.strictEqual(dom.button.textContent, 'Date ↑');
});

run('flipping back clears the field rather than writing a second spelling', () => {
  const dom = sortElements('oldest');
  const page = loadHistory({ elements: dom.elements });

  page.window.updateHistorySort();

  assert.strictEqual(dom.field.value, '', 'the default is the ABSENCE of the param');
  assert.strictEqual(dom.button.textContent, 'Date ↓');
});

run('flipping the sort re-fires the form, or the list never changes', () => {
  const dom = sortElements('');
  const page = loadHistory({ elements: dom.elements });

  page.window.updateHistorySort();

  assert.deepStrictEqual(dom.form.dispatched, ['historyRefresh']);
});

run('two flips return to exactly where they started', () => {
  const dom = sortElements('');
  const page = loadHistory({ elements: dom.elements });

  page.window.updateHistorySort();
  page.window.updateHistorySort();

  assert.strictEqual(dom.field.value, '');
  assert.strictEqual(dom.button.textContent, 'Date ↓');
  assert.deepStrictEqual(dom.form.dispatched, ['historyRefresh', 'historyRefresh']);
});

// ------------------------------------------------------------ jump to page

run('a page jump replaces the history entry and never pushes one', () => {
  const page = loadHistory({ search: '?interval=last-7-days' });

  page.window.__paginationAjaxHandler(3);

  assert.strictEqual(page.replaced.length, 1);
  assert.deepStrictEqual(page.pushed, []);
});

run('a page jump keeps the other filters and targets the history list', () => {
  const page = loadHistory({ search: '?q=liquid&page=9' });

  page.window.__paginationAjaxHandler(2);

  const url = new URL(page.replaced[0], 'http://localhost');
  assert.strictEqual(url.searchParams.get('page'), '2');
  assert.strictEqual(url.searchParams.get('q'), 'liquid');
  assert.strictEqual(page.ajax[0].opts.target, '#historyResults');
});

// -------------------------------------------------------- the request veto

run('an incomplete custom range stops the form request and says why', () => {
  const page = loadHistory({ rangeProblem: RANGE_INCOMPLETE });

  const evt = {
    detail: { elt: { id: 'historyFilters' }, parameters: {} },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  page.bodyListeners['htmx:configRequest'](evt);

  assert.strictEqual(evt.prevented, 1);
  assert.deepStrictEqual(page.shownErrors, [RANGE_INCOMPLETE]);
  assert.deepStrictEqual(page.pruned, []);
});

run('a valid range lets the request through and prunes its empty params', () => {
  const page = loadHistory({ rangeProblem: RANGE_OK });
  const parameters = { q: '', interval: '' };

  const evt = {
    detail: { elt: { id: 'historyFilters' }, parameters },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  page.bodyListeners['htmx:configRequest'](evt);

  assert.strictEqual(evt.prevented, 0);
  assert.deepStrictEqual(page.pruned, [parameters]);
});

run('a boosted pagination link is never vetoed, even mid-typo', () => {
  const page = loadHistory({ rangeProblem: RANGE_INCOMPLETE });

  const evt = {
    detail: { elt: { id: 'paginationLink' }, parameters: {} },
    prevented: 0,
    preventDefault() { this.prevented += 1; },
  };
  page.bodyListeners['htmx:configRequest'](evt);

  assert.strictEqual(evt.prevented, 0);
  assert.deepStrictEqual(page.shownErrors, []);
});

// ------------------------------------------------------- interval + retry

run('the Time Period select syncs the history custom-range container', () => {
  const page = loadHistory({});

  page.window.updateHistoryInterval();

  assert.deepStrictEqual(page.syncedRanges, ['historyCustomDates'],
                         'the container id differs from the Top pages - a copy-paste would swap them');
});

run('a failed swap offers a retry that re-requests the current URL', () => {
  const page = loadHistory({ search: '?q=liquid' });
  assert.strictEqual(page.swapFailure.targetId, 'historyResults');

  page.swapFailure.retry();

  assert.strictEqual(page.ajax[0].url, '/history?q=liquid');
  assert.strictEqual(page.ajax[0].opts.target, '#historyResults');
});

(async () => {
  for (const { name, fn } of results) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      console.error(`FAIL - ${name}`);
      console.error(err);
      process.exit(1);
    }
  }
  console.log(`all ${results.length} history-page tests passed`);
})();
