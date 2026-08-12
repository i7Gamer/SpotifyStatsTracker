import io
import unittest
from unittest.mock import patch, MagicMock, mock_open
import logging
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase
from Database.Importers.AutoImporter import AutoImporter, Watchdog, WATCHDOG_THREAD_NAME_PREFIX


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


class TestWatchdogSurvivesTransientPollFailures(unittest.TestCase):
    """The poll loop used to sit inside one try/except, so a single transient
    failure ended the thread. Nothing restarts it (startAutoImporter runs once
    per activation; the periodic login pass only restarts listeners), so
    drop-folder imports stayed silently dead until the app was restarted."""

    @patch("Database.Importers.AutoImporter.os.path.getsize")
    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_a_failed_poll_does_not_end_the_watcher(self, mock_listdir, mock_makedirs, mock_exists, mock_getsize):
        mock_exists.return_value = True
        mock_getsize.return_value = 100
        mock_listdir.side_effect = [
            [],                                        #< initial scan
            OSError("folder locked by another process"),   #< transient failure
            ["a.json"], ["a.json"],                    #< recovers, file stabilizes
        ]

        wd = Watchdog()
        calls = []

        def callback(paths):
            calls.append(paths)
            wd.run = False

        with patch("Database.Importers.AutoImporter.os.path.isfile", return_value=True):
            wd.watchFolder_blocking("/dummy/path", callback, checkInterval=0.01, callbackInitialFiles=True)

        self.assertEqual(calls, [[os.path.join("/dummy/path", "a.json")]])

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_stop_still_ends_the_loop_after_a_failed_poll(self, mock_listdir, mock_makedirs, mock_exists):
        """Retrying must not outlive a stop request."""
        mock_exists.return_value = True
        wd = Watchdog()
        polls = []

        def listdir(_path):
            if not polls:          #< initial scan succeeds
                polls.append("initial")
                return []
            polls.append("poll")
            wd.signalStop()        #< stop arrives while the poll is failing
            raise OSError("still broken")

        mock_listdir.side_effect = listdir

        with patch("Database.Importers.AutoImporter.os.path.isfile", return_value=True):
            wd.watchFolder_blocking("/dummy/path", lambda paths: None, checkInterval=0.01)

        self.assertEqual(polls.count("poll"), 1)   #< did not keep retrying after the stop


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


