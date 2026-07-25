"""A returning user whose Spotify session cookies expired must be able to fix
it by logging in again - without a restart of the whole process. Covers
SpotifyDashboardApp._refresh_user_session() and its use from the /login route
for a username that already has a live Database (get_user_db() is otherwise a
no-op for a username already in self.user_databases, so the stale listener and
cached login-status result would linger until the next process restart).
"""
import time
import unittest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from _app_factory import AppTestCase, makeApp as _makeApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'



class TestRefreshUserSession(unittest.TestCase):
    def test_stops_the_old_listener_and_starts_a_new_one(self):
        dash = _makeApp()
        email, username = "alice@example.com", "alice"
        mockDb = MagicMock()
        oldListener = MagicMock()
        mockDb.listener = oldListener
        dash.user_databases[username] = mockDb

        dash._refresh_user_session(username, email)

        oldListener.stop.assert_called_once()
        mockDb.startListener.assert_called_once_with(email=email)

    def test_handles_a_user_with_no_listener_yet(self):
        dash = _makeApp()
        email, username = "carol@example.com", "carol"
        mockDb = MagicMock()
        mockDb.listener = None
        dash.user_databases[username] = mockDb

        dash._refresh_user_session(username, email)  # must not raise

        mockDb.startListener.assert_called_once_with(email=email)

    def test_clears_the_cached_login_status_for_this_email(self):
        dash = _makeApp()
        email, username = "bob@example.com", "bob"
        mockDb = MagicMock()
        mockDb.listener = None
        dash.user_databases[username] = mockDb
        dash._login_cache[email] = (False, time.monotonic() + 1000)

        dash._refresh_user_session(username, email)

        self.assertNotIn(email, dash._login_cache)

    def test_is_a_noop_for_a_username_with_no_live_database(self):
        dash = _makeApp()
        dash._refresh_user_session("ghost", "ghost@example.com")  # must not raise
        self.assertNotIn("ghost", dash.user_databases)


class TestLoginCheckInFlightDuringReLogin(unittest.TestCase):
    """is_user_logged_in evaluates isListenerLoggedIn() outside any lock - a
    live Spotify round-trip that can take seconds. A check that started against
    the OLD, dead listener must not finish after a successful re-login and
    write its stale False over the freshly cleared cache: that marked a
    just-authenticated user logged out for the whole TTL."""

    def _dash(self, email, username):
        dash = _makeApp()
        dash.repo.upsertUser(username, email)
        dash.repo.setUserCookies(username, {"sp_dc": "cookie"})
        return dash

    def test_a_stale_in_flight_result_is_not_cached_after_a_re_login(self):
        email, username = "alice@example.com", "alice"
        dash = self._dash(email, username)
        deadDb = MagicMock()

        def slowCheckAgainstDeadListener():
            # The re-login lands while this check is still in flight.
            dash._invalidateLoginCache(email)
            return False

        deadDb.isListenerLoggedIn.side_effect = slowCheckAgainstDeadListener

        with patch.object(dash, "get_user_db", return_value=deadDb):
            result = dash.is_user_logged_in(email)

        self.assertFalse(result)                        #< this request's answer still stands
        self.assertNotIn(email, dash._login_cache)      #< but it must not linger for the TTL

    def test_an_uninterrupted_result_is_still_cached(self):
        email, username = "bob@example.com", "bob"
        dash = self._dash(email, username)
        liveDb = MagicMock()
        liveDb.isListenerLoggedIn.return_value = True

        with patch.object(dash, "get_user_db", return_value=liveDb):
            result = dash.is_user_logged_in(email)

        self.assertTrue(result)
        self.assertIn(email, dash._login_cache)

    def test_the_next_check_after_a_re_login_hits_the_new_listener(self):
        """End state that matters to the user: after the invalidation, the
        following request re-checks rather than serving a cached logout."""
        email, username = "carol@example.com", "carol"
        dash = self._dash(email, username)
        db = MagicMock()
        db.isListenerLoggedIn.side_effect = [False, True]

        with patch.object(dash, "get_user_db", return_value=db):
            dash._invalidateLoginCache(email)
            first = dash.is_user_logged_in(email)
            dash._invalidateLoginCache(email)
            second = dash.is_user_logged_in(email)

        self.assertFalse(first)
        self.assertTrue(second)
        self.assertEqual(db.isListenerLoggedIn.call_count, 2)


class TestLoginRestartsExistingSession(AppTestCase):
    """Integration through the actual /login route: a re-login for a username
    that already has a live Database must refresh it instead of silently doing
    nothing (which is what get_user_db() alone would do)."""

    def test_relogin_of_existing_user_calls_refresh_not_get_user_db(self):
        dash = self._makeApp()
        dash.user_databases["alice"] = MagicMock()

        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_or_create_user', return_value='alice'), \
             patch.object(dash, '_refresh_user_session') as mockRefresh, \
             patch.object(dash, 'get_user_db') as mockGetUserDb, \
             patch.object(dash.repo, 'setUserCookies'):
            client = dash.app.test_client()
            resp = client.post("/login", data={"step": "2", "email": "alice@example.com", "cookies": "sp_dc=abc"})

        self.assertEqual(resp.status_code, 302)
        mockRefresh.assert_called_once_with('alice', 'alice@example.com')
        mockGetUserDb.assert_not_called()

    def test_first_time_login_calls_get_user_db_not_refresh(self):
        dash = self._makeApp()

        with patch.object(dash, '_verifyCookiesMatchEmail', return_value=True), \
             patch.object(dash, 'get_or_create_user', return_value='newuser'), \
             patch.object(dash, '_refresh_user_session') as mockRefresh, \
             patch.object(dash, 'get_user_db') as mockGetUserDb, \
             patch.object(dash.repo, 'setUserCookies'):
            client = dash.app.test_client()
            resp = client.post("/login", data={"step": "2", "email": "new@example.com", "cookies": "sp_dc=abc"})

        self.assertEqual(resp.status_code, 302)
        mockGetUserDb.assert_called_once_with('newuser', 'new@example.com')
        mockRefresh.assert_not_called()


if __name__ == "__main__":
    unittest.main()
