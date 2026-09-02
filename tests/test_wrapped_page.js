// Plain-node unit test for the /wrapped browser logic (static/js/wrapped.js).
// Run with: node tests/test_wrapped_page.js
//
// The largest of the untested browser scripts, and the one whose own comments
// record the most already-paid-for bugs. Three of them are pinned here:
//
//   * loadChartData deliberately sets NO `interval` key. charts.js reads that as
//     "these buckets are hours" and splits each label on a space; Wrapped's day
//     buckets are whole dates, so the old loader's `interval = groupBy` turned
//     every x-axis label and tooltip into "undefined" the moment someone chose
//     Trend buckets = Day.
//   * applyStatsFilter falls back to All when the chosen category is not in the
//     year just loaded. A category with nothing in it is hidden server-side, so
//     switching years can take it away underneath the user - and without the
//     fallback they get a blank page.
//   * the remembered filter survives a swap. The server re-renders the nav and
//     has no idea which category is open (it is not in the URL), so without the
//     module-level `activeStatsFilter` a sort change bounces the user to All.
//
// eslint.config.js names wrapped.js as one of the two files that shipped a
// ReferenceError to production. That is the floor this file is raising.
const assert = require('assert');
const path = require('path');

const SCRIPT = path.join(__dirname, '..', 'static', 'js', 'wrapped.js');

function makeClassList(initial) {
  const classes = new Set(initial || []);
  return {
    classes,
    contains: (n) => classes.has(n),
    add: (n) => classes.add(n),
    remove: (n) => classes.delete(n),
    toggle(n, force) { if (force) { classes.add(n); } else { classes.delete(n); } },
  };
}

function makeElement(extra) {
  return Object.assign({
    id: '', style: {}, value: '', textContent: '', innerHTML: null, className: '',
    dataset: {}, classList: makeClassList(), attrs: {},
    setAttribute(name, value) { this.attrs[name] = String(value); },
    getAttribute(name) { return name in this.attrs ? this.attrs[name] : null; },
    addEventListener(type, fn) { (this.handlers = this.handlers || {})[type] = fn; },
    closest() { return null; },
    matches() { return false; },
    querySelector() { return null; },
    prepend(node) { (this.prepended = this.prepended || []).push(node); },
    focus() { this.focused = (this.focused || 0) + 1; },
  }, extra || {});
}

function filterButton(name, hidden) {
  return makeElement({ dataset: { filter: name }, style: { display: hidden ? 'none' : '' } });
}

function categoryDiv(name) {
  return makeElement({ dataset: { category: name } });
}

// Everything the PNG export touches. Asserting each fillText would pin the
// layout, not the behaviour - what matters is that it picks the theme's accent
// and hands the browser a download with the right filename.
function makeCanvasContext(record) {
  const noop = () => {};
  return {
    createLinearGradient() { return { addColorStop(stop, colour) { record.gradient.push(colour); } }; },
    fillRect: noop, strokeRect: noop, beginPath: noop, arc: noop, fill: noop, stroke: noop,
    moveTo: noop, lineTo: noop, closePath: noop, save: noop, restore: noop, translate: noop,
    measureText() { return { width: 10 }; },
    fillText(text) { record.texts.push(text); },
    set fillStyle(v) { record.fills.push(v); },
    get fillStyle() { return ''; },
    set strokeStyle(v) { record.strokes.push(v); },
    get strokeStyle() { return ''; },
    font: '', textAlign: '', lineWidth: 0,
  };
}

