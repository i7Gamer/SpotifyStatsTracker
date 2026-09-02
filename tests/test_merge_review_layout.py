"""Merge review's chrome: the queue holds still, and its entry point is a button.

* The review queue is a work surface - you hover a row to aim at its two
  verdict buttons. The generic `.card` lift/scale slid the row out from under
  the cursor mid-aim, so the queue card opts out of the transform.
* The Admin panel reaches Merge review through a link that looked like body
  text next to a column of Save buttons; it wears the same button classes now.
* A row's two verdicts come from different button bases, and only one of them
  carried a height - so the primary one sat short and top-aligned beside it.

Cheap file-level assertions - no browser needed.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")
_MERGE_REVIEW_PATH = os.path.join(_ROOT, "templates", "merge_review.html")
_ADMIN_PATH = os.path.join(_ROOT, "templates", "admin.html")

_STATIC_CARD_CLASS = "card-static"
_BUTTON_CLASSES = ("primary-button", "button-small")
#< the row's primary verdict ("Same Recording - Merge"), and the base its
#  neighbour ("Not the Same") is built on
_MERGE_VERDICT_SELECTOR = ".merge-release-actions .primary-button"
_NEIGHBOUR_SELECTOR = ".button"


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _declarations(css, selector):
    """What one flat rule declares. Anchored at the line start so `.button`
    cannot match the `.x .button` of a descendant rule above it."""
    match = re.search(r"(?m)^" + re.escape(selector) + r"\s*\{([^}]*)\}", css)
    return match.group(1) if match else None


class TestQueueCardDoesNotMoveOnHover(unittest.TestCase):
    def test_the_queue_card_opts_out_of_the_card_hover_transform(self):
        markup = _read(_MERGE_REVIEW_PATH)
        #< the queue lives in the last card; the message/error cards are tiny
        self.assertIn('class="card %s"' % _STATIC_CARD_CLASS, markup)

    def test_the_opt_out_class_cancels_the_transform_in_css(self):
        css = _read(_CSS_PATH)
        rule = re.search(r"\.%s:hover\s*\{([^}]*)\}" % _STATIC_CARD_CLASS, css)
        self.assertIsNotNone(rule, "no .%s:hover rule in style.css" % _STATIC_CARD_CLASS)
        self.assertIn("transform: none", rule.group(1))

    def test_the_generic_card_hover_still_lifts_everything_else(self):
        css = _read(_CSS_PATH)
        rule = re.search(r"\n\.card:hover\s*\{([^}]*)\}", css)
        self.assertIsNotNone(rule, "the generic .card:hover lift went missing")
        self.assertIn("translateY", rule.group(1))


class TestVerdictButtonsShareOneHeight(unittest.TestCase):
    """The two verdicts sit side by side but come from different bases:
    .primary-button is an inline-block with padding only, .button is an
    inline-flex with a min-height. The row's forms stretch to the taller of
    the two, so the merge button was drawn at its own ~26px and pinned to the
    TOP of the 44px the row had become - reported as "not centered
    vertically". It is also the page's primary action, wearing half the hit
    area of the button it stands next to.

    Asserted AGAINST the neighbour rather than against a literal: the pair
    reads as one control, so what matters is that the two cannot drift apart -
    a test naming 44px twice would pass while they did."""

    def setUp(self):
        self.css = _read(_CSS_PATH)

    def test_the_merge_button_is_as_tall_as_the_button_beside_it(self):
        neighbour = _declarations(self.css, _NEIGHBOUR_SELECTOR)
        self.assertIsNotNone(neighbour, "%s missing from style.css" % _NEIGHBOUR_SELECTOR)
        height = re.search(r"min-height:\s*([^;]+);", neighbour)
        self.assertIsNotNone(height, "%s lost its min-height" % _NEIGHBOUR_SELECTOR)

        verdict = _declarations(self.css, _MERGE_VERDICT_SELECTOR)
        self.assertIsNotNone(verdict, "%s missing from style.css" % _MERGE_VERDICT_SELECTOR)
        self.assertIn("min-height: %s;" % height.group(1), verdict)

    def test_the_merge_button_centers_its_own_label(self):
        """A min-height alone would leave the label sitting on the top line of
        a taller box - the same complaint one level in."""
        verdict = _declarations(self.css, _MERGE_VERDICT_SELECTOR)
        self.assertIsNotNone(verdict, "%s missing from style.css" % _MERGE_VERDICT_SELECTOR)
        self.assertIn("display: inline-flex", verdict)
        self.assertIn("align-items: center", verdict)


class TestAdminEntryPointIsAButton(unittest.TestCase):
    def test_the_merge_review_link_wears_the_admin_button_classes(self):
        markup = _read(_ADMIN_PATH)
        anchor = re.search(r"<a[^>]*adminMergeReview[^>]*>", markup)
        self.assertIsNotNone(anchor, "the Merge review link went missing from admin.html")
        for cssClass in _BUTTON_CLASSES:
            self.assertIn(cssClass, anchor.group(0))


if __name__ == "__main__":
    unittest.main()
