// Plain-node unit tests for static/js/profile-page.js.
// Run with: node tests/test_profile_page.js
'use strict';

const assert = require('assert');

/* Captured before any test runs. installNavigableProfileDom stubs both, and a
   stub left installed would silently serve a later test that feature-detects
   what it replaced - '_parse returns a queryable document' below skips itself on
   exactly `typeof DOMParser === 'undefined'`. The last test asserts they were
   handed back. Same guard as tests/test_admin_page.js. */
const BASELINE_FETCH = global.fetch;
const BASELINE_PARSER = global.DOMParser;

/* ------------------------------------------------------------------ */
/* Minimal DOM stub                                                     */
/* ------------------------------------------------------------------ */

function makeEl(tag, attrs) {
  attrs = attrs || {};
  const children = [];
  const handlers = {};
  const el = {
    tagName:     tag.toUpperCase(),
    className:   attrs.className || '',
    innerHTML:   '',
    outerHTML:   '',
    _attrs:      Object.assign({}, attrs),
    _id:         attrs.id || '',
    children,
    classList: {
      _set: new Set((attrs.className || '').split(' ').filter(Boolean)),
      add(c)    { this._set.add(c); el.className = [...this._set].join(' '); },
      remove(c) { this._set.delete(c); el.className = [...this._set].join(' '); },
      toggle(c, force) {
        if (force === undefined ? this._set.has(c) : !force) this.remove(c);
        else this.add(c);
      },
      contains(c) { return this._set.has(c); },
    },
    appendChild(child)        { child._parent = el; children.push(child); return child; },
    insertBefore(child, ref)  {
      child._parent = el;
      const i = ref ? children.indexOf(ref) : -1;
      if (i >= 0) children.splice(i, 0, child);
      else children.push(child);
      return child;
    },
    removeChild(child) {
      const i = children.indexOf(child);
      if (i >= 0) children.splice(i, 1);
      return child;
    },
    replaceChild(fresh, stale) {
      const i = children.indexOf(stale);
      if (i >= 0) {
        fresh._parent = el;
        children.splice(i, 1, fresh);
      }
      return stale;
    },
    addEventListener(type, fn)  { (handlers[type] = handlers[type] || []).push(fn); },
    dispatchEvent(type, evt)    { (handlers[type] || []).forEach(fn => fn(evt || {})); },
    getAttribute(name)          { return el._attrs[name] !== undefined ? el._attrs[name] : null; },
    setAttribute(name, val)     { el._attrs[name] = val; if (name === 'id') el._id = val; },
    removeAttribute(name)       { delete el._attrs[name]; },
    cloneNode(deep)             {
      const clone = makeEl(tag, Object.assign({}, el._attrs));
      if (deep) el.children.forEach(c => clone.appendChild(c.cloneNode(true)));
      return clone;
    },
    closest(sel) {
      // Very naive: only handles tag and .class selectors used in the tests.
      if (sel === 'a[href]' && el.tagName === 'A') return el;
      return null;
    },
    querySelectorAll(sel) {
      if (sel === 'script') return [];
      if (sel === 'a[href]') return children.filter(c => c.tagName === 'A');
      return [];
    },
    get nextElementSibling() {
      if (!el._parent) return null;
      const siblings = el._parent.children;
      const i = siblings.indexOf(el);
      return siblings[i + 1] || null;
    },
    get parentNode() { return el._parent || null; },
    get firstChild()  { return children[0] || null; },
  };
  Object.defineProperty(el, 'id', {
    get() { return el._id; },
    set(v) { el._id = v; el._attrs.id = v; },
  });
  return el;
}