class TestAutoImporterUnreadableFileRouting(unittest.TestCase):
    """A file whose CONTENT can't be decoded is as dead as one the importer
    rejects, so it goes to FAILED/ for the same reason: left in the watch
    folder it is re-read and re-logged on every restart (the watchdog adds a
    name to knownFiles before the callback, so it isn't retried within a
    process, but the startup scan hands it over again), and it stays invisible
    to anyone not reading the log.

    A file that is merely unreadable RIGHT NOW is the opposite case and must be
    left alone: an antivirus scanner or backup tool holding the file, or a
    still-being-copied file the startup scan picked up before the size-
    stabilization gate could apply, both surface as OSError and both succeed on
    a later pass (see Watchdog._fileSizeOrNone, which documents the same
    transient states)."""

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_an_undecodable_file_moves_to_FAILED(self, mock_move, mock_makedirs, mock_exists):
        mock_exists.return_value = False
        import_callback = MagicMock(return_value=[])
        importer = AutoImporter("/dummy/path", import_callback)
        badBytes = UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=badBytes)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR") as log_capture:
                importer._handleImport(["/dummy/path/mojibake.json"])

        import_callback.assert_not_called()
        destination = mock_move.call_args[0][1]
        self.assertIn("FAILED", os.path.normpath(destination).split(os.sep))
        self.assertTrue(any("mojibake.json" in record for record in log_capture.output))

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_a_transiently_locked_file_is_left_in_place(self, mock_move, mock_makedirs, mock_exists):
        mock_exists.return_value = False
        importer = AutoImporter("/dummy/path", MagicMock(return_value=[]))
        locked = PermissionError("being used by another process")

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=locked)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
                importer._handleImport(["/dummy/path/still-copying.json"])

        mock_move.assert_not_called()

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_a_transiently_locked_file_asks_to_be_delivered_again(self, mock_move, mock_makedirs, mock_exists):
        """"Left in place" was only half a policy. The watchdog adds a name to
        knownFiles BEFORE calling back, and discovery is
        `currentFiles - knownFiles - pendingSizes`, so a file left in place is
        never re-sighted: the arm above promises "reads fine on a later pass"
        and there is no later pass within the process. The handler has to ask
        for the retry explicitly."""
        mock_exists.return_value = False
        importer = AutoImporter("/dummy/path", MagicMock(return_value=[]))
        locked = PermissionError("being used by another process")

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=locked)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
                retry = importer._handleImport(["/dummy/path/still-copying.json"])

        self.assertEqual(list(retry), ["/dummy/path/still-copying.json"])
        mock_move.assert_not_called()

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_a_file_that_stays_locked_is_quarantined_rather_than_retried_forever(
            self, mock_move, mock_makedirs, mock_exists):
        """The retry must be bounded: an unbounded one is a 5s log-spam loop
        for the life of the process, which is exactly why the once-per-process
        rule existed. After the last attempt the file goes to FAILED/, where it
        is visible to someone who never reads the log."""
        from Database.Importers.AutoImporter import MAX_TRANSIENT_READ_ATTEMPTS
        mock_exists.return_value = False
        importer = AutoImporter("/dummy/path", MagicMock(return_value=[]))
        locked = PermissionError("being used by another process")
        path = "/dummy/path/still-copying.json"

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=locked)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
                for _ in range(MAX_TRANSIENT_READ_ATTEMPTS - 1):
                    self.assertEqual(list(importer._handleImport([path])), [path])
                lastRetry = importer._handleImport([path])

        self.assertEqual(list(lastRetry), [])
        destination = mock_move.call_args[0][1]
        self.assertIn("FAILED", os.path.normpath(destination).split(os.sep))

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_a_file_that_reads_on_a_later_pass_forgets_its_earlier_failures(
            self, mock_move, mock_makedirs, mock_exists):
        """The counter is per file and must reset on success, or a folder used
        for months accumulates attempts against names that keep coming back.

        Driven to the very edge of the budget on BOTH sides of the successful
        read: a single failure either side proves nothing, because 1 and 2 are
        both under a MAX of 3 and produce the same retryable list. With the
        reset deleted, the second post-success failure quarantines instead."""
        from Database.Importers.AutoImporter import MAX_TRANSIENT_READ_ATTEMPTS
        mock_exists.return_value = False
        importer = AutoImporter("/dummy/path", MagicMock(return_value=["imported"]))
        path = "/dummy/path/eventually-fine.json"
        locked = PermissionError("being used by another process")

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=locked)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
                for _ in range(MAX_TRANSIENT_READ_ATTEMPTS - 1):
                    importer._handleImport([path])
        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=_fakeOpenByName)):
            importer._handleImport([path])   #< reads fine this time

        self.assertEqual(importer._readAttempts, {})   #< the direct seam

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=locked)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
                for _ in range(MAX_TRANSIENT_READ_ATTEMPTS - 1):
                    retry = importer._handleImport([path])

        self.assertEqual(list(retry), [path],
                         "the successful read must have cleared the earlier attempts")
        #< the successful read imports and moves to DONE/, so "not moved" is the
        #  wrong assertion here - what must never happen is a QUARANTINE
        for call in mock_move.call_args_list:
            self.assertNotIn("FAILED", os.path.normpath(call[0][1]).split(os.sep))

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_a_freshly_dropped_file_gets_its_own_retry_budget(
            self, mock_move, mock_makedirs, mock_exists):
        """_readAttempts is keyed by path and outlives the file it describes:
        nothing pops it when the user pulls a half-copied export back out. The
        recurring case here is the same filename every week - the second copy
        would inherit the first's attempts and be quarantined on its FIRST
        lock, under a message naming a count that never happened."""
        from Database.Importers.AutoImporter import MAX_TRANSIENT_READ_ATTEMPTS
        mock_exists.return_value = False
        importer = AutoImporter("/dummy/path", MagicMock(return_value=["imported"]))
        path = "/dummy/path/export.json"
        locked = PermissionError("being used by another process")
        stamps = iter([(100, 1000.0)] * (MAX_TRANSIENT_READ_ATTEMPTS - 1) + [(250, 2000.0)])

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=locked)), \
             patch.object(AutoImporter, "_fileStamp", lambda self, p: next(stamps)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
                for _ in range(MAX_TRANSIENT_READ_ATTEMPTS - 1):
                    importer._handleImport([path])
                retry = importer._handleImport([path])   #< a different file, same name

        self.assertEqual(list(retry), [path], "the new copy must get the full budget")
        mock_move.assert_not_called()

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_a_keyword_mismatch_whose_move_fails_is_not_treated_as_unreadable(
            self, mock_move, mock_makedirs, mock_exists):
        """The keyword branch's move to DONE/ sits in the same try as the read.
        Once that except started counting attempts and quarantining, a file
        that merely failed to MOVE - never unreadable, never meant to be
        imported - was logged as unreadable and ended up in FAILED/ after three
        deliveries, with a message naming the wrong cause."""
        mock_exists.return_value = False
        importer = AutoImporter("/dummy/path", MagicMock(return_value=[]), keyword="Weekly")
        mock_move.side_effect = PermissionError("held by another process")

        with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
            retry = importer._handleImport(["/dummy/path/other.json"])

        self.assertEqual(list(retry), [])   #< nothing to re-read; it was never read
        self.assertEqual(importer._readAttempts, {})
        for call in mock_move.call_args_list:
            self.assertNotIn("FAILED", os.path.normpath(call[0][1]).split(os.sep))

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_a_file_whose_quarantine_move_also_fails_stays_reachable(
            self, mock_move, mock_makedirs, mock_exists):
        """The expected outcome for the case the retry exists for: on Windows
        the exclusive lock that makes open() raise also makes the move fail. If
        the file is dropped from the retry list at that point it is stranded
        for the life of the process - the exact dead end this whole change was
        written to remove - while the log claims it went to FAILED/."""
        from Database.Importers.AutoImporter import MAX_TRANSIENT_READ_ATTEMPTS
        mock_exists.return_value = False
        importer = AutoImporter("/dummy/path", MagicMock(return_value=[]))
        mock_move.side_effect = PermissionError("held by another process")
        path = "/dummy/path/locked.json"

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=PermissionError("locked"))):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
                # Exactly the delivery that quarantines. Anything the watchdog
                # is not handed back on THIS call it never sights again, so a
                # later _handleImport call would only be reachable in a test.
                for _ in range(MAX_TRANSIENT_READ_ATTEMPTS):
                    retry = importer._handleImport([path])

        self.assertEqual(list(retry), [path],
                         "a file that could be neither read nor quarantined must stay reachable")

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_an_undecodable_file_whose_quarantine_fails_stays_reachable(
            self, mock_move, mock_makedirs, mock_exists):
        """The decode arm's own comment says the file "simply stays for the
        next pass". Since the watchdog keeps a delivered name in knownFiles,
        there IS no next pass for anything the callback does not hand back -
        which is why the sibling read-failure arm re-queues on a failed move.
        This arm was left behind, so its comment described the sibling's
        behaviour rather than its own."""
        mock_exists.return_value = False
        importer = AutoImporter("/dummy/path", MagicMock(return_value=[]))
        badBytes = UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")
        mock_move.side_effect = PermissionError("held by another process")

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=badBytes)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
                retry = importer._handleImport(["/dummy/path/mojibake.json"])

        self.assertEqual(list(retry), ["/dummy/path/mojibake.json"])

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_a_repeatedly_unquarantinable_file_is_announced_once_not_every_poll(
            self, mock_move, mock_makedirs, mock_exists):
        """Re-queuing on a failed move buys recovery at the price of noise, and
        the read-failure arm pays for it with a counter: loud on the delivery
        that gives up, DEBUG after. The decode arm consults no counter at all,
        so an undecodable file on a read-only mount logged BOTH its lines at
        ERROR on every delivery - two lines per poll interval, for the life of
        the process. Unbounded retry is intended; unbounded shouting is not."""
        mock_exists.return_value = False
        importer = AutoImporter("/dummy/path", MagicMock(return_value=[]))
        badBytes = UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")
        mock_move.side_effect = PermissionError("read-only mount")
        path = "/dummy/path/mojibake.json"

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=badBytes)):
            with self.assertLogs("Database.Importers.AutoImporter", level="DEBUG") as captured:
                for _ in range(5):
                    retry = importer._handleImport([path])

        errors = [r for r in captured.records if r.levelno >= logging.ERROR]
        self.assertEqual(len(errors), 2, f"one announcement + one move failure, got: {errors}")
        self.assertEqual(list(retry), [path], "it must still be re-queued every time")

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_one_undecodable_file_does_not_stop_the_rest_of_the_batch(self, mock_move, mock_makedirs, mock_exists):
        mock_exists.return_value = False
        import_callback = MagicMock(return_value=["imported"])
        importer = AutoImporter("/dummy/path", import_callback)
        badBytes = UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")

        def openOrFail(path, *args, **kwargs):
            if "mojibake" in str(path):
                raise badBytes
            return _fakeOpenByName(path, *args, **kwargs)

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=openOrFail)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
                importer._handleImport(["/dummy/path/good.json", "/dummy/path/mojibake.json"])

        self.assertEqual(import_callback.call_count, 1)   #< the readable file still imported
        destinations = {os.path.basename(call[0][0]): os.path.normpath(call[0][1]).split(os.sep)
                        for call in mock_move.call_args_list}
        self.assertIn("DONE", destinations["good.json"])
        self.assertIn("FAILED", destinations["mojibake.json"])

    @patch("Database.Importers.AutoImporter.os.path.exists")
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.shutil.move")
    def test_a_quarantine_move_that_itself_fails_does_not_kill_the_batch(self, mock_move, mock_makedirs, mock_exists):
        """The quarantine move can hit the same lock that broke the read. It
        must not take down the poll - the file just stays for the next pass."""
        mock_exists.return_value = False
        import_callback = MagicMock(return_value=["imported"])
        importer = AutoImporter("/dummy/path", import_callback)
        badBytes = UnicodeDecodeError("utf-8", b"\xff\xfe", 0, 1, "invalid start byte")
        mock_move.side_effect = OSError("move denied")

        def openOrFail(path, *args, **kwargs):
            if "mojibake" in str(path):
                raise badBytes
            return _fakeOpenByName(path, *args, **kwargs)

        with patch("Database.Importers.AutoImporter.open", MagicMock(side_effect=openOrFail)):
            with self.assertLogs("Database.Importers.AutoImporter", level="ERROR"):
                importer._handleImport(["/dummy/path/good.json", "/dummy/path/mojibake.json"])

        self.assertEqual(import_callback.call_count, 1)


