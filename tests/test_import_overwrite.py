"""Overwrite imports: delete the user's plays/skips in the covered-year
segments of the uploaded files' span (missing years protected), bypass the
already-imported hash gate, clear cached Wrapped for covered years - all in
ONE transaction shared with every file's import, committed once at the very
end. A failure anywhere (an unrecognized file, the delete pass itself, or any
single file's import) rolls back everything and aborts the batch, so the
overwrite either fully lands or leaves the original data untouched."""
import datetime
import hashlib
import json
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, RecordingConnection, normalizeTrackForTest
from Database.database import Database
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
        #< the progress denominator _stageImportData asks the importer for:
        #  a count of PLAYS, which is len(parsed) for every non-Musicolet
        #  format (see Importer.expectedEntryCount)
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)
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


class TestOverwriteAtTheEndOfRepresentableTime(_OverwriteTestBase):
    """A far-future played_at - a corrupt export; _plausibleStart is
    deliberately a floor with no ceiling - put the delete's year loop on year
    9999, whose segment end was computed as "the start of year 10000, less an
    epsilon". datetime refuses that year, so the import died with "year must be
    in 1..9999, not 10000": the transaction rolls back so nothing is lost, but
    the message names nothing actionable and re-running never succeeds
    (2026-09-03 review, L9).

    The span's own end is the answer there anyway - nothing inside it can be
    later - which is what the min() already picked for the last year."""

    def test_a_year_9999_span_imports_instead_of_dying_on_year_10000(self):
        db = self._makeDb({}, [
            {"id": "old9999", "playedAt": _ts(9999, 6), "timePlayed": 60000},
        ])
        fileSpecs = {
            "far future": ((_ts(9999, 2), _ts(9999, 11), {9999}),
                           lambda: iter([_meta("new9999", _ts(9999, 3))])),
        }

        outcomes = self._runBatch(db, fileSpecs)

        self.assertEqual(outcomes, ["imported"])
        playedAts = self._playedAts(db)
        self.assertNotIn(_ts(9999, 6), playedAts)   #< the covered year is still wiped
        self.assertIn(_ts(9999, 3), playedAts)      #< and re-imported

    def test_an_earlier_year_in_the_same_span_still_stops_at_its_own_boundary(self):
        """The clamp must apply to the LAST representable year only - an
        ordinary year in the same batch keeps segmenting on its own boundary,
        so a play in an uncovered year between them still survives."""
        db = self._makeDb({}, [
            {"id": "keep9998", "playedAt": _ts(9998, 6), "timePlayed": 60000},
            {"id": "wipe9999", "playedAt": _ts(9999, 6), "timePlayed": 60000},
        ])
        fileSpecs = {
            "far future": ((_ts(9998, 2), _ts(9999, 11), {9999}), lambda: iter([])),
        }

        self._runBatch(db, fileSpecs)

        playedAts = self._playedAts(db)
        self.assertIn(_ts(9998, 6), playedAts)      #< 9998 is inside the span but uncovered
        self.assertNotIn(_ts(9999, 6), playedAts)


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
        #< the progress denominator _stageImportData asks the importer for:
        #  a count of PLAYS, which is len(parsed) for every non-Musicolet
        #  format (see Importer.expectedEntryCount)
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)
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
        #< deleteUserWrappedFromYear, not deleteUserWrapped: c165ac0 moved this
        #  path onto the from-year delete, and patching the old name injected
        #  no failure at all - the test kept passing while guarding nothing
        with patch.object(db.repo, "deleteUserWrappedFromYear", side_effect=RuntimeError("boom")):
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

    _WRAPPED_INSERT = """
        INSERT INTO user_wrapped (
            username, year, calculated_at, max_played_at, total_plays, total_ms,
            longest_streak, unique_songs, unique_artists, discovered_songs, discovered_artists,
            time_series_day, time_series_week, time_series_month,
            top_songs, top_artists, top_albums,
            discovered_songs_list, discovered_artists_list, discovered_albums_list
        ) VALUES (?, ?, 0, 0, 1, 1, 1, 1, 1, 0, 0,
                  '[]', '[]', '[]', '[]', '[]', '[]', '[]', '[]', '[]')
    """

    def _dbWithCachedWrappedYears(self, *years):
        db = self._makeDb({}, [])
        conn = db.repo._conn()
        with conn:
            for year in years:
                conn.execute(self._WRAPPED_INSERT, (db.user, year))
        return db

    def _cachedYears(self, db):
        return {r["year"] for r in db.repo._conn().execute(
            "SELECT year FROM user_wrapped WHERE username=?", (db.user,)).fetchall()}

    def test_overwrite_clears_the_rewritten_year_and_every_later_one(self):
        """2022's own plays are untouched by a 2019 overwrite, and its cached
        play_count and max_played_at do not move - so _wrappedCacheNeedsRecalc
        would never rebuild it. But its DISCOVERIES can change: they are
        anchored on all-time first listens, so re-writing 2019 can move an
        artist into or out of "discovered in 2022".

        This is deliberately wider than _wrappedYearsToInvalidate, which still
        answers the narrower question it is named for - which years' PLAYS the
        delete rewrote."""
        db = self._dbWithCachedWrappedYears(2019, 2022)

        self._runBatch(db, {"file 2019": ((_ts(2019, 2), _ts(2019, 11), {2019}), lambda: iter([]))})

        self.assertEqual(self._cachedYears(db), set())

    def test_overwrite_keeps_the_years_before_the_one_it_rewrote(self):
        """The bound on the above, and the reason this is not
        deleteAllUserWrapped: a first listen can only move within or after the
        year the changed play lands in, so 2015 cannot be affected by a 2019
        overwrite - and dropping it would buy a full-year recomputation for
        nothing."""
        db = self._dbWithCachedWrappedYears(2015, 2019, 2022)

        self._runBatch(db, {"file 2019": ((_ts(2019, 2), _ts(2019, 11), {2019}), lambda: iter([]))})

        self.assertEqual(self._cachedYears(db), {2015})