/* Build a minimal profile-card DOM tree and attach to global.document. */
function installProfileDom() {
  const byId  = {};
  const bySel = {};

  const card    = makeEl('div', { className: 'login-card profile-card' });
  const details = makeEl('div', { className: 'profile-details' });
  const nav     = makeEl('nav', { className: 'profile-subnav' });

  const linkAcct  = makeEl('a', { href: '/profile',       className: 'active' });
  const linkShare = makeEl('a', { href: '/profile/sharing' });
  nav.appendChild(linkAcct);
  nav.appendChild(linkShare);
  linkAcct.closest  = (sel) => sel === 'a[href]' ? linkAcct  : null;
  linkShare.closest = (sel) => sel === 'a[href]' ? linkShare : null;

  const section = makeEl('section', { className: 'profile-section' });
  section.innerHTML = '<h2 id="preferences">Prefs</h2>';

  const logout = makeEl('div', { className: 'profile-logout-row' });

  card.appendChild(details);
  card.appendChild(nav);
  card.appendChild(section);
  card.appendChild(logout);

  bySel['.profile-subnav']     = nav;
  bySel['.profile-logout-row'] = logout;
  bySel['.login-card']         = card;

  const popHandlers = [];

  global.window = {
    addEventListener(type, fn) {
      if (type === 'popstate') popHandlers.push(fn);
    },
    _firePopstate(state) {
      popHandlers.forEach(fn => fn({ state }));
    },
    ProfilePage: undefined,
  };
  global.history = {
    _pushed: [],
    pushState(state, title, url) { this._pushed.push({ state, url }); },
  };
  global.location = { href: '/profile', origin: '' };

  global.document = {
    createElement(tag) {
      const el = makeEl(tag);
      // Simulate innerHTML setter for the swap helper.
      Object.defineProperty(el, 'innerHTML', {
        get() { return el._innerHTML || ''; },
        set(v) {
          el._innerHTML = v;
          // Parse children naively: just track text.
        },
      });
      // No `el.firstChild = null` here: makeEl already exposes firstChild as a
      // getter-only accessor, so assigning to it throws TypeError under the
      // 'use strict' at the top of this file - which made document.createElement
      // unusable for every tag the individual tests didn't special-case.
      return el;
    },
    querySelector(sel) {
      return bySel[sel] || null;
    },
    querySelectorAll(sel) { return []; },
  };

  return { card, nav, section, logout, linkAcct, linkShare, bySel };
}

const ProfilePage = require('../static/js/profile-page.js');

/* ------------------------------------------------------------------ */
/* Helpers                                                             */
/* ------------------------------------------------------------------ */

/* Collected, then awaited in sequence at the bottom. The navigate() tests below
   are async - they hold two fetches open and resolve them out of order - and
   calling them here would report a pass before their assertions ran. Failures
   still don't stop the run: every test reports, and the exit code carries. */
const results = [];
function run(name, fn) { results.push({ name, fn }); }

/* ------------------------------------------------------------------ */
/* HTML extraction tests (no DOM required)                             */
/* ------------------------------------------------------------------ */

const SAMPLE_HTML = `<!doctype html><html><body>
<div class="login-card profile-card">
  <nav class="profile-subnav">
    <a href="/profile" class="active" aria-current="page">Account</a>
    <a href="/profile/sharing">Sharing</a>
  </nav>
  <section class="profile-section"><h2 id="preferences">Prefs</h2></section>
  <section class="profile-section"><h2 id="display-name">Name</h2></section>
  <div class="profile-logout-row"><button>Log out</button></div>
</div>
</body></html>`;

run('SUBNAV_SEL constant matches the nav class', () => {
  assert.strictEqual(ProfilePage.SUBNAV_SEL, '.profile-subnav');
});

run('init seeds its own state even when a foreign one is already present', () => {
  /* Same shape as admin-page.js (see tests/test_admin_page.js): `!history.state`
     also skipped the seed for an entry whose state came from somebody else, and
     the popstate handler only re-renders on state.profileTab - which is exactly
     the "Back leaves the swapped tab on screen" case the seed exists for. */
  installProfileDom();
  const replaced = [];
  //< installProfileDom's history stub carries pushState only, and init feature-
  //  detects replaceState before seeding
  global.history.replaceState = function (state, title, url) {
    replaced.push({ state, url });
    global.history.state = state;
  };
  global.history.state = { notOurs: true };

  ProfilePage.init();

  assert.strictEqual(replaced.length, 1, 'a state without profileTab is not this page\'s - seed ours');
  assert.strictEqual(replaced[0].state.profileTab, '/profile');
});

