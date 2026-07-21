"""An oversized /import-history upload must be rejected with a 413 (converted
to a friendly redirect back to the import page) instead of buffering the
whole request body into memory uncapped.
"""
import io
import unittest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import SpotifyDashboardApp
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestUploadSizeLimit(AppTestCase):
    def _makeDb(self):
        db = MagicMock()
        db.readProgress.return_value = {"status": "idle", "current": 0, "total": 0, "percentage": 0, "message": "", "error": False}
        return db

    def test_max_content_length_is_configured_on_the_flask_app(self):
        dash = self._makeApp()
        self.assertEqual(dash.app.config["MAX_CONTENT_LENGTH"], appModule.MAX_UPLOAD_MB * 1024 * 1024)

    def test_oversized_upload_redirects_to_import_page_with_error(self):
        dash = self._makeApp()
        dash.app.config["MAX_CONTENT_LENGTH"] = 10  # tiny cap, just for this test
        db = self._makeDb()

        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.post(
                '/import-history',
                data={'history_file': (io.BytesIO(b'x' * 1000), 'history.json')},
                content_type='multipart/form-data',
            )

        self.assertEqual(resp.status_code, 302)
        self.assertIn('error=upload_too_large', resp.headers['Location'])

    def test_upload_within_the_limit_is_not_rejected(self):
        dash = self._makeApp()
        db = self._makeDb()

        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.post(
                '/import-history',
                data={'history_file': (io.BytesIO(b'{}'), 'history.json')},
                content_type='multipart/form-data',
            )

        self.assertEqual(resp.status_code, 302)
        self.assertNotIn('error', resp.headers['Location'])

    def test_import_page_shows_friendly_message_for_upload_too_large(self):
        dash = self._makeApp()
        db = self._makeDb()

        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get('/import?error=upload_too_large')

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'too large', resp.data)

    def test_import_page_shows_no_error_message_normally(self):
        dash = self._makeApp()
        db = self._makeDb()

        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get('/import')

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'too large', resp.data)


if __name__ == "__main__":
    unittest.main()
