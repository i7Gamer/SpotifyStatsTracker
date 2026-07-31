"""Overwrite imports: delete the user's plays/skips in the covered-year
segments of the uploaded files' span (missing years protected), bypass the
already-imported hash gate, clear cached Wrapped for covered years - all in
ONE transaction shared with every file's import, committed once at the very
end. A failure anywhere (an unrecognized file, the delete pass itself, or any
single file's import) rolls back everything and aborts the batch, so the
overwrite either fully lands or leaves the original data untouched."""
import datetime
import hashlib
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.utils import getTimezone


def _ts(year, month=6, day=1, hour=12):
    """Timestamp in the app timezone - the tz coverage()/the delete
    segmentation bucket years in."""
    return datetime.datetime(year, month, day, hour, tzinfo=getTimezone()).timestamp()


def _meta(trackId, playedAt, timePlayed=60000):
    track = normalizeTrackForTest({"id": trackId, "name": f"Song {trackId}", "artists": []})
    track["playedAt"] = playedAt
    track["timePlayed"] = timePlayed
    track["playedFrom"] = None
    return track


class _OverwriteTestBase(DatabaseTestCase):
    """Mocked importer with per-file coverage: fileSpecs maps content ->
    (coverage tuple or None, generator factory)."""

    def _mockImporter(self, fileSpecs):
        importer = MagicMock()

        def convertToList(content):
            if content not in fileSpecs:
                return [], "None"
            return [{}], "spotifyExtendedExport"

        def coverage(parsedHistory, exportType):
            # Called once per file in the pre-pass, in upload order
            return next(coverageResults)

        coverageResults = iter([fileSpecs[c][0] for c in fileSpecs])
        importer._convertToList.side_effect = convertToList
        importer.coverage.side_effect = coverage
        importer.importHistory.side_effect = [spec[1]() for spec in fileSpecs.values()]
        return importer

    def _runBatch(self, db, fileSpecs, overwriteRange=True):
        with patch("Database.database.Importer", return_value=self._mockImporter(fileSpecs)):
            return db.importHistoryBatch(list(fileSpecs.keys()), overwriteRange=overwriteRange)

    def _playedAts(self, db):
        rows = db.repo._conn().execute(
            "SELECT played_at FROM plays WHERE username=? ORDER BY played_at", (db.user,)).fetchall()
        return [r["played_at"] for r in rows]


