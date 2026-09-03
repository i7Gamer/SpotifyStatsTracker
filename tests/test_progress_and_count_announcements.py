# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Two controls whose VALUE is the whole message, and which said nothing to a
screen reader (2026-09-03 review, C-7 and C-9).

The import page's <progress> had no accessible name at all - it is announced
as a bare "progress bar", with the sentence that gives it meaning sitting in
a separate <p> right above it. And the playlist exporter's match counter is
rewritten by playlists.js on every tag toggle with nothing announcing the new
number, so the one piece of feedback the filters give was invisible to anyone
not watching that spot on the page.

Static markup guards - no browser, no app - like the sibling layout files.
"""
import os
import unittest

import bs4

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_TEMPLATES = os.path.join(_ROOT, 'templates')


def _soup(name):
    with open(os.path.join(_TEMPLATES, name), encoding='utf-8') as fh:
        return bs4.BeautifulSoup(fh.read(), 'html.parser')


class TestImportProgressHasAnAccessibleName(unittest.TestCase):
    def setUp(self):
        self.soup = _soup('import.html')
        self.bar = self.soup.find('progress', id='progress-bar')

    def test_the_bar_is_named_by_the_message_above_it(self):
        self.assertIsNotNone(self.bar, 'the progress bar must still be there to name')
        self.assertEqual(self.bar.get('aria-labelledby'), 'progress-message')

    def test_the_element_it_names_exists(self):
        """An aria-labelledby pointing at nothing is worse than none: the
        element is then announced with no name at all, and silently."""
        self.assertIsNotNone(self.soup.find(id=self.bar.get('aria-labelledby')))


class TestPlaylistMatchCountIsALiveRegion(unittest.TestCase):
    def setUp(self):
        self.count = _soup('playlists.html').find(id='previewCount')

    def test_the_counter_announces_itself(self):
        self.assertIsNotNone(self.count)
        self.assertEqual(self.count.get('role'), 'status')

    def test_it_is_polite_rather_than_assertive(self):
        """It changes on every tag toggle; assertive would interrupt the
        user mid-word on each one."""
        self.assertEqual(self.count.get('aria-live'), 'polite')


if __name__ == '__main__':
    unittest.main()
