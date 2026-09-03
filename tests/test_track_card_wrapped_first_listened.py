# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wrapped item cards' "First Listened" is year-scoped, not lifetime - but the
label read the same as the Top pages' lifetime "First Listened", sitting right
below Wrapped's own lifetime-aware "Songs Discovered" tile
(_wrapped_results.html's discoveredSongsCount). A viewer had no way to tell the
two apart.

`_track_card.html` now takes an opt-in `wrappedCard` flag (the same contract
`showSkipStats`/`suppressDetailLinks` already use) that swaps the caption
wording to "First heard this year" - only set by _wrapped_results.html.

A second flag on the same caption, `rangeScopedFirstListen`, covers the Top
pages and Compare: their MIN(played_at) is scoped to the SELECTED range too, so
only All Time is the lifetime value the plain wording claims (2026-09-03 review,
M3, in the second class below). An unflagged include - the dashboard, the detail
pages, the history list - still renders byte-for-byte as before.

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
    def _card(self, section, wrappedCard=None, rangeScopedFirstListen=None):
        context = {
            "track": {"id": "e1", "name": "Fixture Entity", "firstListenedText": "Mar 2026"},
            "section": section, "username": "tester", "publicView": False,
        }
        if wrappedCard is not None:
            context["wrappedCard"] = wrappedCard
        if rangeScopedFirstListen is not None:
            context["rangeScopedFirstListen"] = rangeScopedFirstListen
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


class TrackCardRangeScopedFirstListenTestCase(unittest.TestCase):
    """The same caption, wrong for a second reason (2026-09-03 review, M3):
    getTopSongs/Artists/Albums compute MIN(p.played_at) AS first_listened_at
    inside a statement whose WHERE already carries the range clause. On
    /top-songs?interval=week a song first played in 2019 and played six times
    this week rendered "First Listened: <this week>" - and this partial's own
    comment asserted the opposite ("The Top pages' identical caption is a
    genuine lifetime first play"). Only All Time was ever right."""

    def _card(self, section, **flags):
        context = {
            "track": {"id": "e1", "name": "Fixture Entity", "firstListenedText": "Mar 2026"},
            "section": section, "username": "tester", "publicView": False,
        }
        context.update(flags)
        return _renderTrackCard(**context)

    def test_a_range_scoped_card_qualifies_the_label(self):
        for section in ("top_songs", "top_artists", "top_albums"):
            with self.subTest(section=section):
                html = self._card(section, rangeScopedFirstListen=True)

                self.assertIn("First heard in this range: Mar 2026", html)
                self.assertNotIn("First Listened:", html)

    def test_an_all_time_card_keeps_the_unqualified_label(self):
        """All Time IS the lifetime range, so the plain wording is correct
        there - and it is the default every other include of this partial
        (the dashboard, the detail pages, the history list) keeps."""
        for section in ("top_songs", "top_artists", "top_albums"):
            with self.subTest(section=section):
                html = self._card(section, rangeScopedFirstListen=False)

                self.assertIn("First Listened: Mar 2026", html)
                self.assertNotIn("in this range", html)

    def test_wrapped_wording_wins_when_both_flags_are_set(self):
        """Wrapped renders a year-scoped range, so both flags are true of it -
        but its own caption already names the scope, and naming it twice
        ("First heard in this range" on a page titled 2026 Wrapped) is worse
        than either."""
        html = self._card("top_songs", wrappedCard=True, rangeScopedFirstListen=True)

        self.assertIn("First heard this year: Mar 2026", html)
        self.assertNotIn("in this range", html)


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
