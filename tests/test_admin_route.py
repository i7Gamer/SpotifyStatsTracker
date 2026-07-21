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
        patches = [
            patch.object(dash.repo, 'getGlobalDatabaseStats', return_value=self._MOCK_STATS),
            patch.object(dash.repo, 'getAllUsersDetails', return_value=self._MOCK_USERS if users is None else users),
            patch.object(dash.repo, 'isAdmin', return_value=isAdmin),
            patch.object(dash.repo, 'getPlaysCount', return_value=123),
            patch.object(dash.repo, 'getSkipCount', return_value=7),
            patch.object(dash.repo, 'getAdminUsernames', return_value=['alice']),
            patch.object(dash, 'is_user_logged_in', return_value=loggedIn),
            patch.object(dash, 'get_username_for_email', return_value='alice'),
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

    def test_does_not_start_listener_for_credential_less_users(self):
        """get_user_db() constructs a live Database (starts the listener,
        auto-importer, and background worker threads) - it must never be
        called just to render a row for a user with neither cookies nor a
        Last.fm key configured."""
        dash = self._makeApp()
        users = [
            {"username": "alice", "email": "alice@example.com",
             "cookies_json": '{"sp_dc": "123"}',
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": True},
            {"username": "orphan", "email": "orphan@example.com",
             "cookies_json": None,
             "spotify_client_id": None, "spotify_refresh_token": None,
             "lastfm_api_key": None, "created_at": None, "is_admin": False},
        ]
        patches = [p for p in self._patches(dash, isAdmin=True, users=users) if p.attribute != 'get_user_db']

        with contextlib.ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            stack.enter_context(patch.object(dash, 'get_user_db', return_value=self._makeDb()))
            client = dash.app.test_client()
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get("/admin")
            calledUsernames = [call.args[0] for call in dash.get_user_db.call_args_list]

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"orphan", resp.data)
        self.assertNotIn("orphan", calledUsernames)
        self.assertIn("alice", calledUsernames)


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


class TestAdminRefreshLastfmEntity(AdminRouteTestBase):
    """/admin/lastfm/refresh/<kind>/<entity_id> - the detail pages' "Refresh
    Last.fm Data" button. Database.refreshLastfmEntity itself is covered by
    tests/test_lastfm_refresh_entity.py; this only exercises the route's
    admin gating and its status -> redirect/message mapping."""

    def _postRefresh(self, dash, kind, entity_id, isAdmin=True, loggedIn=True, db=None, data=None):
        with patch.object(dash.repo, 'isAdmin', return_value=isAdmin), \
             patch.object(dash, 'is_user_logged_in', return_value=loggedIn), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db or self._makeDb()):
            client = dash.app.test_client()
            if loggedIn:
                with client.session_transaction() as sess:
                    sess['email'] = 'alice@example.com'
                    sess['username'] = 'alice'
            return client.post(f"/admin/lastfm/refresh/{kind}/{entity_id}", data=data or {})

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
