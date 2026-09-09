"""Automatic milestone-date recalculation after imports.

An import rewrites play history, so milestone rows recorded afterwards (and
dates derived earlier) go stale - migrate1_35_0 fixed the backlog once, this
keeps the "dates are data-derived" invariant standing. importHistoryBatch
raises an in-memory per-user flag; the periodic milestone pass
(_detectMilestonesSafely) consumes it before detection, then re-derives every
date after newly crossed rows are recorded via recalculateMilestoneDates. A pass
that recorded rows triggers the same re-derivation even without the flag
(organic crossings get exact timestamps, and it self-heals a flag lost to a
restart). Everything is gated by the instance-wide admin toggle
(milestone_recalc_enabled) on top of the milestones kill switch.

The same toggle also suppresses the badge flood a big import would cause:
crossings surfaced by imported history are recorded as already seen
(detectMilestones' markSeen - same no-notification contract as first-pass
seeding). While an import is still running the whole pass is skipped (its
outcome would be redone by the settled flag-consuming pass anyway), and that
settled pass alone may prune rows a shrinking overwrite import's rewritten
history no longer supports (removeUnsupported) - organic passes never delete,
so a tightened skip threshold can't cause delete/re-notify churn.

The recalculation logic itself is covered by test_milestone_recalc.py and
markSeen's record-level behavior by test_milestones.py; this file covers the
trigger wiring on both ends.
"""
import os
import sys
import datetime
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from conftest import DatabaseTestCase, normalizeTrackForTest
from Database.database import Database
from services.milestones import MILESTONE_PLAYS_THRESHOLDS

RETRY_FAILURE_COUNT = 2
RETRY_BASELINE_TS = 1000.0
PARTIAL_RECALC_ROW_COUNT = 2


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
        #< the progress denominator _stageImportData asks the importer for:
        #  a count of PLAYS, which is len(parsed) for every non-Musicolet
        #  format (see Importer.expectedEntryCount)
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)
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

    def test_a_failed_file_does_not_report_the_batch_as_settled(self):
        """The milestone pass skips while an import is "running", and that
        status is the ONLY thing holding it off - the flag it consumes is not
        raised until the whole batch is done. A per-file failure wrote the
        TERMINAL status 'failed' while the batch went on to the next file, and
        that file's status stays 'failed' until a fresh Importer has finished
        a live Spotify login (seconds). A pass landing in that window sees a
        settled import with no flag raised, so it records every threshold the
        already-imported files crossed as UNSEEN - years-old achievements
        arriving as new notifications, which markSeen exists to prevent, and
        which no later pass repairs (recalculateMilestoneDates never touches
        seen flags).

        Asserted from inside the next file, which is exactly where the window
        is."""
        db = self._makeDb({}, [])
        seen = []

        def importHistory(content, **kwargs):
            seen.append(db.readProgress().get("status"))
            if len(seen) == 1:
                #< what the real importHistory does on a per-file failure: the
                #  terminal write (import_service.py:204/565) and then raise
                db.writeProgress("failed", 1, 2, "Import failed: corrupt export", error=True)
                raise RuntimeError("corrupt export")

        with patch.object(type(db), "importHistory", side_effect=importHistory,
                          autospec=False, create=True):
            db.importHistoryBatch(["file-one", "file-two"])

        #< the second file's view of the world while the batch is still going
        self.assertEqual(seen[1], "running")

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


def _seenRows(count):
    """`count` detection rows recorded as already seen - the shape
    detectMilestonesDetailed returns, minus anything that would queue an
    email (these tests pin the recalc wiring, not the notification)."""
    return [{"kind": "plays", "threshold": index, "detail": None,
             "achieved_at": 0.0, "seen": 1} for index in range(count)]