function loadWrapped(options) {
  options = options || {};
  const calls = {
    bodyListeners: {}, docListeners: {}, pruned: [], ajax: [], fetched: [],
    created: [], canvas: { gradient: [], texts: [], fills: [], strokes: [] },
    charts: 0, swapFailure: null, downloads: [],
  };
  const elements = options.elements || {};
  const buttons = options.buttons || [];
  const categories = options.categories || [];

  global.window = {
    location: { pathname: '/wrapped', search: options.search || '', href: 'http://localhost/wrapped' },
    AjaxStatus: options.noAjaxStatus ? undefined : {
      redirectIfUnauthorized(response) { return response.status === 401; },
    },
  };
  global.document = {
    documentElement: { className: options.theme || 'theme-rose' },
    addEventListener(type, fn) { calls.docListeners[type] = fn; },
    body: { addEventListener(type, fn) { calls.bodyListeners[type] = fn; } },
    getElementById(id) { return elements[id] || null; },
    querySelector(selector) {
      const match = /\.stats-filter-button\[data-filter="(.*)"\]/.exec(selector);
      if (match) return buttons.find(b => b.dataset.filter === match[1]) || null;
      return null;
    },
    querySelectorAll(selector) {
      if (selector === '.stats-filter-button') return buttons;
      if (selector === '[data-category]') return categories;
      return [];
    },
    createElement(tag) {
      const node = tag === 'canvas'
        ? {
            tag, width: 0, height: 0,
            getContext() { return makeCanvasContext(calls.canvas); },
            toDataURL() { return 'data:image/png;base64,STUB'; },
          }
        : Object.assign(makeElement(), { tag, click() { calls.downloads.push({ name: this.download, href: this.href }); } });
      calls.created.push(node);
      return node;
    },
  };
  global.renderTimeSeriesChart = function () { calls.charts += 1; };
  global.HtmxFilters = {
    pruneEmptyParams(parameters) { calls.pruned.push(parameters); },
    onSwapFailure(targetId, retry) { calls.swapFailure = { targetId, retry }; },
  };
  global.htmx = { ajax(method, url, opts) { calls.ajax.push({ method, url, opts }); } };
  global.FormData = function FormDataStub(form) { this.form = form; };
  global.fetch = function (url, init) {
    calls.fetched.push({ url: String(url), init });
    return options.respond ? options.respond() : Promise.reject(new Error('no stub'));
  };

  delete require.cache[require.resolve(SCRIPT)];
  require(SCRIPT);
  calls.window = global.window;
  calls.buttons = buttons;
  calls.categories = categories;
  return calls;
}

function clickOn(page, node) {
  page.bodyListeners.click({ target: { closest: (sel) => (node.selectors || []).includes(sel) ? node : null } });
}

const tick = () => new Promise(resolve => setImmediate(resolve));

const results = [];
function run(name, fn) { results.push({ name, fn }); }

// ------------------------------------------------------------- chart data

run('the bootstrap island becomes the chart data at parse time', () => {
  const page = loadWrapped({
    elements: { 'wrapped-bootstrap': makeElement({ textContent: '{"timeSeries":{"buckets":["2026-01-01"]}}' }) },
  });

  assert.deepStrictEqual(page.window.__chartData, { timeSeries: { buckets: ['2026-01-01'] } });
});

run('the chart data carries no interval key, or every label reads "undefined"', () => {
  const page = loadWrapped({
    elements: { 'wrapped-bootstrap': makeElement({ textContent: '{"timeSeries":{},"groupBy":"day"}' }) },
  });

  assert.deepStrictEqual(Object.keys(page.window.__chartData), ['timeSeries'],
                         'charts.js reads `interval` as "these buckets are hours"');
});

run('a page with no island leaves the chart data alone', () => {
  const page = loadWrapped({});

  assert.strictEqual(page.window.__chartData, undefined);
});

// -------------------------------------------------------- the stats filter

function filterSetup(options) {
  const all = filterButton('all');
  const songs = filterButton('songs');
  const podcasts = filterButton('podcasts', options && options.podcastsHidden);
  const songSection = categoryDiv('songs');
  const podcastSection = categoryDiv('podcasts');
  const page = loadWrapped(Object.assign({
    buttons: [all, songs, podcasts],
    categories: [songSection, podcastSection],
  }, options || {}));
  return { page, all, songs, podcasts, songSection, podcastSection };
}

