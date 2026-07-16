import io
import unittest
from unittest.mock import patch, MagicMock, mock_open
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.Importers.AutoImporter import AutoImporter, Watchdog


def _fakeOpenByName(path, *args, **kwargs):
    """open() replacement returning per-file content, so batch-order
    assertions can tell files apart."""
    return io.StringIO(f"data:{os.path.basename(path)}")

class TestAutoImporterLogging(unittest.TestCase):
    def setUp(self):
        # Set up a caplog-like context or use logging assertLogs
        self.logger = logging.getLogger("Database.Importers.AutoImporter")
        self.original_level = self.logger.level
        self.logger.setLevel(logging.INFO)

    def tearDown(self):
        self.logger.setLevel(self.original_level)

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_watchdog_monitoring_log(self, mock_listdir, mock_makedirs, mock_exists):
        mock_exists.return_value = True
        mock_listdir.return_value = []
        
        wd = Watchdog()
        wd.run = False  # Stop the loop immediately in the test
        
        with self.assertLogs("Database.Importers.AutoImporter", level="INFO") as log_capture:
            wd.watchFolder_blocking("/dummy/path", lambda x: None, callbackInitialFiles=False)
            
        self.assertTrue(any("Monitoring /dummy/path for new files (Polling)..." in record for record in log_capture.output))

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_watchdog_file_found_log(self, mock_listdir, mock_makedirs, mock_exists):
        mock_exists.return_value = True
        mock_listdir.side_effect = [["file1.txt"]]
        
        wd = Watchdog()
        wd.run = False
        
        with patch("Database.Importers.AutoImporter.os.path.isfile", return_value=True):
            with self.assertLogs("Database.Importers.AutoImporter", level="INFO") as log_capture:
                wd.watchFolder_blocking("/dummy/path", lambda x: None, callbackInitialFiles=True)
                
        self.assertTrue(any("File found:" in record for record in log_capture.output))

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_auto_importer_import_success_log(self, mock_move, mock_exists):
        mock_exists.side_effect = lambda p: os.path.normpath(p) == os.path.normpath("/dummy/path/DONE")

        import_callback = MagicMock()
        importer = AutoImporter("/dummy/path", import_callback)

        m_open = mock_open(read_data="dummy data")
        with patch("Database.Importers.AutoImporter.open", m_open):
            with self.assertLogs("Database.Importers.AutoImporter", level="INFO") as log_capture:
                importer._handleImport(["/dummy/path/file.txt"])

        self.assertTrue(any("Successfully imported file.txt" in record for record in log_capture.output))
        self.assertTrue(any("Successfully moved file.txt to DONE/" in record for record in log_capture.output))