class TestWatchdogRedeliversWhatTheCallbackCouldNotTake(unittest.TestCase):
    """knownFiles.add(name) happens BEFORE callback(readyPaths), so a file the
    callback could not read is never re-sighted: `knownFiles &= currentFiles`
    keeps the name while the file is still on disk, and discovery subtracts
    knownFiles. That ordering is deliberate - it is what stops an unreadable
    file being re-delivered every 5s - so the callback gets to name the ones it
    wants back instead."""

    #< enough polls for: initial scan (empty), sight, deliver, re-sight, re-deliver
    _POLLS_FOR_TWO_DELIVERIES = 6

    def _pollOnce(self, callback, mock_listdir, mock_getsize):
        """The folder starts EMPTY - the initial scan puts anything already
        present straight into knownFiles, so a file that exists from the first
        listdir is never sighted at all. It appears on the next poll, is stable
        on the one after (that is the delivery), and the remaining polls are
        where a re-delivery would show up."""
        watchdog = Watchdog()
        seen = []

        def listdir(_path):
            seen.append(1)
            if len(seen) > self._POLLS_FOR_TWO_DELIVERIES:
                watchdog.run = False
            return [] if len(seen) == 1 else ["export.json"]

        mock_listdir.side_effect = listdir
        mock_getsize.return_value = 100
        watchdog.watchFolder_blocking("/dummy/path", callback, checkInterval=0,
                                      callbackInitialFiles=False)
        return watchdog

    @patch("Database.Importers.AutoImporter.os.path.getsize")
    @patch("Database.Importers.AutoImporter.os.path.exists", return_value=True)
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.path.isfile", return_value=True)
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_a_returned_path_is_delivered_again(self, mock_listdir, _isfile, _makedirs,
                                                _exists, mock_getsize):
        batches = []

        def callback(paths):
            batches.append(list(paths))
            return list(paths)   #< "I could not take these"

        self._pollOnce(callback, mock_listdir, mock_getsize)

        self.assertGreater(len(batches), 1, "a returned path must be re-delivered")

    @patch("Database.Importers.AutoImporter.os.path.getsize")
    @patch("Database.Importers.AutoImporter.os.path.exists", return_value=True)
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.path.isfile", return_value=True)
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_a_taken_path_is_not_delivered_again(self, mock_listdir, _isfile, _makedirs,
                                                 _exists, mock_getsize):
        """The once-per-process rule still holds for everything the callback
        accepted - including the legacy callbacks that return None."""
        batches = []

        def callback(paths):
            batches.append(list(paths))
            return None

        self._pollOnce(callback, mock_listdir, mock_getsize)

        self.assertEqual(len(batches), 1)

    @patch("Database.Importers.AutoImporter.os.path.getsize")
    @patch("Database.Importers.AutoImporter.os.path.exists", return_value=True)
    @patch("Database.Importers.AutoImporter.os.makedirs")
    @patch("Database.Importers.AutoImporter.os.path.isfile", return_value=True)
    @patch("Database.Importers.AutoImporter.os.listdir")
    def test_a_callback_returning_a_non_iterable_does_not_kill_the_watcher(
            self, mock_listdir, _isfile, _makedirs, _exists, mock_getsize):
        """The contract says a callback that returns anything other than paths
        has accepted the batch. `for path in retryPaths or ()` does not honour
        that for a truthy NON-iterable (a callback returning True): it raises
        TypeError, and on the INITIAL-SCAN call that lands outside the poll
        loop's per-iteration guard, killing the watcher thread for the life of
        the process - nothing restarts one."""
        watchdog = Watchdog()
        seen = []

        def listdir(_path):
            seen.append(1)
            if len(seen) > 3:
                watchdog.run = False
            return ["export.json"]

        mock_listdir.side_effect = listdir
        mock_getsize.return_value = 100

        watchdog.watchFolder_blocking("/dummy/path", lambda paths: True, checkInterval=0,
                                      callbackInitialFiles=True)   #< must return, not raise

        self.assertFalse(watchdog.run)


