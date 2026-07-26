"""Every function an inline on*= attribute calls must actually exist somewhere.

Templates wire behaviour through attributes like
`onchange="updateFilters()"`, which resolve off `window` at CLICK time - so a
function that moved, was renamed, or was lost while extracting a `<script>`
block into static/js fails silently in the browser and nowhere else. Nothing
else in the suite can see it: the Python tests assert on rendered markup (the
attribute is still there) and ESLint only reads .js files (it never sees the
attribute at all).

That gap is exactly the risk in moving the ~1,000 lines of JavaScript still
living inside template <script> blocks out to static/js, so this pins the
contract before the moves rather than after.

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


def _inlineHandlerCalls():
    """{function name: [templates that call it]} across every template."""
    calls: dict[str, list[str]] = {}
    for path in sorted(TEMPLATES_DIR.rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for attributeValue in INLINE_HANDLER_ATTR.findall(text):
            code = STRING_LITERAL.sub("''", attributeValue)
            for name in CALLED_NAME.findall(code):
                if name in BUILTIN_OR_METHOD_CALLS:
                    continue
                calls.setdefault(name, []).append(path.name)
    return calls


def _definitionSources():
    """Everything a template's inline handler could resolve against: the linted
    static scripts, plus the template <script> blocks that haven't moved yet."""
    sources = [path.read_text(encoding="utf-8", errors="replace")
               for path in STATIC_JS_DIR.glob("*.js")]
    sources += [path.read_text(encoding="utf-8", errors="replace")
                for path in TEMPLATES_DIR.rglob("*.html")]
    return sources


def _isDefinedIn(name: str, sources) -> bool:
    patterns = [re.compile(pattern.format(name=re.escape(name))) for pattern in DEFINITION_PATTERNS]
    return any(pattern.search(source) for source in sources for pattern in patterns)


class TestInlineHandlersResolve(unittest.TestCase):
    def test_every_inline_handler_target_is_defined_somewhere(self):
        sources = _definitionSources()
        calls = _inlineHandlerCalls()

        self.assertTrue(calls, "no inline handlers found - the scan itself is broken")

        missing = {name: templates for name, templates in calls.items()
                   if not _isDefinedIn(name, sources)}

        self.assertEqual(missing, {},
                         "inline on*= handlers call functions nothing defines - they will fail "
                         "silently in the browser")

    def test_the_scan_would_notice_a_missing_definition(self):
        """A guard test that never fails is worse than none: prove it can."""
        self.assertFalse(_isDefinedIn("aFunctionNobodyDefines", _definitionSources()))


if __name__ == "__main__":
    unittest.main()
