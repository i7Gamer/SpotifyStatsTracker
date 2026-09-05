# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Wrapped's Top Artists/Top Albums cards sit right below the page's own
"Unique Songs" tile - a single MERGED count for the whole year
(_wrapped_results.html's uniqueSongsCount) - so a card's "played N ... by X"
caption must say what KIND of count N is, or it reads as the same claim as
the tile.

The ALBUM count is per-release (Database/queries/plays.py's album aggregates
count DISTINCT p.track_id and never collapse a merge group, because an album
page lists per-release rows), so the album card says "song release(s)" -
option (a) from the 2026-09-02 review (UT-10), caption-only, no query change.

The ARTIST count MERGES since 429f148 (2026-09-05): an artist's song list
shows a merged song once, and _uniqueSongCountSql collapses the artist's
"unique songs" to match it. So the artist card's number is the same kind of
count as the tile, and "song releases" would now mislead - it says "songs",
on Wrapped like everywhere else. (With no merge in the catalog the two
counting rules give the same number, so "songs" is never wrong.) Found by the
2026-09-05 range review of 429f148.

Rendered through a bare Jinja environment, like test_track_card_none_name.py."""
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


class TrackCardWrappedReleaseCaptionTestCase(unittest.TestCase):
    def _artistCard(self, wrappedCard=None):
        context = {
            "track": {"id": "ar1", "name": "Fixture Artist", "plays": 4, "uniqueSongCount": 4},
            "section": "top_artists", "username": "tester", "publicView": False,
        }
        if wrappedCard is not None:
            context["wrappedCard"] = wrappedCard
        return _renderTrackCard(**context)

    def _albumCard(self, wrappedCard=None, uniqueSongCount=4):
        context = {
            "track": {"id": "al1", "name": "Fixture Album", "plays": 4, "uniqueSongCount": uniqueSongCount},
            "section": "top_albums", "username": "tester", "publicView": False,
        }
        if wrappedCard is not None:
            context["wrappedCard"] = wrappedCard
        return _renderTrackCard(**context)

    def test_wrapped_artist_card_says_songs_because_its_count_merges(self):
        html = self._artistCard(wrappedCard=True)

        self.assertIn("You played 4 different songs by Fixture Artist", html)
        self.assertNotIn("song releases by", html)

    def test_wrapped_album_card_names_releases_not_songs(self):
        html = self._albumCard(wrappedCard=True)

        self.assertIn("You played 4 song releases from Fixture Album", html)

    def test_wrapped_album_card_release_wording_singular(self):
        html = self._albumCard(wrappedCard=True, uniqueSongCount=1)

        self.assertIn("You played 1 song release from Fixture Album", html)
        self.assertNotIn("song releases", html)

    def test_a_top_artists_page_card_keeps_the_unqualified_wording(self):
        """Negative control: wrappedCard unset (Top Artists page, Compare) must
        render byte-identically to before."""
        html = self._artistCard()

        self.assertIn("You played 4 different songs by Fixture Artist", html)
        self.assertNotIn("releases", html)

    def test_a_top_albums_page_card_keeps_the_unqualified_wording(self):
        html = self._albumCard()

        self.assertIn("You played 4 songs from Fixture Album", html)
        self.assertNotIn("releases", html)


if __name__ == "__main__":
    unittest.main()