class TestAutoImporterWiring(DatabaseTestCase):
    def test_database_wires_batch_import_callback(self):
        """Database must feed the AutoImporter through importHistoryBatch so
        the per-batch run state (and per-file error tolerance) applies to
        auto-imported files too."""
        db = self._makeDb({}, [])
        self.assertEqual(db.autoImporter.importCallback.__func__, type(db).importHistoryBatch)


class TestWatchdogThreadNaming(unittest.TestCase):
    """The poll thread used to be anonymous ("Thread-17"), so a watcher that
    outlived whoever started it was unattributable in a thread dump - and the
    suite's leaked-thread guard (conftest._noLeakedUserThreads) can only
    recognize one by name."""

    def test_thread_is_named_after_the_watched_folder(self):
        wd = Watchdog()
        # A startup delay the thread parks on, so it never touches the disk:
        # stop() sets the event and wait() returns at once, set before or after
        # the thread reaches it.
        wd.watchFolder(os.path.join("autoImport", "alice"), MagicMock(), startupDelaySeconds=30)
        self.addCleanup(wd.stop)

        self.assertEqual(wd.thread.name, f"{WATCHDOG_THREAD_NAME_PREFIX}alice")


class TestWatchdogSignalStop(unittest.TestCase):
    """signalStop() is the join-free half of stop(), used by shutdown's
    signal-everything-first phase."""

    def test_signal_stop_sets_flags_without_joining(self):
        wd = Watchdog()
        wd.thread = MagicMock()
        wd.thread.is_alive.return_value = True

        wd.signalStop()

        self.assertFalse(wd.run)
        self.assertTrue(wd._stop_event.is_set())
        wd.thread.join.assert_not_called()

    def test_stop_still_joins_with_timeout(self):
        wd = Watchdog()
        wd.thread = MagicMock()
        wd.thread.is_alive.return_value = True

        wd.stop()

        self.assertFalse(wd.run)
        wd.thread.join.assert_called_once()
        self.assertIn("timeout", wd.thread.join.call_args.kwargs)


if __name__ == "__main__":
    unittest.main()