run('All Stats shows every category on load', () => {
  const dom = filterSetup();

  assert.strictEqual(dom.songSection.classList.contains('visible'), true);
  assert.strictEqual(dom.podcastSection.classList.contains('visible'), true);
  assert.strictEqual(dom.all.classList.contains('active'), true);
  //< the state a screen reader hears - written beside the class, every time
  assert.strictEqual(dom.all.getAttribute('aria-pressed'), 'true');
  assert.strictEqual(dom.songs.getAttribute('aria-pressed'), 'false');
});

run('choosing a category shows only it', () => {
  const dom = filterSetup();
  dom.songs.selectors = ['.stats-filter-button'];

  clickOn(dom.page, dom.songs);

  assert.strictEqual(dom.songSection.classList.contains('visible'), true);
  assert.strictEqual(dom.podcastSection.classList.contains('visible'), false);
  assert.strictEqual(dom.songs.classList.contains('active'), true);
  assert.strictEqual(dom.all.classList.contains('active'), false);
  assert.strictEqual(dom.songs.getAttribute('aria-pressed'), 'true');
  assert.strictEqual(dom.all.getAttribute('aria-pressed'), 'false');
});

run('a category the new year does not have falls back to All, not a blank page', () => {
  const dom = filterSetup({ podcastsHidden: true });
  dom.podcasts.selectors = ['.stats-filter-button'];

  clickOn(dom.page, dom.podcasts);

  assert.strictEqual(dom.all.classList.contains('active'), true);
  assert.strictEqual(dom.songSection.classList.contains('visible'), true,
                     'everything shows rather than nothing');
});

run('the chosen category survives a swap, instead of bouncing back to All', () => {
  const dom = filterSetup();
  dom.songs.selectors = ['.stats-filter-button'];
  clickOn(dom.page, dom.songs);

  dom.page.bodyListeners['htmx:afterSwap']({ target: { id: 'wrappedResults' } });

  assert.strictEqual(dom.songs.classList.contains('active'), true,
                     'the server has no idea which category is open - this file remembers');
  assert.strictEqual(dom.podcastSection.classList.contains('visible'), false);
});

// ---------------------------------------------------------------- swaps

run('a swap of the recap reloads the data and redraws once', () => {
  const page = loadWrapped({
    elements: { 'wrapped-bootstrap': makeElement({ textContent: '{"timeSeries":{"buckets":[]}}' }) },
  });
  const before = page.charts;

  page.bodyListeners['htmx:afterSwap']({ target: { id: 'wrappedResults' } });

  assert.strictEqual(page.charts, before + 1);
});

run('an out-of-band region swapping does not redraw the chart again', () => {
  const page = loadWrapped({
    elements: { 'wrapped-bootstrap': makeElement({ textContent: '{"timeSeries":{}}' }) },
  });
  const before = page.charts;

  page.bodyListeners['htmx:afterSwap']({ target: { id: 'shareLinkPanel' } });

  assert.strictEqual(page.charts, before, 'four OOB regions would otherwise redraw it four times');
});

run('the wrapped form prunes its empty params, and nothing else does', () => {
  const page = loadWrapped({});
  const mine = { groupBy: '' };
  const theirs = { groupBy: '' };

  page.bodyListeners['htmx:configRequest']({ detail: { elt: { id: 'wrappedFilters' }, parameters: mine } });
  page.bodyListeners['htmx:configRequest']({ detail: { elt: { id: 'somethingElse' }, parameters: theirs } });

  assert.deepStrictEqual(page.pruned, [mine]);
});

// ---------------------------------------------------------- the PNG export

