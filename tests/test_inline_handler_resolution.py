"""Every inline on*= handler must resolve on the page that renders it.

tests/test_inline_handlers.py already forbids NEW inline handlers. This asks a
different question about the ones that exist: does the function actually exist
*on that page*? The existing shape of that check scans all of static/js, so a
handler whose function lives in a script the page never loads passes it and then
dies in the browser as a silent ReferenceError - nothing 500s, nothing logs, the
control just stops working.

So this resolves per PAGE, the way a browser does:

  * scripts come from the page plus the layout it extends (upward), because that
    is where <script> tags live;
  * handlers come from the page plus every partial it includes, transitively
    (downward), because that is where the on*= attributes live.

Two deliberate limits, both chosen to make this zero-false-positive rather than
maximally strict - a guard that cries wolf gets muted, and a muted guard is
worth less than none:

  * A conditional extends (`{% extends "a.html" if x else "b.html" %}`, which
    wrapped.html uses for its public twin) contributes BOTH layouts, and a
    handler counts as resolved if it resolves under EITHER. Jinja conditionals
    are not evaluated here, so a handler that is itself gated off in one of the
    two layouts (wrapped.html's share modal is `{% if not publicView %}`) must
    not be reported. What still gets caught is the real defect: a handler that
    resolves under NO layout at all.
  * String literals are stripped from a handler body before looking for calls,
    because prose inside one reads as a call otherwise - admin.html's restart
    confirm() says "if the process is supervised (a relaunch-on-exit ...)".
"""
import re
import pathlib
import unittest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "templates"
JS_DIR = REPO_ROOT / "static" / "js"

HANDLER_ATTR = re.compile(r'\son[a-z]+\s*=\s*"([^"]*)"', re.I)
#< every quoted name in the tag, so a conditional extends yields both layouts
EXTENDS_NAMES = re.compile(r'{%-?\s*extends\s(.*?)%}', re.S)
QUOTED = re.compile(r'[\'"]([^\'"]+\.html)[\'"]')
INCLUDE = re.compile(r'{%-?\s*include\s*[\'"]([^\'"]+)[\'"]')
SCRIPT_SRC = re.compile(r"filename='js/([A-Za-z0-9_\-./]+\.js)'")
#< not preceded by a dot: `document.getElementById(` is a method on a global,
#  not a bare function the page has to define
BARE_CALL = re.compile(r'(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(')
STRING_LITERAL = re.compile(r"'(?:[^'\\]|\\.)*'|`(?:[^`\\]|\\.)*`")
DEFINITION = re.compile(
    r'(?:function\s+([A-Za-z_$][\w$]*)'
    r'|window\.([A-Za-z_$][\w$]*)\s*='
    r'|(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=)'
)

# Globals the browser provides, plus the operators BARE_CALL cannot tell from a
# call. Anything genuinely defined by this app must NOT be listed here - that is
# what the test is for.
BROWSER_GLOBALS = {
    "if", "for", "while", "return", "typeof", "new", "switch", "catch", "delete",
    "alert", "confirm", "prompt", "parseInt", "parseFloat", "isNaN", "String",
    "Number", "Boolean", "Array", "Object", "JSON", "Math", "Date", "RegExp",
    "Set", "Map", "Promise", "encodeURIComponent", "decodeURIComponent",
    "setTimeout", "setInterval", "clearTimeout", "clearInterval", "fetch",
    "requestAnimationFrame", "getComputedStyle", "structuredClone",
}


def _read(relativePath: str) -> str:
    path = TEMPLATES / relativePath
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""


def _layoutChain(relativePath: str, seen=None) -> set:
    """`relativePath` plus every template it extends, following BOTH branches of
    a conditional extends."""
    seen = seen if seen is not None else set()
    if relativePath in seen:
        return seen
    seen.add(relativePath)
    for tag in EXTENDS_NAMES.findall(_read(relativePath)):
        for name in QUOTED.findall(tag):
            _layoutChain(name, seen)
    return seen


