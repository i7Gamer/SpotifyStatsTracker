"""The /admin page: every admin-only setting/view relocated off /overview -
the full users table (with per-account admin promote/demote), the 8 feature/
backfill toggles split into 3 forms (user, Last.fm, Spotify), and the
read-only Instance Insights section."""
import contextlib
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from _app_factory import AppTestCase

_INSIGHTS_PATCHES = {
    "getCatalogGenreCoverage": {
        "song": {"covered": 0, "total": 0, "percent": 0.0},
        "album": {"covered": 0, "total": 0, "percent": 0.0},
        "artist": {"covered": 0, "total": 0, "percent": 0.0},
        "overall": {"percent": 0.0},
    },
    "getCatalogBiographyCoverage": {
        "artist": {"covered": 0, "total": 0}, "album": {"covered": 0, "total": 0},
    },
    "getRecentRegistrationCounts": {"last_7_days": 0, "last_30_days": 0},
    "getInstanceShareCounts": {"pending": 0, "accepted": 0},
    "getActiveShareLinksCount": 0,
}


class AdminRouteTestBase(AppTestCase):
    _MOCK_STATS = {"tracks": 10, "artists": 5, "albums": 3, "plays": 100,
                   "total_time_ms": 36000000, "db_size_bytes": 1048576}

    _MOCK_USERS = [
        {
            "username": "alice", "email": "alice@example.com",
            "cookies_json": '{"sp_dc": "123"}',
            "spotify_client_id": "client_id", "spotify_refresh_token": "refresh_token",
            "lastfm_api_key": "enc:v1:something",
            "created_at": 1718000000.0, "is_admin": True,
        },
        {
            "username": "bob", "email": "bob@example.com",
            "cookies_json": '{"sp_dc": "456"}',
            "spotify_client_id": None, "spotify_refresh_token": None,
            "lastfm_api_key": None,
            "created_at": 1718000001.0, "is_admin": False,
        },
    ]

    def _makeDb(self):
        db = MagicMock()
        db.getListenerHealth.return_value = {"status": "HEALTHY", "error_count": 0,
                                             "last_error": None, "seconds_since_last_poll": 5}
        db.getLastfmWorkerStatus.return_value = {"configured": True, "running": True}
        return db

    def _patches(self, dash, isAdmin, users=None, loggedIn=True, extraInsights=None, userDb=None):
        insights = dict(_INSIGHTS_PATCHES)
        if extraInsights:
            insights.update(extraInsights)
        effectiveUsers = self._MOCK_USERS if users is None else users
        # adminPage()'s per-user row now reads dashboard.user_databases (an
        # already-active session) instead of calling get_user_db() - populate
        # it here to simulate every configured user already having a live
        # session, matching this fixture's previous (pre-fix) behavior where
        # get_user_db() was called unconditionally for them.
        dash.user_databases = {
            u["username"]: userDb or self._makeDb()
            for u in effectiveUsers
            if u.get("cookies_json") or u.get("lastfm_api_key")
        }
        patches = [
            patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=self._MOCK_STATS),
            patch.object(dash.repo, 'getAllUsersDetails', return_value=effectiveUsers),
            patch.object(dash.repo, 'isAdmin', return_value=isAdmin),
            patch.object(dash.repo, 'getPlayAndSkipCountsByUser',
                         return_value={u["username"]: {"plays": 123, "skips": 7} for u in effectiveUsers}),
            patch.object(dash.repo, 'getAdminUsernames', return_value=['alice']),
            patch.object(dash, 'is_user_logged_in', return_value=loggedIn),
            patch.object(dash, 'get_username_for_email', return_value='alice'),
            # Still needed: get_current_user_or_redirect() calls this once for
            # the acting admin's own session (e.g. to resolve db.tz) - unrelated
            # to the per-row lookup above.
            patch.object(dash, 'get_user_db', return_value=userDb or self._makeDb()),
        ]
        for name, value in insights.items():
            patches.append(patch.object(dash.repo, name, return_value=value))
        return patches

    def _getAdmin(self, dash, isAdmin=True, users=None, loggedIn=True, extraInsights=None, patches=None):
        patches = patches if patches is not None else self._patches(
            dash, isAdmin, users=users, loggedIn=loggedIn, extraInsights=extraInsights)
        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
            return client.get("/admin")

    def _post(self, dash, path, isAdmin, data, loggedIn=True):
        with patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash, 'is_user_logged_in', return_value=loggedIn), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=self._makeDb()):
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                    sess['username'] = 'alice'
            return client.post(path, data=data)


