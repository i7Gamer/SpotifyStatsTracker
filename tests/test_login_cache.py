"""Tests for the is_user_logged_in() 180-second TTL cache.

Verifies that isListenerLoggedIn() is NOT called on every request and that the
cache expires correctly after LOGIN_CACHE_TTL_SECONDS.
"""
import time
import unittest
from unittest.mock import MagicMock, patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp, LOGIN_CACHE_TTL_SECONDS

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


def _make_app() -> SpotifyDashboardApp:
    """Create a SpotifyDashboardApp with all side-effectful threads suppressed."""
    with patch(_SECRET_KEY_PATCH, return_value='test-secret-key'), \
         patch('app.SpotifyDashboardApp.startVersionCheck_thread'), \
         patch('app.SpotifyDashboardApp.checkLogin_thread'), \
         patch('app.migrateIfNeeded'):
        return SpotifyDashboardApp()


def _seed_cookies(dash: SpotifyDashboardApp, email: str, username: str):
    """Seed a user with stored cookies so is_user_logged_in() passes the
    pre-checks and reaches the isListenerLoggedIn() call."""
    dash.repo.upsertUser(username, email)
    dash.repo.setUserCookies(username, {"sp_dc": "fake"})


class TestLoginCache(unittest.TestCase):
    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _make_mock_db(self, return_value=True):
        db = MagicMock()
        db.isListenerLoggedIn.return_value = return_value
        return db

    # ------------------------------------------------------------------
    # tests
    # ------------------------------------------------------------------

    def test_second_call_uses_cache_and_skips_network(self):
        """isListenerLoggedIn should only be called once within the TTL window."""
        dash = _make_app()
        email, username = "alice@example.com", "alice"
        _seed_cookies(dash, email, username)
        mock_db = self._make_mock_db(return_value=True)
        dash.user_databases[username] = mock_db

        result1 = dash.is_user_logged_in(email)
        result2 = dash.is_user_logged_in(email)

        self.assertTrue(result1)
        self.assertTrue(result2)
        mock_db.isListenerLoggedIn.assert_called_once()  # NOT twice

    def test_cache_expires_after_ttl(self):
        """After TTL expires the next call must re-invoke isListenerLoggedIn."""
        dash = _make_app()
        email, username = "bob@example.com", "bob"
        _seed_cookies(dash, email, username)
        mock_db = self._make_mock_db(return_value=True)
        dash.user_databases[username] = mock_db

        # Manually prime the cache with an already-expired entry
        expired_ts = time.monotonic() - 1  # 1 second in the past
        dash._login_cache[email] = (True, expired_ts)

        dash.is_user_logged_in(email)

        mock_db.isListenerLoggedIn.assert_called_once()  # cache was stale -> real call

    def test_false_result_is_also_cached(self):
        """A False (logged-out) result must be cached too, not re-checked every call."""
        dash = _make_app()
        email, username = "carol@example.com", "carol"
        _seed_cookies(dash, email, username)
        mock_db = self._make_mock_db(return_value=False)
        dash.user_databases[username] = mock_db

        result1 = dash.is_user_logged_in(email)
        result2 = dash.is_user_logged_in(email)

        self.assertFalse(result1)
        self.assertFalse(result2)
        mock_db.isListenerLoggedIn.assert_called_once()

    def test_cache_is_per_user(self):
        """Cache hits for one user must not bleed into another user's entry."""
        dash = _make_app()
        users = [("dave@example.com", "dave"), ("eve@example.com", "eve")]

        for email, username in users:
            _seed_cookies(dash, email, username)

        for email, username in users:
            dash.user_databases[username] = self._make_mock_db(return_value=True)

        dash.is_user_logged_in("dave@example.com")
        dash.is_user_logged_in("eve@example.com")

        dash.user_databases["dave"].isListenerLoggedIn.assert_called_once()
        dash.user_databases["eve"].isListenerLoggedIn.assert_called_once()


    def test_constant_value(self):
        """LOGIN_CACHE_TTL_SECONDS must be 180."""
        self.assertEqual(LOGIN_CACHE_TTL_SECONDS, 180)

    def test_empty_email_returns_false_without_cache_lookup(self):
        """is_user_logged_in('') must short-circuit and never touch the cache."""
        dash = _make_app()
        result = dash.is_user_logged_in("")
        self.assertFalse(result)
        self.assertEqual(dash._login_cache, {})

    def test_user_not_yet_in_user_databases_is_actually_checked_not_assumed_true(self):
        """A user with stored cookies but no live Database yet (e.g. a
        Listener construction failure earlier left them out of
        user_databases - see _ensureAllUsersLogin's per-user try/except)
        must be verified via get_user_db()/isListenerLoggedIn(), not
        blindly trusted - this is the exact check the password-login branch
        relies on to confirm a stored session is still live."""
        dash = _make_app()
        email, username = "grace@example.com", "grace"
        _seed_cookies(dash, email, username)
        self.assertNotIn(username, dash.user_databases)   #< the untested gap

        mock_db = self._make_mock_db(return_value=False)
        with patch.object(dash, 'get_user_db', return_value=mock_db) as mock_get_user_db:
            result = dash.is_user_logged_in(email)

        self.assertFalse(result)
        mock_get_user_db.assert_called_once_with(username, email)
        mock_db.isListenerLoggedIn.assert_called_once()

    def test_database_construction_failure_is_treated_as_logged_out(self):
        """get_user_db() can raise (e.g. the stored cookies are so broken that
        constructing the Listener itself fails) - that must resolve to False,
        not crash the caller (login page, overview page, ...)."""
        dash = _make_app()
        email, username = "heidi@example.com", "heidi"
        _seed_cookies(dash, email, username)

        with patch.object(dash, 'get_user_db', side_effect=RuntimeError("boom")):
            result = dash.is_user_logged_in(email)

        self.assertFalse(result)

    def test_cache_populated_after_first_call(self):
        """After the first call the email must exist in _login_cache with a future expiry."""
        dash = _make_app()
        email, username = "frank@example.com", "frank"
        _seed_cookies(dash, email, username)
        dash.user_databases[username] = self._make_mock_db(return_value=True)

        dash.is_user_logged_in(email)

        self.assertIn(email, dash._login_cache)
        cached_result, expires_at = dash._login_cache[email]
        self.assertTrue(cached_result)
        self.assertGreater(expires_at, time.monotonic())  # not yet expired


if __name__ == "__main__":
    unittest.main()
