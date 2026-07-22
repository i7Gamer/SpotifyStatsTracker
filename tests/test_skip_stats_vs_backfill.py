"""The is_skip split, guarded where fixtures usually can't catch it: a skip
(is_skip=1) must be EXCLUDED from every listening stat but still drive metadata
backfill (genres/bios cover every played track, any duration).

Most other tests use >=5s plays (no skips present), so a query that forgot its
is_skip filter would pass them silently - these insert a real play AND a skip
and assert both sides of the boundary.
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase

_TS_2020 = 1577836800   # 2020-01-01 UTC
_TS_1990 = 631152000    # 1990-01-01 UTC


class SkipStatsVsBackfillTestCase(DatabaseTestCase):
    def _setup(self):
        tracks = {
            "tReal": {"id": "tReal", "name": "Real Song", "explicit": True, "releaseDate": _TS_2020,
                      "duration": 200000, "artists": [{"id": "aReal", "name": "Real Artist"}],
                      "album": {"id": "alReal", "name": "Real Album", "url": "http://x/alReal",
                                "imageId": "alReal", "imageUrl": "", "totalTracks": 1, "releaseDate": _TS_2020}},
            "tSkip": {"id": "tSkip", "name": "Skip Song", "explicit": True, "releaseDate": _TS_1990,
                      "duration": 200000, "artists": [{"id": "aSkip", "name": "Skip Artist"}],
                      "album": {"id": "alSkip", "name": "Skip Album", "url": "http://x/alSkip",
                                "imageId": "alSkip", "imageUrl": "", "totalTracks": 1, "releaseDate": _TS_1990}},
        }
        entries = [{"id": "tReal", "playedAt": 1000, "timePlayed": 60000}]   #< one real play
        db = self._makeDb(tracks, entries)
        # tSkip is ONLY ever skipped.
        db.repo.insertPlay("testuser", "tSkip", 2000, 400, is_skip=1)
        conn = db.repo._conn()
        with conn:
            conn.execute("INSERT INTO track_genres (track_id, genre, position) VALUES ('tReal', 'rock', 0)")
            conn.execute("INSERT INTO track_genres (track_id, genre, position) VALUES ('tSkip', 'jazz', 0)")
            conn.execute("INSERT INTO artist_genres (artist_id, genre, position) VALUES ('aReal', 'rock', 0)")
        db.repo.commit()
        return db

    # ---- listening stats: skip EXCLUDED -----------------------------------------

    def test_explicit_ratio_excludes_skip(self):
        db = self._setup()
        self.assertEqual(db.getExplicitRatio(), {"explicit": 1, "clean": 0})

    def test_release_decade_distribution_excludes_skip(self):
        db = self._setup()
        self.assertEqual(db.getReleaseDecadeDistribution(), {"2020s": 1})   #< not 1990s

    def test_genre_distribution_excludes_skip(self):
        db = self._setup()
        self.assertEqual(db.getGenreDistribution(), {"rock": 1})   #< no "jazz" from the skip

    def test_genre_play_stats_excludes_skip(self):
        db = self._setup()
        # jazz is only on the skipped track, so the genre has zero real plays.
        self.assertEqual(db.repo.getGenrePlayStats("testuser", "jazz", 1, None, None)["plays"], 0)

    def test_play_totals_and_global_stats_exclude_skip(self):
        db = self._setup()
        self.assertEqual(db.repo.getPlayTotals("testuser"), (1, 60000))
        self.assertEqual(db.repo.getGlobalDatabaseStats()["plays"], 1)

    # ---- backfill: skip INCLUDED ------------------------------------------------

    def test_genre_backfill_queue_includes_skip_only_artist(self):
        db = self._setup()
        missing = {a["id"] for a in db.repo.getArtistsMissingGenres(limit=50)}
        self.assertIn("aSkip", missing)   #< a skip-only artist still needs its genres

    def test_bio_backfill_queue_includes_skip_only_artist(self):
        db = self._setup()
        missing = {a["id"] for a in db.repo.getArtistsMissingBiographies(limit=50)}
        self.assertIn("aSkip", missing)

    def test_bio_coverage_counts_skip_only_artist(self):
        db = self._setup()
        # aReal + aSkip both count toward the "played artists" denominator.
        self.assertEqual(db.repo.getBiographyCoverage("testuser")["artist"]["total"], 2)


if __name__ == "__main__":
    import unittest
    unittest.main()
