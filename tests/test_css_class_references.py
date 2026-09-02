"""Ratchets on style.css hygiene (2026-09-02 review, UI-10).

The stylesheet had grown nine class selectors nothing could hit, two pills
declared twice under different names (edit one, forget the other), and two
`:focus` rules declared twice so that the first box-shadow never applied.
None of that shows in a browser, which is why it accumulated; these guards
make the next case fail here instead.

The rules are EVALUATED with soupsieve against stub markup rather than
matched as strings - a string guard only proves the text is present, not whom
the rule reaches (the page-gap guard in test_css_accessibility.py records how
that went wrong twice).
"""
import os
import re
import unittest

import bs4

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")

#< where a class name may legitimately be referenced: the markup, the page
#  scripts and the Python that hands class names to templates. tests/ is
#  deliberately NOT a reference - a test naming a dead class would keep it
#  alive; vendor/ is htmx's own source
_REFERENCE_DIRS = ("templates", "static/js", "routes", "dashboard", "services", "Database")
_REFERENCE_ROOT_FILES = ("app.py", "config.py", "wsgi.py")
_REFERENCE_SUFFIXES = (".html", ".js", ".py")
_SKIPPED_DIRS = {"vendor", "__pycache__", "Data"}

#< class names the stylesheet declares whole but the code builds from a prefix
#  plus a runtime value, so no whole-token grep can see them. Each entry names
#  the file and the literal that constructs it, and the test checks that
#  literal is still there - an entry whose constructor has gone is a dead
#  class hiding behind the allowlist
_CONSTRUCTED_CLASSES = {
    "badge-full": ("templates/_play_log_rows.html", "badge-{{"),
    "badge-partial": ("templates/_play_log_rows.html", "badge-{{"),
    "badge-skip": ("templates/_play_log_rows.html", "badge-{{"),
    "play-type-full": ("templates/_play_log_rows.html", "play-type-{{"),
    "play-type-partial": ("templates/_play_log_rows.html", "play-type-{{"),
    "play-type-skip": ("templates/_play_log_rows.html", "play-type-{{"),
    "leader-mine": ("templates/_compare_stats_table.html", "leader-' ~ side"),
    "leader-theirs": ("templates/_compare_stats_table.html", "leader-' ~ side"),
    "status-healthy": ("static/js/layout-chrome.js", "status-${"),
    "status-degraded": ("static/js/layout-chrome.js", "status-${"),
    "status-dead": ("static/js/layout-chrome.js", "status-${"),
    #< htmx adds this class to the element that owns an in-flight request
    "htmx-request": ("static/js/vendor/htmx.min.js", "htmx-request"),
}

#< the two `:focus` rules that were declared twice: the LATER copy is what
#  applied, so it is the one that survives, and this is its focus ring
_ONCE_DECLARED_FOCUS_SELECTORS = (
    '.filter-search input[type="search"]:focus',
    '.filter-group input[type="date"]:focus',
)
_FOCUS_RING = "0 0 0 2px var(--accent-glow)"

#< soupsieve accepts these but never matches them; stripping them evaluates
#  the rule as if the element were in that state
_STATE_PSEUDO_CLASSES = re.compile(r":(?:hover|active|focus|focus-within|focus-visible)\b")
#< a pseudo-element (`::placeholder`) or vendor pseudo (`:-webkit-...`) styles
#  something soupsieve cannot select and that is not the element itself
_UNSELECTABLE = re.compile(r"::|:-")


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class _Rule:
    """One `selector-list { body }`, with its nesting depth (0 = top level,
    1 = inside an @media block), the prelude of the at-rule enclosing it
    ("" at top level) and source order."""

    def __init__(self, order, depth, selectorText, body, enclosing):
        self.order = order
        self.depth = depth
        self.selectors = [" ".join(part.split()) for part in selectorText.split(",")]
        self.body = body
        self.enclosing = " ".join(enclosing.split())

    def declaration(self, prop):
        found = re.search(r"(?:^|;)\s*" + re.escape(prop) + r"\s*:\s*([^;]+)", self.body)
        return found.group(1).strip() if found else None

    def hits(self, soup, element):
        #< by identity: a bs4 Tag compares by markup, so two stubs with the
        #  same class would read as one (traps memory)
        for selector in self.selectors:
            if _UNSELECTABLE.search(selector):
                continue
            matched = soup.select(_STATE_PSEUDO_CLASSES.sub("", selector))
            if any(hit is element for hit in matched):
                return True
        return False


def _parseRules(css):
    """A brace walk over the comment-stripped stylesheet. Declarations never
    contain `{`, so the text between one brace and the next is either a
    selector list or an at-rule prelude."""
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    rules = []
    depth = 0
    buf = []
    pendingSelector = []
    for ch in css:
        if ch == "{":
            text = "".join(buf).strip()
            buf = []
            pendingSelector.append(text)
            depth += 1
        elif ch == "}":
            text = pendingSelector.pop()
            body = "".join(buf).strip()
            buf = []
            depth -= 1
            if text and not text.startswith("@"):
                enclosing = pendingSelector[-1] if pendingSelector else ""
                rules.append(_Rule(len(rules), depth, text, body, enclosing))
        else:
            buf.append(ch)
    return rules