class TestOverwriteDeletesCoveredRange(_OverwriteTestBase):
    def test_covered_years_are_wiped_and_reimported_others_survive(self):
        db = self._makeDb({}, [
            {"id": "old18", "playedAt": _ts(2018), "timePlayed": 60000},
            {"id": "old19", "playedAt": _ts(2019), "timePlayed": 60000},
            {"id": "old21", "playedAt": _ts(2021), "timePlayed": 60000},  #< outside the span
        ])
        db.repo.upsertTrack(normalizeTrackForTest({"id": "t_skip", "name": "S", "artists": []}))
        db.repo.insertPlay(db.user, "t_skip", _ts(2018, 7), 400, is_skip=1)
        db.repo.commit()

        fileSpecs = {
            "file 2018": ((_ts(2018, 2), _ts(2018, 11), {2018}),
                          lambda: iter([_meta("new18", _ts(2018, 3))])),
            "file 2019": ((_ts(2019, 1, 5), _ts(2019, 12, 20), {2019}),
                          lambda: iter([_meta("new19", _ts(2019, 4))])),
        }
        outcomes = self._runBatch(db, fileSpecs)

        self.assertEqual(outcomes, ["imported", "imported"])
        playedAts = self._playedAts(db)
        self.assertNotIn(_ts(2018), playedAts)   #< covered year wiped
        self.assertNotIn(_ts(2019), playedAts)
        self.assertIn(_ts(2021), playedAts)      #< outside span - untouched
        self.assertIn(_ts(2018, 3), playedAts)   #< re-imported fresh
        self.assertIn(_ts(2019, 4), playedAts)
        skipCount = db.repo._conn().execute("SELECT COUNT(*) FROM plays WHERE is_skip=1").fetchone()[0]
        self.assertEqual(skipCount, 0)           #< covered-range skips wiped too

    def test_gap_inside_a_covered_year_is_wiped_too(self):
        """The export is Spotify's complete record for a covered year - a
        quiet mid-year gap holds no legitimate Spotify data, so stale rows
        there must go."""
        db = self._makeDb({}, [
            {"id": "gapPlay", "playedAt": _ts(2019, 6, 15), "timePlayed": 60000},
        ])
        fileSpecs = {
            "file 2019": ((_ts(2019, 1, 15), _ts(2019, 12, 15), {2019}),
                          lambda: iter([])),
        }
        self._runBatch(db, fileSpecs)

        self.assertEqual(self._playedAts(db), [])

    def test_missing_years_inside_the_span_are_protected(self):
        db = self._makeDb({}, [
            {"id": "old18", "playedAt": _ts(2018), "timePlayed": 60000},
            {"id": "old19", "playedAt": _ts(2019), "timePlayed": 60000},
            {"id": "old20", "playedAt": _ts(2020), "timePlayed": 60000},
            {"id": "old21", "playedAt": _ts(2021), "timePlayed": 60000},
        ])
        fileSpecs = {
            "file 2018": ((_ts(2018, 2), _ts(2018, 11), {2018}), lambda: iter([])),
            "file 2021": ((_ts(2021, 2), _ts(2021, 11), {2021}), lambda: iter([])),
        }
        with self.assertLogs("Database.database", level="INFO") as logCapture:
            self._runBatch(db, fileSpecs)

        playedAts = self._playedAts(db)
        self.assertNotIn(_ts(2018), playedAts)
        self.assertIn(_ts(2019), playedAts)   #< no file covered these years
        self.assertIn(_ts(2020), playedAts)
        self.assertNotIn(_ts(2021), playedAts)
        self.assertTrue(any("2019" in m and "2020" in m and "not covered" in m
                            for m in logCapture.output))

    def test_play_at_next_years_midnight_survives_a_straddling_span(self):
        """A span whose last play straddles New Year reaches into the next
        year without covering it - a play exactly at that year's midnight
        belongs to the uncovered year and must survive."""
        nextYearMidnight = datetime.datetime(2020, 1, 1, 0, 0, 0, tzinfo=getTimezone()).timestamp()
        db = self._makeDb({}, [
            {"id": "boundary", "playedAt": nextYearMidnight, "timePlayed": 60000},
        ])
        fileSpecs = {
            "file 2019": ((_ts(2019, 1, 5), nextYearMidnight + 120, {2019}),
                          lambda: iter([])),
        }
        self._runBatch(db, fileSpecs)

        self.assertEqual(self._playedAts(db), [nextYearMidnight])


