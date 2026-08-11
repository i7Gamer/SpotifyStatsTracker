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
        db.consumeMilestoneRecalcFlag.return_value = pending
        db.readProgress.return_value = {"status": "running" if importing else "idle"}
        return db

    def test_import_flag_runs_recalc_after_detection(self):
        dash = self._makeApp()
        db = self._db(pending=True)
        calls = []
        with patch("app.detectMilestones", side_effect=lambda *a, **k: calls.append("detect") or 0), \
             patch("app.recalculateMilestoneDates", side_effect=lambda *a, **k: calls.append("recalc") or 0) as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        self.assertEqual(calls, ["detect", "recalc"])   #< rows must exist before dates are re-derived
        # The settled post-import pass is also the only one allowed to prune
        # rows the rewritten history no longer supports.
        mockRecalc.assert_called_once_with(db.repo, "alice", db.tz, removeUnsupported=True)

    def test_recorded_crossings_run_recalc_without_flag(self):
        dash = self._makeApp()
        db = self._db(pending=False)
        with patch("app.detectMilestones", return_value=2), \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        # Organic passes re-derive dates but never delete: a tightened skip
        # threshold must not prune rows only to re-notify them later.
        mockRecalc.assert_called_once_with(db.repo, "alice", db.tz, removeUnsupported=False)

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

    def test_pending_flag_marks_crossings_seen(self):
        dash = self._makeApp()
        db = self._db(pending=True)
        with patch("app.detectMilestones", return_value=0) as mockDetect, \
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
        with patch("app.detectMilestones", return_value=3) as mockDetect, \
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
        with patch("app.detectMilestones", return_value=1) as mockDetect, \
             patch("app.recalculateMilestoneDates"):
            dash._detectMilestonesSafely(db, "alice")

        mockDetect.assert_called_once()
        self.assertFalse(mockDetect.call_args.kwargs["markSeen"])

    def test_normal_pass_does_not_mark_seen(self):
        dash = self._makeApp()
        db = self._db(pending=False, importing=False)
        with patch("app.detectMilestones", return_value=1) as mockDetect, \
             patch("app.recalculateMilestoneDates"):
            dash._detectMilestonesSafely(db, "alice")

        self.assertFalse(mockDetect.call_args.kwargs["markSeen"])   #< organic crossings still notify

    def test_toggle_off_does_not_mark_seen(self):
        # Toggle off = the whole import-hygiene behavior off: crossings
        # notify like before, flag untouched, no recalc.
        dash = self._makeApp()
        dash.repo.setMilestoneRecalcEnabled(False)
        db = self._db(pending=True, importing=True)
        with patch("app.detectMilestones", return_value=1) as mockDetect, \
             patch("app.recalculateMilestoneDates") as mockRecalc:
            dash._detectMilestonesSafely(db, "alice")

        self.assertFalse(mockDetect.call_args.kwargs["markSeen"])
        mockRecalc.assert_not_called()


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
