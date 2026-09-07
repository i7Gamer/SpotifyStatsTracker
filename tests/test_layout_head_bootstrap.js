// Plain-node unit test for the inline <head> bootstrap both layouts carry
// (templates/layout.html and templates/layout_public.html): the theme is read
// back out of localStorage, and window.PLACEHOLDER_IMG is defined for any
// <img onerror> that fires while the body is still parsing.
//
// The read used to sit outside any guard. Reading localStorage THROWS - not
// returns null - when a browser blocks site data (Firefox's "block all
// cookies", Safari with storage off, a page in a storage-partitioned frame),
// and the throw aborted the script before the placeholder line ran, so every
// failed cover then requested GET /undefined (2026-09-07 review, item 15).
//
// The script under test is extracted from the templates themselves rather
// than copied here, so this cannot drift from what the pages ship. The one
// Jinja expression in it is substituted with a literal.
//
// No test framework/dependency - run with:
//   node tests/test_layout_head_bootstrap.js
const assert = require('assert');
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const TEMPLATES = ['layout.html', 'layout_public.html'].map(
  (name) => path.join(__dirname, '..', 'templates', name));
const PLACEHOLDER = 'data:image/svg+xml,stub';
const DEFAULT_THEME = 'theme-rose';

function headScript(templatePath) {
  const html = fs.readFileSync(templatePath, 'utf8');
  const head = html.slice(html.indexOf('<head>'), html.indexOf('</head>'));
  // Plain index slicing, not a tag regex: this extracts the ONE inline script
  // from our own template, and a regex shaped like an HTML filter draws
  // CodeQL's js/bad-tag-filter queue (uppercase, `</script >`, comments...)
  // one complaint per scan - the same reason the Python inline-script gate
  // moved to HTMLParser. Bare `<script>` (no attributes) is what the head's
  // inline block is; the later `<script src=...>` tags never match it.
  const lowered = head.toLowerCase();
  const open = lowered.indexOf('<script>');
  const close = open === -1 ? -1 : lowered.indexOf('</script', open);
  assert.ok(open !== -1 && close !== -1, `${templatePath}: no inline <script> in <head>`);
  const source = head.slice(open + '<script>'.length, close);
  assert.ok(source.includes('placeholderImgDataUri'), `${templatePath}: the placeholder line moved out of the head script`);
  return source.replace(/\{\{\s*placeholderImgDataUri\s*\|\s*tojson\s*\}\}/, JSON.stringify(PLACEHOLDER));
}

function runHead(templatePath, localStorage) {
  const context = {
    document: { documentElement: { className: '' } },
    localStorage,
  };
  context.window = context;
  vm.runInNewContext(headScript(templatePath), context);
  return context;
}

const throwingStorage = {
  getItem() { throw new Error('SecurityError: The operation is insecure.'); },
};

function run(name, fn) {
  try {
    fn();
    console.log(`ok - ${name}`);
  } catch (err) {
    console.error(`FAIL - ${name}`);
    throw err;
  }
}

for (const templatePath of TEMPLATES) {
  const label = path.basename(templatePath);

  run(`${label}: a saved theme is applied`, () => {
    const ctx = runHead(templatePath, { getItem: () => 'theme-ocean' });
    assert.strictEqual(ctx.document.documentElement.className, 'theme-ocean');
    assert.strictEqual(ctx.window.PLACEHOLDER_IMG, PLACEHOLDER);
  });

  run(`${label}: no saved theme falls back to the default`, () => {
    const ctx = runHead(templatePath, { getItem: () => null });
    assert.strictEqual(ctx.document.documentElement.className, DEFAULT_THEME);
  });

  run(`${label}: blocked storage still defines the placeholder and the default theme`, () => {
    const ctx = runHead(templatePath, throwingStorage);
    assert.strictEqual(ctx.window.PLACEHOLDER_IMG, PLACEHOLDER);
    assert.strictEqual(ctx.document.documentElement.className, DEFAULT_THEME);
  });

  run(`${label}: a page with no localStorage binding at all still boots`, () => {
    //< the accessor itself is what throws in some embeddings, not getItem
    const context = { document: { documentElement: { className: '' } } };
    context.window = context;
    Object.defineProperty(context, 'localStorage', {
      get() { throw new Error('SecurityError'); },
    });
    vm.runInNewContext(headScript(templatePath), context);
    assert.strictEqual(context.window.PLACEHOLDER_IMG, PLACEHOLDER);
    assert.strictEqual(context.document.documentElement.className, DEFAULT_THEME);
  });
}
