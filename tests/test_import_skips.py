"""Skip routing, behavioral enrichment, and wrapped invalidation in the
import write path (Database._importHistoryLocked) and the listener path.

The importer decides what is a skip (meta["isSkip"], threshold in exactly one
place) - the DB writer only routes on the tag: skips go to play_skips via
INSERT OR IGNORE and never enter the near-time play matching."""
import sys
import os
import time
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.db import BEHAVIORAL_COLUMNS

EXTRAS_FULL = {
    "platform": "ios", "conn_country": "CH", "reason_start": "clickrow",
    "reason_end": "trackdone", "shuffle": 1, "skipped": 0, "offline": 0, "incognito": 0,
}


def _meta(trackId, playedAt, timePlayed=60000, isSkip=False, extras=None):
    track = normalizeTrackForTest({"id": trackId, "name": f"Song {trackId}", "artists": []})
    track["playedAt"] = playedAt
    track["timePlayed"] = timePlayed
    track["playedFrom"] = None
    track["isSkip"] = isSkip
    if extras:
        track["importExtras"] = extras
    return track


class _ImportTestBase(DatabaseTestCase):
    def _mockImporter(self, generatorFactory, parsedCount=2):
        importer = MagicMock()
        importer._convertToList.return_value = ([{}] * parsedCount, "spotifyAcountExport")
        importer.importHistory.return_value = generatorFactory()
        return importer

    def _import(self, db, gen):
        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

    def _skipRows(self, db):
        return [dict(r) for r in db.repo._conn().execute(
            "SELECT * FROM play_skips ORDER BY played_at").fetchall()]

    def _playRows(self, db):
        return [dict(r) for r in db.repo._conn().execute(
            "SELECT * FROM plays ORDER BY played_at").fetchall()]


class TestImportSkipRouting(_ImportTestBase):
    def test_skip_meta_lands_in_play_skips_not_plays(self):
        db = self._makeDb({}, [])

        def gen():
            yield _meta("track_x", 1000, timePlayed=400, isSkip=True, extras=EXTRAS_FULL)

        self._import(db, gen)

        self.assertEqual(self._playRows(db), [])
        skips = self._skipRows(db)
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["time_played"], 400)
        self.assertEqual(skips[0]["reason_end"], "trackdone")
        self.assertEqual(skips[0]["created_reason"], f"history_import (user: {db.user})")
        # The track itself is still cataloged (FK + future skip analytics)
        self.assertIsNotNone(db.repo.getTrack("track_x"))

    def test_skip_reimport_is_a_noop(self):
        db = self._makeDb({}, [])

        def gen():
            yield _meta("track_x", 1000, timePlayed=400, isSkip=True)

        self._import(db, gen)
        self._import(db, gen)

        self.assertEqual(len(self._skipRows(db)), 1)

    def test_skip_never_touches_nearby_plays(self):
        """A skip 3s after an existing play of the same track must not be
        mistaken for a correction of that play - skips bypass matching."""
        db = self._makeDb({}, [{"id": "track_x", "playedAt": 1000, "timePlayed": 60000}])

        def gen():
            yield _meta("track_x", 1003, timePlayed=400, isSkip=True)

        self._import(db, gen)

        plays = self._playRows(db)
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["played_at"], 1000)
        self.assertEqual(plays[0]["time_played"], 60000)
        self.assertEqual(len(self._skipRows(db)), 1)

    def test_skip_then_replay_in_one_file(self):
        db = self._makeDb({}, [])

        def gen():
            yield _meta("track_x", 1000, timePlayed=2500, isSkip=True)
            yield _meta("track_x", 1004, timePlayed=300000)

        self._import(db, gen)

        self.assertEqual(len(self._skipRows(db)), 1)
        plays = self._playRows(db)
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["played_at"], 1004)

    def test_failed_import_rolls_back_skips_too(self):
        db = self._makeDb({}, [])

        def gen():
            yield _meta("track_x", 1000, timePlayed=400, isSkip=True)
            raise RuntimeError("network died mid-import")

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            with self.assertRaises(RuntimeError):
                db.importHistory("raw export")

        self.assertEqual(self._skipRows(db), [])

    def test_summary_message_reports_counts(self):
        db = self._makeDb({}, [{"id": "track_y", "playedAt": 5000, "timePlayed": 5000}])

        def gen():
            yield _meta("track_x", 1000, timePlayed=400, isSkip=True)
            yield _meta("track_z", 9000, timePlayed=60000)
            yield _meta("track_y", 5003, timePlayed=6000)  #< corrects the seeded play

        self._import(db, gen)

        message = db.readProgress()["message"]
        self.assertIn("1 new", message)
        self.assertIn("1 corrected", message)
        self.assertIn("1 skips saved", message)


