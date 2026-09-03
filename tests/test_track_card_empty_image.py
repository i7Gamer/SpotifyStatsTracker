# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""An empty imageId must not build a `/img/<user>/<folder>/.jpeg` src.

_track_card.html used to build the cover <img> src unconditionally
(`{{ imageBase }}/{{ imgFolder }}/{{ imgId }}.jpeg`), so a track/artist/album
whose cover was never fetched (imageId="") still issued one guaranteed-404
request per card, which the client-side onerror then repainted as the
placeholder anyway. On a 50-card /history page that is 50 wasted round trips.

Rendered through a bare Jinja environment, like test_track_card_none_name.py,
so the assertion pins the partial itself.

2026-09-02 review, UT-13."""
import os
import sys
import unittest

import jinja2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config import PLACEHOLDER_IMG_DATA_URI

_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _renderTrackCard(**context):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES), autoescape=True)
    env.globals["url_for"] = lambda *args, **kwargs: "#"
    #< normally supplied by dashboard/context_processors.py's
    #  _injectPlaceholderImage - this bare environment has no context
    #  processors, so it's set explicitly here (2026-09-02 review, UT-13
    #  follow-up: the partial used to hold its own copy of the literal)
    return env.get_template("_track_card.html").render(
        placeholderImgDataUri=PLACEHOLDER_IMG_DATA_URI, **context)


class TrackCardEmptyImageTestCase(unittest.TestCase):
    def _card(self, imageId):
        return _renderTrackCard(
            track={"id": "t1", "name": "Fixture Song", "imageId": imageId, "plays": 3},
            section="top_songs", username="tester", publicView=False,
        )

    def test_an_empty_image_id_never_requests_a_jpeg(self):
        html = self._card("")

        self.assertNotIn(".jpeg", html)

    def test_an_empty_image_id_uses_the_placeholder_data_uri_directly(self):
        html = self._card("")

        self.assertIn('src="data:image/svg+xml,', html)

    def test_a_real_image_id_still_requests_the_normal_src(self):
        """Negative control: the guard must not swallow a real cover."""
        html = self._card("abc123")

        self.assertIn('src="/img/tester/tracks/abc123.jpeg"', html)
        self.assertNotIn("data:image/svg+xml", html)


if __name__ == "__main__":
    unittest.main()
