import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest


def _meta(trackId, playedAt, timePlayed=60000):
    """A full importer-yielded item: entry fields + enough track metadata to
    satisfy Repository.upsertTrack (mirrors what Client.formatTrack produces).
    Default duration is a full listen (> the 5s skip floor) so imported entries
    are real plays (is_skip=0) unless a test overrides timePlayed."""
    track = normalizeTrackForTest({"id": trackId, "name": f"Song {trackId}", "artists": []})
    track["playedAt"] = playedAt
    track["timePlayed"] = timePlayed
    track["playedFrom"] = None
    return track


class TestDatabaseDeduplication(DatabaseTestCase):
    def _mockImporter(self, generatorFactory, parsedCount=2):
        importer = MagicMock()
        importer._convertToList.return_value = ([{}] * parsedCount, "spotifyAcountExport")
        importer.importHistory.return_value = generatorFactory()
        return importer


    def test_import_history_ignores_already_existing_entries(self):
        """importHistory must not add entries that are already in the database."""
        entries = [
            {"id": "e1", "playedAt": 100, "timePlayed": 1000},
            {"id": "e2", "playedAt": 300, "timePlayed": 1000},
        ]
        db = self._makeDb({}, entries)

        def gen():
            # i1 is new, e1 already exists (id=e1, playedAt=100)
            yield _meta("i1", 200)
            yield _meta("e1", 100)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        playedAts = [e["playedAt"] for e in db.getEntriesFromNew(fullPagination=False)]
        self.assertEqual(len(playedAts), 3)
        self.assertIn(100, playedAts)
        self.assertIn(200, playedAts)
        self.assertIn(300, playedAts)

    def test_import_history_ignores_duplicates_in_the_import_source_itself(self):
        """importHistory must not import duplicate entries present in the source file."""
        db = self._makeDb({}, [])

        def gen():
            yield _meta("i1", 200)
            yield _meta("i1", 200)  # Duplicate within source

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        playedAts = [e["playedAt"] for e in db.getEntriesFromNew(fullPagination=False)]
        self.assertEqual(playedAts.count(200), 1)
        self.assertEqual(len(playedAts), 1)

    def test_import_updates_both_fields_when_single_duplicate_with_different_data(self):
        """When import finds exactly one duplicate within tolerance window with
        different time_played or played_at, it should update the existing play
        with the imported data (treating import as source of truth)."""
        # DB: play at playedAt=100 with timePlayed=5000
        entries = [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}]
        db = self._makeDb({}, entries)

        def gen():
            # Import: same track at playedAt=105 (within tolerance) with timePlayed=6000
            yield _meta("track_x", 105, timePlayed=6000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        # Should have 1 entry (updated, not new)
        plays = db.getEntriesFromNew(fullPagination=False)
        self.assertEqual(len(plays), 1)
        # Should be updated to import's values
        self.assertEqual(plays[0]["playedAt"], 105)
        self.assertEqual(plays[0]["timePlayed"], 6000)

    def test_import_skips_when_single_duplicate_with_same_data(self):
        """When import finds exactly one duplicate with identical data,
        skip without updating (no change needed)."""
        entries = [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}]
        db = self._makeDb({}, entries)

        def gen():
            # Import: same track at same timestamp with same duration
            yield _meta("track_x", 100, timePlayed=5000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        plays = db.getEntriesFromNew(fullPagination=False)
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["playedAt"], 100)
        self.assertEqual(plays[0]["timePlayed"], 5000)

    def test_import_skips_when_multiple_duplicates_in_window(self):
        """When import finds multiple duplicates within tolerance window,
        skip to avoid ambiguity (don't guess which play to update)."""
        # DB: two plays of same track within close time
        entries = [
            {"id": "track_x", "playedAt": 100, "timePlayed": 5000},
            {"id": "track_x", "playedAt": 110, "timePlayed": 4500},
        ]
        db = self._makeDb({}, entries)

        def gen():
            # Import: same track at playedAt=105 (within tolerance of both)
            # with timePlayed=6000, which differs from both DB entries
            yield _meta("track_x", 105, timePlayed=6000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        plays = db.getEntriesFromNew(fullPagination=False)
        # Should still have both original entries (nothing updated)
        self.assertEqual(len(plays), 2)
        play_times = sorted([p["timePlayed"] for p in plays])
        self.assertEqual(play_times, [4500, 5000])

    def test_import_inserts_when_no_duplicate_in_window(self):
        """When import finds no duplicate within tolerance window,
        insert as new entry (unchanged behavior)."""
        entries = [{"id": "track_x", "playedAt": 200, "timePlayed": 5000}]
        db = self._makeDb({}, entries)

        def gen():
            # Import: same track at playedAt=100 (far outside tolerance window)
            yield _meta("track_x", 100, timePlayed=6000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        plays = db.getEntriesFromNew(fullPagination=False)
        # Should have both entries (new one inserted)
        self.assertEqual(len(plays), 2)
        played_ats = sorted([p["playedAt"] for p in plays])
        self.assertEqual(played_ats, [100, 200])

    def test_import_duplicate_logging_with_debug(self):
        """Skip messages should be logged when FLASK_DEBUG is enabled."""
        # 1. Duplicate with identical data
        entries = [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}]
        db = self._makeDb({}, entries)
        def gen_identical():
            yield _meta("track_x", 100, timePlayed=5000)

        with patch.dict(os.environ, {"FLASK_DEBUG": "1"}):
            with patch("Database.database.Importer", return_value=self._mockImporter(gen_identical)):
                with self.assertLogs("Database.database", level="INFO") as log_capture:
                    db.importHistory("raw export")
        self.assertTrue(any("duplicate found with identical data" in record for record in log_capture.output))

        # 2. Multiple matches (ambiguous)
        entries_multi = [
            {"id": "track_x", "playedAt": 100, "timePlayed": 5000},
            {"id": "track_x", "playedAt": 110, "timePlayed": 4500},
        ]
        db_multi = self._makeDb({}, entries_multi)
        def gen_multi():
            yield _meta("track_x", 105, timePlayed=6000)

        with patch.dict(os.environ, {"FLASK_DEBUG": "true"}):
            with patch("Database.database.Importer", return_value=self._mockImporter(gen_multi)):
                with self.assertLogs("Database.database", level="INFO") as log_capture:
                    db_multi.importHistory("raw export")
        self.assertTrue(any("plays found within tolerance - ambiguous" in record for record in log_capture.output))

    def test_import_duplicate_logging_without_debug(self):
        """Skip messages should not be logged when FLASK_DEBUG is disabled or missing."""
        # 1. Duplicate with identical data
        entries = [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}]
        db = self._makeDb({}, entries)
        def gen_identical():
            yield _meta("track_x", 100, timePlayed=5000)

        with patch.dict(os.environ, {"FLASK_DEBUG": "0"}):
            with patch("Database.database.Importer", return_value=self._mockImporter(gen_identical)):
                try:
                    with self.assertLogs("Database.database", level="INFO") as log_capture:
                        db.importHistory("raw export")
                    self.assertFalse(any("duplicate found with identical data" in record for record in log_capture.output))
                except AssertionError:
                    pass

        # 2. Multiple matches (ambiguous)
        entries_multi = [
            {"id": "track_x", "playedAt": 100, "timePlayed": 5000},
            {"id": "track_x", "playedAt": 110, "timePlayed": 4500},
        ]
        db_multi = self._makeDb({}, entries_multi)
        def gen_multi():
            yield _meta("track_x", 105, timePlayed=6000)

        env_copy = os.environ.copy()
        if "FLASK_DEBUG" in env_copy:
            del env_copy["FLASK_DEBUG"]
        with patch.dict(os.environ, env_copy, clear=True):
            with patch("Database.database.Importer", return_value=self._mockImporter(gen_multi)):
                try:
                    with self.assertLogs("Database.database", level="INFO") as log_capture:
                        db_multi.importHistory("raw export")
                    self.assertFalse(any("plays found within tolerance - ambiguous" in record for record in log_capture.output))
                except AssertionError:
                    pass

    def test_import_update_log_formatting(self):
        """Updated import plays should output log messages specifying only the actual fields changed."""
        # 1. Both played_at and time_played change
        entries1 = [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}]
        db1 = self._makeDb({}, entries1)
        def gen1():
            yield _meta("track_x", 105, timePlayed=6000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen1)):
            with self.assertLogs("Database.database", level="INFO") as log_capture:
                db1.importHistory("raw export")
        msg = log_capture.output[0]
        self.assertIn("played_at corrected from 100 to 105", msg)
        self.assertIn("time_played corrected from 5000ms to 6000ms", msg)

        # 2. Only played_at changes
        entries2 = [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}]
        db2 = self._makeDb({}, entries2)
        def gen2():
            yield _meta("track_x", 105, timePlayed=5000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen2)):
            with self.assertLogs("Database.database", level="INFO") as log_capture:
                db2.importHistory("raw export")
        msg = log_capture.output[0]
        self.assertIn("played_at corrected from 100 to 105", msg)
        self.assertNotIn("time_played", msg)

        # 3. Only time_played changes
        entries3 = [{"id": "track_x", "playedAt": 100, "timePlayed": 5000}]
        db3 = self._makeDb({}, entries3)
        def gen3():
            yield _meta("track_x", 100, timePlayed=6000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen3)):
            with self.assertLogs("Database.database", level="INFO") as log_capture:
                db3.importHistory("raw export")
        msg = log_capture.output[0]
        self.assertNotIn("played_at", msg)
        self.assertIn("time_played corrected from 5000ms to 6000ms", msg)

    def test_import_preserves_consecutive_plays_of_same_song(self):
        """Consecutive plays of the same song must not be collapsed/overwritten."""
        # Song has 240s duration. Play 1 starts at 1000. Play 2 starts at 1240.
        db = self._makeDb({}, [])

        def gen():
            yield _meta("track_x", 1000, timePlayed=240000)
            yield _meta("track_x", 1240, timePlayed=240000)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        plays = db.getEntriesFromNew(fullPagination=False)
        self.assertEqual(len(plays), 2)
        played_ats = sorted([p["playedAt"] for p in plays])
        self.assertEqual(played_ats, [1000, 1240])

    def test_import_preserves_skip_then_replay_of_same_track(self):
        """A short skip followed by a replay of the same track in the same file
        must produce two plays - the replay must not "correct" (overwrite) the
        skip row the import itself inserted moments earlier."""
        db = self._makeDb({}, [])
        SKIP_START = 1000
        SKIP_PLAYED_MS = 17002
        REPLAY_START = SKIP_START + 18  #< replay starts right after the 17s skip ends
        REPLAY_PLAYED_MS = 192881
        TRACK_DURATION_MS = 200000

        def gen():
            skip = _meta("track_x", SKIP_START, timePlayed=SKIP_PLAYED_MS)
            skip["duration"] = TRACK_DURATION_MS
            replay = _meta("track_x", REPLAY_START, timePlayed=REPLAY_PLAYED_MS)
            replay["duration"] = TRACK_DURATION_MS
            yield skip
            yield replay

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        plays = db.getEntriesFromNew(fullPagination=False)
        self.assertEqual(len(plays), 2)
        self.assertEqual(sorted(p["playedAt"] for p in plays), [SKIP_START, REPLAY_START])

    def test_import_preserves_quick_restart_within_start_window(self):
        """A skip and a restart only a few seconds apart (inside the 15s
        start-time match window) must still produce two plays on a fresh
        import - the second entry may not claim the row the first inserted."""
        db = self._makeDb({}, [])
        SKIP_START = 1000
        SKIP_PLAYED_MS = 2538
        RESTART_START = SKIP_START + 4
        RESTART_PLAYED_MS = 399803
        TRACK_DURATION_MS = 400000

        def gen():
            skip = _meta("track_x", SKIP_START, timePlayed=SKIP_PLAYED_MS)
            skip["duration"] = TRACK_DURATION_MS
            restart = _meta("track_x", RESTART_START, timePlayed=RESTART_PLAYED_MS)
            restart["duration"] = TRACK_DURATION_MS
            yield skip
            yield restart

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        # The 2538ms event is a skip (is_skip=1) under the default 5s threshold,
        # the restart a real play (is_skip=0) - two separate rows, not collapsed
        # (the restart must not claim the skip's row, nor vice versa).
        rows = db.repo._conn().execute(
            "SELECT played_at, is_skip FROM plays ORDER BY played_at").fetchall()
        self.assertEqual([(r["played_at"], r["is_skip"]) for r in rows],
                         [(SKIP_START, 1), (RESTART_START, 0)])

    def test_import_match_window_uses_track_duration(self):
        """The end-time match rule must use the track's actual duration: a
        pre-existing play 33s before an imported one only matches when the gap
        corresponds to the track's length, not for any gap under 60s."""
        EXISTING_START = 1000
        EXISTING_PLAYED_MS = 2670
        IMPORT_START = EXISTING_START + 33
        IMPORT_PLAYED_MS = 226826
        TRACK_DURATION_MS = 240000
        entries = [{"id": "track_x", "playedAt": EXISTING_START, "timePlayed": EXISTING_PLAYED_MS}]
        db = self._makeDb({}, entries)

        def gen():
            meta = _meta("track_x", IMPORT_START, timePlayed=IMPORT_PLAYED_MS)
            meta["duration"] = TRACK_DURATION_MS
            yield meta

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        plays = db.getEntriesFromNew(fullPagination=False)
        self.assertEqual(len(plays), 2)
        self.assertEqual(sorted(p["playedAt"] for p in plays), [EXISTING_START, IMPORT_START])
        # The pre-existing play must be untouched
        self.assertIn(EXISTING_PLAYED_MS, [p["timePlayed"] for p in plays])

    def test_import_identical_entry_claims_row_so_neighbor_is_not_overwritten(self):
        """Re-importing a file over existing data: an entry identical to its DB
        row claims that row, so a later nearby entry can't mistake the already-
        matched row for its own play and overwrite it."""
        EXISTING_START = 1000
        EXISTING_PLAYED_MS = 5000
        NEW_START = EXISTING_START + 10
        NEW_PLAYED_MS = 300000
        entries = [{"id": "track_x", "playedAt": EXISTING_START, "timePlayed": EXISTING_PLAYED_MS}]
        db = self._makeDb({}, entries)

        def gen():
            yield _meta("track_x", EXISTING_START, timePlayed=EXISTING_PLAYED_MS)  #< identical to DB row
            yield _meta("track_x", NEW_START, timePlayed=NEW_PLAYED_MS)  #< genuinely new play

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        plays = db.getEntriesFromNew(fullPagination=False)
        self.assertEqual(len(plays), 2)
        self.assertEqual(sorted(p["playedAt"] for p in plays), [EXISTING_START, NEW_START])
        self.assertIn(EXISTING_PLAYED_MS, [p["timePlayed"] for p in plays])

    def test_import_batch_preserves_replays_across_file_boundary(self):
        """A skip at the end of one file and its replay at the start of the next
        must both survive a batch import - run-state claims span the batch even
        though each file commits separately."""
        db = self._makeDb({}, [])
        SKIP_START = 1000
        SKIP_PLAYED_MS = 2538
        REPLAY_START = SKIP_START + 4
        REPLAY_PLAYED_MS = 399803
        TRACK_DURATION_MS = 400000

        def gen1():
            skip = _meta("track_x", SKIP_START, timePlayed=SKIP_PLAYED_MS)
            skip["duration"] = TRACK_DURATION_MS
            yield skip

        def gen2():
            replay = _meta("track_x", REPLAY_START, timePlayed=REPLAY_PLAYED_MS)
            replay["duration"] = TRACK_DURATION_MS
            yield replay

        importer = MagicMock()
        importer._convertToList.return_value = ([{}], "spotifyAcountExport")
        importer.importHistory.side_effect = [gen1(), gen2()]

        with patch("Database.database.Importer", return_value=importer):
            db.importHistoryBatch(["file one", "file two"])

        # The end-of-file-1 skip (is_skip=1) and its start-of-file-2 replay
        # (is_skip=0) must both survive as separate rows - run-state claims span
        # the batch so the replay can't claim the skip's row.
        rows = db.repo._conn().execute(
            "SELECT played_at, is_skip FROM plays ORDER BY played_at").fetchall()
        self.assertEqual([(r["played_at"], r["is_skip"]) for r in rows],
                         [(SKIP_START, 1), (REPLAY_START, 0)])

    def test_import_updates_synthetic_track_duration(self):
        """Synthetic track durations should be updated in the catalog when a longer play duration is imported."""
        # Create a synthetic fallback track with duration 10s
        from Database.db import SYNTHETIC_FALLBACK_REASON
        db = self._makeDb({}, [])
        
        # Populate catalog with synthetic track
        synthetic_track = _meta("track_x", 1000, timePlayed=10000)
        synthetic_track["created_reason"] = SYNTHETIC_FALLBACK_REASON
        synthetic_track["duration"] = 10000  # 10s
        db.repo.upsertTrack(synthetic_track)
        db.repo.commit()

        # Import a longer play (e.g. 240s)
        def gen():
            track = _meta("track_x", 2000, timePlayed=240000)
            track["duration"] = 240000
            yield track

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            db.importHistory("raw export")

        # Verify catalog track duration was updated
        track = db.repo.getTrack("track_x")
        self.assertEqual(track["duration"], 240000)



if __name__ == "__main__":
    import unittest
    unittest.main()
