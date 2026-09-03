# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wrapped item cards' "First Listened" is year-scoped, not lifetime - but the
label read the same as the Top pages' lifetime "First Listened", sitting right
below Wrapped's own lifetime-aware "Songs Discovered" tile
(_wrapped_results.html's discoveredSongsCount). A viewer had no way to tell the
two apart.

`_track_card.html` now takes an opt-in `wrappedCard` flag (the same contract
`showSkipStats`/`suppressDetailLinks` already use) that swaps the caption
wording to "First heard this year" - only set by _wrapped_results.html, so
every other page (Top Songs/Artists/Albums, Compare) keeps the unqualified
"First Listened" label byte-for-byte.

Rendered through a bare Jinja environment, like test_track_card_none_name.py.

2026-09-02 review, UT-22."""
import os
import sys
import unittest

import jinja2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _renderTrackCard(**context):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES), autoescape=True)
    env.globals["url_for"] = lambda *args, **kwargs: "#"
    return env.get_template("_track_card.html").render(**context)


class TrackCardWrappedFirstListenedTestCase(unittest.TestCase):
    def _card(self, section, wrappedCard=None):
        context = {
            "track": {"id": "e1", "name": "Fixture Entity", "firstListenedText": "Mar 2026"},
            "section": section, "username": "tester", "publicView": False,
        }
        if wrappedCard is not None:
            context["wrappedCard"] = wrappedCard
        return _renderTrackCard(**context)

    def test_wrapped_top_songs_card_says_first_heard_this_year(self):
        html = self._card("top_songs", wrappedCard=True)

        self.assertIn("First heard this year: Mar 2026", html)
        self.assertNotIn("First Listened:", html)

    def test_wrapped_top_artists_card_says_first_heard_this_year(self):
        html = self._card("top_artists", wrappedCard=True)

        self.assertIn("First heard this year: Mar 2026", html)

    def test_wrapped_top_albums_card_says_first_heard_this_year(self):
        html = self._card("top_albums", wrappedCard=True)

        self.assertIn("First heard this year: Mar 2026", html)

    def test_a_top_songs_page_card_keeps_the_unqualified_label(self):
        """Negative control: wrappedCard unset (Top pages, Compare) must render
        byte-identically to before - no accidental scope creep."""
        html = self._card("top_songs")

        self.assertIn("First Listened: Mar 2026", html)
        self.assertNotIn("First heard this year", html)

    def test_a_top_artists_page_card_keeps_the_unqualified_label(self):
        html = self._card("top_artists")

        self.assertIn("First Listened: Mar 2026", html)
        self.assertNotIn("First heard this year", html)

    def test_a_top_albums_page_card_keeps_the_unqualified_label(self):
        html = self._card("top_albums")

        self.assertIn("First Listened: Mar 2026", html)
        self.assertNotIn("First heard this year", html)

    def test_wrapped_card_with_flag_explicitly_false_keeps_the_unqualified_label(self):
        html = self._card("top_songs", wrappedCard=False)

        self.assertIn("First Listened: Mar 2026", html)


class WrappedResultsSetsTheFlagAtAllSixSitesTestCase(unittest.TestCase):
    """_wrapped_results.html must set wrappedCard=true at all six of its
    _track_card.html includes (top_songs/top_artists/top_albums x the regular
    and discoveries sections) - a partial rollout would leave some Wrapped
    cards saying "First heard this year" and others still saying "First
    Listened", which is worse than not fixing it at all."""

    def _renderWrappedResults(self, **overrides):
        env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES), autoescape=True)
        env.globals["url_for"] = lambda *args, **kwargs: "#"
        env.filters["displayName"] = lambda username: username
        context = {
            "username": "tester", "publicView": True, "year": 2026,
            "totalPlays": 10, "totalTime": "1h", "longestStreak": 3,
            "peakListeningTime": None, "uniqueSongsCount": 5, "uniqueArtistsCount": 4,
            "discoveredSongsCount": 1, "discoveredArtistsCount": 1,
            "genresPreserved": True, "timeSeries": [],
            "topSongs": [{"id": "s1", "name": "Song One", "firstListenedText": "Mar 2026"}],
            "topArtists": [{"id": "a1", "name": "Artist One", "firstListenedText": "Feb 2026"}],
            "topAlbums": [{"id": "al1", "name": "Album One", "firstListenedText": "Jan 2026"}],
            "discoveredSongs": [{"id": "s2", "name": "Song Two", "firstListenedText": "Apr 2026"}],
            "discoveredArtists": [{"id": "a2", "name": "Artist Two", "firstListenedText": "May 2026"}],
            "discoveredAlbums": [{"id": "al2", "name": "Album Two", "firstListenedText": "Jun 2026"}],
        }
        context.update(overrides)
        return env.get_template("_wrapped_results.html").render(**context)

    def test_all_six_sections_say_first_heard_this_year(self):
        html = self._renderWrappedResults()

        self.assertEqual(html.count("First heard this year:"), 6)
        self.assertNotIn("First Listened:", html)


if __name__ == "__main__":
    unittest.main()
