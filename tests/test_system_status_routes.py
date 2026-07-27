"""The three polled status endpoints, plus the import routes' guard branches.

Found by running the suite under coverage: routes/system.py was the
lowest-covered real module (78%), and the untested block was three endpoints that
every page or the import page polls continuously -

  /import-progress    - polled by static/js/import-page.js while an import runs
  /version_status     - fetched by layout-chrome.js on every page load
  /api/listener-status - polled for the topbar's sync-status pill

None of them had a single test. They are small, which is presumably why, but they
are also the kind of endpoint whose 401 shape the client depends on: an
unauthenticated answer that is not the expected JSON leaves a poller retrying
against a response it cannot read, with nothing on screen to say so.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase


class SystemRouteTestCase(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()

    def _db(self):
        db = MagicMock()
        db.readProgress.return_value = {"status": "running", "percentage": 40, "message": "Working"}
        db.getListenerHealth.return_value = {"state": "connected", "lastPlayAt": 123.0}
        return db

    def _get(self, path, loggedIn=True, db=None):
        client = self.dash.app.test_client()
        with patch.object(self.dash, "is_user_logged_in", return_value=loggedIn), \
             patch.object(self.dash, "get_username_for_email", return_value="alice"), \
             patch.object(self.dash, "get_user_db", return_value=db or self._db()):
            if loggedIn:
                with client.session_transaction() as sess:
                    sess["email"] = "alice@example.com"
            return client.get(path)


class TestImportProgress(SystemRouteTestCase):
    def test_it_reports_the_stored_progress(self):
        resp = self._get("/import-progress")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json(), {"status": "running", "percentage": 40, "message": "Working"})

    def test_an_unauthenticated_poll_gets_json_401_not_a_redirect(self):
        """import-page.js reads this with response.json(); a 302 to the login page
        would be followed transparently and parsed as HTML."""
        resp = self._get("/import-progress", loggedIn=False)

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.mimetype, "application/json")


class TestListenerStatus(SystemRouteTestCase):
    def test_it_passes_the_health_payload_through(self):
        resp = self._get("/api/listener-status")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()["state"], "connected")

    def test_an_unauthenticated_poll_gets_json_401(self):
        resp = self._get("/api/listener-status", loggedIn=False)

        self.assertEqual(resp.status_code, 401)
        self.assertEqual(resp.mimetype, "application/json")


class TestVersionStatus(SystemRouteTestCase):
    """Deliberately unauthenticated - it carries no user data, and the badge is
    rendered on the public layout too."""

    def _versionStatus(self, current, latest):
        self.dash.currentVersion = current
        self.dash.latestVersion = latest
        return self.dash.app.test_client().get("/version_status").get_json()

    def test_a_newer_release_is_reported(self):
        self.assertEqual(self._versionStatus("1.45.0", "1.46.0"),
                         {"current": "1.45.0", "latest": "1.46.0"})

    def test_the_same_version_reports_no_update(self):
        self.assertIsNone(self._versionStatus("1.45.0", "1.45.0")["latest"])

    def test_an_older_remote_reports_no_update(self):
        """A yanked release, or a fork ahead of upstream - never advertise a
        downgrade as an update."""
        self.assertIsNone(self._versionStatus("1.45.0", "1.44.0")["latest"])

    def test_never_checked_reports_no_update(self):
        self.assertIsNone(self._versionStatus("1.45.0", None)["latest"])

    def test_an_unparseable_remote_version_is_not_an_update(self):
        """_is_version_newer swallows the comparison error and says "not newer" -
        so a garbled response from the update check shows no badge rather than
        500ing the page that fetches it."""
        payload = self._versionStatus("1.45.0", "not-a-version")

        self.assertIsNone(payload["latest"])
        self.assertEqual(payload["current"], "1.45.0")

    def test_it_needs_no_session(self):
        resp = self.dash.app.test_client().get("/version_status")

        self.assertEqual(resp.status_code, 200)


class TestImportRouteGuards(SystemRouteTestCase):
    def test_the_import_page_redirects_when_not_logged_in(self):
        resp = self._get("/import", loggedIn=False)

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_posting_history_redirects_when_not_logged_in(self):
        client = self.dash.app.test_client()
        with patch.object(self.dash, "is_user_logged_in", return_value=False):
            resp = client.post("/import-history", data={})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_posting_no_files_goes_back_to_the_import_page(self):
        """The form can be submitted empty; that is not an error, and it must not
        start a batch."""
        db = self._db()
        client = self.dash.app.test_client()
        with patch.object(self.dash, "is_user_logged_in", return_value=True), \
             patch.object(self.dash, "get_username_for_email", return_value="alice"), \
             patch.object(self.dash, "get_user_db", return_value=db):
            with client.session_transaction() as sess:
                sess["email"] = "alice@example.com"
            resp = client.post("/import-history", data={},
                               content_type="multipart/form-data")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/import", resp.headers["Location"])
        db.importHistoryBatch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
