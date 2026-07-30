# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""_track_card.html must never render the literal string "None".

A track/album row can carry name=None (a fallback record whose metadata never
arrived). The card title guards this with `track.get('name') or ''` - but the
top_albums stat line used `track.get('name', '')`, whose default only covers a
MISSING key, not a present-but-None value: the card showed an empty <h3> above
"You played 4 songs from None".

Rendered through a bare Jinja environment rather than a full route so the
assertion pins the partial itself, whatever page includes it.
"""
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


class TrackCardNoneNameTestCase(unittest.TestCase):
    def _albumCard(self, name):
        return _renderTrackCard(
            track={"id": "al1", "name": name, "plays": 4, "uniqueSongCount": 4},
            section="top_albums", username="tester", publicView=False,
        )

    def test_a_none_name_does_not_render_as_the_word_none(self):
        html = self._albumCard(None)

        self.assertNotIn("None", html)
        self.assertIn("played 4 songs from", html)

    def test_a_real_name_still_renders_everywhere(self):
        """Negative control: the guard must not blank real names."""
        html = self._albumCard("Fixture Album")

        self.assertIn("played 4 songs from Fixture Album", html)
        self.assertIn("<h3>Fixture Album</h3>", html)


if __name__ == "__main__":
    unittest.main()
