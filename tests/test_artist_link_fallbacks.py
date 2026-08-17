"""_artist_links.html's macro against an artist that carries no id.

The detail-link branch was taken on `not suppressDetailLinks` alone and fed
whatever `artist.get('id')` returned straight to url_for. Werkzeug DROPS a None
value before building, and artistDetailPage requires artist_id, so the result is
not a dead "/artist/None" link - it is a BuildError, which takes down the whole
page render for one bad row in one list. An empty-string id builds "/artist/",
which matches no rule: a 404 on click.

Every render site is a database row today, and track_artists.artist_id is NOT
NULL, so this is a guard rather than a live bug. It is cheap insurance on a
macro that every list page in the app renders, and the two fallbacks it hands
to (the external Spotify link, then plain text) already exist for exactly this
- they were simply unreachable unless the caller passed suppressDetailLinks.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import MAX_INLINE_ARTISTS, MIN_HIDDEN_ARTISTS
from _app_factory import AppTestCase

RENDER = (
    "{% from '_artist_links.html' import artistLinks with context %}"
    "{{ artistLinks(artists) }}"
)


class TestArtistLinkFallbacks(AppTestCase):
    def _render(self, artists):
        dash = self._makeApp()
        with dash.app.test_request_context("/"):
            #< the collapse thresholds normally arrive via a context processor,
            #  which only runs for render_template - passed in directly here so
            #  the macro is exercised on its own rather than through a page
            return dash.app.jinja_env.from_string(RENDER).render(
                artists=artists,
                MAX_INLINE_ARTISTS=MAX_INLINE_ARTISTS,
                MIN_HIDDEN_ARTISTS=MIN_HIDDEN_ARTISTS)

    def test_an_artist_with_no_id_renders_instead_of_breaking_the_page(self):
        html = self._render([{"name": "No Id Artist", "url": ""}])

        self.assertIn("No Id Artist", html)

    def test_an_artist_with_no_id_never_links_to_a_detail_page(self):
        for artist in ({"name": "A", "url": ""},
                       {"id": None, "name": "A", "url": ""},
                       {"id": "", "name": "A", "url": ""}):
            with self.subTest(artist=artist):
                html = self._render([artist])

                self.assertNotIn("/artist/", html)

    def test_an_id_less_artist_falls_back_to_its_spotify_link(self):
        """The same fallback order the Compare page's counterpart items use:
        a real external URL beats no link at all."""
        html = self._render([{"name": "A", "url": "https://open.spotify.com/artist/x"}])

        self.assertIn("https://open.spotify.com/artist/x", html)

    def test_an_id_less_artist_with_no_url_is_plain_text(self):
        html = self._render([{"name": "Only A Name", "url": ""}])

        self.assertIn("Only A Name", html)
        self.assertNotIn("<a", html)

    def test_a_normal_artist_still_links_to_its_detail_page(self):
        """The guard must not cost the ordinary case its link."""
        html = self._render([{"id": "art1", "name": "Real Artist", "url": ""}])

        self.assertIn("/artist/art1", html)

    def test_one_bad_artist_does_not_take_the_others_links_with_it(self):
        html = self._render([
            {"id": "art1", "name": "Real Artist", "url": ""},
            {"name": "No Id Artist", "url": ""},
        ])

        self.assertIn("/artist/art1", html)
        self.assertIn("No Id Artist", html)


if __name__ == "__main__":
    unittest.main()