class TestOverwriteGating(_OverwriteTestBase):
    def test_overwrite_bypasses_the_already_imported_hash_gate(self):
        db = self._makeDb({}, [])
        content = "file 2019"
        db.repo.markFileImported(db.user, hashlib.sha256(content.encode("utf-8")).hexdigest())
        db.repo.commit()

        fileSpecs = {
            content: ((_ts(2019, 2), _ts(2019, 11), {2019}),
                      lambda: iter([_meta("new19", _ts(2019, 4))])),
        }
        outcomes = self._runBatch(db, fileSpecs)

        self.assertEqual(outcomes, ["imported"])
        self.assertEqual(self._playedAts(db), [_ts(2019, 4)])

    def test_without_overwrite_the_hash_gate_still_skips(self):
        db = self._makeDb({}, [])
        content = "file 2019"
        db.repo.markFileImported(db.user, hashlib.sha256(content.encode("utf-8")).hexdigest())
        db.repo.commit()

        fileSpecs = {
            content: ((_ts(2019, 2), _ts(2019, 11), {2019}),
                      lambda: iter([_meta("new19", _ts(2019, 4))])),
        }
        outcomes = self._runBatch(db, fileSpecs, overwriteRange=False)

        self.assertEqual(outcomes, ["skipped"])
        self.assertEqual(self._playedAts(db), [])

    def test_unrecognized_file_aborts_before_anything_is_deleted(self):
        db = self._makeDb({}, [
            {"id": "old19", "playedAt": _ts(2019), "timePlayed": 60000},
        ])
        importer = MagicMock()

        def convertToList(content):
            if content == "good file":
                return [{}], "spotifyExtendedExport"
            return [], "None"

        importer._convertToList.side_effect = convertToList
        importer.coverage.return_value = (_ts(2019, 2), _ts(2019, 11), {2019})

        with patch("Database.database.Importer", return_value=importer):
            outcomes = db.importHistoryBatch(["good file", "corrupt file"], overwriteRange=True)

        self.assertEqual(outcomes, ["failed", "failed"])
        self.assertEqual(self._playedAts(db), [_ts(2019)])   #< nothing deleted
        importer.importHistory.assert_not_called()
        self.assertEqual(db.readProgress()["status"], "failed")

    def test_file_failure_rolls_back_delete_and_earlier_files_in_the_batch(self):
        """Atomicity: file 1 succeeds (staged, uncommitted) and file 2 raises
        mid-import - the whole transaction must roll back, so both the delete
        and file 1's staged insert vanish along with file 2's failure."""
        db = self._makeDb({}, [
            {"id": "old18", "playedAt": _ts(2018), "timePlayed": 60000},
            {"id": "old19", "playedAt": _ts(2019), "timePlayed": 60000},
        ])

        def failingGen():
            raise RuntimeError("simulated import failure")
            yield  # pragma: no cover - makes this a generator, never reached

        fileSpecs = {
            "file 2018": ((_ts(2018, 2), _ts(2018, 11), {2018}),
                          lambda: iter([_meta("new18", _ts(2018, 3))])),
            "file 2019": ((_ts(2019, 1, 5), _ts(2019, 12, 20), {2019}),
                          failingGen),
        }
        outcomes = self._runBatch(db, fileSpecs)

        self.assertEqual(outcomes, ["failed", "failed"])
        playedAts = self._playedAts(db)
        self.assertIn(_ts(2018), playedAts)          #< delete rolled back
        self.assertIn(_ts(2019), playedAts)
        self.assertNotIn(_ts(2018, 3), playedAts)    #< file 1's staged insert rolled back too
        self.assertEqual(db.readProgress()["status"], "failed")

    def test_delete_phase_failure_rolls_back_and_aborts(self):
        """A failure inside _deleteCoveredRange itself (not just a file's
        import) must also roll back cleanly - the delete is no longer
        committed on its own, so nothing survives a mid-delete exception."""
        db = self._makeDb({}, [
            {"id": "old18", "playedAt": _ts(2018), "timePlayed": 60000},
            {"id": "old19", "playedAt": _ts(2019), "timePlayed": 60000},
        ])
        fileSpecs = {
            "file 2018": ((_ts(2018, 2), _ts(2018, 11), {2018}), lambda: iter([])),
            "file 2019": ((_ts(2019, 1, 5), _ts(2019, 12, 20), {2019}), lambda: iter([])),
        }
        with patch.object(db.repo, "deleteSkipsInRange", side_effect=RuntimeError("boom")):
            outcomes = self._runBatch(db, fileSpecs)

        self.assertEqual(outcomes, ["failed", "failed"])
        playedAts = self._playedAts(db)
        self.assertIn(_ts(2018), playedAts)
        self.assertIn(_ts(2019), playedAts)
        self.assertEqual(db.readProgress()["status"], "failed")

    def test_a_wrapped_cleanup_failure_does_not_report_a_committed_import_as_failed(self):
        """repo.commit() is the point of no return: dropping the stale Wrapped
        caches and queueing cover art after it are repairable side effects, not
        part of the atomic apply. A failure there used to fall into the
        rollback handler, which told the user 'no changes were applied,
        original data is intact' about an overwrite that had durably landed -
        and returned all-failed, so AutoImporter moved the successfully
        imported files to FAILED/ and the milestone recalc flag never rose."""
        db = self._makeDb({}, [
            {"id": "old19", "playedAt": _ts(2019), "timePlayed": 60000},
        ])
        fileSpecs = {
            "file 2019": ((_ts(2019, 2), _ts(2019, 11), {2019}),
                          lambda: iter([_meta("new19", _ts(2019, 3))])),
        }
        with patch.object(db.repo, "deleteUserWrapped", side_effect=RuntimeError("boom")):
            outcomes = self._runBatch(db, fileSpecs)

        self.assertEqual(outcomes, ["imported"])
        playedAts = self._playedAts(db)
        self.assertIn(_ts(2019, 3), playedAts)    #< the committed apply stands
        self.assertNotIn(_ts(2019), playedAts)    #< and so does the delete
        self.assertEqual(db.readProgress()["status"], "complete")

    def test_an_image_queue_failure_does_not_report_a_committed_import_as_failed(self):
        """Same point-of-no-return rule for the cover-art queueing (its
        executor raises RuntimeError once shut down)."""
        db = self._makeDb({}, [])
        fileSpecs = {
            "file 2019": ((_ts(2019, 2), _ts(2019, 11), {2019}),
                          lambda: iter([_meta("new19", _ts(2019, 3))])),
        }
        with patch.object(db, "saveImagesFromTrack", side_effect=RuntimeError("shut down")):
            outcomes = self._runBatch(db, fileSpecs)

        self.assertEqual(outcomes, ["imported"])
        self.assertIn(_ts(2019, 3), self._playedAts(db))
        self.assertEqual(db.readProgress()["status"], "complete")

    def test_overwrite_clears_wrapped_for_covered_years_only(self):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        wrappedInsert = """
            INSERT INTO user_wrapped (
                username, year, calculated_at, max_played_at, total_plays, total_ms,
                longest_streak, unique_songs, unique_artists, discovered_songs, discovered_artists,
                time_series_day, time_series_week, time_series_month,
                top_songs, top_artists, top_albums,
                discovered_songs_list, discovered_artists_list, discovered_albums_list
            ) VALUES (?, ?, 0, 0, 1, 1, 1, 1, 1, 0, 0,
                      '[]', '[]', '[]', '[]', '[]', '[]', '[]', '[]', '[]')
        """
        with conn:
            conn.execute(wrappedInsert, (db.user, 2019))
            conn.execute(wrappedInsert, (db.user, 2022))

        fileSpecs = {
            "file 2019": ((_ts(2019, 2), _ts(2019, 11), {2019}), lambda: iter([])),
        }
        self._runBatch(db, fileSpecs)

        years = {r["year"] for r in db.repo._conn().execute(
            "SELECT year FROM user_wrapped WHERE username=?", (db.user,)).fetchall()}
        self.assertEqual(years, {2022})


