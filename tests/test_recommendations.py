import sys
import os
import datetime
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase


def _ts(year, month, day, hour=12):
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


class TestRecommendedArtists(DatabaseTestCase):
    def _seed(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song 1", "artists": [{"id": "a1", "name": "Top Artist"}]},
            "t2": {"id": "t2", "name": "Song 2", "artists": [{"id": "a2", "name": "Rock Gem"}]},
            "t3": {"id": "t3", "name": "Song 3", "artists": [{"id": "a3", "name": "Rock Indie"}]},
            "t4": {"id": "t4", "name": "Song 4", "artists": [{"id": "a4", "name": "Jazz Only"}]},
        }
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 1, 1), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 2), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 3), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 4), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 5), "timePlayed": 1000},
            {"id": "t3", "playedAt": _ts(2026, 1, 6), "timePlayed": 1000},
            {"id": "t3", "playedAt": _ts(2026, 1, 7), "timePlayed": 1000},
            {"id": "t2", "playedAt": _ts(2026, 1, 8), "timePlayed": 1000},
            {"id": "t4", "playedAt": _ts(2026, 1, 9), "timePlayed": 1000},
        ]
        db = self._makeDb(tracks, entries)
        # Track-level genres drive the user's top-genre pool (getGenreDistribution).
        db.repo.replaceTrackGenres("t1", ["rock"], inherited=False)
        db.repo.replaceTrackGenres("t3", ["indie"], inherited=False)
        # Artist-level genres drive candidate matching.
        db.repo.replaceArtistGenres("a1", ["rock"])
        db.repo.replaceArtistGenres("a2", ["rock"])
        db.repo.replaceArtistGenres("a3", ["rock", "indie"])
        db.repo.replaceArtistGenres("a4", ["jazz"])
        return db

    def test_no_genre_data_returns_empty(self):
        tracks = {"t1": {"id": "t1", "name": "Song 1", "artists": [{"id": "a1", "name": "A"}]}}
        db = self._makeDb(tracks, [{"id": "t1", "playedAt": _ts(2026, 1, 1), "timePlayed": 1000}])
        self.assertEqual(db.getRecommendedArtists(limit=10, genrePool=10, excludeTopN=5), [])

    def test_orders_by_shared_genres_then_underplayed(self):
        db = self._seed()
        recs = db.getRecommendedArtists(limit=10, genrePool=10, excludeTopN=1)
        ids = [r["id"] for r in recs]
        # a3 shares 2 genres (rock+indie) -> first; a2 shares 1 -> next.
        self.assertEqual(ids, ["a3", "a2"])
        self.assertEqual(recs[0]["sharedGenreCount"], 2)
        self.assertEqual(sorted(recs[0]["matchedGenres"]), ["indie", "rock"])
        self.assertEqual(recs[1]["sharedGenreCount"], 1)

    def test_excludes_top_artists(self):
        db = self._seed()
        recs = db.getRecommendedArtists(limit=10, genrePool=10, excludeTopN=1)
        self.assertNotIn("a1", [r["id"] for r in recs])

    def test_non_matching_genre_not_recommended(self):
        db = self._seed()
        recs = db.getRecommendedArtists(limit=10, genrePool=10, excludeTopN=1)
        self.assertNotIn("a4", [r["id"] for r in recs])

    def test_equal_shared_count_prefers_underplayed(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song 1", "artists": [{"id": "top", "name": "Top"}]},
            "t5": {"id": "t5", "name": "Song 5", "artists": [{"id": "a5", "name": "Played Once"}]},
            "t6": {"id": "t6", "name": "Song 6", "artists": [{"id": "a6", "name": "Played Thrice"}]},
        }
        entries = [
            # `top` is the clear most-played artist so excludeTopN=1 drops it,
            # leaving a5 (1 play) and a6 (3 plays) to test the underplayed tiebreak.
            {"id": "t1", "playedAt": _ts(2026, 2, 1), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 2, 6), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 2, 7), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 2, 8), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 2, 9), "timePlayed": 1000},
            {"id": "t5", "playedAt": _ts(2026, 2, 2), "timePlayed": 1000},
            {"id": "t6", "playedAt": _ts(2026, 2, 3), "timePlayed": 1000},
            {"id": "t6", "playedAt": _ts(2026, 2, 4), "timePlayed": 1000},
            {"id": "t6", "playedAt": _ts(2026, 2, 5), "timePlayed": 1000},
        ]
        db = self._makeDb(tracks, entries)
        db.repo.replaceTrackGenres("t1", ["rock"], inherited=False)
        db.repo.replaceArtistGenres("top", ["rock"])
        db.repo.replaceArtistGenres("a5", ["rock"])
        db.repo.replaceArtistGenres("a6", ["rock"])
        recs = db.getRecommendedArtists(limit=10, genrePool=10, excludeTopN=1)
        ids = [r["id"] for r in recs]
        self.assertEqual(ids, ["a5", "a6"])
        self.assertEqual(recs[0]["playCount"], 1)
        self.assertEqual(recs[1]["playCount"], 3)

    def test_limit_caps_results(self):
        db = self._seed()
        recs = db.getRecommendedArtists(limit=1, genrePool=10, excludeTopN=1)
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["id"], "a3")


if __name__ == "__main__":
    unittest.main()
