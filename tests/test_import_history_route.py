"""POST /import-history must never 500 on a malformed upload, and must not
block a Waitress worker thread for a full second on every submission.
"""
import io
import threading
import unittest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'

# A failure deadline, not a pace: _importStarted below returns the moment the
# import thread makes its call, so a passing run never waits this long. Only a
# thread that never runs at all spends it, and then the test should fail.
_IMPORT_THREAD_DEADLINE_SECONDS = 5


class TestImportHistoryRoute(AppTestCase):
    def _makeDb(self):
        db = MagicMock()
        db.readProgress.return_value = {"status": "idle", "current": 0, "total": 0, "percentage": 0, "message": "", "error": False}
        return db

    def _importStarted(self, db):
        """An Event the mocked importHistoryBatch sets when the daemon import
        thread actually reaches it. Waiting on the call itself is what makes
        these tests deterministic - a fixed sleep is both slower than needed
        and, on a loaded runner, sometimes shorter than needed."""
        started = threading.Event()
        db.importHistoryBatch.side_effect = lambda *args, **kwargs: started.set()
        return started

    def _awaitImport(self, started):
        self.assertTrue(started.wait(_IMPORT_THREAD_DEADLINE_SECONDS),
                        "the background import thread never called importHistoryBatch")

    def _postImport(self, dash, db, files):
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.post('/import-history', data=files, content_type='multipart/form-data')

    def test_non_utf8_upload_does_not_crash_the_request(self):
        """A file that isn't valid UTF-8 text used to raise UnicodeDecodeError
        straight out of the route handler (an unhandled 500) instead of being
        skipped like AutoImporter already skips an unreadable file."""
        dash = self._makeApp()
        db = self._makeDb()
        garbageBytes = b'\xff\xfe\x00\x01 not valid utf-8 \xfa\xfb'

        resp = self._postImport(dash, db, {'history_file': (io.BytesIO(garbageBytes), 'history.json')})

        self.assertEqual(resp.status_code, 302)

    def test_non_utf8_file_alongside_valid_file_still_imports_the_valid_one(self):
        """One bad file in a multi-file upload must not drop the good ones too."""
        dash = self._makeApp()
        db = self._makeDb()
        started = self._importStarted(db)
        garbageBytes = b'\xff\xfe\x00\x01 not valid utf-8 \xfa\xfb'

        self._postImport(dash, db, {
            'history_file': [
                (io.BytesIO(garbageBytes), 'bad.json'),
                (io.BytesIO(b'{"msPlayed": 1}'), 'good.json'),
            ]
        })

        self._awaitImport(started)
        db.importHistoryBatch.assert_called_once()
        importedContents = db.importHistoryBatch.call_args.args[0]
        self.assertEqual(importedContents, ['{"msPlayed": 1}'])

    def test_all_files_failing_to_decode_redirects_without_starting_an_import(self):
        dash = self._makeApp()
        db = self._makeDb()
        garbageBytes = b'\xff\xfe\x00\x01 not valid utf-8 \xfa\xfb'

        resp = self._postImport(dash, db, {'history_file': (io.BytesIO(garbageBytes), 'bad.json')})

        self.assertEqual(resp.status_code, 302)
        db.importHistoryBatch.assert_not_called()

    def test_overwrite_checkbox_flag_is_passed_to_the_batch(self):
        dash = self._makeApp()
        db = self._makeDb()
        started = self._importStarted(db)

        self._postImport(dash, db, {
            'history_file': (io.BytesIO(b'{"msPlayed": 1}'), 'history.json'),
            'overwrite_range': 'on',
        })

        self._awaitImport(started)
        db.importHistoryBatch.assert_called_once()
        self.assertTrue(db.importHistoryBatch.call_args.kwargs.get("overwriteRange"))

    def test_missing_overwrite_checkbox_defaults_to_false(self):
        dash = self._makeApp()
        db = self._makeDb()
        started = self._importStarted(db)

        self._postImport(dash, db, {'history_file': (io.BytesIO(b'{"msPlayed": 1}'), 'history.json')})

        self._awaitImport(started)
        db.importHistoryBatch.assert_called_once()
        self.assertFalse(db.importHistoryBatch.call_args.kwargs.get("overwriteRange"))

    def test_progress_is_claimed_running_synchronously_before_the_redirect(self):
        """The route used to rely on time.sleep(1) after starting the
        background thread to give it a chance to write "running" progress
        itself - instead the route claims it directly (atomically), so the
        state is guaranteed correct the instant the response is returned, with
        no sleep needed."""
        dash = self._makeApp()
        db = self._makeDb()

        self._postImport(dash, db, {'history_file': (io.BytesIO(b'{}'), 'history.json')})

        db.tryClaimImportRunning.assert_called_once()

    def test_concurrent_import_is_rejected_without_starting_a_second(self):
        """When the running slot is already claimed (a double-submit), the
        second request must redirect without launching another import thread."""
        dash = self._makeApp()
        db = self._makeDb()
        db.tryClaimImportRunning.return_value = False

        resp = self._postImport(dash, db, {'history_file': (io.BytesIO(b'{"msPlayed": 1}'), 'history.json')})

        self.assertEqual(resp.status_code, 302)
        db.importHistoryBatch.assert_not_called()

    def test_request_does_not_block_on_a_sleep(self):
        """The route used to time.sleep(1) after spawning the import thread
        (see test_progress_is_marked_running_synchronously_before_the_
        redirect). Guarded by asserting no sleep happens on the request path
        at all, rather than a wall-clock elapsed bound - timing thresholds
        flake on loaded CI runners."""
        dash = self._makeApp()
        db = self._makeDb()

        with patch("app.time.sleep") as mock_sleep:
            self._postImport(dash, db, {'history_file': (io.BytesIO(b'{}'), 'history.json')})

        mock_sleep.assert_not_called()