class TestOverwriteStagingProgress(_OverwriteTestBase):
    """Phase 1 of an overwrite (parse + Spotify metadata fetch, the
    limiter-paced phase that dominates a big export) used to hand
    _stageImportData a no-op reporter, so the row read "File 1/1: Fetching
    metadata" at 0% from the first lookup to the last and then jumped to
    complete. The no-op exists for Phase 2 only - writeProgress self-commits,
    and nothing may self-commit while the covered-range delete and the
    staged rows sit uncommitted on the connection (the INVARIANT in
    Database/import_service.py). Staging holds no transaction, so the real
    reporter is safe there (2026-09-02 review, CORE-5).

    The second test pins that invariant structurally, with the recording
    technique test_guard_transactions uses: every import_progress write must
    run with NO transaction open, so a reporter handed to Phase 2 by mistake
    shows up as a write inside one."""

    ENTRY_COUNT = 3 * Database.PROGRESS_UPDATE_INTERVAL
    _PLAY_SPACING_SECONDS = 600   #< well past the near-time dedup tolerance

    def _fileSpecsWithEntries(self):
        metas = [_meta("tX", _ts(2019, 3) + i * self._PLAY_SPACING_SECONDS)
                 for i in range(self.ENTRY_COUNT)]
        return {"file 2019": ((_ts(2019, 1, 5), _ts(2019, 12, 20), {2019}),
                              lambda: iter(metas))}

    def _mockImporter(self, fileSpecs):
        """The base mock parses every file to ONE entry; staging counts
        progress against len(parsedHistory), so the file has to parse to as
        many entries as the generator yields."""
        importer = super()._mockImporter(fileSpecs)
        importer._convertToList.side_effect = lambda content: ([{}] * self.ENTRY_COUNT, "spotifyExtendedExport")
        return importer

    def test_the_bar_moves_per_entry_while_metadata_is_fetched(self):
        db = self._makeDb({}, [])
        with patch.object(db, "writeProgress", wraps=db.writeProgress) as progress:
            outcomes = self._runBatch(db, self._fileSpecsWithEntries())

        self.assertEqual(outcomes, ["imported"])
        messages = [call.args[3] for call in progress.call_args_list]
        fetching = next(i for i, m in enumerate(messages) if "Fetching metadata" in m)
        applying = next(i for i, m in enumerate(messages) if m.startswith("Overwrite: applying"))
        entryRows = [(call.args[1], call.args[2]) for call in progress.call_args_list[fetching + 1:applying]
                     if call.args[2] == self.ENTRY_COUNT and call.args[1] > 0]
        interval = Database.PROGRESS_UPDATE_INTERVAL
        self.assertEqual(entryRows, [(interval, self.ENTRY_COUNT), (2 * interval, self.ENTRY_COUNT),
                                     (self.ENTRY_COUNT, self.ENTRY_COUNT)])
        self.assertEqual(db.readProgress()["status"], "complete")

    def test_no_progress_write_runs_inside_the_overwrite_transaction(self):
        db = self._makeDb({}, [
            {"id": "old19", "playedAt": _ts(2019), "timePlayed": 60000},   #< something for Phase 2 to delete
        ])
        log = []
        proxy = RecordingConnection(db.repo._conn(), log)
        #< instance attribute shadowing the class method; `del` restores the
        #  class lookup exactly (the test_guard_transactions pattern)
        db.repo._conn = lambda: proxy
        try:
            outcomes = self._runBatch(db, self._fileSpecsWithEntries())
        finally:
            del db.repo._conn

        self.assertEqual(outcomes, ["imported"])
        progressWrites = [inTx for sql, inTx in log if "import_progress" in sql.lower()]
        self.assertGreater(len(progressWrites), 2, "the fixture reported no per-entry progress")
        self.assertTrue(any(inTx for _, inTx in log), "Phase 2 never opened a transaction")
        self.assertFalse(any(progressWrites), "a progress write ran inside the overwrite transaction")


