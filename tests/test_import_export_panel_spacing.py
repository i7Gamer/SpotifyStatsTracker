# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Static guard: the Export panel is spaced like the Import panel above it.

Both panels are `.import-card` sections stacked on the Import & Export
page, so any difference in their inner spacing reads as one panel being
squashed relative to the other. `.export-card` used to carry a tighter
`padding` and a wider `gap` of its own; these assertions compare the two rules
against each other rather than against literals, so the panels stay matched
even if the shared values change.

Cheap file-level assertions - no browser needed.
"""
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_CSS_PATH = os.path.join(_ROOT, "static", "css", "style.css")


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


class TestImportExportPanelSpacing(unittest.TestCase):
    def setUp(self):
        self.css = _readFile(_CSS_PATH)

    def test_export_panel_has_the_import_panel_padding(self):
        # `.export-card` is also an `.import-card`, so no padding of its own
        # means it inherits the one above; an explicit copy is fine too as long
        # as it matches.
        exportPadding = self._declaration(".export-card", "padding")
        importPadding = self._declaration(".import-card", "padding")
        self.assertIsNotNone(importPadding, "the import panel lost its padding")
        if exportPadding is not None:
            self.assertEqual(exportPadding, importPadding)

    def test_export_panel_gap_matches_the_import_form_gap(self):
        # The import panel's own rhythm is the grid gap of the form it wraps.
        self.assertEqual(
            self._declaration(".export-card", "gap"),
            self._declaration("#import-form", "gap"),
        )

    def test_the_two_panel_headings_share_one_rule(self):
        # Both panels are headed now ("Import your history" / "Export your
        # history"). One rule for both keeps the import title from drifting
        # from the export title it was added to match - and it has to outrank
        # the `.card h2` rule the import panel still carries.
        for block in re.finditer(r"([^{}]+)\{([^}]*)\}", self.css):
            selectors = block.group(1)
            if ".export-card-text h2" in selectors:
                self.assertIn(".import-card .import-card-heading", selectors)
                return
        self.fail("missing CSS rule for the export panel heading")

    def _ruleFor(self, selector):
        match = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(match, "missing CSS rule for " + selector)
        return match.group(1)

    def _declaration(self, selector, prop):
        match = re.search(
            r"(?:^|;)\s*" + re.escape(prop) + r"\s*:\s*([^;]+)",
            self._ruleFor(selector),
        )
        return match.group(1).strip() if match else None


if __name__ == "__main__":
    unittest.main()