class TestOverwriteStagesBeforeDeleting(_OverwriteTestBase):
    """Item 1 (2026-07-24 review): the network-bound metadata staging must run
    BEFORE the covered-range delete opens the write transaction, so SQLite's
    single write lock is never held across Spotify lookups (which would time
    concurrent writers - the live listener - out and lose their plays)."""

    def test_all_metadata_is_staged_before_the_delete_opens_the_transaction(self):
        db = self._makeDb({}, [{"id": "old20", "playedAt": _ts(2020), "timePlayed": 60000}])

        events = []

        def gen():
            # A generator body runs on first next(), i.e. when staging consumes
            # it - not when _mockImporter creates it. So this marks staging time.
            events.append("stage")
            yield _meta("n20", _ts(2020, 6, 2))

        fileSpecs = {"exportA": ((_ts(2020, 1), _ts(2020, 12), {2020}), gen)}

        realDelete = db.repo.deletePlaysInRange

        def recordingDelete(*args, **kwargs):
            events.append("delete")
            return realDelete(*args, **kwargs)

        db.repo.deletePlaysInRange = recordingDelete
        self._runBatch(db, fileSpecs)

        self.assertIn("stage", events)
        self.assertIn("delete", events)
        self.assertLess(
            events.index("stage"), events.index("delete"),
            "staging (network) must complete before the delete opens the write transaction",
        )


