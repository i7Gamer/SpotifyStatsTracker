// SPDX-FileCopyrightText: 2026 i7Gamer
// SPDX-License-Identifier: AGPL-3.0-or-later

// Plain-node unit tests for the tag widget (static/js/tags.js). No test
// framework/dependency - run with:
//   node tests/test_tags_widget.js            (every case)
//   node tests/test_tags_widget.js <text>     (only cases whose name contains it)
// The filter exists for mutation checks: the runner stops at the first
// failure, so proving that a SPECIFIC case catches a broken guard means
// running that case alone against the break.
//
// Two halves. tagUpdateOutcome is exported and tested directly: it pins the
// fix for silently-swallowed /api/tags failures (the old handlers did
// `if (data.success) { ... }` with no else, so a 401 after session expiry, a
// 400 on a rejected tag, or the admin kill switch's 404 HTML all left the
// widget looking untouched with nothing logged or shown). The submit path is
// not exported, so the second half drives it the way the page does - fire
// DOMContentLoaded, submit the add form - against a DOM stub, with fetch
// settled by hand so two in-flight submits can land in either order.
const assert = require('assert');

const MODULE_PATH = require.resolve('../static/js/tags.js');
const { tagUpdateOutcome } = require(MODULE_PATH);

//< the runner reports through this; freshWidget() below replaces console.error
//  to capture what the submit path's catch logs
const realConsoleError = console.error;

const tests = [];
function run(name, fn) { tests.push([name, fn]); }

// --- tagUpdateOutcome -------------------------------------------------------

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

// --- the submit path --------------------------------------------------------

function makeEl() {
  const el = {
    className: '', textContent: '', dataset: {}, style: {}, title: '', type: '',
    children: [], handlers: {}, _html: '',
    appendChild(child) { this.children.push(child); return child; },
    setAttribute() {},
    addEventListener(type, fn) { this.handlers[type] = fn; },
    //< real DOM's querySelectorAll searches the whole subtree; renderTags
    //  nests the remove buttons a level under the chips this returns, so
    //  bindRemoveButtons needs a real (if minimal - class selectors only)
    //  walk, not the always-empty stub this used to be
    querySelectorAll(selector) {
      const cls = String(selector).replace(/^\./, '');
      const matches = [];
      (function walk(node) {
        (node.children || []).forEach((child) => {
          if (child.className === cls) matches.push(child);
          walk(child);
        });
      }(this));
      return matches;
    },
  };
  //< renderTags empties the row with innerHTML = '' before re-appending
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(value) { this._html = value; if (value === '') this.children.length = 0; },
  });
  return el;
}

/* Load a FRESH copy of the module (the widget's state lives in its
 * DOMContentLoaded closure) against a DOM stub carrying one .tag-widget, fire
 * DOMContentLoaded, and hand back the add-form submit plus the fetch queue.
 * window.AjaxStatus is a recorder for the 401 peel, so the case that needs a
 * 401 to still redirect can see it happen. */
