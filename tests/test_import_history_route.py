"""POST /import-history must never 500 on a malformed upload, and must not
block a Waitress worker thread for a full second on every submission.
"""
import io
import time
import unittest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestImportHistoryRoute(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _makeDb(self):
        db = MagicMock()
        db.readProgress.return_value = {"status": "idle", "current": 0, "total": 0, "percentage": 0, "message": "", "error": False}
        return db

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
        garbageBytes = b'\xff\xfe\x00\x01 not valid utf-8 \xfa\xfb'

        self._postImport(dash, db, {
            'history_file': [
                (io.BytesIO(garbageBytes), 'bad.json'),
                (io.BytesIO(b'{"msPlayed": 1}'), 'good.json'),
            ]
        })

        time.sleep(0.05)   #< let the daemon import thread run
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

    def test_progress_is_marked_running_synchronously_before_the_redirect(self):
        """The route used to rely on time.sleep(1) after starting the
        background thread to give it a chance to write "running" progress
        itself - instead the route must write it directly, so the state is
        guaranteed correct the instant the response is returned, with no
        sleep needed."""
        dash = self._makeApp()
        db = self._makeDb()

        self._postImport(dash, db, {'history_file': (io.BytesIO(b'{}'), 'history.json')})

        db.writeProgress.assert_called_once_with("running", 0, 0, "Starting import")

    def test_request_does_not_block_for_a_full_second(self):
        dash = self._makeApp()
        db = self._makeDb()

        start = time.monotonic()
        self._postImport(dash, db, {'history_file': (io.BytesIO(b'{}'), 'history.json')})
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 0.5)


if __name__ == "__main__":
    unittest.main()
