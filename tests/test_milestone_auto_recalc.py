"""Automatic milestone-date recalculation after imports.

An import rewrites play history, so milestone rows recorded afterwards (and
dates derived earlier) go stale - migrate1_35_0 fixed the backlog once, this
keeps the "dates are data-derived" invariant standing. importHistoryBatch
raises an in-memory per-user flag; the periodic milestone pass
(_detectMilestonesSafely) consumes it AFTER detection has recorded any newly
crossed rows and re-derives every date via recalculateMilestoneDates. A pass
that recorded rows triggers the same re-derivation even without the flag
(organic crossings get exact timestamps, and it self-heals a flag lost to a
restart). Everything is gated by the instance-wide admin toggle
(milestone_recalc_enabled) on top of the milestones kill switch.

The recalculation logic itself is covered by test_milestone_recalc.py; this
file covers the trigger wiring on both ends.
"""
import os
import sys
import datetime
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from conftest import DatabaseTestCase, normalizeTrackForTest


def _meta(trackId, playedAt, timePlayed=60000):
    track = normalizeTrackForTest({"id": trackId, "name": f"Song {trackId}", "artists": []})
    track["playedAt"] = playedAt
    track["timePlayed"] = timePlayed
    track["playedFrom"] = None
    track["isSkip"] = False
    return track


class TestImportRaisesRecalcFlag(DatabaseTestCase):
    """importHistoryBatch raises the flag only when a file actually imported -
    all-skipped and all-failed batches change no play data, so there's nothing
    to re-derive. The overwrite branch merges into the same outcome check."""

    def _mockImporter(self, generatorFactory):
        importer = MagicMock()
        importer._convertToList.return_value = ([{}], "spotifyAcountExport")
        importer.importHistory.return_value = generatorFactory()
        return importer

    def _importBatch(self, db, contents):
        def gen():
            yield _meta("track_x", 1000)
        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            return db.importHistoryBatch(contents)

    def test_flag_starts_lowered(self):
        db = self._makeDb({}, [])
        self.assertFalse(db.consumeMilestoneRecalcFlag())

    def test_successful_batch_raises_flag_and_consume_is_one_shot(self):
        db = self._makeDb({}, [])
        outcomes = self._importBatch(db, ["raw export"])

        self.assertEqual(outcomes, ["imported"])
        self.assertTrue(db.consumeMilestoneRecalcFlag())
        self.assertFalse(db.consumeMilestoneRecalcFlag())   #< consumed

    def test_skipped_only_batch_leaves_flag_lowered(self):
        db = self._makeDb({}, [])
        self._importBatch(db, ["raw export"])
        db.consumeMilestoneRecalcFlag()   #< clear the first import's flag

        outcomes = self._importBatch(db, ["raw export"])   #< same hash - skipped

        self.assertEqual(outcomes, ["skipped"])
        self.assertFalse(db.consumeMilestoneRecalcFlag())

    def test_failed_batch_leaves_flag_lowered(self):
        db = self._makeDb({}, [])
        with patch("Database.database.Importer", side_effect=RuntimeError("boom")):
            outcomes = db.importHistoryBatch(["raw export"])

        self.assertEqual(outcomes, ["failed"])
        self.assertFalse(db.consumeMilestoneRecalcFlag())

    def test_partial_batch_still_raises_flag(self):
        # One good file among failures did change history - recalc is due.
        db = self._makeDb({}, [])

        def gen():
            yield _meta("track_x", 1000)
        good = self._mockImporter(gen)
        with patch("Database.database.Importer", side_effect=[RuntimeError("boom"), good]):
            outcomes = db.importHistoryBatch(["bad file", "good file"])

        self.assertEqual(outcomes, ["failed", "imported"])
        self.assertTrue(db.consumeMilestoneRecalcFlag())


class TestAutoRecalcWiring(AppTestCase):
    """_detectMilestonesSafely: detect first (so import-crossed rows exist),
    then re-derive dates when the import flag was raised or the pass recorded
    rows - gated by the admin toggle, which must also leave an unconsumed flag
    in place so enabling later still catches up."""

    def _db(self, pending=False):
        db = MagicMock()
        db.tz = datetime.timezone.utc
        db.consumeMilestoneRecalcFlag.return_value = pending
        return db

    def test_import_flag_runs_recalc_after_detection(self):
        dash = self._makeApp()
        db = self._db(pending=True)
        calls = []
        with patch("app.detectMilestones", side_effect=lambda *a, **k: calls.append("detect") or 0), \
             patch("app.recalculateMilestoneDates", side_effect=lambda *a, **k: calls.append("recalc") or 0) as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        self.assertEqual(calls, ["detect", "recalc"])   #< rows must exist before dates are re-derived
        mockRecalc.assert_called_once_with(db.repo, "alice", db.tz)

    def test_recorded_crossings_run_recalc_without_flag(self):
        dash = self._makeApp()
        db = self._db(pending=False)
        with patch("app.detectMilestones", return_value=2), \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        mockRecalc.assert_called_once()

    def test_quiet_pass_skips_recalc(self):
        dash = self._makeApp()
        db = self._db(pending=False)
        with patch("app.detectMilestones", return_value=0), \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        mockRecalc.assert_not_called()

    def test_toggle_off_skips_recalc_and_keeps_the_flag(self):
        dash = self._makeApp()
        dash.repo.setMilestoneRecalcEnabled(False)
        db = self._db(pending=True)
        with patch("app.detectMilestones", return_value=2), \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        mockRecalc.assert_not_called()
        db.consumeMilestoneRecalcFlag.assert_not_called()   #< enabling later still catches up

    def test_kill_switch_skips_detection_and_recalc(self):
        dash = self._makeApp()
        dash.repo.setMilestonesEnabled(False)
        db = self._db(pending=True)
        with patch("app.detectMilestones") as mockDetect, \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        mockDetect.assert_not_called()
        mockRecalc.assert_not_called()

    def test_recalc_failure_does_not_stall_the_loop(self):
        dash = self._makeApp()
        db = self._db(pending=True)
        with patch("app.detectMilestones", return_value=0), \
             patch("app.recalculateMilestoneDates", side_effect=RuntimeError("boom")):
            dash._detectMilestonesSafely(db, "alice")   #< must not raise


if __name__ == "__main__":
    unittest.main()
