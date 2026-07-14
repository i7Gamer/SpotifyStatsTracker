import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest


def _meta(trackId, playedAt):
    """A full importer-yielded item: entry fields + enough track metadata to
    satisfy Repository.upsertTrack (mirrors what Client.formatTrack produces)."""
    track = normalizeTrackForTest({"id": trackId, "name": f"Song {trackId}", "artists": []})
    track["playedAt"] = playedAt
    track["timePlayed"] = 1000
    track["playedFrom"] = None
    return track


class TestImportHistoryCommit(DatabaseTestCase):
    """importHistory must commit atomically: a mid-import failure may not leave
    half-imported entries behind, and a successful import may not drop entries
    the listener recorded meanwhile."""

    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [
            {"id": "e1", "playedAt": 100, "timePlayed": 1000},
            {"id": "e2", "playedAt": 300, "timePlayed": 1000},
        ])

    def _mockImporter(self, generatorFactory, parsedCount=2):
        importer = MagicMock()
        importer._convertToList.return_value = ([{}] * parsedCount, "spotifyAcountExport")
        importer.importHistory.return_value = generatorFactory()
        return importer

    def _playedAts(self):
        return [e["playedAt"] for e in self.db.getEntriesFromOld(fullPagination=False)]

    def test_successful_import_merges_and_sorts(self):
        def gen():
            yield _meta("i1", 200)
            yield _meta("i2", 50)   #< out of order on purpose

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistory("raw export")

        self.assertEqual(self._playedAts(), [50, 100, 200, 300])
        self.assertIsNotNone(self.db.repo.getTrack("i1"))
        self.assertIsNotNone(self.db.repo.getTrack("i2"))
        self.assertEqual(self.db.readProgress()["status"], "complete")

    def test_failed_import_leaves_database_untouched(self):
        def gen():
            yield _meta("i1", 200)
            raise RuntimeError("network died mid-import")

        entriesBefore = self._playedAts()

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            with self.assertRaises(RuntimeError):
                self.db.importHistory("raw export")

        self.assertEqual(self._playedAts(), entriesBefore)
        self.assertIsNone(self.db.repo.getTrack("i1"), "a failed import must not persist anything")
        self.assertEqual(self.db.readProgress()["status"], "failed")

    def test_listener_entries_recorded_during_import_are_kept(self):
        # Seed the track the "listener" play references, same as a real concurrent
        # appendMetadata() call would (it always upserts the track before the play).
        self.db.repo.upsertTrack(normalizeTrackForTest({"id": "L1", "name": "Live Song", "artists": []}))
        self.db.repo.commit()

        def gen():
            yield _meta("i1", 200)
            # Simulate the listener recording a play while the import is running.
            self.db.appendEntries({"id": "L1", "playedAt": 250, "timePlayed": 1000})
            yield _meta("i2", 50)

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistory("raw export")

        ids = [e["id"] for e in self.db.getEntriesFromOld(fullPagination=False)]
        self.assertIn("L1", ids)
        self.assertEqual(self._playedAts(), [50, 100, 200, 250, 300])

    def test_unrecognized_export_is_a_noop(self):
        importer = MagicMock()
        importer._convertToList.return_value = ([], "None")

        with patch("Database.database.Importer", return_value=importer):
            self.db.importHistory("not an export")

        self.assertEqual(len(self._playedAts()), 2)
        importer.importHistory.assert_not_called()

    def test_progress_prefix_is_included_in_messages(self):
        def gen():
            yield _meta("i1", 200)

        capturedMessages = []
        originalWriteProgress = self.db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedMessages.append(message)
            originalWriteProgress(status, current, total, message, error)

        self.db.writeProgress = captureWriteProgress

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistory("raw export", progressPrefix="File 1/1: ")

        self.assertTrue(capturedMessages)
        self.assertTrue(all(m.startswith("File 1/1: ") for m in capturedMessages))