run('LOADING_CLASS constant is correct', () => {
  assert.strictEqual(ProfilePage.LOADING_CLASS, 'profile-tab-loading');
});

run('_parse returns a queryable document', () => {
  if (typeof DOMParser === 'undefined') {
    console.log('# _parse: DOMParser not available in node - skipped');
    return;
  }
  const doc = ProfilePage._parse(SAMPLE_HTML);
  assert.ok(doc.querySelector('.profile-subnav'), 'subnav should be found');
});

/* Node has no DOMParser; test _extractBody/_extractNav via a stub. */
run('_extractBody and _extractNav work with a stub document', () => {
  // Build a tiny stub document mimicking the parsed result.
  function el(cls, innerHTML) {
    const siblings = [];
    return {
      className: cls,
      outerHTML: `<div class="${cls}">${innerHTML}</div>`,
      querySelector(sel) {
        if (sel === '.profile-subnav')     return siblings[0] || null;
        if (sel === '.profile-logout-row') return siblings[siblings.length - 1] || null;
        return null;
      },
      _siblings: siblings,
    };
  }

  // Simulate what DOMParser would produce.
  const nav     = { outerHTML: '<nav class="profile-subnav"></nav>' };
  const sec1    = { outerHTML: '<section class="profile-section"><h2>A</h2></section>' };
  const sec2    = { outerHTML: '<section class="profile-section"><h2>B</h2></section>' };
  const logout  = { outerHTML: '<div class="profile-logout-row"></div>' };

  // Link siblings
  nav.nextElementSibling    = sec1;
  sec1.nextElementSibling   = sec2;
  sec2.nextElementSibling   = logout;
  logout.nextElementSibling = null;

  const doc = {
    querySelector(sel) {
      if (sel === '.profile-subnav')     return nav;
      if (sel === '.profile-logout-row') return logout;
      return null;
    },
  };

  const body = ProfilePage._extractBody(doc);
  assert.ok(body.includes('<section'), 'should contain section markup: ' + body);
  assert.ok(!body.includes('profile-logout-row'), 'should not include logout row: ' + body);

  const navHtml = ProfilePage._extractNav(doc);
  assert.ok(navHtml.includes('profile-subnav'), 'should include subnav: ' + navHtml);
});

run('_extractBody returns null when subnav is missing', () => {
  const doc = { querySelector() { return null; } };
  assert.strictEqual(ProfilePage._extractBody(doc), null);
});

/* ------------------------------------------------------------------ */
/* DOM manipulation tests (require the global DOM stub)                */
/* ------------------------------------------------------------------ */

run('_syncNav marks the matching link active and clears others', () => {
  const { nav, linkAcct, linkShare } = installProfileDom();

  // Both links need a querySelectorAll mock.
  nav.querySelectorAll = (sel) => {
    if (sel === 'a') return [linkAcct, linkShare];
    return [];
  };

  ProfilePage._syncNav('/profile/sharing');

  assert.ok(!linkAcct.classList.contains('active'), 'account link should lose active');
  assert.ok(linkShare.classList.contains('active'), 'sharing link should gain active');
  assert.strictEqual(linkShare._attrs['aria-current'], 'page');
  assert.strictEqual(linkAcct._attrs['aria-current'], undefined);
});

run('_swapBody removes old siblings and inserts new ones', () => {
  const { card, nav, section, logout } = installProfileDom();

  // Check initial state
  assert.ok(card.children.includes(section), 'section should start in card');

  // Build a replacement section that _swapBody will insert.
  const newSection = makeEl('section', { className: 'profile-section' });

  // Patch document.createElement so the temporary parsing div holds newSection
  // in a real queue: the while((child = tmp.firstChild)) loop pops it out,
  // mirroring how a real browser moves the child on insertBefore.
  const origCreate = global.document.createElement;
  global.document.createElement = (tag) => {
    if (tag === 'div') {
      const queue = [newSection];
      const tmp = makeEl('div');
      Object.defineProperty(tmp, 'firstChild', {
        get() { return queue.length ? queue[0] : null; },
      });
      // Simulate the browser removing the child from tmp when inserted elsewhere.
      const origInsert = card.insertBefore.bind(card);
      card.insertBefore = (child, ref) => {
        const i = queue.indexOf(child);
        if (i >= 0) queue.splice(i, 1);
        return origInsert(child, ref);
      };
      return tmp;
    }
    return origCreate(tag);
  };

  ProfilePage._swapBody('<section class="profile-section"><h2>New</h2></section>');

  global.document.createElement = origCreate;

  assert.ok(!card.children.includes(section), 'old section should be removed');
  assert.ok(card.children.includes(newSection), 'new section should be inserted');
});

