"""Imports for one user must serialize across entry points.

The web upload route runs importHistoryBatch on its own thread and the
AutoImporter runs it on the watchdog thread, with nothing coordinating them
(the route's "already running" progress check is check-then-act). Two
concurrent runs interleave their staged transactions and defeat the
batch-scoped duplicate reconciliation (_ImportRunState), and a double-submit
of the same file could import it twice because the file hash is only
recorded at the end of the first run.
"""
import sys
import os
import threading
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from conftest import DatabaseTestCase, normalizeTrackForTest
from _concurrency import WaiterCountingLock


def _meta(trackId, playedAt):
    track = normalizeTrackForTest({"id": trackId, "name": f"Song {trackId}", "artists": []})
    track["playedAt"] = playedAt
    track["timePlayed"] = 60000   #< a full listen (> the 5s skip floor) -> real play
    track["playedFrom"] = None
    return track


def _importerFactory(metasByCall):
    """Each Importer() construction pops the next scripted item list and
    yields it from importHistory."""
    def factory(**kwargs):
        importer = MagicMock()
        metas = metasByCall.pop(0)
        importer._convertToList.return_value = ([{}] * len(metas), "spotifyAcountExport")
        #< the progress denominator _stageImportData asks the importer for:
        #  a count of PLAYS, which is len(parsed) for every non-Musicolet
        #  format (see Importer.expectedEntryCount)
        importer.expectedEntryCount.side_effect = lambda parsed, exportType: len(parsed)
        importer.importHistory.return_value = iter(metas)
        return importer
    return factory


class TestImportSerialization(DatabaseTestCase):
    def test_concurrent_batches_never_run_an_import_simultaneously(self):
        """The first thread inside the import does not move until the second
        has provably reached the lock.

        It used to sleep 50ms there instead, on the reasoning that the second
        thread would surely have arrived by then. Under load it may not have -
        and then the two never overlap, nothing is proven, and the test passes
        while reporting nothing. Waiting on the arrival itself is both exact
        and faster: on an idle machine the second thread is there in
        microseconds (see tests/_concurrency.py)."""
        db = self._makeDb({}, [])
        importLock = WaiterCountingLock(db._importLock)
        db._importLock = importLock
        state = {"active": 0, "peak": 0, "entered": 0, "bothArrived": None}
        stateLock = threading.Lock()
        originalImportHistory = db.importHistory

        def instrumented(*args, **kwargs):
            with stateLock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
                isFirstIn = state["entered"] == 0
                state["entered"] += 1
            try:
                if isFirstIn:
                    # Only the first: hold here until BOTH batches are at the
                    # lock - this one, and the other blocked behind it. If
                    # serialization is broken the other is not blocked, it is
                    # in here too, and `peak` says so. The second import must
                    # NOT wait, or it would sit here until the timeout with
                    # nothing left to arrive.
                    state["bothArrived"] = importLock.waitFor(2)
                return originalImportHistory(*args, **kwargs)
            finally:
                with stateLock:
                    state["active"] -= 1

        db.importHistory = instrumented

        def runBatch(content):
            try:
                db.importHistoryBatch([content])
            finally:
                # Connections are thread-local; close THIS worker's before it
                # exits, or the still-open handle keeps the temp DB file
                # locked on Windows past the test's cleanup.
                db.repo.connectionManager.close()

        metasByCall = [[_meta("a1", 100)], [_meta("b1", 200)]]
        with patch("Database.database.Importer", side_effect=_importerFactory(metasByCall)):
            threads = [
                threading.Thread(target=runBatch, args=("export A",)),
                threading.Thread(target=runBatch, args=("export B",)),
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        #< without this the peak assertion below is vacuous: it would also hold
        #  for two batches that simply never met
        self.assertTrue(state["bothArrived"],
                        "the second batch never reached the import lock - nothing was contended")
        self.assertEqual(state["peak"], 1, "two imports for the same user ran concurrently")
        recordedIds = {e["id"] for e in db.getEntriesFromOld(fullPagination=False)}
        self.assertEqual(recordedIds, {"a1", "b1"})

    def test_double_submit_of_the_same_file_imports_it_once(self):
        """Both submissions pass the route's 'already running' check; with
        runs serialized, the second sees the first's recorded file hash and
        skips instead of importing the same plays again."""
        db = self._makeDb({}, [])

        # At most one construction happens per run that actually imports; the
        # second scripted entry only exists in case serialization is broken.
        metasByCall = [[_meta("dup1", 100)], [_meta("dup1", 100)]]
        outcomes = []
        outcomesLock = threading.Lock()

        def runBatch():
            try:
                result = db.importHistoryBatch(["same export content"])
                with outcomesLock:
                    outcomes.extend(result)
            finally:
                db.repo.connectionManager.close()   #< see the comment in the test above

        with patch("Database.database.Importer", side_effect=_importerFactory(metasByCall)):
            threads = [threading.Thread(target=runBatch) for _ in range(2)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        self.assertEqual(sorted(outcomes), ["imported", "skipped"])
        self.assertEqual(db.getEntriesCount(), 1)


if __name__ == "__main__":
    unittest.main()