class TestImportHistoryBatch(DatabaseTestCase):
    """importHistoryBatch imports multiple files sequentially (cached and
    processed one after another), mirroring AutoImporter's existing
    one-file-at-a-time folder-watching behavior."""

    def setUp(self):
        super().setUp()
        self.db = self._makeDb({}, [])

    def _mockImporter(self, generatorFactory, parsedCount=1):
        importer = MagicMock()
        importer._convertToList.return_value = ([{}] * parsedCount, "spotifyAcountExport")
        importer.importHistory.return_value = generatorFactory()
        return importer

    def _ids(self):
        return [e["id"] for e in self.db.getEntriesFromOld(fullPagination=False)]

    def test_files_are_imported_sequentially_and_merged(self):
        def gen1():
            yield _meta("f1i1", 100)

        def gen2():
            yield _meta("f2i1", 200)

        with patch("Database.database.Importer",
                    side_effect=[self._mockImporter(gen1), self._mockImporter(gen2)]):
            self.db.importHistoryBatch(["export one", "export two"])

        self.assertEqual(self._ids(), ["f1i1", "f2i1"])
        self.assertEqual(self.db.readProgress()["status"], "complete")

    def test_one_failing_file_does_not_block_the_rest(self):
        def failing():
            raise RuntimeError("bad file")
            yield  # unreachable - keeps this a generator function

        def gen2():
            yield _meta("f2i1", 200)

        with patch("Database.database.Importer",
                    side_effect=[self._mockImporter(failing), self._mockImporter(gen2)]):
            self.db.importHistoryBatch(["bad export", "good export"])

        self.assertEqual(self._ids(), ["f2i1"])
        progress = self.db.readProgress()
        self.assertEqual(progress["status"], "complete")
        self.assertIn("1 failed", progress["message"])
        self.assertTrue(progress["error"])

    def test_error_flag_is_not_cleared_on_subsequent_import_steps(self):
        def failing():
            raise RuntimeError("bad file")
            yield

        def gen2():
            yield _meta("f2i1", 200)

        capturedErrors = []
        originalWriteProgress = self.db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedErrors.append(error)
            originalWriteProgress(status, current, total, message, error)

        self.db.writeProgress = captureWriteProgress

        with patch("Database.database.Importer",
                    side_effect=[self._mockImporter(failing), self._mockImporter(gen2)]):
            self.db.importHistoryBatch(["bad export", "good export"])

        # Once the first file fails, all subsequent writeProgress calls must preserve error=True.
        self.assertTrue(capturedErrors[-1])  # Final status has error=True
        # Check that starting file 2 sets error=True instead of False.
        self.assertTrue(capturedErrors[2])

    def test_all_files_failing_marks_progress_failed(self):
        def failing():
            raise RuntimeError("bad file")
            yield  # unreachable - keeps this a generator function

        with patch("Database.database.Importer",
                    side_effect=[self._mockImporter(failing), self._mockImporter(failing)]):
            self.db.importHistoryBatch(["bad one", "bad two"])

        progress = self.db.readProgress()
        self.assertEqual(progress["status"], "failed")
        self.assertTrue(progress["error"])

    def test_progress_prefix_identifies_current_file(self):
        def gen():
            yield _meta("i1", 100)

        capturedMessages = []
        originalWriteProgress = self.db.writeProgress

        def captureWriteProgress(status, current=0, total=0, message="", error=False):
            capturedMessages.append(message)
            originalWriteProgress(status, current, total, message, error)

        self.db.writeProgress = captureWriteProgress

        with patch("Database.database.Importer", return_value=self._mockImporter(gen)):
            self.db.importHistoryBatch(["only export"])

        self.assertTrue(any("File 1/1" in m for m in capturedMessages))

    def test_empty_file_list_is_a_noop(self):
        self.db.importHistoryBatch([])
        self.assertEqual(self._ids(), [])


if __name__ == "__main__":
    import unittest
    unittest.main()