class TestOverwriteAbortsOnAmbiguousMatches(_OverwriteTestBase):
    """The apply-time twin of the staging guards below: an entry whose
    near-time lookup finds SEVERAL candidate rows is skipped rather than
    guessing which to correct. Outside an overwrite that is safe - the entry
    is already recorded, nothing was deleted. Inside one it is not: the
    entry's own rows are gone with the covered range, the survivors are rows
    just outside the span, and the skip drops a play permanently under a
    'complete' banner. The batch must abort instead, exactly as it does for a
    play that never made it through staging."""

    def _dbWithTwoSurvivorsPastTheSpan(self):
        """Two same-track plays sitting just AFTER the file's span end, so the
        covered-range delete leaves them (it is bounded by the span, not the
        year) while the staged entry's own row inside the span is removed."""
        spanEnd = _ts(2019, 12, 20)
        db = self._makeDb({}, [
            {"id": "tX", "playedAt": _ts(2019, 3), "timePlayed": 60000},      #< inside the span: deleted
            {"id": "tX", "playedAt": spanEnd + 5, "timePlayed": 60000},       #< survivors, both within
            {"id": "tX", "playedAt": spanEnd + 12, "timePlayed": 60000},      #  IMPORT_MATCH_START_WINDOW_SECONDS
        ])
        return db, spanEnd

    def _fileSpecsLandingOnTheSurvivors(self, spanEnd):
        return {"file 2019": ((_ts(2019, 1, 5), spanEnd, {2019}),
                              lambda: iter([_meta("tX", spanEnd + 8)]))}

    def test_an_ambiguous_match_aborts_the_overwrite(self):
        db, spanEnd = self._dbWithTwoSurvivorsPastTheSpan()

        outcomes = self._runBatch(db, self._fileSpecsLandingOnTheSurvivors(spanEnd))

        self.assertEqual(outcomes, ["failed"])
        self.assertEqual(db.readProgress()["status"], "failed")

    def test_the_original_data_survives_the_abort(self):
        """The delete shares the batch's transaction, so the rollback has to
        put the whole covered range back - including the row the entry that
        could not be applied was meant to replace."""
        db, spanEnd = self._dbWithTwoSurvivorsPastTheSpan()

        self._runBatch(db, self._fileSpecsLandingOnTheSurvivors(spanEnd))

        self.assertEqual(self._playedAts(db), [_ts(2019, 3), spanEnd + 5, spanEnd + 12])

    def test_the_abort_message_carries_no_user_data(self):
        """Import error text is matched by substring to classify failures, so
        a track id or title interpolated into it would be classified on."""
        db, spanEnd = self._dbWithTwoSurvivorsPastTheSpan()

        self._runBatch(db, self._fileSpecsLandingOnTheSurvivors(spanEnd))

        message = db.readProgress()["message"]
        self.assertNotIn("tX", message)
        self.assertIn("original data is intact", message)

    def test_a_single_match_still_corrects_in_place(self):
        """The guard fires on ambiguity only - one candidate is still claimed
        and corrected, which is the normal overwrite path."""
        spanEnd = _ts(2019, 12, 20)
        db = self._makeDb({}, [{"id": "tX", "playedAt": spanEnd + 5, "timePlayed": 60000}])

        outcomes = self._runBatch(db, self._fileSpecsLandingOnTheSurvivors(spanEnd))

        self.assertEqual(outcomes, ["imported"])
        self.assertEqual(self._playedAts(db), [spanEnd + 8])

    def test_a_normal_import_still_skips_without_aborting(self):
        """Nothing was deleted there, so the presumption 'already recorded'
        holds and an ambiguous entry stays a skip, not a failure."""
        spanEnd = _ts(2019, 12, 20)
        db = self._makeDb({}, [
            {"id": "tX", "playedAt": spanEnd + 5, "timePlayed": 60000},
            {"id": "tX", "playedAt": spanEnd + 12, "timePlayed": 60000},
        ])

        outcomes = self._runBatch(db, self._fileSpecsLandingOnTheSurvivors(spanEnd),
                                  overwriteRange=False)

        self.assertEqual(outcomes, ["imported"])
        self.assertEqual(self._playedAts(db), [spanEnd + 5, spanEnd + 12])


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

    def _runAppendBatchCapturingMessages(self, db, importer, content):
        """One append-mode batch of `content` through `importer`, returning
        (outcomes, every progress message written). The batch's own summary
        line overwrites the per-file completion line in import_progress, so
        the completion line is only observable on the way past."""
        capturedMessages = []
        originalWriteProgress = db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedMessages.append(message)
            originalWriteProgress(status, current, total, message, error)

        db.writeProgress = captureWriteProgress
        try:
            with patch("Database.database.Importer", return_value=importer):
                outcomes = db.importHistoryBatch([content], overwriteRange=False)
        finally:
            db.writeProgress = originalWriteProgress
        return outcomes, capturedMessages

    def test_a_retryable_drop_leaves_the_file_unmarked_so_a_reimport_retries_it(self):
        """The test above and the importer's own drop comment both promise
        that "a later re-import picks it up". It could not: the apply phase
        recorded the file's content hash regardless of the drop counters, and
        the append batch answers a known hash with "skipped" before the
        importer is even built - so the recovery the log line tells the user
        to try was refused as "already imported", and the dropped plays were
        gone for good unless the user edited the file or ran an overwrite.

        The hash's job is to stop a COMPLETE import repeating. A file with
        retryable drops is not one, so it stays unmarked and re-imports in
        full; the plays that did land dedup against themselves."""
        db = self._seededDb()
        content = "file 2019"

        droppingImporter = self._mockImporter(self._fileSpecs())

        def importHistoryDroppingOne(*args, stats=None, **kwargs):
            stats["droppedTransient"] = 1   #< the lookup for a second play failed
            return iter([_meta("new19", _ts(2019, 3))])

        droppingImporter.importHistory.side_effect = importHistoryDroppingOne
        firstOutcomes, firstMessages = self._runAppendBatchCapturingMessages(db, droppingImporter, content)

        healthyImporter = self._mockImporter(self._fileSpecs())
        healthyImporter.importHistory.side_effect = lambda *args, stats=None, **kwargs: iter([
            _meta("new19", _ts(2019, 3)),       #< already landed: dedups
            _meta("retried19", _ts(2019, 5)),   #< the play the outage dropped
        ])
        secondOutcomes, _ = self._runAppendBatchCapturingMessages(db, healthyImporter, content)

        self.assertEqual(firstOutcomes, ["imported"])
        self.assertEqual(secondOutcomes, ["imported"])   #< not "skipped"
        self.assertEqual(self._playedAts(db), [_ts(2019, 3), _ts(2019, 5), _ts(2019)])
        completionLines = [m for m in firstMessages if "Import complete" in m]
        self.assertEqual(len(completionLines), 1)
        #< the drop is named, with the count, and the user is told what retries it
        self.assertIn("1 could not be looked up", completionLines[0])
        self.assertIn("re-import", completionLines[0])

    def test_a_clean_append_import_is_still_hash_marked(self):
        """The other half of the rule: no retryable drop, and the second run
        of the same file is refused as before."""
        db = self._seededDb()
        content = "file 2019"

        outcomes = None
        for _ in range(2):
            importer = self._mockImporter(self._fileSpecs())
            importer.importHistory.side_effect = lambda *args, stats=None, **kwargs: iter(
                [_meta("new19", _ts(2019, 3))])
            outcomes, _messages = self._runAppendBatchCapturingMessages(db, importer, content)

        self.assertEqual(outcomes, ["skipped"])
        self.assertEqual(self._playedAts(db), [_ts(2019, 3), _ts(2019)])

    def test_batch_final_line_also_reports_retryable_drops(self):
        """66c4a3a put the retryable-drop count on the PER-FILE completion
        line (see test_a_retryable_drop_leaves_the_file_unmarked...), but
        _importHistoryBatchLocked writes its own final "Imported N/M files"
        line after every file - see _runAppendBatchCapturingMessages's
        docstring: that line overwrites the per-file one in import_progress.
        So the count landed somewhere the /import page never renders. What
        the user's browser actually polls is db.readProgress() once the
        batch has finished, which must name the drop too."""
        db = self._seededDb()
        content = "file 2019"
        importer = self._mockImporter(self._fileSpecs())

        def importHistoryDroppingOne(*args, stats=None, **kwargs):
            stats["droppedTransient"] = 1   #< the lookup for a second play failed
            return iter([_meta("new19", _ts(2019, 3))])

        importer.importHistory.side_effect = importHistoryDroppingOne
        with patch("Database.database.Importer", return_value=importer):
            outcomes = db.importHistoryBatch([content], overwriteRange=False)

        self.assertEqual(outcomes, ["imported"])
        finalMessage = db.readProgress()["message"]
        self.assertIn("1 could not be looked up", finalMessage)
        self.assertIn("re-import", finalMessage)


