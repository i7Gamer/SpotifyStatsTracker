# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""_compare_macros.html's coverImg macro must not build a
`/img/<user>/<folder>/None.jpeg` src.

Before this fix coverImg's <img src=...> was built unconditionally from
item.imageId - the compare page's `spotifyCell`/`theirCell` cells already
skip a missing url, but their neighbouring cover images did not skip a
missing imageId, so a fabricated/never-fetched item requested a literal
`.../None.jpeg` (Jinja stringifies a Python None as "None"), a
guaranteed-404 request the client-side onerror then repainted as the
placeholder anyway - the same defect _track_card.html had (see
test_track_card_empty_image.py, 2026-09-02 review UT-13) before its own fix.

Rendered through a bare Jinja environment: coverImg is a macro, not a whole
template, so the fixture calls Template.make_module(vars=...) rather than
.render() - that's what makes the macro's own reference to
placeholderImgDataUri resolve, the same way `with context` on the real
`{% from %}` imports in _compare_similarities.html/_compare_stats_table.html
makes it resolve in the app (a bare environment has no context processors,
so it's supplied explicitly here, like test_track_card_empty_image.py does
for _track_card.html).

2026-09-04 review, P1."""
import os
import sys
import unittest

import jinja2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from config import PLACEHOLDER_IMG_DATA_URI

_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _coverImgModule():
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES), autoescape=True)
    template = env.get_template("_compare_macros.html")
    #< normally supplied by dashboard/context_processors.py's
    #  _injectPlaceholderImage, and reaches the macro because the real
    #  importing templates use `with context` - this bare environment has
    #  no context processors, so it's set explicitly here
    return template.make_module(vars={"placeholderImgDataUri": PLACEHOLDER_IMG_DATA_URI})


class CoverImgMissingImageIdTestCase(unittest.TestCase):
    def _render(self, imageId):
        module = _coverImgModule()
        item = {"id": "t1", "name": "Fixture Song", "imageId": imageId}
        return str(module.coverImg(item, "tester", "song"))

    def test_a_missing_image_id_never_requests_a_none_jpeg(self):
        html = self._render(None)

        self.assertNotIn("None.jpeg", html)

    def test_a_missing_image_id_uses_the_placeholder_data_uri_directly(self):
        """Only the prefix, not the full literal: autoescape (on by default,
        like the real app) HTML-entity-escapes the SVG's own quotes/angle
        brackets, same reason test_track_card_empty_image.py checks this."""
        html = self._render(None)

        self.assertIn('src="data:image/svg+xml,', html)

    def test_a_real_image_id_still_requests_the_normal_src(self):
        """Negative control: the guard must not swallow a real cover."""
        html = self._render("abc123")

        self.assertIn('src="/img/tester/tracks/abc123.jpeg"', html)
        self.assertNotIn("data:image/svg+xml", html)


if __name__ == "__main__":
    unittest.main()
