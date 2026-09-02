"""`.loading` is the placeholder htmx replaces; `.empty-state` is the answer
(2026-09-02 review, UI-12).

Nineteen "no results" lines were styled with class="loading" because the two
happened to look the same - muted body text. That is a trap for anyone who
gives .loading a spinner or the .loading-fade opacity: every "No plays
recorded yet." would start loading. The two names now mean what they say,
and these ratchets keep them apart in both directions.
"""
import glob
import os
import re
import unittest

import bs4

from tests.test_css_class_references import _parseRules

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")
_TEMPLATES_GLOB = os.path.join(_ROOT, "templates", "*.html")

#< an element with either class, with its inner markup - DOTALL because the
#  compare.html placeholder spreads its hx- attributes over several lines
_CLASSED_ELEMENT = re.compile(r'<(\w+) class="(loading|empty-state)"[^>]*>(.*?)</\1>', re.S)
_LOADING_TEXT = "Loading"


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _classedElements():
    """(template name, class, inner text) for every .loading / .empty-state
    element in the templates."""
    found = []
    for path in sorted(glob.glob(_TEMPLATES_GLOB)):
        for _tag, cls, inner in _CLASSED_ELEMENT.findall(_readFile(path)):
            found.append((os.path.basename(path), cls, " ".join(inner.split())))
    return found


class TestTheTwoClassesMeanWhatTheySay(unittest.TestCase):
    def test_a_loading_element_only_ever_says_loading(self):
        misnamed = [(name, text) for name, cls, text in _classedElements()
                    if cls == "loading" and _LOADING_TEXT not in text]

        self.assertEqual(misnamed, [])

    def test_an_empty_state_element_never_says_loading(self):
        misnamed = [(name, text) for name, cls, text in _classedElements()
                    if cls == "empty-state" and _LOADING_TEXT in text]

        self.assertEqual(misnamed, [])

    def test_the_empty_states_exist(self):
        """The ratchet above would pass with the class renamed away entirely;
        this pins that the swap happened rather than the sites vanishing."""
        self.assertTrue(any(cls == "empty-state" for _name, cls, _text in _classedElements()))


class TestEmptyStateIsMutedBodyText(unittest.TestCase):
    """Evaluated against the stylesheet, not string-matched: the rule must
    reach a bare <p class="empty-state"> and paint it muted, at body size -
    what these lines looked like under .loading, so the rename moved no
    pixels."""

    def setUp(self):
        self.rules = _parseRules(_readFile(_CSS_PATH))

    def _declarationsFor(self, markup):
        soup = bs4.BeautifulSoup(markup, "html.parser")
        element = soup.find(True)
        declared = {}
        for rule in self.rules:
            if rule.depth == 0 and rule.hits(soup, element):
                for prop in ("color", "font-size"):
                    value = rule.declaration(prop)
                    if value is not None:
                        declared[prop] = value
        return declared

    def test_empty_state_is_muted_at_body_size(self):
        declared = self._declarationsFor('<p class="empty-state"></p>')

        self.assertEqual(declared.get("color"), "var(--muted)")
        self.assertNotIn("font-size", declared)


if __name__ == "__main__":
    unittest.main()