class TestAdminPageAuthGate(AdminRouteTestBase):
    def test_anonymous_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_non_admin_gets_403(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=False)
        self.assertEqual(resp.status_code, 403)

    def test_admin_gets_200(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Registered Users & Sync Status", resp.data)


class TestAdminUsersTable(AdminRouteTestBase):
    def test_shows_every_user(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        self.assertIn(b"alice", resp.data)
        self.assertIn(b"bob", resp.data)
        self.assertIn(b"HEALTHY", resp.data)
        self.assertIn(b"123", resp.data)

    def test_shows_total_skips_column(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        self.assertIn(b"Total Skips", resp.data)
        self.assertIn(b"7", resp.data)

    def test_shows_needs_reauth_badge_instead_of_configured(self):
        """A user whose stored refresh token was confirmed to lack the
        recently-played scope (users.spotify_needs_reauth) must show as
        distinctly needing re-authorization, not as a healthy 'Configured' -
        otherwise nothing on /admin distinguishes them from an account that's
        actually fine."""
        dash = self._makeApp()
        users = [dict(self._MOCK_USERS[0], spotify_needs_reauth=True, lastfm_api_key=None)]
        resp = self._getAdmin(dash, isAdmin=True, users=users)
        body = resp.data.decode()
        self.assertIn("NEEDS RE-AUTH", body)
        self.assertNotIn(">CONFIGURED<", body)   #< only the Last.fm column would say it, and it's unconfigured here

    def test_configured_user_without_reauth_flag_still_shows_configured(self):
        dash = self._makeApp()
        users = [dict(self._MOCK_USERS[0], spotify_needs_reauth=False, lastfm_api_key=None)]
        resp = self._getAdmin(dash, isAdmin=True, users=users)
        body = resp.data.decode()
        self.assertIn(">CONFIGURED<", body)
        self.assertNotIn("NEEDS RE-AUTH", body)

    def test_headers_are_relabeled(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        body = resp.data.decode()
        self.assertIn("Spotify API Backfill", body)
        self.assertIn("Last.fm API Backfill", body)

    def test_disabled_toggle_adds_qualifier_to_header(self):
        dash = self._makeApp()
        dash.repo.setSpotifyApiBackfillEnabled(False)
        resp = self._getAdmin(dash, isAdmin=True)
        body = resp.data.decode()
        self.assertIn("Spotify API Backfill", body)
        self.assertIn("(disabled)", body)

    def test_last_user_row_has_no_bottom_border(self):
        dash = self._makeApp()
        resp = self._getAdmin(dash, isAdmin=True)
        body = resp.data.decode()
        aliceRowStart = body.find("<tr", body.find(">alice<") - 200)
        bobRowStart = body.find("<tr", body.find(">bob<") - 200)
        aliceRow = body[aliceRowStart:body.find(">alice<")]
        bobRow = body[bobRowStart:body.find(">bob<")]
        self.assertIn("border-bottom", aliceRow)
        self.assertNotIn("border-bottom", bobRow)

    def test_never_activates_a_database_for_other_users_rows(self):
        """get_user_db() constructs a live Database (starts the listener,
        auto-importer, and background worker threads, including a live
        Spotify poll). Rendering the users table must never call it for any
        row - not bob's, not orphan's, not even alice's own row as a table
        entry - since doing so would silently activate a live session for
        every configured user on every /admin view. The single legitimate
        call is get_current_user_or_redirect()'s own resolution of the
        acting admin's session, which happens exactly once regardless of
        how many rows the table has."""
        dash = self._makeApp()
        users = [
            {"username": "alice", "email": "alice@example.com",
             "cookies_json": '{"sp_dc": "123"}',
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": True},
            {"username": "bob", "email": "bob@example.com",
             "cookies_json": '{"sp_dc": "456"}',
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": False},
            {"username": "orphan", "email": "orphan@example.com",
             "cookies_json": None,
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": False},
        ]
        patches = self._patches(dash, isAdmin=True, users=users)

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get("/admin")
            callCount = dash.get_user_db.call_count
            calledUsernames = [call.args[0] for call in dash.get_user_db.call_args_list]

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"bob", resp.data)
        self.assertIn(b"orphan", resp.data)
        self.assertEqual(callCount, 1)
        self.assertEqual(calledUsernames, ["alice"])

    def test_configured_but_inactive_user_shows_inactive_not_healthy(self):
        """A user with cookies configured but no entry in
        dashboard.user_databases (no live session currently running in this
        process) must be reported as Inactive - distinct from both a real
        HEALTHY session and a genuinely unconfigured account."""
        dash = self._makeApp()
        users = [
            {"username": "alice", "email": "alice@example.com",
             "cookies_json": '{"sp_dc": "123"}',
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": True},
            {"username": "bob", "email": "bob@example.com",
             "cookies_json": '{"sp_dc": "456"}',
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": False},
        ]
        patches = self._patches(dash, isAdmin=True, users=users)
        # bob has credentials configured but no active session this process.
        dash.user_databases = {}

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get("/admin")
            body = resp.data.decode()

        self.assertEqual(resp.status_code, 200)
        self.assertIn("INACTIVE", body)
        self.assertNotIn(b"HEALTHY", resp.data)


class TestAdminUserSettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/user_settings", isAdmin=False,
                          data={"data_sharing": "1", "registration": "1", "share_links": "1"})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/user_settings", isAdmin=True, data={}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_toggle_all_three(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isDataSharingEnabled())
        self.assertTrue(dash.repo.isRegistrationEnabled())
        self.assertTrue(dash.repo.isShareLinksEnabled())

        resp = self._post(dash, "/admin/user_settings", isAdmin=True, data={})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertFalse(dash.repo.isDataSharingEnabled())
        self.assertFalse(dash.repo.isRegistrationEnabled())
        self.assertFalse(dash.repo.isShareLinksEnabled())

        resp = self._post(dash, "/admin/user_settings", isAdmin=True,
                          data={"data_sharing": "1", "registration": "1", "share_links": "1"})
        self.assertTrue(dash.repo.isDataSharingEnabled())
        self.assertTrue(dash.repo.isRegistrationEnabled())
        self.assertTrue(dash.repo.isShareLinksEnabled())

    def test_does_not_touch_lastfm_or_spotify_settings(self):
        dash = self._makeApp()
        self._post(dash, "/admin/user_settings", isAdmin=True, data={})
        self.assertTrue(dash.repo.isSpotifyApiBackfillEnabled())
        self.assertTrue(dash.repo.isLastfmGenreBackfillEnabled())
        self.assertTrue(dash.repo.isArtistBioEnabled())
        self.assertTrue(dash.repo.isAlbumBioEnabled())

    def test_toggles_email_verification(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isEmailVerificationEnabled())
        self._post(dash, "/admin/user_settings", isAdmin=True, data={})   #< nothing checked -> disabled
        self.assertFalse(dash.repo.isEmailVerificationEnabled())
        self._post(dash, "/admin/user_settings", isAdmin=True, data={"email_verification": "1"})
        self.assertTrue(dash.repo.isEmailVerificationEnabled())

    def test_toggles_milestones(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isMilestonesEnabled())
        self._post(dash, "/admin/user_settings", isAdmin=True, data={})   #< nothing checked -> disabled
        self.assertFalse(dash.repo.isMilestonesEnabled())
        self._post(dash, "/admin/user_settings", isAdmin=True, data={"milestones": "1"})
        self.assertTrue(dash.repo.isMilestonesEnabled())

    def test_toggles_milestone_recalc(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isMilestoneRecalcEnabled())   #< absent row = enabled
        self._post(dash, "/admin/user_settings", isAdmin=True, data={})   #< nothing checked -> disabled
        self.assertFalse(dash.repo.isMilestoneRecalcEnabled())
        self._post(dash, "/admin/user_settings", isAdmin=True, data={"milestone_recalc": "1"})
        self.assertTrue(dash.repo.isMilestoneRecalcEnabled())


class TestAdminMilestoneWorkerHealth(AdminRouteTestBase):
    """The Worker Health panel's Milestone Detection entry. The milestone pass
    has no thread of its own - it rides the periodic login-check loop - so its
    health is that hosting thread's: RUNNING while alive, INACTIVE otherwise,
    DISABLED when the admin kill switch turns the whole feature off, plus a
    warning badge when the import-hygiene auto-recalc toggle is off."""

    def _milestoneSection(self, resp):
        """The Milestone Detection badge markup only - RUNNING/INACTIVE also
        appear in other Worker Health sections, so assertions must scope to
        this section's marker id."""
        self.assertIn(b'id="milestoneWorkerStatus"', resp.data)
        return resp.data.split(b'id="milestoneWorkerStatus"')[1][:300]

    def _makeAppWithLoopThread(self):
        dash = self._makeApp()
        dash._checkLoginThread = MagicMock()
        dash._checkLoginThread.is_alive.return_value = True
        return dash

    def test_inactive_without_the_login_check_loop(self):
        # The test app never starts checkLogin_thread, so the hosting loop
        # thread is absent - the panel must say so instead of implying health.
        dash = self._makeApp()
        resp = self._getAdmin(dash)
        self.assertIn(b"Milestone Detection", resp.data)
        self.assertIn(b"INACTIVE", self._milestoneSection(resp))

    def test_running_with_a_live_loop_thread(self):
        dash = self._makeAppWithLoopThread()
        resp = self._getAdmin(dash)
        section = self._milestoneSection(resp)
        self.assertIn(b"RUNNING", section)
        self.assertNotIn(b"AUTO-RECALC OFF", section)

    def test_disabled_by_the_kill_switch(self):
        dash = self._makeAppWithLoopThread()
        dash.repo.setMilestonesEnabled(False)
        resp = self._getAdmin(dash)
        section = self._milestoneSection(resp)
        self.assertIn(b"DISABLED", section)
        self.assertNotIn(b"RUNNING", section)
        self.assertNotIn(b"AUTO-RECALC OFF", section)   #< moot while the whole feature is off

    def test_warns_when_auto_recalc_is_off(self):
        dash = self._makeAppWithLoopThread()
        dash.repo.setMilestoneRecalcEnabled(False)
        resp = self._getAdmin(dash)
        section = self._milestoneSection(resp)
        self.assertIn(b"RUNNING", section)
        self.assertIn(b"AUTO-RECALC OFF", section)


class TestAdminSkipSettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/skip_settings", isAdmin=False,
                          data={"skip_mode": "seconds", "skip_value": "30"})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/skip_settings", isAdmin=True,
                          data={"skip_mode": "seconds", "skip_value": "30"}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_set_seconds_threshold(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/skip_settings", isAdmin=True,
                          data={"skip_mode": "seconds", "skip_value": "30"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertEqual(dash.repo.getSkipThreshold(), ("seconds", 30))

    def test_admin_can_set_percent_threshold(self):
        dash = self._makeApp()
        self._post(dash, "/admin/skip_settings", isAdmin=True,
                   data={"skip_mode": "percent", "skip_value": "20"})
        self.assertEqual(dash.repo.getSkipThreshold(), ("percent", 20))

    def test_value_is_clamped_to_mode_bounds(self):
        dash = self._makeApp()
        self._post(dash, "/admin/skip_settings", isAdmin=True,
                   data={"skip_mode": "seconds", "skip_value": "999"})
        self.assertEqual(dash.repo.getSkipThreshold(), ("seconds", 60))

    def test_invalid_value_redirects_with_error(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/skip_settings", isAdmin=True,
                          data={"skip_mode": "seconds", "skip_value": "abc"})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])

    def test_saving_recomputes_skip_flags(self):
        dash = self._makeApp()
        with patch.object(dash.repo, "recomputeSkipFlags") as recompute:
            self._post(dash, "/admin/skip_settings", isAdmin=True,
                       data={"skip_mode": "seconds", "skip_value": "30"})
            recompute.assert_called_once()

    def test_saves_completion_percent(self):
        dash = self._makeApp()
        self._post(dash, "/admin/skip_settings", isAdmin=True,
                   data={"skip_mode": "seconds", "skip_value": "5", "completion_complete_percent": "70"})
        self.assertEqual(dash.repo.getCompletionCompletePercent(), 70)


class TestAdminTuningSettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/tuning_settings", isAdmin=False,
                          data={"discover_artist_limit": "10"})
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_set_discover_limit(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/tuning_settings", isAdmin=True,
                          data={"discover_artist_limit": "12"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(dash.repo.getDiscoverArtistLimit(5), 12)

    def test_worker_counts_are_clamped(self):
        dash = self._makeApp()
        self._post(dash, "/admin/tuning_settings", isAdmin=True,
                   data={"image_download_workers": "999"})
        self.assertEqual(dash.repo.getImageDownloadWorkers(5), 32)

    def test_blank_field_is_left_unchanged(self):
        dash = self._makeApp()
        dash.repo.setIntSetting("discover_artist_limit", 8, 1, 25)
        self._post(dash, "/admin/tuning_settings", isAdmin=True,
                   data={"discover_artist_limit": ""})
        self.assertEqual(dash.repo.getDiscoverArtistLimit(5), 8)


class TestAdminRestart(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/restart", isAdmin=False, data={})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/restart", isAdmin=True, data={}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_disabled_by_default_does_not_schedule_exit(self):
        dash = self._makeApp()
        with patch("threading.Timer") as timer, patch.dict(os.environ, clear=False):
            os.environ.pop("ALLOW_INSTANCE_RESTART", None)
            resp = self._post(dash, "/admin/restart", isAdmin=True, data={})
        self.assertEqual(resp.status_code, 302)
        self.assertIn("error=", resp.headers["Location"])
        timer.assert_not_called()

    def test_enabled_schedules_graceful_exit(self):
        # threading.Timer is mocked, so the scheduled shutdown+os._exit never
        # fires - the test only asserts the exit was scheduled.
        dash = self._makeApp()
        with patch("threading.Timer") as timer, \
             patch.dict(os.environ, {"ALLOW_INSTANCE_RESTART": "1"}):
            resp = self._post(dash, "/admin/restart", isAdmin=True, data={})
        self.assertEqual(resp.status_code, 302)
        timer.assert_called_once()


class TestAdminLastfmSettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/lastfm_settings", isAdmin=False, data={})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/lastfm_settings", isAdmin=True, data={}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_toggle_all_four(self):
        dash = self._makeApp()

        resp = self._post(dash, "/admin/lastfm_settings", isAdmin=True, data={})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertFalse(dash.repo.isLastfmGenreBackfillEnabled())
        self.assertFalse(dash.repo.isArtistBioEnabled())
        self.assertFalse(dash.repo.isAlbumBioEnabled())
        self.assertFalse(dash.repo.isInheritedGenresEnabled())

        resp = self._post(dash, "/admin/lastfm_settings", isAdmin=True, data={
            "lastfm_backfill": "1", "artist_bio": "1", "album_bio": "1", "include_inherited": "1",
        })
        self.assertTrue(dash.repo.isLastfmGenreBackfillEnabled())
        self.assertTrue(dash.repo.isArtistBioEnabled())
        self.assertTrue(dash.repo.isAlbumBioEnabled())
        self.assertTrue(dash.repo.isInheritedGenresEnabled())

    def test_does_not_touch_user_or_spotify_settings(self):
        dash = self._makeApp()
        self._post(dash, "/admin/lastfm_settings", isAdmin=True, data={})
        self.assertTrue(dash.repo.isDataSharingEnabled())
        self.assertTrue(dash.repo.isSpotifyApiBackfillEnabled())

    def test_saves_backfill_retry_days(self):
        dash = self._makeApp()
        self._post(dash, "/admin/lastfm_settings", isAdmin=True,
                   data={"genre_backfill_retry_days": "14", "bio_backfill_retry_days": "60"})
        self.assertEqual(dash.repo.getGenreBackfillRetryDays(), 14)
        self.assertEqual(dash.repo.getBioBackfillRetryDays(), 60)


class TestAdminBackupSettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/backup_settings", isAdmin=False, data={"backup_interval_hours": "12"})
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_set_interval_and_retention(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/backup_settings", isAdmin=True,
                          data={"backup_interval_hours": "12", "backup_retention_count": "10"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(dash.repo.getBackupIntervalHours(24), 12)
        self.assertEqual(dash.repo.getBackupRetentionCount(7), 10)

    def test_zero_disables(self):
        dash = self._makeApp()
        self._post(dash, "/admin/backup_settings", isAdmin=True, data={"backup_interval_hours": "0"})
        self.assertEqual(dash.repo.getBackupIntervalHours(24), 0)


class TestAdminCreateBackup(AdminRouteTestBase):
    def _postBackup(self, dash, isAdmin=True, loggedIn=True, backupWorker=None, headers=None):
        from pathlib import Path
        with patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash, 'is_user_logged_in', return_value=loggedIn), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=self._makeDb()):
            dash.backupWorker = backupWorker
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                    sess['username'] = 'alice'
            return client.post("/admin/create_backup", headers=headers or {})

    def test_unauthenticated_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._postBackup(dash, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._postBackup(dash, isAdmin=False)
        self.assertEqual(resp.status_code, 403)

    def test_ajax_success_returns_json(self):
        from pathlib import Path
        dash = self._makeApp()
        mock_worker = MagicMock()
        mock_worker.runBackup.return_value = Path("/fake/Backups/spotify_stats_backup_20260724_120000.db")
        resp = self._postBackup(dash, backupWorker=mock_worker, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertEqual(payload["kind"], "success")
        self.assertIn("spotify_stats_backup_20260724_120000.db", payload["message"])
        mock_worker.runBackup.assert_called_once()

    def test_form_success_redirects_with_message(self):
        from pathlib import Path
        dash = self._makeApp()
        mock_worker = MagicMock()
        mock_worker.runBackup.return_value = Path("/fake/Backups/spotify_stats_backup_20260724_120000.db")
        resp = self._postBackup(dash, backupWorker=mock_worker)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertIn("message=", resp.headers["Location"])

    def test_runs_even_when_scheduler_disabled(self):
        from pathlib import Path
        dash = self._makeApp()
        mock_worker = MagicMock()
        mock_worker.isEnabled.return_value = False
        mock_worker.runBackup.return_value = Path("/fake/Backups/spotify_stats_backup_20260724_120000.db")
        resp = self._postBackup(dash, backupWorker=mock_worker, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(resp.status_code, 200)
        mock_worker.runBackup.assert_called_once()

    def test_ajax_error_returns_json_200(self):
        dash = self._makeApp()
        mock_worker = MagicMock()
        mock_worker.runBackup.side_effect = RuntimeError("disk full")
        resp = self._postBackup(dash, backupWorker=mock_worker, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["kind"], "error")
        self.assertIn("disk full", payload["message"])

    def test_form_error_redirects_with_error_param(self):
        dash = self._makeApp()
        mock_worker = MagicMock()
        mock_worker.runBackup.side_effect = RuntimeError("disk full")
        resp = self._postBackup(dash, backupWorker=mock_worker)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertIn("error=", resp.headers["Location"])

    def test_missing_backup_worker_returns_error(self):
        dash = self._makeApp()
        resp = self._postBackup(dash, backupWorker=None, headers={"X-Requested-With": "XMLHttpRequest"})
        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["kind"], "error")
        self.assertIn("not available", payload["message"])



class TestAdminRefreshLastfmEntity(AdminRouteTestBase):
    """/admin/lastfm/refresh/<kind>/<entity_id> - the detail pages' "Refresh
    Last.fm Data" button. Database.refreshLastfmEntity itself is covered by
    tests/test_lastfm_refresh_entity.py; this only exercises the route's
    admin gating and its status -> redirect/message mapping."""

    def _postRefresh(self, dash, kind, entity_id, isAdmin=True, loggedIn=True, db=None, data=None, headers=None):
        with patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash, 'is_user_logged_in', return_value=loggedIn), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db or self._makeDb()):
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                    sess['username'] = 'alice'
            return client.post(f"/admin/lastfm/refresh/{kind}/{entity_id}", data=data or {},
                               headers=headers or {})

    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._postRefresh(dash, "artist", "aX", isAdmin=False)
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._postRefresh(dash, "artist", "aX", loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_unknown_kind_is_404(self):
        dash = self._makeApp()
        resp = self._postRefresh(dash, "playlist", "aX")
        self.assertEqual(resp.status_code, 404)

    def test_artist_success_redirects_with_success_message_and_group_by(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.refreshLastfmEntity.return_value = {"status": "ok", "name": "Artist X"}
        resp = self._postRefresh(dash, "artist", "aX", db=db, data={"groupBy": "month"})
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertIn("/artist/aX", location)
        self.assertIn("success=", location)
        self.assertIn("groupBy=month", location)
        db.refreshLastfmEntity.assert_called_once_with("artist", "aX")

    def test_album_error_status_redirects_with_error_message(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.refreshLastfmEntity.return_value = {"status": "no_artist"}
        resp = self._postRefresh(dash, "album", "alP", db=db)
        self.assertEqual(resp.status_code, 302)
        location = resp.headers["Location"]
        self.assertIn("/album/alP", location)
        self.assertIn("error=", location)

    def test_track_kind_redirects_to_the_song_page(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.refreshLastfmEntity.return_value = {"status": "ok", "name": "Song A"}
        resp = self._postRefresh(dash, "track", "tA", db=db)
        self.assertIn("/song/tA", resp.headers["Location"])

    def test_ajax_post_returns_json_instead_of_redirecting(self):
        """The detail pages submit the form via fetch (admin-refresh.js) so a
        refresh doesn't navigate away and reset tab/sort/page state - the
        route answers XHR posts with the message JSON instead of a redirect."""
        dash = self._makeApp()
        db = self._makeDb()
        db.refreshLastfmEntity.return_value = {"status": "ok", "name": "Artist X"}

        resp = self._postRefresh(dash, "artist", "aX", db=db,
                                 headers={"X-Requested-With": "XMLHttpRequest"})

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertEqual(payload["kind"], "success")
        self.assertIn("Artist X", payload["message"])

    def test_ajax_post_returns_error_kind_for_error_statuses(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.refreshLastfmEntity.return_value = {"status": "transient"}

        resp = self._postRefresh(dash, "album", "alP", db=db,
                                 headers={"X-Requested-With": "XMLHttpRequest"})

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertEqual(payload["kind"], "error")
        self.assertIn("didn't respond", payload["message"])


class TestAdminSpotifySettings(AdminRouteTestBase):
    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/spotify_settings", isAdmin=False, data={"spotify_backfill": "1"})
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._post(dash, "/admin/spotify_settings", isAdmin=True, data={}, loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_toggle_it(self):
        dash = self._makeApp()
        self.assertTrue(dash.repo.isSpotifyApiBackfillEnabled())

        resp = self._post(dash, "/admin/spotify_settings", isAdmin=True, data={})

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertFalse(dash.repo.isSpotifyApiBackfillEnabled())

        resp = self._post(dash, "/admin/spotify_settings", isAdmin=True, data={"spotify_backfill": "1"})
        self.assertTrue(dash.repo.isSpotifyApiBackfillEnabled())


class TestAdminManageAdmins(AdminRouteTestBase):
    def _postSetAdmin(self, dash, username, isAdmin, makeAdmin, adminUsernames, loggedIn=True):
        with patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash.repo, 'getAdminUsernames', return_value=adminUsernames), \
             patch.object(dash, 'is_user_logged_in', return_value=loggedIn), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=self._makeDb()):
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                    sess['username'] = 'alice'
            return client.post(f"/admin/users/{username}/admin",
                               data={"make_admin": "1" if makeAdmin else "0"})

    def test_non_admin_post_is_forbidden(self):
        dash = self._makeApp()
        resp = self._postSetAdmin(dash, "bob", isAdmin=False, makeAdmin=True, adminUsernames=["alice"])
        self.assertEqual(resp.status_code, 403)

    def test_anonymous_post_redirects_to_login(self):
        dash = self._makeApp()
        resp = self._postSetAdmin(dash, "bob", isAdmin=True, makeAdmin=True, adminUsernames=["alice"], loggedIn=False)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])

    def test_admin_can_promote_another_user(self):
        dash = self._makeApp()
        dash.repo.upsertUser("bob", "bob@example.com")
        self.assertFalse(dash.repo.isAdmin("bob"))

        resp = self._postSetAdmin(dash, "bob", isAdmin=True, makeAdmin=True, adminUsernames=["alice"])

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertTrue(dash.repo.isAdmin("bob"))

    def test_admin_can_demote_another_admin_when_not_the_last_one(self):
        dash = self._makeApp()
        dash.repo.upsertUser("bob", "bob@example.com")
        dash.repo.setUserAdmin("bob", True)

        resp = self._postSetAdmin(dash, "bob", isAdmin=True, makeAdmin=False, adminUsernames=["alice", "bob"])

        self.assertEqual(resp.status_code, 302)
        self.assertFalse(dash.repo.isAdmin("bob"))

    def test_cannot_demote_the_last_admin(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.setUserAdmin("alice", True)

        resp = self._postSetAdmin(dash, "alice", isAdmin=True, makeAdmin=False, adminUsernames=["alice"])

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/admin", resp.headers["Location"])
        self.assertIn("error=", resp.headers["Location"])
        self.assertTrue(dash.repo.isAdmin("alice"), "the last admin must stay an admin")


class TestAdminInsights(AdminRouteTestBase):
    def test_renders_catalog_coverage_worker_health_and_activity(self):
        dash = self._makeApp()
        extra = {
            "getCatalogGenreCoverage": {
                "song": {"covered": 5, "total": 10, "percent": 50.0},
                "album": {"covered": 5, "total": 10, "percent": 50.0},
                "artist": {"covered": 5, "total": 10, "percent": 50.0},
                "overall": {"percent": 50.0},
            },
            "getRecentRegistrationCounts": {"last_7_days": 2, "last_30_days": 9},
            "getInstanceShareCounts": {"pending": 3, "accepted": 4},
            "getActiveShareLinksCount": 6,
        }

        resp = self._getAdmin(dash, isAdmin=True, extraInsights=extra)
        body = resp.data.decode()

        self.assertIn("Catalog Backfill Coverage", body)
        self.assertIn("50.0%", body)
        self.assertIn("Worker Health", body)
        self.assertIn("RUNNING:", body)
        self.assertIn("Activity", body)
        self.assertIn("2", body)   # last_7_days
        self.assertIn("9", body)  # last_30_days


if __name__ == "__main__":
    unittest.main()
