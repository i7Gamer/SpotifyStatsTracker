// Request-event tests using the real page handlers, without a browser preview.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadPage() {
  const handlers = {};
  const form = { id: 'compareFilters', getAttribute: () => '/compare' };
  let problem = 'ok';
  const context = {
    window: { location: { pathname: '/compare' }, AjaxStatus: { showBanner() {}, clearBanner() {} } },
    document: {
      body: { addEventListener: (name, handler) => { handlers[name] = handler; } },
      getElementById: (id) => id === form.id ? form : null,
      querySelectorAll: () => [], querySelector: () => null,
    },
    HtmxFilters: { RANGE_OK: 'ok', rangeProblemFromDom: () => problem, showRangeError() {} },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/js/compare.js'), 'utf8'), context);
  return {
    handlers,
    request(id, rangeProblem = 'ok') {
      problem = rangeProblem;
      const event = {
        prevented: false,
        detail: {
          elt: id === form.id ? form : { id },
          path: id === 'sortBy' ? '/compare?scope=sortable' : '/compare',
          parameters: { interval: 'custom', startDate: '2026-08-01', endDate: '2026-08-31',
            with: 'carol', groupBy: '', sortBy: 'time', ...(id === 'sortBy' ? { scope: 'sortable' } : {}) },
        },
        preventDefault() { this.prevented = true; },
      };
      handlers['htmx:configRequest'](event);
      return event;
    },
  };
}

for (const origin of ['compareFilters', 'sortBy']) {
  for (const problem of ['missing', 'inverted']) {
    assert.strictEqual(loadPage().request(origin, problem).prevented, true, `${origin} must reject ${problem} dates`);
  }
}
const badge = loadPage().request('counterpartBadge', 'missing');
assert.strictEqual(badge.prevented, false, 'counterpart navigation must bypass the local date guard');
assert.strictEqual(badge.detail.parameters.groupBy, '', 'badge parameters are untouched');

const normal = loadPage().request('sortBy');
assert.strictEqual(normal.prevented, false);
assert.strictEqual(normal.detail.path, '/compare?scope=sortable', 'healthy sorts retain the cheap endpoint');
assert.strictEqual(normal.detail.parameters.scope, 'sortable');
assert.strictEqual(normal.detail.parameters.groupBy, undefined);
assert.strictEqual(normal.detail.parameters.sortBy, 'time');
assert.strictEqual(normal.detail.parameters.interval, 'custom');

const page = loadPage();
const full = page.request('compareFilters');
page.handlers['htmx:responseError']({ detail: { requestConfig: full.detail } });
const recovery = page.request('sortBy');
assert.strictEqual(recovery.detail.path, '/compare', 'sorting after a failed valid-date refresh must refresh every region');
assert.strictEqual(recovery.detail.parameters.scope, undefined);
assert.strictEqual(recovery.detail.parameters.with, 'carol');
page.handlers['htmx:afterRequest']({ detail: { successful: false, requestConfig: recovery.detail } });
assert.strictEqual(page.request('sortBy').detail.path, '/compare', 'failed recovery must not acknowledge a full render');
page.handlers['htmx:afterRequest']({ detail: { successful: true, requestConfig: recovery.detail } });
assert.strictEqual(page.request('sortBy').detail.path, '/compare?scope=sortable', 'successful recovery restores cheap sorting');

const pending = loadPage();
pending.request('compareFilters');
assert.strictEqual(pending.request('sortBy').detail.path, '/compare', 'a sort replacing an in-flight full request must remain full');
console.log('All Compare request guard and recovery tests passed.');