class TestImportEnrichment(_ImportTestBase):
    def test_correction_also_fills_behavioral_columns(self):
        db = self._makeDb({}, [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}])

        def gen():
            yield _meta("track_x", 105, timePlayed=6000, extras=EXTRAS_FULL)

        self._import(db, gen)

        plays = self._playRows(db)
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["played_at"], 105)
        self.assertEqual(plays[0]["time_played"], 6000)
        self.assertEqual(plays[0]["platform"], "ios")
        self.assertEqual(plays[0]["reason_end"], "trackdone")

    def test_identical_play_gets_enriched(self):
        db = self._makeDb({}, [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}])

        def gen():
            yield _meta("track_x", 100, timePlayed=5000, extras=EXTRAS_FULL)

        self._import(db, gen)

        plays = self._playRows(db)
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["platform"], "ios")
        self.assertEqual(plays[0]["shuffle"], 1)
        self.assertIn("1 enriched", db.readProgress()["message"])

    def test_reimport_after_enrichment_changes_nothing(self):
        db = self._makeDb({}, [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}])

        def gen():
            yield _meta("track_x", 100, timePlayed=5000, extras=EXTRAS_FULL)

        self._import(db, gen)
        self._import(db, gen)

        self.assertIn("0 enriched", db.readProgress()["message"])
        self.assertEqual(len(self._playRows(db)), 1)

    def test_none_extras_never_clobber_stored_values(self):
        db = self._makeDb({}, [])
        db.repo.upsertTrack(normalizeTrackForTest({"id": "track_x", "name": "Song", "artists": []}))
        db.repo.insertPlay(db.user, "track_x", 100, 5000, extras={"platform": "ios"})
        db.repo.commit()

        def gen():
            yield _meta("track_x", 100, timePlayed=5000,
                        extras={"platform": None, "conn_country": "DE"})

        self._import(db, gen)

        plays = self._playRows(db)
        self.assertEqual(plays[0]["platform"], "ios")
        self.assertEqual(plays[0]["conn_country"], "DE")


class TestWrappedInvalidationOnCorrection(_ImportTestBase):
    """Corrections that don't change a year's play count or max played_at are
    invisible to _wrappedCacheNeedsRecalc (it never compares total_ms) - the
    import must drop the cached Wrapped rows for corrected years itself."""

    WRAPPED_INSERT = """
        INSERT INTO user_wrapped (
            username, year, calculated_at, max_played_at, total_plays, total_ms,
            longest_streak, unique_songs, unique_artists, discovered_songs, discovered_artists,
            time_series_day, time_series_week, time_series_month,
            top_songs, top_artists, top_albums,
            discovered_songs_list, discovered_artists_list, discovered_albums_list
        ) VALUES (?, ?, 0, 0, 1, 1, 1, 1, 1, 0, 0,
                  '[]', '[]', '[]', '[]', '[]', '[]', '[]', '[]', '[]')
    """

    def _seedWrapped(self, db, year):
        conn = db.repo._conn()
        with conn:
            conn.execute(self.WRAPPED_INSERT, (db.user, year))

    def _wrappedYears(self, db):
        rows = db.repo._conn().execute(
            "SELECT year FROM user_wrapped WHERE username=?", (db.user,)).fetchall()
        return {r["year"] for r in rows}

    def test_correction_only_import_invalidates_that_years_wrapped(self):
        import datetime
        playedAt = datetime.datetime(2024, 6, 1, 12, 0, 0).timestamp()
        db = self._makeDb({}, [{"id": "track_x", "playedAt": playedAt, "timePlayed": 5000}])
        self._seedWrapped(db, 2024)
        self._seedWrapped(db, 2020)

        def gen():
            yield _meta("track_x", playedAt + 5, timePlayed=6000)

        self._import(db, gen)

        self.assertEqual(self._wrappedYears(db), {2020})

    def test_import_without_corrections_keeps_wrapped_cache(self):
        import datetime
        playedAt = datetime.datetime(2024, 6, 1, 12, 0, 0).timestamp()
        db = self._makeDb({}, [{"id": "track_x", "playedAt": playedAt, "timePlayed": 5000}])
        self._seedWrapped(db, 2024)

        def gen():
            yield _meta("track_x", playedAt, timePlayed=5000)  #< identical, no correction

        self._import(db, gen)

        self.assertEqual(self._wrappedYears(db), {2024})


class TestListenerSkipRecording(_ImportTestBase):
    """appendSkipData: the listener-path counterpart of appendTrackData for
    sub-threshold events."""

    def _rawTrack(self, trackId="t_live"):
        return {
            "id": trackId,
            "name": "Live Song",
            "external_urls": {"spotify": f"https://open.spotify.com/track/{trackId}"},
            "duration_ms": 200000,
            "album": {"id": "alb_live", "name": "Live Album",
                      "external_urls": {"spotify": "https://open.spotify.com/album/alb_live"},
                      "images": [], "total_tracks": 1, "release_date": "2020-01-01",
                      "artists": [{"id": "art_live", "name": "Live Artist",
                                   "external_urls": {"spotify": "https://open.spotify.com/artist/art_live"}}]},
        }

    def test_append_skip_data_records_skip_with_source_reason(self):
        db = self._makeDb({}, [])
        playedAt = time.time() - 60

        db.appendSkipData(playedAt, self._rawTrack(), 3000)

        skips = self._skipRows(db)
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["time_played"], 3000)
        self.assertEqual(skips[0]["created_reason"], f"listener_skip (user: {db.user})")
        self.assertIsNotNone(db.repo.getTrack("t_live"))
        self.assertEqual(self._playRows(db), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
