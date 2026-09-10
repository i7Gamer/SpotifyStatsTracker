// Request-event tests using the real page handlers, without a browser preview.
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

function loadPage() {
  const handlers = {};
  const form = { id: 'compareFilters', tagName: 'FORM', getAttribute: () => '/compare' };
  const banner = { visible: false, clears: 0, shows: 0, retry: null };
  let problem = 'ok';
  let page;
  const context = {
    window: { location: { pathname: '/compare' }, AjaxStatus: {
      showBanner(retry) { banner.visible = true; banner.shows++; banner.retry = retry; },
      clearBanner() { banner.visible = false; banner.clears++; },
    } },
    document: {
      body: { addEventListener: (name, handler) => { handlers[name] = handler; } },
      getElementById: (id) => id === form.id ? form : null,
      querySelectorAll: () => [], querySelector: () => null,
    },
    HtmxFilters: { RANGE_OK: 'ok', rangeProblemFromDom: () => problem, showRangeError() {} },
    htmx: { ajax(method, url, options) {
      assert.strictEqual(method, 'GET');
      assert.strictEqual(url, '/compare');
      assert.strictEqual(options.source, form);
      page.retryRequest = page.request('compareFilters');
    } },
  };
  vm.runInNewContext(fs.readFileSync(path.join(__dirname, '../static/js/compare.js'), 'utf8'), context);
  page = {
    handlers, banner,
    request(id, rangeProblem = 'ok') {
      problem = rangeProblem;
      const event = {
        prevented: false,
        detail: {
          elt: id === form.id ? form : { id, tagName: id === 'compareUserBadge2' ? 'A' : 'P',
            closest: (selector) => id === 'compareUserBadge2' && selector === '#compareUserBadges' ? {} : null },
          path: id === 'sortBy' ? '/compare?scope=sortable' : '/compare',
          parameters: { interval: 'custom', startDate: '2026-08-01', endDate: '2026-08-31',
            with: 'carol', groupBy: '', sortBy: 'totalTimeListened' },
        },
        preventDefault() { this.prevented = true; },
      };
      handlers['htmx:configRequest'](event);
      return event;
    },
    finish(request, successful, detached = false) {
      if (detached) request.detail.elt.closest = () => null;
      handlers['htmx:afterRequest']({ target: { id: detached ? 'main-content' : request.detail.elt.id },
        detail: { successful, requestConfig: request.detail } });
    },
    fail(request, eventType = 'htmx:responseError') {
      handlers[eventType]({ detail: { requestConfig: request.detail } });
      this.finish(request, false);
    },
    clickRetry() {
      // AjaxStatus's button clears its banner before invoking the callback.
      context.window.AjaxStatus.clearBanner();
      banner.retry();
    },
  };
  return page;
}

for (const origin of ['compareFilters', 'sortBy']) {
  for (const problem of ['missing', 'inverted']) {
    assert.strictEqual(loadPage().request(origin, problem).prevented, true, `${origin} must reject ${problem} dates`);
  }
}
const badge = loadPage().request('compareUserBadge2', 'missing');
assert.strictEqual(badge.prevented, false, 'counterpart navigation must bypass the local date guard');
assert.strictEqual(badge.detail.parameters.groupBy, '', 'badge parameters are untouched');
assert.strictEqual(badge.detail.compareFullRefresh, true, 'real counterpart links are full requests');

const healthy = loadPage();
healthy.finish(healthy.request('compareInitialLoad'), true, true);
const normal = healthy.request('sortBy');
assert.strictEqual(normal.prevented, false);
assert.strictEqual(normal.detail.path, '/compare?scope=sortable', 'healthy sorts retain the cheap endpoint');
assert.strictEqual(normal.detail.compareFullRefresh, false);
assert.strictEqual(normal.detail.parameters.groupBy, undefined);
assert.strictEqual(normal.detail.parameters.sortBy, 'totalTimeListened');
assert.strictEqual(normal.detail.parameters.interval, 'custom');

