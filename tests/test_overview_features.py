"""The admin-only feature toggles panel on /overview: Spotify API backfill,
Last.fm genre backfill, data sharing, new user registration and public
Wrapped share links - one form, one route (POST /overview/feature_settings),
mirroring the existing inherited-genres toggle's shape."""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'

_ALL_ENABLED_FORM = {
    "spotify_backfill": "1", "lastfm_backfill": "1",
    "data_sharing": "1", "registration": "1", "share_links": "1",
}


class OverviewFeaturesTestBase(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    _MOCK_STATS = {"tracks": 10, "artists": 5, "albums": 3, "plays": 100,
                   "total_time_ms": 36000000, "db_size_bytes": 1048576}

    def _makeDb(self):
        db = MagicMock()
        db.getListenerHealth.return_value = {"status": "HEALTHY", "error_count": 0,
                                             "last_error": None, "seconds_since_last_poll": 5}
        return db

    def _getOverview(self, dash, isAdmin=False):
        with patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=self._MOCK_STATS), \
             patch.object(dash.repo, 'getAllUsersDetails', return_value=[]), \
             patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=self._makeDb()):
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get("/overview")

    def _post(self, dash, isAdmin, data, loggedIn=True):
        with patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash, 'is_user_logged_in', return_value=loggedIn), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=self._makeDb()):
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                    sess['username'] = 'alice'
            return client.post("/overview/feature_settings", data=data)


class TestFeatureSettingsForm(OverviewFeaturesTestBase):
    def test_form_is_admin_only(self):
        dash = self._makeApp()
        respAdmin = self._getOverview(dash, isAdmin=True)
        self.assertIn(b"feature_settings", respAdmin.data)
        respUser = self._getOverview(dash, isAdmin=False)
        self.assertNotIn(b"feature_settings", respUser.data)

    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, isAdmin=False, data=_ALL_ENABLED_FORM)
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, isAdmin=True, data=_ALL_ENABLED_FORM, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_disable_all_five_in_one_submit(self):
        dash = self._makeApp()
        for isEnabled in (dash.repo.isSpotifyApiBackfillEnabled, dash.repo.isLastfmGenreBackfillEnabled,
                          dash.repo.isDataSharingEnabled, dash.repo.isRegistrationEnabled,
                          dash.repo.isShareLinksEnabled):
            self.assertTrue(isEnabled())

        resp = self._post(dash, isAdmin=True, data={})   #< every checkbox unchecked = disable all

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/overview", resp.headers["Location"])
        self.assertFalse(dash.repo.isSpotifyApiBackfillEnabled())
        self.assertFalse(dash.repo.isLastfmGenreBackfillEnabled())
        self.assertFalse(dash.repo.isDataSharingEnabled())
        self.assertFalse(dash.repo.isRegistrationEnabled())
        self.assertFalse(dash.repo.isShareLinksEnabled())

    def test_admin_can_re_enable_a_subset(self):
        dash = self._makeApp()
        dash.repo.setSpotifyApiBackfillEnabled(False)
        dash.repo.setLastfmGenreBackfillEnabled(False)
        dash.repo.setDataSharingEnabled(False)
        dash.repo.setRegistrationEnabled(False)
        dash.repo.setShareLinksEnabled(False)

        self._post(dash, isAdmin=True, data={"lastfm_backfill": "1", "data_sharing": "1"})

        self.assertFalse(dash.repo.isSpotifyApiBackfillEnabled())
        self.assertTrue(dash.repo.isLastfmGenreBackfillEnabled())
        self.assertTrue(dash.repo.isDataSharingEnabled())
        self.assertFalse(dash.repo.isRegistrationEnabled())
        self.assertFalse(dash.repo.isShareLinksEnabled())

    def test_checkboxes_reflect_current_state(self):
        dash = self._makeApp()
        dash.repo.setDataSharingEnabled(False)

        resp = self._getOverview(dash, isAdmin=True)
        body = resp.data.decode()

        self.assertIn('name="spotify_backfill" value="1" checked', body)
        self.assertIn('name="lastfm_backfill" value="1" checked', body)
        self.assertIn('name="registration" value="1" checked', body)
        self.assertIn('name="share_links" value="1" checked', body)
        # data_sharing is disabled: its checkbox must be the unchecked one.
        dataSharingInput = body[body.find('name="data_sharing"'):]
        self.assertNotIn("checked", dataSharingInput[:dataSharingInput.find(">")])


class TestUsersTableDisabledQualifier(OverviewFeaturesTestBase):
    """The Registered Users & Sync Status table's API Backfill/Genre Data
    columns stay visible either way (an admin should be able to see who's
    configured regardless of the current toggle) - but a "(disabled)"
    qualifier on the header clarifies the badges below it aren't doing
    anything right now."""

    def test_headers_carry_no_qualifier_when_both_enabled(self):
        dash = self._makeApp()
        resp = self._getOverview(dash, isAdmin=True)
        body = resp.data.decode()
        self.assertNotIn("API Backfill (disabled)", body)
        self.assertNotIn("Genre Data (disabled)", body)

    def test_spotify_backfill_header_gets_the_qualifier(self):
        dash = self._makeApp()
        dash.repo.setSpotifyApiBackfillEnabled(False)
        resp = self._getOverview(dash, isAdmin=True)
        body = resp.data.decode()
        self.assertIn("API Backfill", body)
        self.assertIn("(disabled)", body)
        self.assertNotIn("Genre Data (disabled)", body)

    def test_lastfm_backfill_header_gets_the_qualifier(self):
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        resp = self._getOverview(dash, isAdmin=True)
        body = resp.data.decode()
        self.assertIn("Genre Data", body)
        self.assertIn("(disabled)", body)
        self.assertNotIn("API Backfill (disabled)", body)

    def test_qualifier_shows_for_a_non_admins_own_row_too(self):
        """The table isn't admin-only - a regular user sees their own row and
        the same misleading-badge risk, so the qualifier must render for them
        too, not just when is_admin gates the settings form."""
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        resp = self._getOverview(dash, isAdmin=False)
        body = resp.data.decode()
        self.assertIn("(disabled)", body)


if __name__ == "__main__":
    unittest.main()