class TestOverwriteAbortsOnAnUnreadableUpload(_OverwriteTestBase):
    """The same hole one layer up. Every guard in TestOverwriteAbortsOnRetryableDrops
    counts ENTRIES, and only entries that reached the batch at all: a whole FILE
    the route could not decode (routes/system.py drops a non-UTF-8 upload) never
    arrives, so the covered range is computed from the survivors and nothing here
    can see that anything is missing.

    It is not hypothetical arithmetic. Spotify splits an extended export into
    numbered part-files; upload three for one year with the MIDDLE one
    undecodable and the survivors' [minStart, maxEnd] brackets it, its year is
    covered, and its plays are deleted with nothing to put them back - under
    "Overwrite import complete: 2/2 files imported".

    The count comes in as a parameter rather than being inferred, because by the
    time the batch runs the evidence is gone: an undecodable upload leaves no
    content to count."""

    def _runBatch(self, db, fileSpecs, unreadableFileCount):
        with patch("Database.database.Importer", return_value=self._mockImporter(fileSpecs)):
            return db.importHistoryBatch(list(fileSpecs.keys()), overwriteRange=True,
                                         unreadableFileCount=unreadableFileCount)

    def _threePartYear(self):
        """2024 already imported in three parts; parts 1 and 3 are re-uploaded
        and part 2 is the one the route could not read."""
        return self._makeDb({}, [
            {"id": "p1", "playedAt": _ts(2024, 1, 10), "timePlayed": 60000},
            {"id": "p2", "playedAt": _ts(2024, 5, 10), "timePlayed": 60000},
            {"id": "p3", "playedAt": _ts(2024, 9, 10), "timePlayed": 60000},
        ])

    def _survivingSpecs(self):
        return {
            "part1": ((_ts(2024, 1, 10), _ts(2024, 1, 10), {2024}),
                      lambda: iter([_meta("p1", _ts(2024, 1, 10))])),
            "part3": ((_ts(2024, 9, 10), _ts(2024, 9, 10), {2024}),
                      lambda: iter([_meta("p3", _ts(2024, 9, 10))])),
        }

    def test_the_bracketed_play_of_a_dropped_file_is_not_deleted(self):
        db = self._threePartYear()

        outcomes = self._runBatch(db, self._survivingSpecs(), unreadableFileCount=1)

        self.assertEqual(outcomes, ["failed", "failed"])
        self.assertEqual(self._playedAts(db),
                         [_ts(2024, 1, 10), _ts(2024, 5, 10), _ts(2024, 9, 10)])
        self.assertEqual(db.readProgress()["status"], "failed")

    def test_the_abort_message_says_nothing_was_deleted_and_names_the_count(self):
        db = self._threePartYear()

        self._runBatch(db, self._survivingSpecs(), unreadableFileCount=2)

        message = db.readProgress()["message"]
        self.assertIn("2", message)
        self.assertIn("nothing was deleted", message)
        #< the same advice split the entry-level guard makes: re-running changes
        #  nothing, so it names the two things that do
        self.assertIn("UTF-8", message)
        self.assertIn("without the overwrite option", message)

    def test_it_aborts_before_the_files_are_even_parsed(self):
        """Placed ahead of _computeCoveredRange, not merely ahead of the delete.
        Nothing has been deleted either way, but the answer is already known -
        parsing every file and logging an Importer into Spotify first is work
        spent on a batch that cannot run."""
        db = self._threePartYear()
        importer = self._mockImporter(self._survivingSpecs())

        with patch("Database.database.Importer", return_value=importer):
            db.importHistoryBatch(["part1", "part3"], overwriteRange=True, unreadableFileCount=1)

        importer._convertToList.assert_not_called()

    def test_a_batch_with_nothing_dropped_still_runs(self):
        """The negative control: this guard must not be a switch that turns
        overwrite import off."""
        db = self._threePartYear()

        outcomes = self._runBatch(db, self._survivingSpecs(), unreadableFileCount=0)

        self.assertEqual(outcomes, ["imported", "imported"])
        self.assertEqual(db.readProgress()["status"], "complete")


