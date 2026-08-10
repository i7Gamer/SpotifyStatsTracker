"""Merge review's chrome: the queue holds still, and its entry point is a button.

* The review queue is a work surface - you hover a row to aim at its two
  verdict buttons. The generic `.card` lift/scale slid the row out from under
  the cursor mid-aim, so the queue card opts out of the transform.
* The Admin panel reaches Merge review through a link that looked like body
  text next to a column of Save buttons; it wears the same button classes now.

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


def _read(path):
    with open(path, encoding="utf-8") as handle:
        return handle.read()


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


class TestAdminEntryPointIsAButton(unittest.TestCase):
    def test_the_merge_review_link_wears_the_admin_button_classes(self):
        markup = _read(_ADMIN_PATH)
        anchor = re.search(r"<a[^>]*adminMergeReview[^>]*>", markup)
        self.assertIsNotNone(anchor, "the Merge review link went missing from admin.html")
        for cssClass in _BUTTON_CLASSES:
            self.assertIn(cssClass, anchor.group(0))


if __name__ == "__main__":
    unittest.main()