function exportSetup(theme) {
  const btn = makeElement({
    dataset: {
      year: '2026', user: 'timo', topsong: 'Aruarian Dance', topartist: 'Nujabes',
      topalbum: 'Modal Soul', peakday: '2026-03-01', peakplays: '120',
      discoveredsongs: '340', discoveredartists: '58',
    },
  });
  btn.selectors = ['#exportWrappedBtn'];
  const page = loadWrapped({ theme });
  clickOn(page, btn);
  return page;
}

run('the export downloads a PNG named for the user and year', () => {
  const page = exportSetup('theme-rose');

  assert.strictEqual(page.downloads.length, 1);
  assert.strictEqual(page.downloads[0].name, 'timo_2026_wrapped_summary.png');
  assert.ok(page.downloads[0].href.startsWith('data:image/png'), page.downloads[0].href);
});

run('the card is drawn in the active theme', () => {
  const page = exportSetup('theme-green');

  assert.ok(page.canvas.gradient.includes('#0b3c1d'), page.canvas.gradient.join());
  assert.ok(page.canvas.fills.includes('#1DB954'), 'the green accent, not the default rose');
});

run('an unknown theme falls back to the default rather than drawing nothing', () => {
  const page = exportSetup('theme-does-not-exist');

  assert.ok(page.canvas.gradient.includes('#3c0b1f'));
  assert.ok(page.canvas.texts.includes('2026 WRAPPED'));
});

// The playlist download that used to be tested here is the shared
// _playlist_download.html control now - its (still delegated) handler lives
// in chrome-common.js and is pinned by test_chrome_common.js instead.

// ----------------------------------------------------------- share modal

function modalSetup(options) {
  const closeBtn = makeElement();
  const modal = makeElement({
    id: 'shareLinkModal',
    querySelector(selector) { return selector === '.share-modal-close' ? closeBtn : null; },
  });
  const openBtn = makeElement();
  const panelBody = makeElement();
  const page = loadWrapped(Object.assign({
    elements: { shareLinkModal: modal, shareWrappedBtn: openBtn, shareLinkPanelBody: panelBody },
  }, options || {}));
  return { page, modal, openBtn, panelBody, closeBtn };
}

run('the Share button opens the modal', () => {
  const dom = modalSetup();

  dom.openBtn.handlers.click();

  assert.strictEqual(dom.modal.style.display, 'flex');
});

run('clicking the backdrop closes it, clicking inside does not', () => {
  const dom = modalSetup();
  dom.openBtn.handlers.click();

  dom.modal.handlers.click.call(dom.modal, { target: makeElement() });
  assert.strictEqual(dom.modal.style.display, 'flex', 'a click on the panel must not dismiss it');

  dom.modal.handlers.click.call(dom.modal, { target: dom.modal });
  assert.strictEqual(dom.modal.style.display, 'none');
});

run('Escape closes the modal', () => {
  const dom = modalSetup();
  dom.openBtn.handlers.click();

  dom.page.docListeners.keydown({ key: 'Escape' });

  assert.strictEqual(dom.modal.style.display, 'none');
});

// A role="dialog" is announced by focus landing inside it; display:flex alone
// says nothing to a screen reader and left focus on the Share button behind
// the overlay. Closing has to hand it back, or Escape drops focus from the now
// display:none Close button to <body>. Same rule as layout-chrome.js's drawer.
run('opening the dialog moves focus into it', () => {
  const dom = modalSetup();

  dom.openBtn.handlers.click();

  assert.strictEqual(dom.closeBtn.focused, 1);
});

run('every close path returns focus to whatever opened the dialog', () => {
  const dom = modalSetup();
  global.document.activeElement = dom.openBtn;

  dom.openBtn.handlers.click();
  dom.page.docListeners.keydown({ key: 'Escape' });
  assert.strictEqual(dom.openBtn.focused, 1, 'Escape');

  dom.openBtn.handlers.click();
  dom.closeBtn.handlers.click();
  assert.strictEqual(dom.openBtn.focused, 2, 'the Close button');
  assert.strictEqual(dom.modal.style.display, 'none', 'the Close button no longer needs an inline handler');

  dom.openBtn.handlers.click();
  dom.modal.handlers.click.call(dom.modal, { target: dom.modal });
  assert.strictEqual(dom.openBtn.focused, 3, 'the overlay');
});