class TestOverwriteClosesSessions(_OverwriteTestBase):
    def test_the_batch_closes_the_one_session_it_builds(self):
        """A fresh TLS login must not outlive the import that opened it.

        Staging's Importer holds one, and is closed. The coverage pre-pass
        builds its Importer WITHOUT cookies, so Spotify.__init__ never logs in
        and there is no session to release - pinned as "never closed" rather
        than simply dropped from the count, because a close() landing there
        would mean a session had been opened after all. Which importer gets
        the cookies is TestCoveredRangeNeedsNoSpotifySession's half; this is
        the release discipline, and one mock per construction is what keeps
        the two halves from being able to swap places unnoticed."""
        db = self._makeDb({}, [])

        def gen():
            yield _meta("i1", _ts(2020))

        fileSpecs = {"file1": ((_ts(2020), _ts(2020), {2020}), gen)}
        built = []

        with patch("Database.database.Importer",
                   side_effect=lambda **kwargs: built.append(self._mockImporter(fileSpecs)) or built[-1]):
            db.importHistoryBatch(list(fileSpecs.keys()), overwriteRange=True)

        self.assertEqual(len(built), 2, "one Importer per phase")
        prePass, staging = built
        self.assertEqual(prePass.sp.close.call_count, 0, "the pre-pass opened no session")
        self.assertEqual(staging.sp.close.call_count, 1, "staging's session was not released")


