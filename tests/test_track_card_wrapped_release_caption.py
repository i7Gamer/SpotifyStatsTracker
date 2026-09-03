# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Database/queries/plays.py's uniqueSongCount is deliberately PER-RELEASE on
artist/album surfaces (its docstring: a merged number "nobody can reconcile
with what is on screen" would be worse, since those pages list per-release
rows). Wrapped's Top Artists/Top Albums cards route through the same query,
but sit right below the page's own "Unique Songs" tile - a single MERGED count
for the whole year (_wrapped_results.html's uniqueSongsCount). The unqualified
"played N different songs by X" / "played N songs from X" caption reads as the
same claim as that tile even though the two use different counting rules.

Option (a) from the 2026-09-02 review (UT-10): caption-only disambiguation -
reword the caption on Wrapped-sourced cards to name the distinction ("song
releases" rather than bare "songs"), changing no query. Reuses the wrappedCard
flag UT-22 added to _track_card.html.

Rendered through a bare Jinja environment, like test_track_card_none_name.py.

2026-09-02 review, UT-10."""
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

    def test_wrapped_artist_card_names_releases_not_songs(self):
        html = self._artistCard(wrappedCard=True)

        self.assertIn("You played 4 different song releases by Fixture Artist", html)
        self.assertNotIn("different songs by", html)

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
