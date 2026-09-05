# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A never-fetched cover on the /genres drill-down must not build a
`/img/<user>/<folder>/None.jpeg` src.

_genre_detail.html built the Top Artists / Top Tracks thumbnails' <img src>
from imageId unconditionally, so an artist or track whose cover was never
fetched (image_id NULL, which Jinja stringifies as "None") issued a
guaranteed-404 request per row that the client-side onerror then repainted
as the placeholder anyway. Same defect _track_card.html's cover had
(2026-09-02 review, UT-13) and _compare_macros.html's coverImg had
(beaede0) - this partial never got the matching guard. Found by the
2026-09-05 preview review: the sandbox's one artist and every top track
404'd on every chip click.

Rendered through a bare Jinja environment, like test_track_card_empty_image.py
and test_compare_cover_image.py, so the assertion pins the partial itself.
"""
import os
import sys
import unittest

import jinja2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config import PLACEHOLDER_IMG_DATA_URI

_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _renderGenreDetail(**context):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES), autoescape=True)
    env.globals["url_for"] = lambda *args, **kwargs: "#"
    #< normally supplied by dashboard/context_processors.py's
    #  _injectPlaceholderImage - this bare environment has no context
    #  processors, so it's set explicitly here
    return env.get_template("_genre_detail.html").render(
        placeholderImgDataUri=PLACEHOLDER_IMG_DATA_URI, **context)


class GenreDetailCoverImageTestCase(unittest.TestCase):
    def _detail(self, artistImageId, trackImageId):
        return _renderGenreDetail(
            selectedGenre="new wave",
            genreStats=None,
            intervalLabel="All Time",
            username="tester",
            topArtists=[{"id": "art1", "name": "Fixture Artist", "imageId": artistImageId, "playCount": 4}],
            topTracks=[{"id": "trk1", "name": "Fixture Song", "artistName": "Fixture Artist",
                        "imageId": trackImageId, "playCount": 3}],
        )

    def test_a_missing_artist_cover_never_requests_none_jpeg(self):
        html = self._detail(artistImageId=None, trackImageId="t-real")

        self.assertNotIn("None.jpeg", html)
        self.assertNotIn("/artists/.jpeg", html)

    def test_a_missing_track_cover_never_requests_none_jpeg(self):
        html = self._detail(artistImageId="a-real", trackImageId=None)

        self.assertNotIn("None.jpeg", html)
        self.assertNotIn("/tracks/.jpeg", html)

    def test_missing_covers_use_the_placeholder_data_uri_directly(self):
        html = self._detail(artistImageId=None, trackImageId=None)

        #< only the prefix: autoescape entity-escapes the SVG's own quotes
        self.assertEqual(html.count('src="data:image/svg+xml,'), 2)

    def test_real_covers_still_request_the_normal_src(self):
        """Negative control: the guard must not swallow a real cover."""
        html = self._detail(artistImageId="a-real", trackImageId="t-real")

        self.assertIn('src="/img/tester/artists/a-real.jpeg"', html)
        self.assertIn('src="/img/tester/tracks/t-real.jpeg"', html)
        self.assertNotIn("data:image/svg+xml", html)


if __name__ == "__main__":
    unittest.main()