class TestWrappedInvalidationUsesTheUsersOwnYears(_OverwriteTestBase):
    """Two year notions meet at the Wrapped invalidation, counted in different
    timezones on purpose.

    coverage() and the delete segmentation bucket years in the INSTANCE zone, so
    the segments line up with the uploaded files' own coverage. A Wrapped year is
    defined in the USER's zone instead - _computeAvailableYears reads db.tz, which
    refreshSettings fills from that user's `timezone` setting.

    Those are the same value on an instance whose users all share its zone, which
    is why this never showed up. For a user who does not, a play near a New Year
    boundary can be covered-year N and Wrapped-year N+1, so handing covered years
    straight to a user-keyed cache drops the wrong one and leaves the right one
    stale until the wrapped worker's staleness check happens to rebuild it.

    The span is therefore re-bucketed in the user's zone and UNIONED in, never
    substituted: an extra year costs one recomputation of a cache that rebuilds on
    demand, a missing year shows wrong numbers. Nothing here touches which plays
    are deleted - that is decided and committed before this runs.
    """

    #< UTC+14, the furthest zone there is, so the boundary case is unambiguous
    #  whatever zone the test host is in
    FAR_ZONE = "Pacific/Kiritimati"

    def _dbInFarZone(self):
        from zoneinfo import ZoneInfo
        db = self._makeDb({}, [])
        db.tz = ZoneInfo(self.FAR_ZONE)
        return db

    def _dbInInstanceZone(self):
        """A user whose zone matches the instance's - the ordinary case, where
        the two year notions coincide and nothing is widened."""
        from Database.utils import getTimezone
        db = self._makeDb({}, [])
        db.tz = getTimezone()
        return db

    def test_the_users_own_year_for_the_span_is_invalidated_too(self):
        from Database.utils import convertToDatetime
        db = self._dbInFarZone()
        #< the last hours of the year in the instance zone, already next year in
        #  the user's
        edge = _ts(2019, 12, 31, 23)
        usersYear = convertToDatetime(edge, tz=db.tz).year

        years = db._wrappedYearsToInvalidate(edge, edge, {2019}, set())

        self.assertIn(usersYear, years)

    def test_the_covered_and_corrected_years_are_never_dropped(self):
        """Widening only. Substituting the user-zone years would trade one stale
        cache for another."""
        db = self._dbInFarZone()

        years = db._wrappedYearsToInvalidate(_ts(2019), _ts(2019), {2015}, {2021})

        self.assertTrue({2015, 2021}.issubset(years))

    def test_a_span_inside_one_year_adds_only_that_year(self):
        """The union is the covered years' own segments, not every year between
        the extremes of the two sets - a mid-year overwrite must not invalidate
        a decade.

        A span always arrives with the years it covers: _computeCoveredRange
        returns (None, None, set()) when the files cover nothing, so a non-None
        minStart and an empty coveredYears cannot both happen."""
        db = self._dbInInstanceZone()

        years = db._wrappedYearsToInvalidate(_ts(2019, 6), _ts(2019, 7), {2019}, set())

        self.assertEqual(years, {2019})

    def test_a_year_the_files_do_not_cover_is_left_alone(self):
        """The span's ENDS are covered, the middle is not - files for 2018 and
        2021 with nothing in between.

        _deletePlaysInCoveredRange deliberately protects those gap years: no
        file covers them, so their plays are untouched and it returns them as
        skippedYears. Invalidating their Wrapped anyway contradicts the delete
        it is supposed to be following, and costs a full-year recomputation on
        the request thread the next time someone opens that year (the cache
        miss in wrapped_builder recalculates synchronously).

        The same protected-gap case is already pinned for the delete side by
        test_missing_years_inside_the_span_are_protected.

        Deliberately NOT in the far zone: a user 14 hours ahead genuinely owns
        part of the neighbouring year, so the last moments of covered 2018 are
        their 2019 and pulling it in is correct. Matching zones isolates the gap
        from the timezone widening, which the test below covers separately."""
        db = self._dbInInstanceZone()

        years = db._wrappedYearsToInvalidate(_ts(2018, 6), _ts(2021, 6), {2018, 2021}, set())

        self.assertNotIn(2019, years)
        self.assertNotIn(2020, years)
        self.assertTrue({2018, 2021}.issubset(years))

    def test_a_covered_year_still_contributes_the_users_year_for_its_own_edges(self):
        """Narrowing to covered years must not lose the timezone widening this
        function exists for: the covered year's segment is what gets re-bucketed
        now, rather than the whole span."""
        from Database.utils import convertToDatetime
        db = self._dbInFarZone()
        #< inside covered year 2019 but already 2020 in the user's zone
        edge = _ts(2019, 12, 31, 23)
        usersYear = convertToDatetime(edge, tz=db.tz).year

        years = db._wrappedYearsToInvalidate(_ts(2019, 6), edge, {2019}, set())

        self.assertIn(usersYear, years)
        self.assertNotEqual(usersYear, 2019)  #< guards the test itself

    def test_an_empty_covered_range_keeps_the_sets_it_was_given(self):
        """minStart is None when nothing was covered; there is no span to bucket
        and the corrected years still have to be honoured."""
        db = self._dbInFarZone()

        years = db._wrappedYearsToInvalidate(None, None, {2019}, {2020})

        self.assertEqual(years, {2019, 2020})