function freshWidget() {
  const chips = makeEl();
  const form = makeEl();
  const input = { value: '', focusCalls: 0, focus() { this.focusCalls += 1; } };
  const errorLine = { textContent: '', style: { display: 'none' } };
  const parts = {
    '.tag-chips-container': chips, '.form-add-tag': form,
    '.input-new-tag': input, '.tag-error': errorLine,
  };
  const widget = {
    dataset: { entityType: 'song', entityId: 't1' },
    querySelector(selector) { return parts[selector] || null; },
  };
  const calls = { redirects: 0, errors: [] };

  const pendingFetches = [];
  global.fetch = (url, init) => new Promise((resolve, reject) => {
    pendingFetches.push({ resolve, reject, url, init });
  });
  global.window = {
    location: { href: 'http://localhost/track/t1' },
    AjaxStatus: {
      redirectIfUnauthorized(res) {
        if (res.status !== 401) return false;
        calls.redirects += 1;
        return true;
      },
    },
  };
  let onReady = null;
  global.document = {
    addEventListener(type, fn) { if (type === 'DOMContentLoaded') onReady = fn; },
    querySelector(selector) { return selector === '.tag-widget' ? widget : null; },
    createElement() { return makeEl(); },
    createTextNode(text) { return { text }; },
  };
  console.error = (...args) => { calls.errors.push(args); };

  delete require.cache[MODULE_PATH];
  require(MODULE_PATH);
  assert.strictEqual(typeof onReady, 'function', 'the widget waits for DOMContentLoaded');
  onReady();

  return {
    input, errorLine, calls, pendingFetches,
    submit(tag) {
      input.value = tag;
      form.handlers.submit({ preventDefault() {} });
    },
    renderedTags() {
      return chips.children.filter((c) => c.className === 'tag-chip').map((c) => c.dataset.tag);
    },
    clickRemove(tag) {
      const btn = chips.querySelectorAll('.btn-remove-tag').find((b) => b.dataset.tag === tag);
      assert.ok(btn, `no remove button rendered for "${tag}"`);
      btn.handlers.click();
    },
  };
}

function okResponse(tags) {
  return { status: 200, ok: true, json: () => Promise.resolve({ success: true, tags }) };
}

//< the refetch's response (FOLLOW-UP B, 2026-09-02 review): GET
//  /api/tags/entity, the same {tags: [...]} shape as every add/remove
//  response - no more re-fetching and re-parsing the whole detail page.
function okEntityTagsResponse(tags) {
  return { status: 200, ok: true, json: () => Promise.resolve({ tags }) };
}

//< drains the whole promise chain (fetch -> json -> outcome -> apply/catch)
function settle() { return new Promise((resolve) => setImmediate(resolve)); }

run('a successful add repaints the row and clears the input', async () => {
  const widget = freshWidget();

  widget.submit('rock');
  widget.pendingFetches[0].resolve(okResponse(['rock']));
  await settle();

  assert.deepStrictEqual(widget.renderedTags(), ['rock']);
  assert.strictEqual(widget.input.value, '');
});

run('two requests in flight at once repaint once from a drained refetch, not either POST body', async () => {
  /* Add "rock", then "chill" before the first answer is back. Each answer
   * carries the server's full list AS OF that request, so the first says
   * ['rock'] and the second ['rock', 'chill'] - the OLD guard (tagSeq alone):
   * landing last, the first used to paint the row without the tag the server
   * had already stored. UT-3 goes further: the server can commit+read the
   * SECOND request before the FIRST's commit lands, so even the tagSeq-
   * favoured latest response's OWN body can be missing a tag - no client
   * sequencing fixes a response that is itself wrong, so neither body is
   * trusted while its sibling is still in flight; only the refetch the drain
   * schedules (once both have settled) may repaint. */
  const widget = freshWidget();

  widget.submit('rock');    //< A - answers LAST
  widget.submit('chill');   //< B - answers first
  widget.pendingFetches[1].resolve(okResponse(['rock', 'chill']));
  await settle();
  assert.deepStrictEqual(widget.renderedTags(), [],
                         'B is still not trusted alone - A is still in flight');

  widget.pendingFetches[0].resolve(okResponse(['rock']));
  await settle();
  assert.strictEqual(widget.pendingFetches.length, 3, 'the drain fires exactly one refetch');
  widget.pendingFetches[2].resolve(okEntityTagsResponse(['rock', 'chill']));
  await settle();

  assert.deepStrictEqual(widget.renderedTags(), ['rock', 'chill'],
                         'the server holds both; the refetch is what repaints them');
});