class TestAutoRecalcWiring(AppTestCase):
    """_detectMilestonesSafely: detect first (so import-crossed rows exist),
    then re-derive dates when the import flag was raised or the pass recorded
    rows - gated by the admin toggle, which must also leave an unconsumed flag
    in place so enabling later still catches up.

    The same toggle suppresses the badge flood: crossings surfaced by an
    import are recorded as already seen (markSeen) on the flag-consuming pass
    after the batch. Passes landing mid-import (the loop runs every 5
    minutes, large imports span that - readProgress is the signal) skip
    milestone work entirely: the settled pass redoes it all anyway."""

    def _db(self, pending=False, importing=False):
        db = MagicMock()
        db.tz = datetime.timezone.utc
        # Real locked, one-shot flag behavior: a fixed mock return value would
        # hide work lost between the failing pass and its next retry.
        db.milestonesRecalcPending = pending
        db._milestone_flag_lock = threading.Lock()
        db.consumeMilestoneRecalcFlag.side_effect = lambda: Database.consumeMilestoneRecalcFlag(db)
        db.raiseMilestoneRecalcFlag.side_effect = lambda: Database.raiseMilestoneRecalcFlag(db)
        db.readProgress.return_value = {"status": "running" if importing else "idle"}
        return db

    def test_import_flag_runs_recalc_after_detection(self):
        dash = self._makeApp()
        db = self._db(pending=True)
        calls = []
        with patch("app.detectMilestonesDetailed", side_effect=lambda *a, **k: calls.append("detect") or []), \
             patch("app.recalculateMilestoneDates", side_effect=lambda *a, **k: calls.append("recalc") or 0) as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        self.assertEqual(calls, ["detect", "recalc"])   #< rows must exist before dates are re-derived
        # The settled post-import pass is also the only one allowed to prune
        # rows the rewritten history no longer supports.
        mockRecalc.assert_called_once_with(db.repo, "alice", db.tz, removeUnsupported=True)

    def test_recorded_crossings_run_recalc_without_flag(self):
        dash = self._makeApp()
        db = self._db(pending=False)
        with patch("app.detectMilestonesDetailed", return_value=_seenRows(2)), \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        # Organic passes re-derive dates but never delete: a tightened skip
        # threshold must not prune rows only to re-notify them later.
        mockRecalc.assert_called_once_with(db.repo, "alice", db.tz, removeUnsupported=False)

    def test_quiet_pass_skips_recalc(self):
        dash = self._makeApp()
        db = self._db(pending=False)
        with patch("app.detectMilestonesDetailed", return_value=_seenRows(0)), \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        mockRecalc.assert_not_called()

    def test_toggle_off_skips_recalc_and_keeps_the_flag(self):
        dash = self._makeApp()
        dash.repo.setMilestoneRecalcEnabled(False)
        db = self._db(pending=True)
        with patch("app.detectMilestonesDetailed", return_value=_seenRows(2)), \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        mockRecalc.assert_not_called()
        db.consumeMilestoneRecalcFlag.assert_not_called()   #< enabling later still catches up

    def test_kill_switch_skips_detection_and_recalc(self):
        dash = self._makeApp()
        dash.repo.setMilestonesEnabled(False)
        db = self._db(pending=True)
        with patch("app.detectMilestonesDetailed", return_value=[]) as mockDetect, \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        mockDetect.assert_not_called()
        mockRecalc.assert_not_called()

    def test_recalc_failure_does_not_stall_the_loop(self):
        dash = self._makeApp()
        db = self._db(pending=True)
        with patch("app.detectMilestonesDetailed", return_value=_seenRows(0)), \
             patch("app.recalculateMilestoneDates", side_effect=RuntimeError("boom")):
            dash._detectMilestonesSafely(db, "alice")   #< must not raise

    def test_pending_flag_marks_crossings_seen(self):
        dash = self._makeApp()
        db = self._db(pending=True)
        with patch("app.detectMilestonesDetailed", return_value=_seenRows(0)) as mockDetect, \
             patch("app.recalculateMilestoneDates"):
            dash._detectMilestonesSafely(db, "alice")

        self.assertTrue(mockDetect.call_args.kwargs["markSeen"])

    def test_running_import_skips_the_whole_pass(self):
        # Mid-import, every milestone outcome would be redone by the settled
        # pass anyway (rows land seen=1 and get re-dated) while the detection
        # queries compete with the import's writes - so nothing runs at all,
        # and the end-of-batch flag keeps its one shot for settled data.
        dash = self._makeApp()
        db = self._db(pending=False, importing=True)
        with patch("app.detectMilestonesDetailed", return_value=_seenRows(3)) as mockDetect, \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        mockDetect.assert_not_called()
        mockRecalc.assert_not_called()
        db.consumeMilestoneRecalcFlag.assert_not_called()

    def test_toggle_off_keeps_detection_running_mid_import(self):
        # The hygiene toggle off = pre-1.36.0 behavior wholesale, including
        # detection during an import (crossings notify as they always did).
        dash = self._makeApp()
        dash.repo.setMilestoneRecalcEnabled(False)
        db = self._db(pending=False, importing=True)
        with patch("app.detectMilestonesDetailed", return_value=_seenRows(1)) as mockDetect, \
             patch("app.recalculateMilestoneDates"):
            dash._detectMilestonesSafely(db, "alice")

        mockDetect.assert_called_once()
        self.assertFalse(mockDetect.call_args.kwargs["markSeen"])

    def test_normal_pass_does_not_mark_seen(self):
        dash = self._makeApp()
        db = self._db(pending=False, importing=False)
        with patch("app.detectMilestonesDetailed", return_value=_seenRows(1)) as mockDetect, \
             patch("app.recalculateMilestoneDates"):
            dash._detectMilestonesSafely(db, "alice")

        self.assertFalse(mockDetect.call_args.kwargs["markSeen"])   #< organic crossings still notify

    def test_toggle_off_does_not_mark_seen(self):
        # Toggle off = the whole import-hygiene behavior off: crossings
        # notify like before, flag untouched, no recalc.
        dash = self._makeApp()
        dash.repo.setMilestoneRecalcEnabled(False)
        db = self._db(pending=True, importing=True)
        with patch("app.detectMilestonesDetailed", return_value=_seenRows(1)) as mockDetect, \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        self.assertFalse(mockDetect.call_args.kwargs["markSeen"])
        mockRecalc.assert_not_called()

    def test_detection_failure_retries_until_a_quiet_successful_pass(self):
        dash = self._makeApp()
        db = self._db(pending=True)
        outcomes = [RuntimeError("detection failed") for _ in range(RETRY_FAILURE_COUNT)] + [[]]
        with patch("app.detectMilestonesDetailed", side_effect=outcomes) as detect, \
             patch("app.recalculateMilestoneDates") as recalc, \
             patch("app.queue_email_notification") as queue:
            for _ in range(RETRY_FAILURE_COUNT):
                dash._detectMilestonesSafely(db, "alice")
                self.assertTrue(db.milestonesRecalcPending)
                recalc.assert_not_called()
            dash._detectMilestonesSafely(db, "alice")

        self.assertTrue(all(call.kwargs["markSeen"] for call in detect.call_args_list))
        recalc.assert_called_once_with(db.repo, "alice", db.tz, removeUnsupported=True)
        self.assertFalse(db.milestonesRecalcPending)
        queue.assert_not_called()

    def test_partial_recalculation_finishes_despite_detection_change_cache(self):
        dash = self._makeApp()
        db = self._db(pending=True)
        db.repo = dash.repo
        db.repo.upsertUser("alice", "alice@example.com")
        db.repo.setMilestoneBaselineAt("alice", RETRY_BASELINE_TS)
        # An overwrite left no plays supporting these previously earned rows.
        for threshold in MILESTONE_PLAYS_THRESHOLDS[:PARTIAL_RECALC_ROW_COUNT]:
            db.repo.recordMilestone("alice", "plays", threshold, None, RETRY_BASELINE_TS, True)
        db.getPlayTotals.return_value = (0, 0)
        db.getCurrentStreak.return_value = {"days": 0}
        db.getTopArtists.return_value = []
        delete = db.repo.deleteMilestone
        with patch.object(db.repo, "deleteMilestone") as deleting:
            # Apply the first deletion for real, then fail on the next row.
            def partiallyDelete(rowId):
                if deleting.call_count == PARTIAL_RECALC_ROW_COUNT:
                    raise RuntimeError("delete failed")
                delete(rowId)
            deleting.side_effect = partiallyDelete
            dash._detectMilestonesSafely(db, "alice")

        self.assertTrue(db.milestonesRecalcPending)
        self.assertEqual(len(db.repo.getMilestonesForUser("alice")), 1)
        self.assertEqual(dash._milestoneChangeCache["alice"], (0, 0))
        with patch("app.queue_email_notification") as queue:
            dash._detectMilestonesSafely(db, "alice")
        self.assertEqual(db.repo.getMilestonesForUser("alice"), [])
        self.assertFalse(db.milestonesRecalcPending)
        db.getCurrentStreak.assert_called_once()  # retry used the cached detection fast path
        queue.assert_not_called()

    def test_organic_failure_does_not_authorize_import_pruning(self):
        dash = self._makeApp()
        for failingTarget in ("app.detectMilestonesDetailed", "app.recalculateMilestoneDates"):
            with self.subTest(target=failingTarget):
                db = self._db()
                with patch("app.detectMilestonesDetailed", return_value=_seenRows(1)), \
                     patch("app.recalculateMilestoneDates"), \
                     patch(failingTarget, side_effect=RuntimeError("organic failure")):
                    dash._detectMilestonesSafely(db, "alice")
                self.assertFalse(db.milestonesRecalcPending)
                db.raiseMilestoneRecalcFlag.assert_not_called()
                with patch("app.detectMilestonesDetailed", return_value=[]), \
                     patch("app.recalculateMilestoneDates") as recalc:
                    dash._detectMilestonesSafely(db, "alice")
                recalc.assert_not_called()

    def test_errors_before_consumption_preserve_the_original_error_and_flag(self):
        dash = self._makeApp()
        for pending in (False, True):
            for stage in ("settings", "progress", "consume"):
                with self.subTest(stage=stage, pending=pending):
                    db = self._db(pending=pending)
                    target, method = {
                        "settings": (dash.repo, "isMilestoneRecalcEnabled"),
                        "progress": (db, "readProgress"),
                        "consume": (db, "consumeMilestoneRecalcFlag"),
                    }[stage]
                    error = RuntimeError(f"{stage} failed")
                    with patch.object(target, method, side_effect=error), \
                         patch("app.detectMilestonesDetailed") as detect, \
                         patch("app.recalculateMilestoneDates") as recalc, \
                         patch("app.logger.warning") as warning:
                        dash._detectMilestonesSafely(db, "alice")
                    self.assertIs(warning.call_args.args[-1], error)
                    self.assertEqual(db.milestonesRecalcPending, pending)
                    db.raiseMilestoneRecalcFlag.assert_not_called()
                    detect.assert_not_called()
                    recalc.assert_not_called()

    def test_success_preserves_a_concurrent_import_raise(self):
        dash = self._makeApp()
        for pending in (False, True):
            with self.subTest(pending=pending):
                db = self._db(pending=pending)
                def anotherImport(*args, **kwargs):
                    db.raiseMilestoneRecalcFlag()
                with patch("app.detectMilestonesDetailed", return_value=_seenRows(1)), \
                     patch("app.recalculateMilestoneDates", side_effect=anotherImport):
                    dash._detectMilestonesSafely(db, "alice")
                self.assertTrue(db.consumeMilestoneRecalcFlag())
                self.assertFalse(db.consumeMilestoneRecalcFlag())

    def test_later_notification_error_does_not_rearm_completed_recalculation(self):
        dash = self._makeApp()
        for pending in (False, True):
            with self.subTest(pending=pending):
                db = self._db(pending=pending)
                # Import detection normally yields seen rows; deliberately
                # exercise the later-error boundary with an unseen result too.
                unseen = [{**_seenRows(1)[0], "seen": False}]
                with patch("app.detectMilestonesDetailed", return_value=unseen), \
                     patch("app.recalculateMilestoneDates") as recalc, \
                     patch("app.queue_email_notification", side_effect=RuntimeError("queue failed")) as queue:
                    dash._detectMilestonesSafely(db, "alice")
                recalc.assert_called_once_with(db.repo, "alice", db.tz, removeUnsupported=pending)
                queue.assert_called_once()
                db.raiseMilestoneRecalcFlag.assert_not_called()
                self.assertFalse(db.milestonesRecalcPending)

    def test_mid_pass_toggle_off_preserves_work_until_reenabled(self):
        dash = self._makeApp()
        for fails in (False, True):
            with self.subTest(fails=fails):
                dash.repo.setMilestoneRecalcEnabled(True)
                db = self._db(pending=True)
                def toggleOff(*args, **kwargs):
                    dash.repo.setMilestoneRecalcEnabled(False)
                    if fails:
                        raise RuntimeError("recalc failed after disabling")
                with patch("app.detectMilestonesDetailed", return_value=[]), \
                     patch("app.recalculateMilestoneDates", side_effect=toggleOff) as recalc:
                    dash._detectMilestonesSafely(db, "alice")
                recalc.assert_called_once_with(db.repo, "alice", db.tz, removeUnsupported=True)
                self.assertEqual(db.milestonesRecalcPending, fails)
                with patch("app.detectMilestonesDetailed", return_value=[]), \
                     patch("app.recalculateMilestoneDates") as retry:
                    dash._detectMilestonesSafely(db, "alice")
                    retry.assert_not_called()
                    self.assertEqual(db.milestonesRecalcPending, fails)
                    dash.repo.setMilestoneRecalcEnabled(True)
                    dash._detectMilestonesSafely(db, "alice")
                if fails:
                    retry.assert_called_once_with(db.repo, "alice", db.tz, removeUnsupported=True)
                else:
                    retry.assert_not_called()
                self.assertFalse(db.milestonesRecalcPending)