class TestOverwriteAbortsOnRetryableDrops(_OverwriteTestBase):
    """The covered range to delete is computed from a parse-only pass, but only
    the plays that survive staging get re-inserted. A play dropped in between
    (rate-limited lookup, unexpected error) would therefore be deleted and never
    replaced - permanently, silently, inside the one transaction. Retryable
    drops abort the batch before the delete; permanent ones (podcast rows that
    can never resolve) must still be allowed through, or nobody with podcasts in
    their export could ever run an overwrite import."""

    def _runBatchWithStats(self, db, fileSpecs, stats):
        """Like _runBatch, but the mocked importHistory also populates the stats
        dict the real one fills as it drops plays."""
        importer = self._mockImporter(fileSpecs)
        generators = iter([spec[1]() for spec in fileSpecs.values()])

        def importHistory(*args, stats=None, **kwargs):
            if stats is not None:
                for key, count in statsToApply.items():
                    stats[key] = stats.get(key, 0) + count
            return next(generators)

        statsToApply = stats
        importer.importHistory.side_effect = importHistory
        with patch("Database.database.Importer", return_value=importer):
            return db.importHistoryBatch(list(fileSpecs.keys()), overwriteRange=True)

    def _seededDb(self):
        return self._makeDb({}, [{"id": "old19", "playedAt": _ts(2019), "timePlayed": 60000}])

    def _fileSpecs(self):
        return {"file 2019": ((_ts(2019, 1, 5), _ts(2019, 12, 20), {2019}),
                              lambda: iter([_meta("new19", _ts(2019, 3))]))}

    def test_transient_drop_aborts_before_anything_is_deleted(self):
        db = self._seededDb()

        outcomes = self._runBatchWithStats(db, self._fileSpecs(), {"droppedTransient": 1})

        self.assertEqual(outcomes, ["failed"])
        self.assertEqual(self._playedAts(db), [_ts(2019)])   #< original play intact
        self.assertEqual(db.readProgress()["status"], "failed")

    def test_unexpected_drop_aborts_too(self):
        db = self._seededDb()

        outcomes = self._runBatchWithStats(db, self._fileSpecs(), {"droppedUnexpected": 2})

        self.assertEqual(outcomes, ["failed"])
        self.assertEqual(self._playedAts(db), [_ts(2019)])

    def test_abort_message_says_nothing_was_deleted_and_names_the_retry(self):
        db = self._seededDb()

        self._runBatchWithStats(db, self._fileSpecs(), {"droppedTransient": 3})

        message = db.readProgress()["message"]
        self.assertIn("3", message)
        self.assertIn("nothing was deleted", message)
        self.assertIn("try", message.lower())

    def test_an_unreadable_entry_aborts_before_anything_is_deleted(self):
        """The gap this guard was missing entirely. An entry that never parsed
        yields no play, but the covered range is computed from the same file and
        the OTHER entries still mark its year covered - so the delete would take
        out a play (recorded earlier by the listener, or by a previous import)
        that nothing re-inserts. It bumped no counter, so nothing noticed."""
        db = self._seededDb()

        outcomes = self._runBatchWithStats(db, self._fileSpecs(), {"droppedMalformed": 1})

        self.assertEqual(outcomes, ["failed"])
        self.assertEqual(self._playedAts(db), [_ts(2019)])   #< original play intact
        self.assertEqual(db.readProgress()["status"], "failed")

    def test_the_unreadable_entry_message_does_not_tell_the_user_to_retry(self):
        """Different advice from the retryable guard: re-running changes
        nothing, because the file itself is what could not be read."""
        db = self._seededDb()

        self._runBatchWithStats(db, self._fileSpecs(), {"droppedMalformed": 4})

        message = db.readProgress()["message"]
        self.assertIn("4", message)
        self.assertIn("nothing was deleted", message)
        self.assertNotIn("try the import again", message)

    def test_a_negative_play_time_does_not_abort(self):
        """A pre-existing deliberate filter, counted only for visibility -
        aborting on it would block exports that have always been importable."""
        db = self._seededDb()

        outcomes = self._runBatchWithStats(db, self._fileSpecs(), {"droppedNegativeTime": 3})

        self.assertEqual(outcomes, ["imported"])
        self.assertEqual(self._playedAts(db), [_ts(2019, 3)])

    def test_permanent_no_track_drops_do_not_abort(self):
        """Podcast/audiobook rows can never resolve - aborting on them would
        make overwrite import impossible for most real exports."""
        db = self._seededDb()

        outcomes = self._runBatchWithStats(db, self._fileSpecs(), {"droppedNoTrack": 2})

        self.assertEqual(outcomes, ["imported"])
        self.assertEqual(self._playedAts(db), [_ts(2019, 3)])   #< range replaced as intended

    def test_completion_message_reports_permanent_drops(self):
        """Overwrite mode routes per-file progress to a no-op, so the only place
        a drop can surface is the batch's own completion line."""
        db = self._seededDb()

        self._runBatchWithStats(db, self._fileSpecs(), {"droppedNoTrack": 2})

        message = db.readProgress()["message"]
        self.assertIn("2", message)
        self.assertIn("dropped", message.lower())

    def test_clean_batch_message_is_unchanged(self):
        db = self._seededDb()

        self._runBatchWithStats(db, self._fileSpecs(), {})

        self.assertEqual(db.readProgress()["message"], "Overwrite import complete: 1/1 files imported")

    def test_append_mode_still_imports_despite_a_transient_drop(self):
        """Append mode never deletes, so a dropped play just isn't added yet -
        a later re-import picks it up and dedups. Only overwrite must abort."""
        db = self._seededDb()
        importer = self._mockImporter(self._fileSpecs())
        generators = iter([iter([_meta("new19", _ts(2019, 3))])])

        def importHistory(*args, stats=None, **kwargs):
            if stats is not None:
                stats["droppedTransient"] = 1
            return next(generators)

        importer.importHistory.side_effect = importHistory
        with patch("Database.database.Importer", return_value=importer):
            outcomes = db.importHistoryBatch(["file 2019"], overwriteRange=False)

        self.assertEqual(outcomes, ["imported"])
        self.assertEqual(self._playedAts(db), [_ts(2019, 3), _ts(2019)])   #< ordered by played_at


if __name__ == "__main__":
    import unittest
    unittest.main()