const page = loadPage();
const full = page.request('compareFilters');
page.handlers['htmx:responseError']({ detail: { requestConfig: full.detail } });
const recovery = page.request('sortBy');
assert.strictEqual(recovery.detail.path, '/compare', 'sorting after a failed valid-date refresh must refresh every region');
assert.strictEqual(recovery.detail.compareFullRefresh, true);
assert.strictEqual(recovery.detail.parameters.with, 'carol');
assert.strictEqual(recovery.detail.parameters.startDate, '2026-08-01');
assert.strictEqual(recovery.detail.parameters.endDate, '2026-08-31');
assert.strictEqual(recovery.detail.parameters.sortBy, 'totalTimeListened');
page.handlers['htmx:afterRequest']({ detail: { successful: false, requestConfig: recovery.detail } });
assert.strictEqual(page.request('sortBy').detail.path, '/compare', 'failed recovery must not acknowledge a full render');
page.handlers['htmx:afterRequest']({ detail: { successful: true, requestConfig: recovery.detail } });
const recovered = page.request('sortBy');
assert.strictEqual(recovered.detail.path, '/compare?scope=sortable', 'successful recovery restores cheap sorting');
assert.strictEqual(recovered.detail.compareFullRefresh, false);

for (const failureType of ['htmx:responseError', 'htmx:sendError', 'htmx:sendAbort']) {
  const initialPage = loadPage();
  const initial = initialPage.request('compareInitialLoad', 'missing');
  assert.strictEqual(initial.prevented, false, 'initial URL bypasses incomplete local date inputs');
  if (failureType === 'htmx:sendAbort') initialPage.finish(initial, false);
  else initialPage.fail(initial, failureType);
  // HTMX configures a queued sort only after the initial request completes.
  const queued = initialPage.request('sortBy');
  assert.strictEqual(queued.detail.path, '/compare', `${failureType} requires a complete recovery`);
  initialPage.fail(queued);
  assert.strictEqual(initialPage.banner.visible, true);
  initialPage.clickRetry();
  assert.strictEqual(initialPage.banner.visible, false);
  initialPage.fail(initialPage.retryRequest);
  assert.strictEqual(initialPage.banner.visible, true, 'failed Retry restores its banner');
  const finalRecovery = initialPage.request('sortBy');
  assert.strictEqual(finalRecovery.detail.compareFullRefresh, true);
  initialPage.finish(finalRecovery, true);
  assert.strictEqual(initialPage.banner.visible, false);
  assert.strictEqual(initialPage.request('sortBy').detail.path, '/compare?scope=sortable');
}

const counterpartPage = loadPage();
counterpartPage.finish(counterpartPage.request('compareInitialLoad'), true, true);
const counterpart = counterpartPage.request('compareUserBadge2', 'inverted');
assert.strictEqual(counterpart.prevented, false);
counterpartPage.fail(counterpart);
assert.strictEqual(counterpartPage.request('sortBy').detail.path, '/compare');
counterpartPage.finish(counterpartPage.request('compareUserBadge2'), true, true);
assert.strictEqual(counterpartPage.request('sortBy').detail.path, '/compare?scope=sortable',
  'detached badge completion must acknowledge the full render');

const outstanding = loadPage();
outstanding.finish(outstanding.request('compareInitialLoad'), true);
const earlierSort = outstanding.request('sortBy');
outstanding.fail(outstanding.request('compareFilters'));
const clearsBefore = outstanding.banner.clears;
outstanding.finish(outstanding.request('unrelated'), true);
outstanding.finish(earlierSort, true);
assert.strictEqual(outstanding.banner.clears, clearsBefore, 'only complete recovery may clear this banner');
assert.strictEqual(outstanding.request('sortBy').detail.path, '/compare');
const unrelatedPage = loadPage();
unrelatedPage.fail(unrelatedPage.request('unrelated'));
assert.strictEqual(unrelatedPage.banner.shows, 0, 'unrelated failures do not own the Compare banner');
console.log('All Compare request guard and recovery tests passed.');