/* ------------------------------------------------------------------ */
/* Out-of-order tab responses                                          */
/* ------------------------------------------------------------------ */

/* A window whose fetches are held open, so a test decides the order the tab
   responses land in. The parsed-document stub is the minimum _extractBody
   walks: a subnav, one sibling carrying the response, and no logout row. */
function installNavigableProfileDom() {
  const dom = installProfileDom();
  dom.nav.querySelectorAll = (sel) => (sel === 'a' ? [dom.linkAcct, dom.linkShare] : []);

  const pending = [];
  const priorFetch = global.fetch;
  const priorParser = global.DOMParser;
  global.fetch = () => new Promise((resolve) => { pending.push(resolve); });
  global.DOMParser = function () {
    this.parseFromString = (html) => {
      const carried = { outerHTML: html, nextElementSibling: null };
      const parsedNav = { outerHTML: '<nav class="profile-subnav"></nav>', nextElementSibling: carried };
      return { querySelector: (sel) => (sel === '.profile-subnav' ? parsedNav : null) };
    };
  };
  /* Restored even when an assertion throws: node has no DOMParser, and
     '_parse returns a queryable document' above SKIPS itself on exactly that.
     Leaving the stub installed would silently arm that test against a fake
     parser the day someone reorders this file. */
  const restore = () => { global.fetch = priorFetch; global.DOMParser = priorParser; };
  return Object.assign(dom, { pending, restore });
}

function tabResponse(html) {
  return { ok: true, text: () => Promise.resolve(html) };
}

run('a superseded tab response never lands', async () => {
  /* Two quick clicks. Nothing cancels the first fetch, so when it answered last
     it swapped its body in and re-synced the nav to the tab the user had
     already left - under the newer tab's URL. */
  const { pending, linkAcct, linkShare, restore } = installNavigableProfileDom();
  try {
    const first = ProfilePage.navigate('/profile');
    const second = ProfilePage.navigate('/profile/sharing');
    assert.strictEqual(pending.length, 2, 'both requests are in flight');

    pending[1](tabResponse('<section>SHARING</section>'));
    await second;
    pending[0](tabResponse('<section>ACCOUNT</section>'));
    await first;

    assert.ok(linkShare.classList.contains('active'),
              'the tab the user asked for must stay current');
    assert.ok(!linkAcct.classList.contains('active'),
              'the abandoned tab must not take the highlight back');
  } finally {
    restore();
  }
});

run('a superseded response does not clear the loading state of the live one', async () => {
  const { card, pending, restore } = installNavigableProfileDom();
  try {
    const first = ProfilePage.navigate('/profile');
    const second = ProfilePage.navigate('/profile/sharing');

    pending[0](tabResponse('<section>ACCOUNT</section>'));   //< the abandoned tab answers first
    await first;

    assert.ok(card.classList.contains('profile-tab-loading'),
              'the newer request is still open, so the card must still read as loading');

    pending[1](tabResponse('<section>SHARING</section>'));
    await second;
    assert.ok(!card.classList.contains('profile-tab-loading'),
              'and is cleared once the live one lands');
  } finally {
    restore();
  }
});

/* ------------------------------------------------------------------ */
/* Inline-script revival (CSP)                                         */
/* ------------------------------------------------------------------ */

/* Put `script` elements where _runInlineScripts walks, i.e. on the siblings
 * between the subnav and the logout row. Returns the stale script node. */