class TestMusicoletOverwriteAbortEndToEnd(_OverwriteTestBase):
    """Through the REAL Importer, not the mocked one the rest of this file
    drives: the unreadable-row abort exists because every Musicolet row is
    anchored at the same synthetic timestamp, so the surviving rows' covered
    range always spans a dropped row's plays - an overwrite that proceeded
    would delete them with nothing left to re-insert. The expansion's counters
    and the abort guard are each unit-tested; what neither half can see is the
    JOINT - staging threading its stats dict into the Musicolet entry point -
    and severing that joint re-opens silent play deletion without failing
    either unit test. This is that pin."""

    _HEADER = "FILE_PATH,TITLE,ARTIST,ALBUM,ALBUM_ARTIST,COMPOSER,GENRE,YEAR,DURATION_MS,PLAY_COUNT"
    _GOOD_ROW = "/music/a.mp3,Good Song,Good Artist,Good Album,Good Artist,,Pop,2020,200000,2"
    _SHIFTED_ROW = "/music/b.mp3,Shifted"   #< too few columns: unreadable, and counted as such

    #< the synthetic-anchor year (MUSICOLET_SYNTHETIC_TIME_ANCHOR) every
    #  Musicolet play lands in. The seeded play below stands for pre-existing
    #  data in that year that must come through the abort untouched - it sits
    #  at noon, OUTSIDE the file's few-minute covered span, so what actually
    #  proves the abort is the "failed" outcome, the guard's own message, and
    #  that a proceeding import would have GROWN playedAts by inserting
    _ANCHOR_YEAR = 2000

    def _realImporter(self):
        """A real Importer whose one outward call - the name search - finds
        nothing, which the importer documents as the track genuinely being
        gone from Spotify: every row takes the synthetic-track path, and the
        import runs fully offline with none of the transient-drop stats that
        would trip the OTHER overwrite guard and pass this test for the wrong
        reason."""
        from Database.Importers.StreamingHistoryImporter import Importer
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.search.return_value = {"tracks": {"items": []}}
        return importer

    def _runReal(self, db, content):
        with patch("Database.database.Importer", return_value=self._realImporter()):
            return db.importHistoryBatch([content], overwriteRange=True)

    def test_an_unreadable_row_aborts_the_overwrite_before_anything_is_deleted(self):
        db = self._makeDb({}, [
            {"id": "anchored", "playedAt": _ts(self._ANCHOR_YEAR, 1), "timePlayed": 60000},
        ])
        before = self._playedAts(db)

        outcomes = self._runReal(db, "\n".join([self._HEADER, self._GOOD_ROW, self._SHIFTED_ROW]))

        self.assertEqual(outcomes, ["failed"])
        #< the UNREADABLE guard specifically - aborting via a lookup failure or
        #  a phase-2 crash would leave the data intact too, while proving
        #  nothing about the counters this test exists to pin
        self.assertIn("could not be read", db.readProgress()["message"])
        self.assertEqual(self._playedAts(db), before,
                         "the abort must land before the covered-range delete")

    def test_a_well_formed_file_still_overwrites(self):
        """The other direction: the counters run on every Musicolet import
        now, so over-counting a readable row would turn every overwrite into
        a refusal. What the covered-range delete does and does not take is the
        rest of this file's subject - this only pins that a clean file sails
        through the guard and lands."""
        db = self._makeDb({}, [])

        outcomes = self._runReal(db, "\n".join([self._HEADER, self._GOOD_ROW]))

        self.assertEqual(outcomes, ["imported"])
        self.assertEqual(db.readProgress()["message"],
                         "Overwrite import complete: 1/1 files imported")
        self.assertEqual(len(self._playedAts(db)), 2, "both expanded plays landed")


