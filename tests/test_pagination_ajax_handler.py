# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static guard: every AJAX-paginated list registers the jump-box hook.

`_pagination.html` prints Prev/Next links AND a "Go to page" input in the same
include. The links are handled per page by a delegated click listener; the
input is not - it calls the shared `handleJumpToPageKeydown`
(static/js/layout-chrome.js), which only stays on the page when the list has
registered `window.__paginationAjaxHandler`, and otherwise falls back to a
full page load.

So the two halves of one control drift apart silently: the detail pages'
Prev/Next swapped in place while the jump box beside them reloaded the whole
page, because detail-history.js never registered a hook. Nothing failed - it
just quietly stopped being an AJAX page for that one input.

Cheap file-level assertions - no browser needed.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_JS_DIR = os.path.join(_ROOT, "static", "js")
_TEMPLATE_DIR = os.path.join(_ROOT, "templates")

_HOOK_NAME = "window.__paginationAjaxHandler"

# The results partial each module swaps, and the module that swaps it. Kept
# explicit rather than derived: the mapping is what a reader has to know to
# add the next paginated list, and there are only three.
_PAGINATED_LISTS = {
    "_history_results.html": "history-page.js",
    "_top_list_results.html": "top-list.js",
    "_detail_history_results.html": "detail-history.js",
}


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestPaginationAjaxHandler(unittest.TestCase):
    def test_every_partial_in_the_map_still_includes_the_pagination_control(self):
        """Guards the map itself: a partial that stopped including
        _pagination.html has no jump box, and its entry below would then be
        asserting nothing at all."""
        for partial in _PAGINATED_LISTS:
            with self.subTest(partial=partial):
                self.assertIn("_pagination.html", _readFile(os.path.join(_TEMPLATE_DIR, partial)))

    def test_the_shared_input_still_defers_to_the_hook(self):
        """If layout-chrome.js stopped consulting the hook, every registration
        below would be dead code and every jump box a full reload again."""
        chrome = _readFile(os.path.join(_JS_DIR, "layout-chrome.js"))
        self.assertIn("handleJumpToPageKeydown", chrome)
        self.assertIn(_HOOK_NAME, chrome)

    def test_every_paginated_list_registers_a_handler(self):
        for partial, module in _PAGINATED_LISTS.items():
            with self.subTest(module=module):
                source = _readFile(os.path.join(_JS_DIR, module))
                self.assertRegex(
                    source, re.escape(_HOOK_NAME) + r"\s*=",
                    f"{module} swaps {partial} (which prints a 'Go to page' input) but never "
                    f"assigns {_HOOK_NAME}, so that input full-reloads while the Prev/Next "
                    "links beside it navigate in place")

    def test_no_handler_pushes_history_state(self):
        """In-page URL updates replaceState so Back leaves the page instead of
        stepping back through page numbers - the same rule the rest of these
        modules follow."""
        for module in _PAGINATED_LISTS.values():
            with self.subTest(module=module):
                self.assertNotIn("history.pushState", _readFile(os.path.join(_JS_DIR, module)))


if __name__ == "__main__":
    unittest.main()
