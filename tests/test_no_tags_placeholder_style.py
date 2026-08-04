# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static guard: the tag widget's "No tags added yet" placeholder is styled by
a class, not by two hand-copied inline strings.

Both copies carried `... color: var(--text-muted, #888888); italic;` - a
declaration with a VALUE and no property name, which every browser drops, so
the placeholder had never once rendered italic. It sat in two places at once
(the template renders the initial state, tags.js re-renders it after the last
tag is removed), which is why editing one would not have been enough anyway.

Cheap file-level assertions - no browser needed.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")
_JS_PATH = os.path.join(_ROOT, "static", "js", "tags.js")
_TEMPLATE_PATH = os.path.join(_ROOT, "templates", "_tag_widget.html")

_PLACEHOLDER_CLASS = "no-tags-text"
# A CSS declaration is `property: value`. A run of `;`-separated pieces that
# has no colon at all is a bare value - the shape that made this a bug.
_DECLARATION_WITHOUT_A_PROPERTY = re.compile(r";\s*([A-Za-z-]+)\s*;")


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestNoTagsPlaceholderStyle(unittest.TestCase):
    def test_the_class_is_styled_in_the_stylesheet(self):
        rule = re.search(rf"\.{_PLACEHOLDER_CLASS}\s*\{{([^}}]*)\}}", _readFile(_CSS_PATH))
        self.assertIsNotNone(rule, f".{_PLACEHOLDER_CLASS} has no rule in style.css")
        self.assertIn("font-style", rule.group(1),
                      "the placeholder is meant to be italic - that is what the broken "
                      "inline copies were trying to say")

    def test_neither_copy_still_carries_its_own_inline_style(self):
        """The two sites drifted apart precisely because each owned a string."""
        for path in (_JS_PATH, _TEMPLATE_PATH):
            with self.subTest(path=os.path.basename(path)):
                for line in _readFile(path).splitlines():
                    if _PLACEHOLDER_CLASS not in line:
                        continue
                    self.assertNotIn("font-size", line,
                                     "style the placeholder through the class, not inline")

    def test_no_style_attribute_in_the_widget_declares_a_value_without_a_property(self):
        """The original defect in its general form: `...; italic;` parses as a
        declaration with no property name and is silently discarded, so the
        style simply never applies and nothing reports it."""
        for path in (_JS_PATH, _TEMPLATE_PATH):
            with self.subTest(path=os.path.basename(path)):
                orphan = _DECLARATION_WITHOUT_A_PROPERTY.search(_readFile(path))
                self.assertIsNone(
                    orphan,
                    f"{orphan.group(1) if orphan else ''!r} is a CSS value with no property name")


if __name__ == "__main__":
    unittest.main()
