"""SpotifyDashboardApp._resortByMetric: re-sorts an already-fetched pool of
song/artist/album dicts by a chosen metric (see app.py) without re-querying
the DB. Used by the Wrapped page's cached-pool path. Ties must resolve the
same deterministic way Repository.getSongsPage/getAlbumsPage/
getArtistAggregates already do in SQL: metric -> the other metric -> name.
"""
import sys
import os
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp


def _item(itemId, name, **extra):
    return {"id": itemId, "name": name, **extra}


class TestResortByMetric(unittest.TestCase):
    def test_plays_ties_break_by_time_played(self):
        items = [
            _item("lo", "Bravo", plays=5, totalTimeListened=1000),
            _item("hi", "Alpha", plays=5, totalTimeListened=5000),
        ]

        result = SpotifyDashboardApp._resortByMetric(items, "plays")

        self.assertEqual([i["id"] for i in result], ["hi", "lo"])

    def test_time_played_ties_break_by_plays(self):
        """The previously-broken case: sortBy="totalTimeListened" ties used
        to silently keep the input pool's original (plays-ranked) order
        instead of an explicit tiebreak."""
        items = [
            _item("lo", "Bravo", plays=1, totalTimeListened=1000),
            _item("hi", "Alpha", plays=9, totalTimeListened=1000),
        ]

        result = SpotifyDashboardApp._resortByMetric(items, "totalTimeListened")

        self.assertEqual([i["id"] for i in result], ["hi", "lo"])

    def test_full_ties_fall_back_to_name(self):
        items = [
            _item("z", "Zeta", plays=5, totalTimeListened=1000),
            _item("a", "Alpha", plays=5, totalTimeListened=1000),
        ]

        result = SpotifyDashboardApp._resortByMetric(items, "plays")

        self.assertEqual([i["id"] for i in result], ["a", "z"])

    def test_name_sort_is_unaffected(self):
        items = [
            _item("z", "Zeta", plays=1),
            _item("a", "Alpha", plays=9),
        ]

        result = SpotifyDashboardApp._resortByMetric(items, "name")

        self.assertEqual([i["id"] for i in result], ["a", "z"])


if __name__ == "__main__":
    unittest.main()