def _includeTree(relativePath: str, seen=None) -> set:
    """`relativePath` plus every partial it includes, transitively."""
    seen = seen if seen is not None else set()
    if relativePath in seen:
        return seen
    seen.add(relativePath)
    for name in INCLUDE.findall(_read(relativePath)):
        _includeTree(name, seen)
    return seen


def _namesDefinedIn(scriptName: str) -> set:
    path = JS_DIR / scriptName
    if not path.exists():
        return set()
    found = set()
    for match in DEFINITION.finditer(path.read_text(encoding="utf-8", errors="replace")):
        found |= {name for name in match.groups() if name}
    return found


def _pageTemplates() -> list:
    """Templates that extend a layout, i.e. the ones a browser actually loads.
    Partials are covered through the pages that include them."""
    pages = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        relativePath = path.relative_to(TEMPLATES).as_posix()
        if EXTENDS_NAMES.search(_read(relativePath)):
            pages.append(relativePath)
    return pages


def _resolvableNames(page: str) -> set:
    """Every function name reachable from `page`: its scripts' definitions plus
    anything declared in an inline <script> anywhere in its family."""
    family = set()
    for member in _layoutChain(page):
        family |= _includeTree(member)

    scripts, names = set(), set()
    for member in family:
        text = _read(member)
        scripts |= set(SCRIPT_SRC.findall(text))
        for match in DEFINITION.finditer(text):
            names |= {name for name in match.groups() if name}
    for script in scripts:
        names |= _namesDefinedIn(script)
    return names


def _handlerCalls(page: str) -> list:
    """(partial, handlerBody, functionName) for every bare call in every inline
    handler `page` renders."""
    calls = []
    for member in sorted(_includeTree(page)):
        for body in HANDLER_ATTR.findall(_read(member)):
            withoutStrings = STRING_LITERAL.sub("''", body)
            for name in BARE_CALL.findall(withoutStrings):
                if name not in BROWSER_GLOBALS:
                    calls.append((member, body, name))
    return calls


class TestInlineHandlerResolution(unittest.TestCase):
    def test_every_inline_handler_resolves_on_its_own_page(self):
        unresolved = []
        for page in _pageTemplates():
            available = _resolvableNames(page)
            for member, body, name in _handlerCalls(page):
                if name not in available:
                    unresolved.append(
                        f"{page}: {name}() is not defined by any script this page loads "
                        f"(handler in {member}: {body.strip()[:70]!r})"
                    )
        self.assertEqual(unresolved, [], "\n" + "\n".join(unresolved))

    def test_the_audit_actually_inspects_pages_and_handlers(self):
        """A guard that silently walked zero templates would pass forever. Pins
        that the discovery half found real work to do, so the assertion above is
        a statement about the app rather than about an empty loop."""
        pages = _pageTemplates()
        self.assertGreater(len(pages), 10, "page discovery collapsed")

        checked = {page: _handlerCalls(page) for page in pages}
        self.assertTrue(any(checked.values()), "no inline handlers were examined at all")

        #< the case v1 of this audit missed entirely: a handler that lives in an
        #  INCLUDED partial rather than in the page's own text
        fromPartials = [(page, member) for page, calls in checked.items()
                        for member, _body, _name in calls if member != page]
        self.assertTrue(fromPartials,
                        "no handler was reached through an include - the downward "
                        "walk is not doing anything")

    def test_a_known_handler_resolves_through_the_layout(self):
        """The positive control, naming a real pair: _pagination.html's
        onkeydown calls handleJumpToPageKeydown, which only layout-chrome.js
        defines and only layout.html loads. If the script/definition scanning
        breaks, this fails rather than the whole suite going quietly green."""
        self.assertIn("handleJumpToPageKeydown", _namesDefinedIn("layout-chrome.js"))
        self.assertIn("handleJumpToPageKeydown", _resolvableNames("history.html"))

    def test_an_undefined_handler_would_be_caught(self):
        """The negative control: the resolver must not answer "available" for a
        name nothing defines. Without this, a resolver that returned every
        identifier would pass the main test on any codebase."""
        self.assertNotIn("thisFunctionDoesNotExistAnywhere", _resolvableNames("history.html"))