class TestCoveredRangeNeedsNoSpotifySession(DatabaseTestCase):
    """The covered-range pre-pass is pure parsing, and must stay offline.

    _computeCoveredRange only calls _convertToList and coverage, and neither
    touches Importer.sp - the client exists for the metadata lookups
    (_searchForSong/_fetchTrackMeta), which belong to the staging phase. It
    built one anyway, through _withCookiesFile: a cookies file written and
    deleted, and a full spotapi login with a TLSClient of its own, on every
    overwrite import - on top of the one staging then builds for real.

    Both halves are pinned here. Dropping the cookies from the WRONG importer
    is the regression this split can cause, and it is a quiet one: staging
    would silently stop resolving track metadata and synthesize every track
    instead, which no existing test would notice because they all replace
    Database.database.Importer wholesale."""

    _CONTENT = json.dumps([{
        "ts": "2019-06-01T12:00:00Z",
        "ms_played": 60000,
        "master_metadata_track_name": "Song",
        "master_metadata_album_artist_name": "Artist",
        #< no spotify_track_uri on purpose: that sends staging down
        #  _fetchTrackMeta's URI branch, and the name search is the one call
        #  _realImporter's stub answers (see its docstring)
    }])

    def _importerBuiltWith(self, built):
        """A real Importer per construction - so the constructor under test
        really runs - with its client swapped for the offline stub afterwards,
        recording the kwargs each one was built with."""
        from Database.Importers.StreamingHistoryImporter import Importer

        def build(**kwargs):
            built.append(kwargs)
            importer = Importer(**kwargs)
            importer.sp = MagicMock()
            importer.sp.search.return_value = {"tracks": {"items": []}}
            return importer
        return build

    def test_the_pre_pass_logs_in_to_nothing(self):
        from Database.Spotify import Spotify
        db = self._makeDb({}, [])

        with patch.object(Spotify, "login", autospec=True) as login:
            minStart, maxEnd, coveredYears = db._computeCoveredRange([self._CONTENT])

        self.assertEqual(login.call_count, 0,
                         "the parse-only pre-pass built a Spotify session")
        #< and it still parses. A pre-pass that answered None would abort every
        #  overwrite import - a far worse outcome than the login it saves
        self.assertEqual(coveredYears, {2019})
        self.assertIsNotNone(minStart)
        self.assertIsNotNone(maxEnd)

    def test_staging_still_gets_the_cookies_the_pre_pass_gave_up(self):
        """The metadata lookups are the whole reason a login exists here."""
        from Database.Spotify import Spotify
        db = self._makeDb({}, [])
        built = []

        with patch.object(Spotify, "login", autospec=True), \
             patch("Database.database.Importer",
                   side_effect=self._importerBuiltWith(built)):
            outcomes = db.importHistoryBatch([self._CONTENT], overwriteRange=True)

        self.assertEqual(outcomes, ["imported"])
        self.assertEqual(len(built), 2, "one Importer per phase")
        self.assertIsNone(built[0].get("cookiesFile"), "the pre-pass needs no session")
        self.assertIsNotNone(built[1].get("cookiesFile"), "staging looks tracks up")


if __name__ == "__main__":
    import unittest
    unittest.main()
