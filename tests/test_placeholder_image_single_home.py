# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The placeholder cover/artist-image data URI has one home, config.py's
PLACEHOLDER_IMG_DATA_URI.

It used to be a literal duplicated three times (templates/layout.html,
templates/layout_public.html, and templates/_track_card.html - added
f65fa64), so a change to the placeholder art was a three-place edit with
nothing keeping the copies equal. All three now read it from the shared
template context (dashboard/context_processors.py); the ratchet below is a
source-shape assertion because "the same value" is exactly what a
re-introduced copy would also have, and a behavioural test could not tell
them apart.

Review 2026-09-02, UT-13 follow-up
"""
import os
import pathlib
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_NEEDLE = "data:image/svg+xml"
_SKIPPED_DIRS = {"vendor", "__pycache__"}


class TestPlaceholderImageSingleHome(unittest.TestCase):
    def _filesContainingNeedle(self, root):
        hits = []
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if _SKIPPED_DIRS & set(path.relative_to(REPO_ROOT).parts):
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            if _NEEDLE in text:
                hits.append(path)
        return hits

    def test_config_defines_the_literal(self):
        text = (REPO_ROOT / "config.py").read_text(encoding="utf-8")
        self.assertIn(_NEEDLE, text)

    def test_no_template_or_static_js_file_duplicates_the_literal(self):
        hits = self._filesContainingNeedle(REPO_ROOT / "templates")
        hits += self._filesContainingNeedle(REPO_ROOT / "static" / "js")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