run('a dialog the server opened returns focus to the Share button', () => {
  //< ?openShareModal=1 renders it open, so nothing on the page opened it
  const dom = modalSetup();
  dom.modal.style.display = 'flex';

  dom.page.docListeners.keydown({ key: 'Escape' });

  assert.strictEqual(dom.openBtn.focused, 1);
});

run('Escape with the dialog closed leaves focus where it is', () => {
  const dom = modalSetup();

  dom.page.docListeners.keydown({ key: 'Escape' });

  assert.strictEqual(dom.openBtn.focused, undefined, 'it hears every keypress on the page');
});

run('another key leaves it open', () => {
  const dom = modalSetup();
  dom.openBtn.handlers.click();

  dom.page.docListeners.keydown({ key: 'a' });

  assert.strictEqual(dom.modal.style.display, 'flex');
});

// ------------------------------------------------- creating a share link

function submitShareForm(dom, options) {
  const opts = options || {};
  const form = makeElement({ action: 'http://localhost/wrapped/share-links/2026' });
  form.matches = () => true;
  if (opts.submitButton) form.querySelector = () => opts.submitButton;
  const evt = { target: form, prevented: 0, preventDefault() { this.prevented += 1; } };
  dom.modal.handlers.submit.call(dom.modal, evt);
  return evt;
}

// A fetch whose settlement THIS test controls, so two submits can be resolved
// in the opposite order to the one they were made in - which is the whole
// scenario and is not reproducible with an already-resolved promise.
function deferredShareScenario() {
  const settle = [];
  const dom = shareScenario(() => new Promise((resolve, reject) => settle.push({ resolve, reject })));
  return { dom, settle };
}

function shareScenario(respond) {
  return modalSetup({ respond });
}

run('creating a link posts with ajax=true and swaps the panel body in', async () => {
  const dom = shareScenario(() => Promise.resolve({ status: 200, json: () => Promise.resolve({ html: '<p>link</p>' }) }));

  const evt = submitShareForm(dom);
  await tick(); await tick();

  assert.strictEqual(evt.prevented, 1, 'the browser must not submit this form normally');
  assert.ok(dom.page.fetched[0].url.includes('ajax=true'), dom.page.fetched[0].url);
  assert.strictEqual(dom.page.fetched[0].init.method, 'POST');
  assert.strictEqual(dom.panelBody.innerHTML, '<p>link</p>');
});

run('a refused create shows the route\'s own reason, not a generic line', async () => {
  const dom = shareScenario(() => Promise.resolve({
    status: 400, json: () => Promise.resolve({ error: "You've reached the limit." }),
  }));

  submitShareForm(dom);
  await tick(); await tick();

  const errorEl = dom.panelBody.prepended[0];
  assert.strictEqual(errorEl.textContent, "You've reached the limit.");
  assert.strictEqual(errorEl.className, 'share-link-error');
});

run('an expired session redirects instead of painting an error', async () => {
  const dom = shareScenario(() => Promise.resolve({ status: 401, json: () => Promise.resolve({}) }));

  submitShareForm(dom);
  await tick(); await tick();

  assert.strictEqual(dom.panelBody.prepended, undefined, '"session expired" is not a form error');
  assert.strictEqual(dom.panelBody.innerHTML, null);
});

run('a network failure still says something', async () => {
  const dom = shareScenario(() => Promise.reject(new Error('offline')));

  submitShareForm(dom);
  await tick(); await tick();

  assert.strictEqual(dom.panelBody.prepended[0].textContent, 'Something went wrong. Please try again.');
});

