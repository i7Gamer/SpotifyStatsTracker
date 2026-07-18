"""SpotifyDashboardApp._resortByMetric: re-sorts an already-fetched pool of
song/artist/album dicts by a chosen metric (see app.py) without re-querying
the DB. Used by the Wrapped page's cached-pool path. Ties must resolve the
same deterministic way Repository.getSongsPage/getAlbumsPage/
getArtistAggregates already do in SQL: metric -> the other metric -> name ->
id, and for a name sort: name -> time listened (desc) -> id.
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

    def test_full_ties_including_name_fall_back_to_id(self):
        items = [
            _item("z-id", "Same", plays=5, totalTimeListened=1000),
            _item("a-id", "Same", plays=5, totalTimeListened=1000),
        ]

        result = SpotifyDashboardApp._resortByMetric(items, "plays")

        self.assertEqual([i["id"] for i in result], ["a-id", "z-id"])

    def test_name_sort_is_unaffected(self):
        items = [
            _item("z", "Zeta", plays=1),
            _item("a", "Alpha", plays=9),
        ]

        result = SpotifyDashboardApp._resortByMetric(items, "name")

        self.assertEqual([i["id"] for i in result], ["a", "z"])

    def test_name_sort_ties_break_by_most_time_listened(self):
        """Identical names order by time listened DESC - the same chain
        Repository's by="name" ORDER BY uses, so a resorted pool and a live
        name query agree on tie order. The louder item is listed second AND
        has the larger id, so neither input order nor the id fallback can
        produce this order."""
        items = [
            _item("a-id", "Same", plays=1, totalTimeListened=1000),
            _item("z-id", "Same", plays=1, totalTimeListened=5000),
        ]

        result = SpotifyDashboardApp._resortByMetric(items, "name")

        self.assertEqual([i["id"] for i in result], ["z-id", "a-id"])

    def test_name_sort_full_ties_fall_back_to_id(self):
        """Name AND time ties must not lean on the input pool's incidental
        order - id is the final deterministic leg, like every other chain
        here."""
        items = [
            _item("z-id", "Same", plays=1, totalTimeListened=1000),
            _item("a-id", "Same", plays=1, totalTimeListened=1000),
        ]

        result = SpotifyDashboardApp._resortByMetric(items, "name")

        self.assertEqual([i["id"] for i in result], ["a-id", "z-id"])


if __name__ == "__main__":
    unittest.main()
