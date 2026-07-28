// Plain-node unit tests for static/js/profile-page.js.
// Run with: node tests/test_profile_page.js
'use strict';

const assert = require('assert');

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
      el.firstChild = null;  // will be null for empty div
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

function run(name, fn) {
  try { fn(); console.log('ok - ' + name); }
  catch (err) { console.error('FAIL - ' + name + '\n' + err.message); process.exitCode = 1; }
}

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

console.log('\nAll profile-page tests done.');
