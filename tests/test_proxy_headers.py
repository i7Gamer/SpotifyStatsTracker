"""TRUST_PROXY_HEADERS opts in to honoring X-Forwarded-* headers.

The auth rate limiter keys on request.remote_addr. Behind a reverse proxy
(the normal way a self-hosted instance gets TLS), every visitor shares the
proxy's IP - ten failed login POSTs from anyone would lock the whole
instance out of /login for five minutes. With TRUST_PROXY_HEADERS set,
werkzeug's ProxyFix restores the real client IP from X-Forwarded-For.

It must stay opt-in: when the app is NOT behind a proxy, trusting
X-Forwarded-For would let an attacker rotate a forged header to bypass the
rate limit entirely.
"""
import os
import unittest
from unittest.mock import patch

import sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
from app import SpotifyDashboardApp, _trustedProxyCount
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


class TestTrustedProxyCountParsing(unittest.TestCase):
    def _withEnv(self, value):
        env = {} if value is None else {appModule.TRUST_PROXY_HEADERS_ENV_VAR: value}
        with patch.dict(os.environ, env, clear=False):
            if value is None:
                os.environ.pop(appModule.TRUST_PROXY_HEADERS_ENV_VAR, None)
            return _trustedProxyCount()

    def test_unset_means_no_trusted_proxies(self):
        self.assertEqual(self._withEnv(None), 0)

    def test_empty_means_no_trusted_proxies(self):
        self.assertEqual(self._withEnv(""), 0)

    def test_truthy_value_means_one_proxy_hop(self):
        self.assertEqual(self._withEnv("true"), 1)
        self.assertEqual(self._withEnv("yes"), 1)
        self.assertEqual(self._withEnv("on"), 1)

    def test_numeric_value_is_the_hop_count(self):
        self.assertEqual(self._withEnv("1"), 1)
        self.assertEqual(self._withEnv("2"), 2)

    def test_zero_and_negative_mean_disabled(self):
        self.assertEqual(self._withEnv("0"), 0)
        self.assertEqual(self._withEnv("-3"), 0)

    def test_junk_means_disabled(self):
        self.assertEqual(self._withEnv("banana"), 0)


class _AppTestBase(AppTestCase):
    def _postLogin(self, client, ip, forwardedFor=None):
        headers = {"X-Forwarded-For": forwardedFor} if forwardedFor else {}
        return client.post(
            "/login",
            data={"email": "nobody@example.com", "password": "wrong"},
            environ_base={"REMOTE_ADDR": ip},
            headers=headers,
        )


class TestProxyHeadersIgnoredByDefault(_AppTestBase):
    def test_forged_forwarded_for_cannot_bypass_the_rate_limit(self):
        """Without opt-in, rotating X-Forwarded-For must not defeat the
        per-IP limit - all attempts still count against the socket address."""
        dash = self._makeApp()
        client = dash.app.test_client()

        for i in range(appModule.RATE_LIMIT_MAX_ATTEMPTS):
            resp = self._postLogin(client, "10.9.0.1", forwardedFor=f"172.16.0.{i}")
            self.assertEqual(resp.status_code, 200)

        blocked = self._postLogin(client, "10.9.0.1", forwardedFor="172.16.0.99")
        self.assertEqual(blocked.status_code, 429)


class TestProxyHeadersTrustedWhenEnabled(_AppTestBase):
    def _makeProxiedApp(self):
        with patch.dict(os.environ, {appModule.TRUST_PROXY_HEADERS_ENV_VAR: "1"}):
            return self._makeApp()

    def test_clients_behind_the_proxy_are_limited_independently(self):
        """One abusive client behind the proxy must not exhaust the limit
        for everyone else coming through the same proxy IP."""
        dash = self._makeProxiedApp()
        client = dash.app.test_client()

        for _ in range(appModule.RATE_LIMIT_MAX_ATTEMPTS):
            self._postLogin(client, "10.9.0.2", forwardedFor="172.16.1.1")
        abuserBlocked = self._postLogin(client, "10.9.0.2", forwardedFor="172.16.1.1")
        otherClient = self._postLogin(client, "10.9.0.2", forwardedFor="172.16.1.2")

        self.assertEqual(abuserBlocked.status_code, 429)
        self.assertEqual(otherClient.status_code, 200)


if __name__ == "__main__":
    unittest.main()