function installInlineScript(section, text, attrs) {
  const script = makeEl('script', attrs || {});
  script.textContent = text;
  section.appendChild(script);
  section.querySelectorAll = (sel) => (sel === 'script' ? [script] : []);
  return script;
}

/* Record every element the module creates, so a test can tell a real
 * `<script>` element apart from a string-eval. */
function captureCreatedElements() {
  const created = [];
  const orig = global.document.createElement;
  global.document.createElement = (tag) => {
    const el = orig(tag);
    el.textContent = '';
    created.push(el);
    return el;
  };
  return { created, restore: () => { global.document.createElement = orig; } };
}

run('_runInlineScripts revives an inline script as a real <script> element', () => {
  /* The regression this pins: the module used to call new Function(text)(),
     which every page outside DETAIL_CSP_ENDPOINTS forbids ('unsafe-eval' is
     scoped to the three detail routes - see config.py/DETAIL_PAGE_CSP). The
     EvalError landed in the catch and was swallowed, so the Account tab's
     theme-selector initialiser silently never ran after an AJAX tab swap. */
  const { section } = installProfileDom();
  const stale = installInlineScript(section, 'window.__revived = true;');
  const capture = captureCreatedElements();

  ProfilePage._runInlineScripts();
  capture.restore();

  const scripts = capture.created.filter(el => el.tagName === 'SCRIPT');
  assert.strictEqual(scripts.length, 1, 'exactly one replacement <script> element');
  assert.strictEqual(scripts[0].textContent, 'window.__revived = true;');
  assert.ok(section.children.includes(scripts[0]), 'replacement must be inserted into the document');
  assert.ok(!section.children.includes(stale), 'the inert original must be replaced, not left behind');
});

run('_runInlineScripts carries the type attribute onto the replacement', () => {
  /* A replacement that drops type="module" (or picks up an implicit
     "text/javascript" where the original had a non-executable type) would
     change whether - and how - the browser runs it. */
  const { section } = installProfileDom();
  installInlineScript(section, 'export const x = 1;', { type: 'module' });
  const capture = captureCreatedElements();

  ProfilePage._runInlineScripts();
  capture.restore();

  const script = capture.created.filter(el => el.tagName === 'SCRIPT')[0];
  assert.ok(script, 'a replacement script element was created');
  assert.strictEqual(script.getAttribute('type'), 'module');
});

run('_runInlineScripts leaves src= scripts alone', () => {
  /* An external script inserted via innerHTML is inert too, but re-adding it
     would re-download and re-run a whole file; the tab partials have none. */
  const { section } = installProfileDom();
  const stale = installInlineScript(section, '', { src: '/static/js/whatever.js' });
  stale.src = '/static/js/whatever.js';
  const capture = captureCreatedElements();

  ProfilePage._runInlineScripts();
  capture.restore();

  assert.strictEqual(capture.created.filter(el => el.tagName === 'SCRIPT').length, 0);
  assert.ok(section.children.includes(stale), 'the src= script must be left untouched');
});

run('init wires the subnav exactly once (idempotent)', () => {
  installProfileDom();
  let clickHandlers = 0;
  const nav = global.document.querySelector('.profile-subnav');
  const origAdd = nav.addEventListener.bind(nav);
  nav.addEventListener = (type, fn) => {
    if (type === 'click') clickHandlers++;
    origAdd(type, fn);
  };

  ProfilePage.init();
  ProfilePage.init(); // second call should be a no-op

  assert.strictEqual(clickHandlers, 1, 'click handler registered exactly once');
});

//< runs last on purpose: it checks what the tests above left behind
run('no navigate stub outlived its test', () => {
  assert.strictEqual(global.fetch, BASELINE_FETCH, 'a fetch stub is still installed');
  assert.strictEqual(global.DOMParser, BASELINE_PARSER, 'a DOMParser stub is still installed');
});

(async () => {
  for (const { name, fn } of results) {
    try { await fn(); console.log('ok - ' + name); }
    catch (err) { console.error('FAIL - ' + name + '\n' + err.message); process.exitCode = 1; }
  }
  console.log('\nAll profile-page tests done.');
})();