class TestImportThreadReleasesTheRunningSlot(AppTestCase):
    """The claimed "running" slot is a lock, not just a status.

    tryClaimImportRunning only claims when the stored status is NOT already
    'running', so a thread that dies without writing a terminal state locks
    that user out of importing anything, permanently, with no UI to clear it.

    Database.importHistoryBatch does write a terminal state on every failure it
    can see - but not every line is inside those try blocks (_computeCoveredRange
    runs before the first one, and the terminal writeProgress calls are
    themselves unguarded). This is the backstop for whatever gets through.

    Deterministic throughout: every wait is on an Event the code under test
    sets, so the timeout is an upper bound on a hang rather than a race window.
    """

    # Generous on purpose - only reached when the guard never runs at all,
    # in which case the test is failing regardless of how long it waited.
    _THREAD_TIMEOUT_SECONDS = 10

    def _makeDb(self, status):
        db = MagicMock()
        db.readProgress.return_value = {
            "status": status, "current": 0, "total": 0,
            "percentage": 0, "message": "", "error": False,
        }
        return db

    def _postImport(self, dash, db):
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.post(
                '/import-history',
                data={'history_file': (io.BytesIO(b'{"msPlayed": 1}'), 'history.json')},
                content_type='multipart/form-data',
            )

    def _onProgressRead(self, db, status):
        """Fire an Event when the guard reads the progress row - that read is
        the last thing the thread does, so waiting on it is what makes these
        assertions race-free."""
        checked = threading.Event()
        row = {"status": status, "current": 0, "total": 0,
               "percentage": 0, "message": "", "error": False}

        def readProgress():
            checked.set()
            return row

        db.readProgress.side_effect = readProgress
        return checked

    def test_a_crashing_import_writes_a_terminal_progress_state(self):
        dash = self._makeApp()
        db = self._makeDb("running")
        db.importHistoryBatch.side_effect = RuntimeError("boom")
        written = threading.Event()
        db.writeProgress.side_effect = lambda *a, **k: written.set()

        resp = self._postImport(dash, db)

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(written.wait(self._THREAD_TIMEOUT_SECONDS),
                        "the crashed thread left the slot claimed forever")
        self.assertNotEqual(db.writeProgress.call_args.args[0], "running")

    def test_the_replacement_state_says_the_import_failed(self):
        """It has to read as an error, not as a quiet 'complete' - the user
        needs to know their history did not land."""
        dash = self._makeApp()
        db = self._makeDb("running")
        db.importHistoryBatch.side_effect = RuntimeError("boom")
        written = threading.Event()
        db.writeProgress.side_effect = lambda *a, **k: written.set()

        self._postImport(dash, db)
        self.assertTrue(written.wait(self._THREAD_TIMEOUT_SECONDS))

        self.assertEqual(db.writeProgress.call_args.args[0], "failed")
        self.assertTrue(db.writeProgress.call_args.kwargs.get("error"))

    def test_a_successful_import_keeps_the_progress_it_wrote_itself(self):
        """The batch writes a detailed terminal message ("Imported 3/4 files
        (1 skipped)"). Overwriting that with the generic backstop text would
        make every successful import report less than it knows."""
        dash = self._makeApp()
        db = self._makeDb("complete")
        db.importHistoryBatch.return_value = ["imported"]
        checked = self._onProgressRead(db, "complete")

        self._postImport(dash, db)
        self.assertTrue(checked.wait(self._THREAD_TIMEOUT_SECONDS))

        db.writeProgress.assert_not_called()

    def test_a_batch_that_reported_failure_itself_is_not_overwritten(self):
        dash = self._makeApp()
        db = self._makeDb("failed")
        db.importHistoryBatch.return_value = ["failed"]
        checked = self._onProgressRead(db, "failed")

        self._postImport(dash, db)
        self.assertTrue(checked.wait(self._THREAD_TIMEOUT_SECONDS))

        db.writeProgress.assert_not_called()

    def test_a_missing_progress_row_is_treated_as_still_claimed(self):
        """readProgress returns None when there is no row at all. That is not
        'someone already finished' - it is a row that went missing after the
        claim, and leaving it would look exactly like a stuck import."""
        dash = self._makeApp()
        db = self._makeDb("running")
        db.importHistoryBatch.side_effect = RuntimeError("boom")
        db.readProgress.return_value = None
        written = threading.Event()
        db.writeProgress.side_effect = lambda *a, **k: written.set()

        self._postImport(dash, db)

        self.assertTrue(written.wait(self._THREAD_TIMEOUT_SECONDS))
        self.assertEqual(db.writeProgress.call_args.args[0], "failed")

    def test_a_failing_guard_does_not_take_the_process_down(self):
        """The guard runs because the database misbehaved, so its own
        readProgress/writeProgress can misbehave too. An exception escaping
        here would surface as an unraisable-exception warning from a daemon
        thread and nothing else useful."""
        dash = self._makeApp()
        db = self._makeDb("running")
        db.importHistoryBatch.side_effect = RuntimeError("boom")
        attempted = threading.Event()

        def explode(*args, **kwargs):
            attempted.set()
            raise RuntimeError("the database is gone")

        db.readProgress.side_effect = explode

        resp = self._postImport(dash, db)

        self.assertEqual(resp.status_code, 302)
        self.assertTrue(attempted.wait(self._THREAD_TIMEOUT_SECONDS))


if __name__ == "__main__":
    unittest.main()
