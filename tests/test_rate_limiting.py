"""POST /login, /register, /reset-password must be rate limited per source IP -
without this, a network-reachable instance is brute-forceable indefinitely.
"""
import unittest
from unittest.mock import patch

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import SpotifyDashboardApp
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestRateLimiting(AppTestCase):
    def _postFrom(self, client, path, ip, data):
        return client.post(path, data=data, environ_base={"REMOTE_ADDR": ip})

    def test_login_is_rate_limited_after_max_attempts(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        data = {"email": "nobody@example.com", "password": "wrong"}

        for _ in range(appModule.RATE_LIMIT_MAX_ATTEMPTS):
            resp = self._postFrom(client, "/login", "10.0.0.1", data)
            self.assertEqual(resp.status_code, 200)

        resp = self._postFrom(client, "/login", "10.0.0.1", data)

        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Too many attempts", resp.data)

    def test_register_is_rate_limited_after_max_attempts(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        data = {"email": "nobody@example.com", "password": "", "confirm_password": "", "cookies": ""}

        for _ in range(appModule.RATE_LIMIT_MAX_ATTEMPTS):
            resp = self._postFrom(client, "/register", "10.0.0.2", data)
            self.assertEqual(resp.status_code, 200)

        resp = self._postFrom(client, "/register", "10.0.0.2", data)

        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Too many attempts", resp.data)

    def test_reset_password_is_rate_limited_after_max_attempts(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        data = {"email": "nobody@example.com", "password": "", "confirm_password": "", "cookies": ""}

        for _ in range(appModule.RATE_LIMIT_MAX_ATTEMPTS):
            resp = self._postFrom(client, "/reset-password", "10.0.0.3", data)
            self.assertEqual(resp.status_code, 200)

        resp = self._postFrom(client, "/reset-password", "10.0.0.3", data)

        self.assertEqual(resp.status_code, 429)
        self.assertIn(b"Too many attempts", resp.data)

    def test_different_ips_are_tracked_independently(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        data = {"email": "nobody@example.com", "password": "wrong"}

        for _ in range(appModule.RATE_LIMIT_MAX_ATTEMPTS):
            self._postFrom(client, "/login", "10.0.0.4", data)
        blockedResp = self._postFrom(client, "/login", "10.0.0.4", data)
        freshIpResp = self._postFrom(client, "/login", "10.0.0.5", data)

        self.assertEqual(blockedResp.status_code, 429)
        self.assertEqual(freshIpResp.status_code, 200)

    def test_different_routes_are_tracked_independently(self):
        """Exhausting the /login limit for an IP must not also block that
        same IP's /register or /reset-password attempts."""
        dash = self._makeApp()
        client = dash.app.test_client()
        loginData = {"email": "nobody@example.com", "password": "wrong"}
        registerData = {"email": "nobody@example.com", "password": "", "confirm_password": "", "cookies": ""}

        for _ in range(appModule.RATE_LIMIT_MAX_ATTEMPTS):
            self._postFrom(client, "/login", "10.0.0.8", loginData)
        blockedLoginResp = self._postFrom(client, "/login", "10.0.0.8", loginData)
        registerResp = self._postFrom(client, "/register", "10.0.0.8", registerData)

        self.assertEqual(blockedLoginResp.status_code, 429)
        self.assertEqual(registerResp.status_code, 200)

    def test_get_requests_are_never_rate_limited(self):
        dash = self._makeApp()
        client = dash.app.test_client()

        for _ in range(appModule.RATE_LIMIT_MAX_ATTEMPTS + 5):
            resp = client.get("/login", environ_base={"REMOTE_ADDR": "10.0.0.6"})
            self.assertEqual(resp.status_code, 200)

    def test_limit_resets_after_the_window_passes(self):
        dash = self._makeApp()
        client = dash.app.test_client()
        data = {"email": "nobody@example.com", "password": "wrong"}

        for _ in range(appModule.RATE_LIMIT_MAX_ATTEMPTS):
            self._postFrom(client, "/login", "10.0.0.7", data)
        blockedResp = self._postFrom(client, "/login", "10.0.0.7", data)
        self.assertEqual(blockedResp.status_code, 429)

        # Age out every recorded hit past the window, rather than sleeping
        # for real or patching the global time module.
        key = ("login", "10.0.0.7")
        dash._authRateLimiter._hits[key] = [
            t - appModule.RATE_LIMIT_WINDOW_SECONDS - 1 for t in dash._authRateLimiter._hits[key]
        ]

        recoveredResp = self._postFrom(client, "/login", "10.0.0.7", data)
        self.assertEqual(recoveredResp.status_code, 200)


class TestRateLimiterSweep(unittest.TestCase):
    """The hits map must not grow without bound: a key for a (bucket, IP) that
    stops sending requests has to be swept once its window fully ages out,
    otherwise a network-reachable instance scanned from many IPs leaks one entry
    per IP forever."""

    def test_fully_expired_keys_are_swept_on_a_later_hit(self):
        from app import _RateLimiter

        base = 1000.0
        with patch("app.time.monotonic", return_value=base):
            limiter = _RateLimiter(maxAttempts=5, windowSeconds=100)
            for i in range(50):
                limiter.hit("login", f"ip{i}")
            self.assertEqual(len(limiter._hits), 50)

        # A hit past both the window and the once-per-window sweep interval:
        # every old key has aged out, so only the new one should remain.
        with patch("app.time.monotonic", return_value=base + 200):
            limiter.hit("login", "ipNew")

        self.assertEqual(len(limiter._hits), 1)
        self.assertIn(("login", "ipNew"), limiter._hits)

    def test_sweep_keeps_still_active_keys(self):
        from app import _RateLimiter

        base = 1000.0
        with patch("app.time.monotonic", return_value=base):
            limiter = _RateLimiter(maxAttempts=5, windowSeconds=100)
            limiter.hit("login", "old")   #< at base, will expire

        with patch("app.time.monotonic", return_value=base + 150):
            limiter.hit("login", "recent")   #< triggers a sweep; itself stays

        self.assertNotIn(("login", "old"), limiter._hits)
        self.assertIn(("login", "recent"), limiter._hits)


if __name__ == "__main__":
    unittest.main()
