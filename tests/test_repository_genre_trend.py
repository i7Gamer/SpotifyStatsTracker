"""Genres-page data layer: monthly genre trends, per-genre stat strip, and
per-genre top artists/tracks."""
import sys
import os
import datetime
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase


def _ts(year, month, day, hour=12):
    return datetime.datetime(year, month, day, hour, tzinfo=datetime.timezone.utc).timestamp()


class GenreTrendTestCase(DatabaseTestCase):
    def _seed(self):
        tracks = {
            "t1": {"id": "t1", "name": "Song 1", "artists": [{"id": "a1", "name": "Artist One"}]},
            "t2": {"id": "t2", "name": "Song 2", "artists": [{"id": "a1", "name": "Artist One"}]},
            "t3": {"id": "t3", "name": "Song 3", "artists": [{"id": "a2", "name": "Artist Two"}]},
            "t4": {"id": "t4", "name": "Song 4", "artists": [{"id": "a3", "name": "Artist Three"}]},
        }
        entries = [
            {"id": "t1", "playedAt": _ts(2026, 1, 5), "timePlayed": 1000},
            {"id": "t1", "playedAt": _ts(2026, 1, 6), "timePlayed": 1000},
            {"id": "t2", "playedAt": _ts(2026, 2, 10), "timePlayed": 1000},
            {"id": "t3", "playedAt": _ts(2026, 1, 15), "timePlayed": 1000},
            {"id": "t4", "playedAt": _ts(2026, 1, 20), "timePlayed": 1000},
        ]
        db = self._makeDb(tracks, entries)
        db.tz = datetime.timezone.utc
        db.repo.replaceTrackGenres("t1", ["rock"], inherited=False)
        db.repo.replaceTrackGenres("t2", ["rock"], inherited=False)
        db.repo.replaceTrackGenres("t3", ["rock", "indie"], inherited=False)
        db.repo.replaceTrackGenres("t4", ["jazz"], inherited=False)
        return db

    # ---- getGenreTrends -----------------------------------------------------

    def test_trends_empty_genre_list(self):
        db = self._seed()
        self.assertEqual(db.getGenreTrends([]), {"buckets": [], "series": []})

    def test_trends_unknown_genre_is_empty(self):
        db = self._seed()
        self.assertEqual(db.getGenreTrends(["nonexistent"]), {"buckets": [], "series": []})

    def test_trends_multi_genre_shared_buckets(self):
        db = self._seed()
        trend = db.getGenreTrends(["rock", "indie"])
        self.assertEqual(trend["buckets"], ["2026-01", "2026-02"])
        series = {s["name"]: s["data"] for s in trend["series"]}
        self.assertEqual(series["rock"], [3, 1])
        self.assertEqual(series["indie"], [1, 0])

    def test_trends_single_genre(self):
        db = self._seed()
        trend = db.getGenreTrends(["rock"])
        self.assertEqual(trend["buckets"], ["2026-01", "2026-02"])
        self.assertEqual(trend["series"][0]["name"], "rock")
        self.assertEqual(trend["series"][0]["data"], [3, 1])

    def test_trends_respect_inherited_toggle(self):
        db = self._seed()
        # Add an inherited-only rock play in a fresh month.
        db.repo.insertPlay("testuser", "t4", _ts(2026, 3, 1), 1000)
        db.repo.commit()
        db.repo.replaceTrackGenres("t4", ["jazz", "rock"], inherited=True)
        # t4 already had own "jazz"; give it an inherited rock too. With
        # inherited excluded, March has no rock; with it included, it does.
        with_inh = db.getGenreTrends(["rock"], includeInherited=True)
        self.assertIn("2026-03", with_inh["buckets"])
        without = db.getGenreTrends(["rock"], includeInherited=False)
        self.assertNotIn("2026-03", without["buckets"])

    # ---- getGenreStats ------------------------------------------------------

    def test_stats_basic(self):
        db = self._seed()
        stats = db.getGenreStats("rock")
        self.assertEqual(stats["plays"], 4)
        self.assertEqual(stats["listenMs"], 4000)
        self.assertEqual(stats["firstPlayedTs"], _ts(2026, 1, 5))
        # 4 rock plays out of 5 genre-tagged plays.
        self.assertEqual(stats["sharePercent"], 80.0)

    def test_stats_unknown_genre(self):
        db = self._seed()
        stats = db.getGenreStats("nonexistent")
        self.assertEqual(stats["plays"], 0)
        self.assertEqual(stats["listenMs"], 0)
        self.assertIsNone(stats["firstPlayedTs"])
        self.assertEqual(stats["sharePercent"], 0.0)

    # ---- getTopArtistsForGenre / getTopTracksForGenre -----------------------

    def test_top_artists_for_genre(self):
        db = self._seed()
        artists = db.getTopArtistsForGenre("rock", limit=10)
        self.assertEqual([(a["id"], a["playCount"]) for a in artists], [("a1", 3), ("a2", 1)])

    def test_top_artists_unknown_genre_empty(self):
        db = self._seed()
        self.assertEqual(db.getTopArtistsForGenre("nonexistent", limit=10), [])

    def test_top_tracks_for_genre(self):
        db = self._seed()
        tracks = db.getTopTracksForGenre("rock", limit=10)
        self.assertEqual([(t["id"], t["playCount"]) for t in tracks], [("t1", 2), ("t2", 1), ("t3", 1)])
        self.assertEqual(tracks[0]["artistName"], "Artist One")

    def test_top_tracks_limit(self):
        db = self._seed()
        tracks = db.getTopTracksForGenre("rock", limit=1)
        self.assertEqual([t["id"] for t in tracks], ["t1"])


if __name__ == "__main__":
    unittest.main()