run('a stale failure does not put an error over a row that is current', async () => {
  const widget = freshWidget();

  widget.submit('rock');    //< A - the network drops it, slowly
  widget.submit('chill');   //< B
  widget.pendingFetches[1].resolve(okResponse(['rock', 'chill']));
  await settle();

  widget.pendingFetches[0].reject(new Error('network down'));
  await settle();

  assert.strictEqual(widget.errorLine.style.display, 'none',
                     'an error about a request the user has moved past is not theirs to act on');
  assert.strictEqual(widget.calls.errors.length, 1, 'it is still logged');

  assert.strictEqual(widget.pendingFetches.length, 3, 'two were in flight, so the drain still refetches once');
  widget.pendingFetches[2].resolve(okEntityTagsResponse(['rock', 'chill']));
  await settle();
  assert.deepStrictEqual(widget.renderedTags(), ['rock', 'chill']);
});

run('two overlapping adds where the second response lacks the first tag', async () => {
  const widget = freshWidget();

  widget.submit('rock');    //< A
  widget.submit('chill');   //< B - answers first; its body omits A's tag entirely
  widget.pendingFetches[1].resolve(okResponse(['chill']));
  await settle();
  assert.deepStrictEqual(widget.renderedTags(), [], 'not trusted while A is still in flight');

  widget.pendingFetches[0].resolve(okResponse(['rock', 'chill']));
  await settle();
  //< FOLLOW-UP B (2026-09-02 review): the drained refetch hits the per-entity
  //  endpoint, not window.location.href - a small JSON response instead of
  //  re-fetching and re-parsing the whole detail page
  assert.strictEqual(widget.pendingFetches[2].url, '/api/tags/entity?entity_type=song&entity_id=t1');
  widget.pendingFetches[2].resolve(okEntityTagsResponse(['rock', 'chill']));
  await settle();

  assert.deepStrictEqual(widget.renderedTags(), ['rock', 'chill'], 'the refetch shows both');
  assert.strictEqual(widget.input.value, '',
                     "the refetch still runs the latest request's onApplied (clears the input)");
});

run('a single add still repaints from its own response, with no extra GET', async () => {
  const widget = freshWidget();

  widget.submit('rock');
  widget.pendingFetches[0].resolve(okResponse(['rock']));
  await settle();

  assert.deepStrictEqual(widget.renderedTags(), ['rock']);
  assert.strictEqual(widget.pendingFetches.length, 1, 'nothing overlapped, so no refetch is issued');
});

run('removing a tag repaints the row and returns focus to the tag input (UT-4c)', async () => {
  // The clicked remove button is gone once the row repaints (renderTags
  // rebuilds the chip row wholesale) - the browser's default is to drop
  // focus to <body>.
  const widget = freshWidget();
  widget.submit('rock');
  widget.pendingFetches[0].resolve(okResponse(['rock']));
  await settle();
  assert.deepStrictEqual(widget.renderedTags(), ['rock']);

  widget.clickRemove('rock');
  widget.pendingFetches[1].resolve(okResponse([]));
  await settle();

  assert.deepStrictEqual(widget.renderedTags(), []);
  assert.strictEqual(widget.input.focusCalls, 1,
                     'focus must not be left on the now-removed button');
});

run('a superseded 401 still sends the user to log in', async () => {
  /* The guard sits AFTER the 401 peel and never before it: a 401 is news
   * about the SESSION, which every in-flight submit shares, so whichever one
   * notices acts on it (the wrapped.js rule). */
  const widget = freshWidget();

  widget.submit('rock');    //< A - meets the expired session, answers last
  widget.submit('chill');   //< B
  widget.pendingFetches[1].resolve(okResponse(['rock', 'chill']));
  await settle();

  widget.pendingFetches[0].resolve({ status: 401, ok: false, json: () => Promise.resolve({}) });
  await settle();

  assert.strictEqual(widget.calls.redirects, 1);
});

(async () => {
  const only = process.argv[2] || '';
  const selected = tests.filter(([name]) => name.includes(only));
  assert.ok(selected.length > 0, `no case matches "${only}"`);
  for (const [name, fn] of selected) {
    try {
      await fn();
      console.log(`ok - ${name}`);
    } catch (err) {
      realConsoleError(`FAIL - ${name}`);
      realConsoleError(err);
      process.exit(1);
    }
  }
  console.log(`All ${selected.length} tags-widget tests passed.`);
})();