// The panel lists one revoke form PER LINK next to the create form, and every
// one of them answers with the WHOLE panel - so two overlapping submits is the
// ordinary shape of this UI, not an edge case. This was the only async path in
// static/js with no in-flight guard (contrast playlists.js's previewToken, the
// pages' _navSeq, detail-chart.js's activeLoad, and hx-sync everywhere else).
run('a superseded response cannot repaint a panel the newer one replaced', async () => {
  const { dom, settle } = deferredShareScenario();

  submitShareForm(dom);   //< Revoke on link A
  submitShareForm(dom);   //< Revoke on link B, a moment later
  await tick();

  //< B answers first, with the panel as it stands now that both are gone
  settle[1].resolve({ status: 200, json: () => Promise.resolve({ html: '<p>A and B gone</p>' }) });
  await tick(); await tick(); await tick();
  //< then A's answer lands - rendered while B still existed
  settle[0].resolve({ status: 200, json: () => Promise.resolve({ html: '<p>B still listed</p>' }) });
  await tick(); await tick(); await tick();

  assert.strictEqual(dom.panelBody.innerHTML, '<p>A and B gone</p>',
    'a superseded response put a revoked link back on screen, with a live Copy '
    + 'button handing out a dead URL and a Revoke that now 403s');
});

run('a superseded failure cannot show an error over a newer success', async () => {
  const { dom, settle } = deferredShareScenario();

  submitShareForm(dom);
  submitShareForm(dom);
  await tick();

  settle[1].resolve({ status: 200, json: () => Promise.resolve({ html: '<p>done</p>' }) });
  await tick(); await tick(); await tick();
  settle[0].reject(new Error('offline'));
  await tick(); await tick(); await tick();

  assert.strictEqual(dom.panelBody.prepended, undefined,
    'an error about a request the user has already moved past is a lie');
  assert.strictEqual(dom.panelBody.innerHTML, '<p>done</p>');
});

run('the submit button is disabled while its own request is in flight', async () => {
  const button = makeElement({ disabled: false });
  const { dom, settle } = deferredShareScenario();

  submitShareForm(dom, { submitButton: button });
  await tick();

  assert.strictEqual(button.disabled, true,
    'a double-click on Create is what put a bucket one over its cap');

  settle[0].resolve({ status: 400, json: () => Promise.resolve({ error: 'nope' }) });
  await tick(); await tick(); await tick();

  assert.strictEqual(button.disabled, false, 'a refused create has to stay retryable');
});

run('an unrelated form inside the modal is left to submit normally', () => {
  const dom = modalSetup();
  const form = makeElement();
  form.matches = () => false;
  const evt = { target: form, prevented: 0, preventDefault() { this.prevented += 1; } };

  dom.modal.handlers.submit.call(dom.modal, evt);

  assert.strictEqual(evt.prevented, 0);
  assert.deepStrictEqual(dom.page.fetched, []);
});

// ---------------------------------------------------------------- failure

// The scoping is the shared helper's (tests/test_htmx_filters.js pins it): a
// failure on some OTHER region must not blank the recap and offer a Retry for
// a request that never failed. The hand-rolled body listeners this replaced
// took no event at all, so they answered every failed swap on the page.
run('a failed swap is reported through the shared helper, scoped to the recap', () => {
  const page = loadWrapped({});

  assert.strictEqual(page.swapFailure.targetId, 'wrappedResults');
  assert.strictEqual(page.bodyListeners['htmx:responseError'], undefined,
    'an unscoped listener of its own is the shape the helper exists to replace');
  assert.strictEqual(page.bodyListeners['htmx:sendError'], undefined);
});

run('the retry re-requests the recap', () => {
  const page = loadWrapped({ search: '?year=2026' });

  page.swapFailure.retry();

  assert.strictEqual(page.ajax[0].url, '/wrapped?year=2026');
  assert.strictEqual(page.ajax[0].opts.target, '#wrappedResults');
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
  console.log(`all ${results.length} wrapped tests passed`);
})();