class TestRecalcFlagIsAtomic(DatabaseTestCase):
    """The read and the clear used to be two separate statements, so an import
    raising the flag in between had its raise erased - and that import's pruning
    of milestones its rewritten history no longer supports never ran."""

    def test_a_raise_during_consume_is_not_swallowed(self):
        import threading

        db = self._makeDb({}, [])
        db.raiseMilestoneRecalcFlag()
        started = threading.Event()
        release = threading.Event()

        # Stand in for the window between the read and the clear: a concurrent
        # import lands its raise while the consumer holds the lock.
        original = db._milestone_flag_lock

        class SlowLock:
            def __enter__(self):
                original.acquire()
                started.set()
                release.wait(5)
                return self

            def __exit__(self, *exc):
                original.release()
                return False

        db._milestone_flag_lock = SlowLock()
        result = {}

        def consume():
            result["pending"] = db.consumeMilestoneRecalcFlag()

        consumer = threading.Thread(target=consume)
        consumer.start()
        self.assertTrue(started.wait(5))

        raiser = threading.Thread(target=db.raiseMilestoneRecalcFlag)
        raiser.start()
        #< deliberately NOT `raiser.join(0.2); assertTrue(raiser.is_alive())`:
        #  that cannot tell a thread blocked on the lock from one the OS has not
        #  scheduled yet, so it asserted a timing symptom and would pass for the
        #  wrong reason under a loaded parallel suite. 346ae68 removed the same
        #  pattern from two other files. The two outcome assertions below, plus
        #  started.wait(5) above, already pin the atomicity: the raise cannot be
        #  observed until the consume completes.

        release.set()
        consumer.join(timeout=5)
        raiser.join(timeout=5)

        self.assertTrue(result["pending"])                     #< the first import's flag was seen
        self.assertTrue(db.milestonesRecalcPending)            #< and the second's survived

    def test_consume_is_still_one_shot(self):
        db = self._makeDb({}, [])
        db.raiseMilestoneRecalcFlag()

        self.assertTrue(db.consumeMilestoneRecalcFlag())
        self.assertFalse(db.consumeMilestoneRecalcFlag())


if __name__ == "__main__":
    unittest.main()
