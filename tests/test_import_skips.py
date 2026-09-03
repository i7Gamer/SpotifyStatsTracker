"""Skip routing, behavioral enrichment, and wrapped invalidation in the
import write path (Database._importHistoryLocked) and the listener path.

The importer tags sub-floor events (meta["isSkip"], the fixed 5s import floor
in exactly one place) - the DB writer only routes on the tag: tagged events are
recorded into plays as is_skip=1 via INSERT OR IGNORE and never enter the
near-time play matching."""
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


def _meta(trackId, playedAt, timePlayed=60000, isSkip=False, extras=None, duration=0):
    track = normalizeTrackForTest({"id": trackId, "name": f"Song {trackId}", "artists": [],
                                   "duration": duration})
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
        #< the progress denominator _stageImportData asks the importer for:
        #  a count of PLAYS, which is len(parsed) for every non-Musicolet
        #  format (see Importer.expectedEntryCount)
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)
        importer.importHistory.return_value = generatorFactory()
        return importer

    def _import(self, db, gen):
        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

    def _skipRows(self, db):
        return [dict(r) for r in db.repo._conn().execute(
            "SELECT * FROM plays WHERE is_skip=1 ORDER BY played_at").fetchall()]

    def _playRows(self, db):
        return [dict(r) for r in db.repo._conn().execute(
            "SELECT * FROM plays WHERE is_skip=0 ORDER BY played_at").fetchall()]


class TestImportSkipRouting(_ImportTestBase):
    def test_skip_meta_lands_as_is_skip_row_not_real_play(self):
        db = self._makeDb({}, [])

        def gen():
            yield _meta("track_x", 1000, timePlayed=400, isSkip=True, extras=EXTRAS_FULL)

        self._import(db, gen)

        self.assertEqual(self._playRows(db), [])   #< no real (is_skip=0) play
        skips = self._skipRows(db)
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["time_played"], 400)
        self.assertEqual(skips[0]["is_skip"], 1)
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


class TestSkipNearTimeDedup(_ImportTestBase):
    """The live listener records sub-threshold events too, and the two sources'
    played_at for one physical skip can differ by seconds (Spotify's documented
    start-vs-end ambiguity). plays' UNIQUE constraint needs an exact timestamp
    match, so the same skip landed twice and inflated skip counts."""

    def _seedListenerSkip(self, db, trackId, playedAt):
        db.repo.upsertTrack(normalizeTrackForTest({"id": trackId, "name": "Song", "artists": []}))
        db.repo.insertPlay(db.user, trackId, playedAt, 400, created_reason="listener", is_skip=1)
        db.repo.commit()

    def test_import_skip_near_a_listener_skip_is_not_recorded_twice(self):
        db = self._makeDb({}, [])
        self._seedListenerSkip(db, "track_x", 1000)

        def gen():
            yield _meta("track_x", 1004, timePlayed=400, isSkip=True)   #< same event, 4s apart

        self._import(db, gen)

        self.assertEqual(len(self._skipRows(db)), 1)

    def test_a_skip_well_outside_the_window_is_still_recorded(self):
        db = self._makeDb({}, [])
        self._seedListenerSkip(db, "track_x", 1000)

        def gen():
            yield _meta("track_x", 1600, timePlayed=400, isSkip=True)   #< 10 minutes later

        self._import(db, gen)

        self.assertEqual(len(self._skipRows(db)), 2)

    def test_two_skips_in_the_same_export_are_both_kept(self):
        """Own-run writes are never dedup candidates: two genuinely distinct
        skips of one track must not collapse into a single row."""
        db = self._makeDb({}, [])

        def gen():
            yield _meta("track_x", 1000, timePlayed=400, isSkip=True)
            yield _meta("track_x", 1003, timePlayed=400, isSkip=True)

        self._import(db, gen)

        self.assertEqual(len(self._skipRows(db)), 2)

    def test_one_recorded_skip_answers_for_only_one_export_entry(self):
        """_ImportRunState's contract: an existing row can be claimed by at most
        one import entry per run, because one physical play is one export entry.

        The skip branch matched a nearby row without CLAIMING it, so a second
        export entry inside the window matched the very same row and was dropped
        too - silently, with no stat reporting it. Re-importing exists to repair
        exactly the skip the listener missed, and here it repaired nothing."""
        db = self._makeDb({}, [])
        self._seedListenerSkip(db, "track_x", 1000)

        def gen():
            yield _meta("track_x", 1001, timePlayed=400, isSkip=True)   #< the recorded one
            yield _meta("track_x", 1006, timePlayed=400, isSkip=True)   #< the one it missed

        self._import(db, gen)

        #< the claimed row plus the genuinely missing second skip
        self.assertEqual(len(self._skipRows(db)), 2)

    def test_the_nearest_recorded_skip_is_the_one_claimed(self):
        """With two recorded rows in range, pairing has to be deterministic, or
        which entry survives depends on row order."""
        db = self._makeDb({}, [])
        self._seedListenerSkip(db, "track_x", 1000)
        db.repo.insertPlay(db.user, "track_x", 1008, 400, created_reason="listener", is_skip=1)
        db.repo.commit()

        def gen():
            yield _meta("track_x", 1001, timePlayed=400, isSkip=True)   #< nearest 1000
            yield _meta("track_x", 1007, timePlayed=400, isSkip=True)   #< nearest 1008

        self._import(db, gen)

        #< both entries paired off against a row already there: nothing added
        self.assertEqual(sorted(row["played_at"] for row in self._skipRows(db)), [1000, 1008])

    def test_a_nearby_real_play_is_never_treated_as_the_same_event(self):
        """Skips match only against skips - a real play must not suppress a
        skip (nor be claimed by one)."""
        db = self._makeDb({}, [{"id": "track_x", "playedAt": 1000, "timePlayed": 60000}])

        def gen():
            yield _meta("track_x", 1003, timePlayed=400, isSkip=True)

        self._import(db, gen)

        self.assertEqual(len(self._playRows(db)), 1)
        self.assertEqual(len(self._skipRows(db)), 1)


