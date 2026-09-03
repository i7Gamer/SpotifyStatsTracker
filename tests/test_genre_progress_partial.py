# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""_genre_progress.html's `{% if not coverage.song.total %}` fires purely
because the SELECTED RANGE has zero song plays - coverage.song.total is a play
count, not a "has API key" flag. So a user with a WORKING Last.fm key who
happened to pick an empty week saw the same "Add a Last.fm API key" pitch as
someone who never configured one at all.

The partial now takes an explicit `lastfmConfigured` flag (its docstring's
existing `coverage` contract, made two-deep): False pitches the API key
regardless of coverage; True with a zero-play range shows a "no plays yet"
message instead; undefined (a caller a sweep might miss) degrades to today's
behaviour rather than erroring.

Rendered through a bare Jinja environment, like test_track_card_none_name.py.

2026-09-02 review, UT-12."""
import os
import sys
import unittest

import jinja2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.genre_gate import GENRE_GATE_OVERALL_MIN_PERCENT, GENRE_GATE_CATEGORY_MIN_PERCENT

_TEMPLATES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates")


def _category(total, covered=0, percent=0):
    return {"total": total, "covered": covered, "percent": percent}


def _coverage(songTotal, albumTotal=100, artistTotal=100):
    return {
        "song": _category(songTotal, songTotal, 100 if songTotal else 0),
        "album": _category(albumTotal, albumTotal, 100 if albumTotal else 0),
        "artist": _category(artistTotal, artistTotal, 100 if artistTotal else 0),
        "overall": {"percent": 100.0},
    }


def _renderGenreProgress(**context):
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES), autoescape=True)
    env.globals["url_for"] = lambda *args, **kwargs: "#"
    env.globals["genreGateOverallMinPercent"] = GENRE_GATE_OVERALL_MIN_PERCENT
    env.globals["genreGateCategoryMinPercent"] = GENRE_GATE_CATEGORY_MIN_PERCENT
    return env.get_template("_genre_progress.html").render(username="tester", publicView=False, **context)


class GenreProgressLastfmConfiguredTestCase(unittest.TestCase):
    def test_not_configured_pitches_the_api_key_regardless_of_coverage(self):
        html = _renderGenreProgress(coverage=_coverage(songTotal=0), lastfmConfigured=False)

        self.assertIn("Add a Last.fm API key", html)

    def test_configured_with_zero_plays_shows_no_plays_message_not_the_pitch(self):
        html = _renderGenreProgress(coverage=_coverage(songTotal=0), lastfmConfigured=True)

        self.assertIn("No plays in this period yet.", html)
        self.assertNotIn("Add a Last.fm API key", html)

    def test_configured_with_plays_shows_neither_message(self):
        html = _renderGenreProgress(coverage=_coverage(songTotal=500), lastfmConfigured=True)

        self.assertNotIn("Add a Last.fm API key", html)
        self.assertNotIn("No plays in this period yet.", html)

    def test_flag_undefined_degrades_to_the_old_behaviour(self):
        """Back-compat: any include site a sweep missed keeps rendering the
        API-key pitch on zero coverage, rather than crashing or silently
        showing neither message."""
        html = _renderGenreProgress(coverage=_coverage(songTotal=0))

        self.assertIn("Add a Last.fm API key", html)


if __name__ == "__main__":
    unittest.main()
