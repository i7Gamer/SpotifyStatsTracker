"""Every function an inline on*= attribute calls must exist ON THE PAGE THAT
RENDERS IT.

Templates wire behaviour through attributes like `onchange="updateFilters()"`,
which resolve off `window` at CLICK time - so a function that moved, was
renamed, or lives in a script this page never loads fails silently in the
browser and nowhere else. Nothing else in the suite can see it: the Python
tests assert on rendered markup (the attribute is still there) and ESLint only
reads .js files (it never sees the attribute at all).

This check originally resolved names against ALL of static/js plus every
template - one global pool - which passed a handler wired to a script the page
never loads. Two tiers close that blind spot, because two render paths exist:

- PAGE templates (an `{% extends %}` tag, or their own `<html>`): the handler
  must be defined within the page's render closure - the page itself, its
  extends chain, its includes/imports (transitively), and the static scripts
  any of those load. A conditional `{% extends "a.html" if x else "b.html" %}`
  contributes BOTH layouts (wrapped.html really renders under either), which
  makes the closure a union over branches: optimistic where an `{% if %}`
  gates which chrome provides a script, but never wrong about a script no
  branch loads - the failure mode this exists to catch.
- FRAGMENT templates (rendered by routes for AJAX/htmx injection, so no page
  closure reaches them statically): which page fetches them is a route-level
  fact no template scan can see, so they keep the global-pool check.

It deliberately checks NAMES, not behaviour: a name that resolves is the one
thing a static check can promise here, and it is the failure mode that would
otherwise reach production.
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
STATIC_JS_DIR = REPO_ROOT / "static" / "js"

# Attribute values are JavaScript, so plenty of what they "call" is built in or
# a method on an expression rather than a global this project defines.
BUILTIN_OR_METHOD_CALLS = {
    "confirm", "alert", "getElementById", "querySelector", "querySelectorAll",
    "preventDefault", "stopPropagation", "reload", "focus", "blur", "submit",
    "parseInt", "parseFloat", "encodeURIComponent", "decodeURIComponent",
    "setTimeout", "clearTimeout", "toLowerCase", "toUpperCase", "trim",
}

INLINE_HANDLER_ATTR = re.compile(r"\bon[a-z]+\s*=\s*\"([^\"]*)\"")
CALLED_NAME = re.compile(r"([A-Za-z_$][\w$]*)\s*\(")
#< prose inside a confirm()/alert() message is not code: "…is supervised (a
#  relaunch-on-exit script)" would otherwise read as a call to `supervised`
STRING_LITERAL = re.compile(r"'[^']*'|`[^`]*`")
#< `function name(`, `window.name =`, `var/let/const name =` - the three shapes
#  a browser global is defined by in this codebase
DEFINITION_PATTERNS = (
    r"function\s+{name}\s*\(",
    r"window\.{name}\s*=",
    r"\b(?:var|let|const)\s+{name}\s*=",
)
#< one tag, all its quoted template names: a conditional extends carries two
REF_TAG = re.compile(r"\{%-?\s*(?:extends|include|import|from)\s+([^%]*?)-?%\}")
QUOTED_TEMPLATE_NAME = re.compile(r"['\"]([^'\"]+\.html)['\"]")
EXTENDS_TAG = re.compile(r"\{%-?\s*extends\b")
SCRIPT_FILENAME = re.compile(r"filename=['\"]js/([^'\"]+)['\"]")


def _readTemplates() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8", errors="replace")
            for path in TEMPLATES_DIR.rglob("*.html")}


def _readStaticJs() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8", errors="replace")
            for path in STATIC_JS_DIR.glob("*.js")}


def _referencedTemplates(body: str) -> list[str]:
    names = []
    for tagBody in REF_TAG.findall(body):
        names.extend(QUOTED_TEMPLATE_NAME.findall(tagBody))
    return names


def _renderClosure(name: str, templates: dict[str, str]) -> set[str]:
    """The template files whose markup can appear in one render of `name`:
    itself, its extends-ancestors and their includes, transitively."""
    seen: set[str] = set()
    stack = [name]
    while stack:
        current = stack.pop()
        if current in seen or current not in templates:
            continue
        seen.add(current)
        stack.extend(_referencedTemplates(templates[current]))
    return seen


def _scriptsLoadedBy(closureNames: set[str], templates: dict[str, str]) -> set[str]:
    loaded: set[str] = set()
    for name in closureNames:
        loaded.update(SCRIPT_FILENAME.findall(templates[name]))
    return loaded


def _handlerCalls(bodies: dict[str, str]) -> dict[str, list[str]]:
    """{function name: [templates that call it]} across the given bodies."""
    calls: dict[str, list[str]] = {}
    for name in sorted(bodies):
        for attributeValue in INLINE_HANDLER_ATTR.findall(bodies[name]):
            code = STRING_LITERAL.sub("''", attributeValue)
            for called in CALLED_NAME.findall(code):
                if called in BUILTIN_OR_METHOD_CALLS:
                    continue
                calls.setdefault(called, []).append(name)
    return calls


def _isDefinedIn(name: str, sources) -> bool:
    patterns = [re.compile(pattern.format(name=re.escape(name))) for pattern in DEFINITION_PATTERNS]
    return any(pattern.search(source) for source in sources for pattern in patterns)


def _pageNames(templates: dict[str, str]) -> list[str]:
    return sorted(name for name, body in templates.items()
                  if EXTENDS_TAG.search(body) or "<html" in body)


def _pageSources(page: str, templates: dict[str, str], js: dict[str, str]):
    """(closure, everything a handler on this page can resolve against)."""
    closure = _renderClosure(page, templates)
    loaded = _scriptsLoadedBy(closure, templates)
    sources = [js[s] for s in sorted(loaded) if s in js]
    sources += [templates[f] for f in sorted(closure)]
    return closure, sources


class TestPageHandlersResolveOnTheirPage(unittest.TestCase):
    def test_every_page_resolves_its_own_handlers(self):
        templates = _readTemplates()
        js = _readStaticJs()
        pages = _pageNames(templates)
        self.assertTrue(pages, "no page templates found - the scan itself is broken")

        anyCalls = False
        for page in pages:
            with self.subTest(page=page):
                closure, sources = _pageSources(page, templates, js)
                calls = _handlerCalls({f: templates[f] for f in closure})
                anyCalls = anyCalls or bool(calls)
                missing = {}
                for name, sites in calls.items():
                    if _isDefinedIn(name, sources):
                        continue
                    definers = sorted(s for s, body in js.items()
                                      if _isDefinedIn(name, [body]))
                    missing[name] = (sorted(set(sites)),
                                     f"defined in {definers}" if definers else "defined NOWHERE")
                self.assertEqual(
                    missing, {},
                    f"{page} renders on*= handlers it cannot resolve - the "
                    "definitions live in scripts this page never loads, so the "
                    "control dies silently in the browser")
        self.assertTrue(anyCalls, "no inline handlers found - the scan itself is broken")


class TestFragmentHandlersResolveSomewhere(unittest.TestCase):
    """Route-rendered fragments execute inside whichever page fetched them;
    that mapping lives in routes and JS, out of a template scan's sight. The
    global pool is the honest check that remains - it still catches a rename
    losing its definition entirely."""

    def test_every_fragment_handler_is_defined_somewhere(self):
        templates = _readTemplates()
        js = _readStaticJs()
        pages = _pageNames(templates)
        reachable: set[str] = set()
        for page in pages:
            reachable |= _renderClosure(page, templates)
        fragments = {name: body for name, body in templates.items()
                     if name not in reachable}
        self.assertTrue(fragments,
                        "no fragment templates found - if the AJAX fragments all "
                        "became page-reachable, fold this tier into the page test")

        sources = list(js.values()) + list(templates.values())
        missing = {name: sorted(set(sites))
                   for name, sites in _handlerCalls(fragments).items()
                   if not _isDefinedIn(name, sources)}
        self.assertEqual(missing, {},
                         "fragment on*= handlers call functions nothing defines - "
                         "they will fail silently in the browser")


class TestTheScanNoticesWhatItMustNotice(unittest.TestCase):
    """Guard tests that never fail are worse than none: prove each tier can."""

    def test_the_scan_would_notice_a_missing_definition(self):
        self.assertFalse(_isDefinedIn("aFunctionNobodyDefines",
                                      list(_readStaticJs().values())))

    def test_a_handler_wired_to_a_script_another_page_loads_is_flagged(self):
        """THE blind spot the per-page tier exists for: the global pool knows
        the name, but this page never loads its script."""
        templates = {
            "layout.html": "<html><script src=\"{{ url_for('static', "
                           "filename='js/common.js') }}\"></script></html>",
            "a.html": '{% extends "layout.html" %}'
                      '<button onclick="sharedThing()">go</button>',
            "b.html": '{% extends "layout.html" %}'
                      "<script src=\"{{ url_for('static', "
                      "filename='js/b-page.js') }}\"></script>",
        }
        js = {"common.js": "", "b-page.js": "function sharedThing() {}"}

        _, sourcesA = _pageSources("a.html", templates, js)
        _, sourcesB = _pageSources("b.html", templates, js)

        self.assertFalse(_isDefinedIn("sharedThing", sourcesA),
                         "a.html never loads b-page.js, so the pool it resolves "
                         "against must not contain sharedThing")
        self.assertTrue(_isDefinedIn("sharedThing", sourcesB))

    def test_a_conditional_extends_contributes_both_layouts(self):
        """wrapped.html extends layout_public.html OR layout.html depending on
        publicView; a scan reading only the first branch reported the owner
        view's scripts as unloaded (found while building this check)."""
        templates = {
            "owner.html": "<html>owner chrome</html>",
            "public.html": "<html>public chrome</html>",
            "page.html": '{% extends "public.html" if publicView else "owner.html" %}',
        }
        closure = _renderClosure("page.html", templates)
        self.assertIn("owner.html", closure)
        self.assertIn("public.html", closure)


if __name__ == "__main__":
    unittest.main()