class TestShortTracksAreNotSkipsJustForBeingShort(_ImportTestBase):
    """The importer's isSkip tag is a fixed 5s floor in one place
    (StreamingHistoryImporter's SKIP_THRESHOLD_MS). Since 73e1a2c that is no
    longer the same question as "is this a skip": computeIsSkip caps its
    threshold at the completion boundary, so a play UNDER 5s is a complete play
    whenever duration x completion% < 5000ms - i.e. any track shorter than
    6.25s at the default 80%.

    Routing on the tag alone therefore stored is_skip=1 for a 3s interlude
    played to its end, disagreeing with the classifier (so the next
    recomputeSkipFlags flipped it), AND sent it down the skip-only dedup path,
    where the listener's correctly-classified row for the same physical event is
    invisible - so one event became two real plays after that recompute.
    """

    SHORT_MS = 3_000   #< a 3s track: complete at 3s, since 3000 >= 3000*0.8

    def test_a_short_track_played_to_the_end_is_stored_as_a_real_play(self):
        db = self._makeDb({}, [])

        def gen():
            yield _meta("track_tiny", 1000, timePlayed=self.SHORT_MS, isSkip=True,
                        duration=self.SHORT_MS, extras=EXTRAS_FULL)

        self._import(db, gen)

        self.assertEqual(self._skipRows(db), [])
        plays = self._playRows(db)
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["time_played"], self.SHORT_MS)
        # The stored flag must be whatever the classifier says, always.
        self.assertEqual(plays[0]["is_skip"],
                         db.repo.computeIsSkip(self.SHORT_MS, self.SHORT_MS))

    def test_a_recompute_does_not_change_what_the_import_stored(self):
        """The disagreement is what made this a double-count: the import row
        flipped to is_skip=0 later, after the dedup had already let it in."""
        db = self._makeDb({}, [])

        def gen():
            yield _meta("track_tiny", 1000, timePlayed=self.SHORT_MS, isSkip=True,
                        duration=self.SHORT_MS)

        self._import(db, gen)
        before = self._playRows(db) + self._skipRows(db)
        db.repo.recomputeSkipFlags()
        after = self._playRows(db) + self._skipRows(db)

        self.assertEqual([r["is_skip"] for r in before], [r["is_skip"] for r in after])

    def test_it_dedups_against_the_listeners_row_for_the_same_event(self):
        """The listener classified the same 3s play correctly (is_skip=0), so it
        is only reachable through the real-play matcher - the skip-only path
        filters is_skip=1 and would have inserted a second row."""
        db = self._makeDb({}, [])
        db.repo.upsertTrack(normalizeTrackForTest(
            {"id": "track_tiny", "name": "Song", "artists": [], "duration": self.SHORT_MS}))
        db.repo.insertPlay(db.user, "track_tiny", 1000, self.SHORT_MS,
                           created_reason="listener", is_skip=0)
        db.repo.commit()

        def gen():
            yield _meta("track_tiny", 1004, timePlayed=self.SHORT_MS, isSkip=True,
                        duration=self.SHORT_MS)   #< same event, 4s apart

        self._import(db, gen)

        self.assertEqual(len(self._playRows(db)) + len(self._skipRows(db)), 1)

    def test_a_genuinely_abandoned_short_play_is_still_a_skip(self):
        """The narrowing must not go the other way: barely-started is still a
        skip on a short track (0.5s of a 3s track is under the 2.4s boundary)."""
        db = self._makeDb({}, [])

        def gen():
            yield _meta("track_tiny", 1000, timePlayed=500, isSkip=True,
                        duration=self.SHORT_MS)

        self._import(db, gen)

        self.assertEqual(self._playRows(db), [])
        self.assertEqual(len(self._skipRows(db)), 1)


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
    """The listener records sub-threshold events through appendTrackData now
    (appendSkipData is gone): they land in plays as is_skip=1, computed from the
    current threshold + the track's duration."""

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

    def test_listener_sub_threshold_event_lands_as_is_skip_row(self):
        db = self._makeDb({}, [])
        playedAt = time.time() - 60

        db.appendTrackData(playedAt, self._rawTrack(), 3000)   #< 3s < default 5s -> skip

        skips = self._skipRows(db)
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["time_played"], 3000)
        self.assertEqual(skips[0]["is_skip"], 1)
        self.assertEqual(skips[0]["created_reason"], f"listener_play (user: {db.user})")
        self.assertIsNotNone(db.repo.getTrack("t_live"))
        self.assertEqual(self._playRows(db), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
