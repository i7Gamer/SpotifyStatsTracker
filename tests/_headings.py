"""The heading-outline assertion shared by the page tests (2026-09-02 review,
UI-05): a rendered page has exactly one h1 and never drops more than one
level between consecutive headings, which is what a screen reader's
heading navigation relies on.

Imported by bare module name (``from _headings import ...``) like the
suite's other shared helpers, since tests/ is on sys.path with no package
__init__.
"""
import re

import bs4

_HEADING_TAG = re.compile(r"^h[1-6]$")


def headingLevels(html):
    """The heading levels of `html` in document order, e.g. [1, 2, 3, 3, 2]."""
    soup = bs4.BeautifulSoup(html, "html.parser")
    return [int(tag.name[1]) for tag in soup.find_all(_HEADING_TAG)]


def assertHeadingOrder(testCase, html, label=""):
    """Exactly one h1, and no consecutive pair that skips a level downwards
    (h1 -> h3). Climbing back up by any amount (h3 -> h2) is fine."""
    levels = headingLevels(html)
    testCase.assertEqual(levels.count(1), 1, f"{label}: expected exactly one h1 in {levels}")
    for previous, following in zip(levels, levels[1:]):
        testCase.assertLessEqual(following, previous + 1,
                                 f"{label}: h{previous} -> h{following} skips a level in {levels}")