def _classSelectors(rules):
    names = set()
    for rule in rules:
        for selector in rule.selectors:
            names.update(re.findall(r"\.([A-Za-z_][\w-]*)", selector))
    return names


def _referenceCorpus():
    chunks = []
    for name in _REFERENCE_ROOT_FILES:
        chunks.append(_readFile(os.path.join(_ROOT, name)))
    for sub in _REFERENCE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(_ROOT, sub)):
            dirnames[:] = [d for d in dirnames if d not in _SKIPPED_DIRS]
            for filename in filenames:
                if filename.endswith(_REFERENCE_SUFFIXES):
                    chunks.append(_readFile(os.path.join(dirpath, filename)))
    return "\n".join(chunks)


class TestEveryClassSelectorIsReferenced(unittest.TestCase):
    def setUp(self):
        self.rules = _parseRules(_readFile(_CSS_PATH))

    def test_every_class_selector_has_a_whole_token_reference(self):
        corpus = _referenceCorpus()
        dead = sorted(
            name for name in _classSelectors(self.rules)
            if name not in _CONSTRUCTED_CLASSES
            and not re.search(r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", corpus)
        )

        self.assertEqual(dead, [], "class selectors in style.css nothing references")

    def test_every_allowlisted_class_is_still_constructed_where_it_says(self):
        declared = _classSelectors(self.rules)
        for name, (relPath, literal) in _CONSTRUCTED_CLASSES.items():
            with self.subTest(name=name):
                self.assertIn(name, declared, "allowlist entry for a class the stylesheet no longer declares")
                #< assertTrue rather than assertIn: a miss would print the whole file
                self.assertTrue(literal in _readFile(os.path.join(_ROOT, relPath)),
                                f"{relPath} no longer constructs {name!r} with {literal!r}")


class TestTheYearBadgeAndTheFilterButtonAreOnePill(unittest.TestCase):
    """`.wrapped-year-badge` (an <a>) and `.stats-filter-button` (a <button>)
    are the same accent pill; they were declared as two rule sets that had
    to be edited in step. Now one selector list serves both, so the
    stylesheet must not be able to tell the two class names apart."""

    def setUp(self):
        self.rules = _parseRules(_readFile(_CSS_PATH))

    def _rulesHitting(self, markup):
        """Orders of the top-level rules that reach the stub - indexes into
        self.rules, which is why that list is not pre-filtered by depth."""
        soup = bs4.BeautifulSoup(markup, "html.parser")
        element = soup.find(True)
        return [rule.order for rule in self.rules if rule.depth == 0 and rule.hits(soup, element)]

    def test_the_two_pill_classes_are_hit_by_the_same_rules(self):
        badge = self._rulesHitting('<a class="wrapped-year-badge active"></a>')
        button = self._rulesHitting('<a class="stats-filter-button active"></a>')

        self.assertTrue(badge)
        self.assertEqual(badge, button)

    def test_the_shared_rules_paint_the_selected_fill(self):
        fills = [self.rules[order].declaration("background")
                 for order in self._rulesHitting('<a class="stats-filter-button active"></a>')]

        self.assertIn("var(--accent)", fills)

    def test_the_two_pill_rows_share_their_layout_rule(self):
        badges = self._rulesHitting('<nav class="wrapped-year-badges"></nav>')
        filters = self._rulesHitting('<nav class="wrapped-stats-filters"></nav>')

        #< a subset, not equality: .wrapped-stats-filters also carries a
        #  page-level margin nudge (`!important`, in the Wrapped block) that
        #  the year badges do not
        self.assertTrue(badges)
        self.assertTrue(set(badges) <= set(filters))
        self.assertIn("flex", [self.rules[order].declaration("display") for order in badges])


class TestFocusRulesAreDeclaredOnce(unittest.TestCase):
    """A selector declared twice at top level means the first copy's
    declarations silently lose to the second; the survivor must be the copy
    that was actually applying."""

    def setUp(self):
        self.rules = [rule for rule in _parseRules(_readFile(_CSS_PATH)) if rule.depth == 0]

    def test_each_focus_selector_is_declared_once_with_the_later_ring(self):
        for selector in _ONCE_DECLARED_FOCUS_SELECTORS:
            with self.subTest(selector=selector):
                declaring = [rule for rule in self.rules if selector in rule.selectors]

                self.assertEqual(len(declaring), 1)
                self.assertEqual(declaring[0].declaration("box-shadow"), _FOCUS_RING)


if __name__ == "__main__":
    unittest.main()