class TestAutoImporterBatching(unittest.TestCase):
    """Files dropped together must go through ONE importCallback call so
    batch-scoped import state (duplicate-claim tracking across file
    boundaries) covers all of them."""

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_handle_import_batches_files_into_one_callback_call(self, mock_move, mock_exists):
        mock_exists.side_effect = lambda p: os.path.normpath(p) == os.path.normpath("/dummy/path/DONE")
        import_callback = MagicMock()
        importer = AutoImporter("/dummy/path", import_callback)

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=_fakeOpenByName)):
            importer._handleImport(["/dummy/path/b.json", "/dummy/path/a.json"])

        # One call, contents in filename order
        import_callback.assert_called_once_with(["data:a.json", "data:b.json"])
        self.assertEqual(mock_move.call_count, 2)

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_keyword_mismatch_skips_import_but_moves_file(self, mock_move, mock_exists):
        mock_exists.side_effect = lambda p: os.path.normpath(p) == os.path.normpath("/dummy/path/DONE")
        import_callback = MagicMock()
        importer = AutoImporter("/dummy/path", import_callback, keyword="Weekly")

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=_fakeOpenByName)):
            importer._handleImport(["/dummy/path/Weekly_1.json", "/dummy/path/other.json"])

        import_callback.assert_called_once_with(["data:Weekly_1.json"])
        self.assertEqual(mock_move.call_count, 2)  #< both moved to DONE

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_failed_batch_leaves_files_in_place_for_retry(self, mock_move, mock_exists):
        mock_exists.side_effect = lambda p: os.path.normpath(p) == os.path.normpath("/dummy/path/DONE")
        import_callback = MagicMock(side_effect=RuntimeError("boom"))
        importer = AutoImporter("/dummy/path", import_callback)

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=_fakeOpenByName)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
                importer._handleImport(["/dummy/path/a.json"])

        mock_move.assert_not_called()

    @patch("Database.Importers.AutoImporter.os.path.getsize")
    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_watchdog_delivers_files_added_in_one_cycle_as_one_batch(self, mock_listdir, mock_makedirs, mock_exists, mock_getsize):
        mock_exists.return_value = True
        mock_getsize.return_value = 100   #< size already stable between the two polls
        #< initial scan empty, then the two new files sighted twice (size-stabilization check)
        mock_listdir.side_effect = [[], ["b.json", "a.json"], ["b.json", "a.json"]]

        wd = Watchdog()
        calls = []

        def callback(paths):
            calls.append(paths)
            wd.run = False

        with patch("Database.Importers.AutoImporter.os.path.isfile", return_value=True):
            wd.watchFolder_blocking("/dummy/path", callback, checkInterval=0.01, callbackInitialFiles=True)

        expected = sorted(os.path.join("/dummy/path", f) for f in ["a.json", "b.json"])
        self.assertEqual(calls, [expected])

    @patch("Database.Importers.AutoImporter.os.path.getsize")
    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_watchdog_waits_until_a_growing_file_stops_growing(self, mock_listdir, mock_makedirs, mock_exists, mock_getsize):
        """A file still being copied into the watch folder must not be read
        mid-copy - it used to be imported the moment it appeared, so a large
        export got picked up half-written, failed to parse, and was silently
        swallowed. Only once its size stops changing between polls is it
        delivered."""
        mock_exists.return_value = True
        mock_listdir.side_effect = [[], ["big.json"], ["big.json"], ["big.json"]]
        mock_getsize.side_effect = [10, 25, 25]   #< still growing on the second poll, stable on the third

        wd = Watchdog()
        calls = []

        def callback(paths):
            calls.append(paths)
            wd.run = False

        with patch("Database.Importers.AutoImporter.os.path.isfile", return_value=True):
            wd.watchFolder_blocking("/dummy/path", callback, checkInterval=0.01, callbackInitialFiles=True)

        self.assertEqual(calls, [[os.path.join("/dummy/path", "big.json")]])
        self.assertEqual(mock_getsize.call_count, 3)

    @patch("Database.Importers.AutoImporter.os.path.getsize")
    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_watchdog_forgets_a_file_deleted_while_pending(self, mock_listdir, mock_makedirs, mock_exists, mock_getsize):
        """A file that vanishes before its size stabilizes (e.g. the user
        pulled it back out) must be dropped from tracking, not delivered."""
        mock_exists.return_value = True
        mock_getsize.return_value = 10

        wd = Watchdog()
        calls = []
        scans = [[], ["gone.json"], [], []]

        def scriptedListdir(path):
            if len(scans) == 1:
                wd.run = False   #< last scripted scan - stop the loop after it
            return scans.pop(0)

        mock_listdir.side_effect = scriptedListdir

        with patch("Database.Importers.AutoImporter.os.path.isfile", return_value=True):
            wd.watchFolder_blocking("/dummy/path", lambda paths: calls.append(paths),
                                    checkInterval=0.01, callbackInitialFiles=True)

        self.assertEqual(calls, [])


class TestAutoImporterOutcomeRouting(unittest.TestCase):
    """_handleImport routes each file by the outcome importHistoryBatch
    reports for it: imported/skipped files go to DONE/, failed files go to
    FAILED/ where they're visible instead of being celebrated as successes
    (the old behavior moved a never-imported corrupt file to DONE/)."""

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_failed_file_moves_to_FAILED_not_DONE(self, mock_move, mock_makedirs, mock_exists):
        mock_exists.return_value = False
        import_callback = MagicMock(return_value=["failed"])
        importer = AutoImporter("/dummy/path", import_callback)

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=_fakeOpenByName)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR") as log_capture:
                importer._handleImport(["/dummy/path/corrupt.json"])

        destination = mock_move.call_args[0][1]
        self.assertIn("FAILED", os.path.normpath(destination).split(os.sep))
        self.assertTrue(any("corrupt.json" in record for record in log_capture.output))

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_each_file_is_routed_by_its_own_outcome(self, mock_move, mock_makedirs, mock_exists):
        mock_exists.return_value = False
        import_callback = MagicMock(return_value=["imported", "failed"])
        importer = AutoImporter("/dummy/path", import_callback)

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=_fakeOpenByName)):
            importer._handleImport(["/dummy/path/a.json", "/dummy/path/b.json"])

        destinations = {os.path.basename(call[0][0]): os.path.normpath(call[0][1]).split(os.sep)
                        for call in mock_move.call_args_list}
        self.assertIn("DONE", destinations["a.json"])
        self.assertIn("FAILED", destinations["b.json"])

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_skipped_files_count_as_success_and_move_to_DONE(self, mock_move, mock_makedirs, mock_exists):
        mock_exists.return_value = False
        import_callback = MagicMock(return_value=["skipped"])
        importer = AutoImporter("/dummy/path", import_callback)

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=_fakeOpenByName)):
            importer._handleImport(["/dummy/path/rerun.json"])

        destination = mock_move.call_args[0][1]
        self.assertIn("DONE", os.path.normpath(destination).split(os.sep))


class TestAutoImporterWiring(DatabaseTestCase):
    def test_database_wires_batch_import_callback(self):
        """Database must feed the AutoImporter through importHistoryBatch so
        the per-batch run state (and per-file error tolerance) applies to
        auto-imported files too."""
        db = self._makeDb({}, [])
        self.assertEqual(db.autoImporter.importCallback.__func__, type(db).importHistoryBatch)


if __name__ == "__main__":
    unittest.main()
