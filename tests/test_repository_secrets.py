"""Repository must store user secrets encrypted at rest.

The users table's cookies_json / spotify_client_secret /
spotify_refresh_token columns hold live Spotify sessions and API secrets;
they must never land in the database file (or its backups) as plaintext.
Rows written before encryption existed stay readable (legacy passthrough).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.repository import Repository
from Database.secret_store import ENCRYPTED_PREFIX


class RepositorySecretsTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.repo = Repository(Path(self._tmpdir.name) / "test.db")
        self.addCleanup(self.repo.connectionManager.close)
        self.repo.upsertUser("alice", "alice@example.com")

    def _rawColumn(self, column):
        row = self.repo.connection().execute(
            f"SELECT {column} FROM users WHERE username='alice'").fetchone()
        return row[column]


class TestCookiesEncryption(RepositorySecretsTestCase):
    def test_cookies_round_trip_but_are_encrypted_at_rest(self):
        cookies = {"sp_dc": "super-secret-session", "sp_key": "another-secret"}

        self.repo.setUserCookies("alice", cookies)

        raw = self._rawColumn("cookies_json")
        self.assertTrue(raw.startswith(ENCRYPTED_PREFIX))
        self.assertNotIn("super-secret-session", raw)
        self.assertEqual(self.repo.getUserCookies("alice"), cookies)

    def test_legacy_plaintext_cookies_stay_readable(self):
        conn = self.repo.connection()
        with conn:
            conn.execute("UPDATE users SET cookies_json=? WHERE username='alice'",
                         ('{"sp_dc": "legacy-cookie"}',))

        self.assertEqual(self.repo.getUserCookies("alice"), {"sp_dc": "legacy-cookie"})

    def test_undecryptable_cookies_read_as_missing(self):
        """A rotated/lost key must degrade to 'no cookies stored' (forcing
        re-login), not crash every request that checks login state."""
        conn = self.repo.connection()
        with conn:
            conn.execute("UPDATE users SET cookies_json=? WHERE username='alice'",
                         (ENCRYPTED_PREFIX + "garbage-token",))

        self.assertIsNone(self.repo.getUserCookies("alice"))

    def test_missing_cookies_still_read_as_none(self):
        self.assertIsNone(self.repo.getUserCookies("alice"))


class TestSpotifyCredentialsEncryption(RepositorySecretsTestCase):
    def test_secret_and_refresh_token_round_trip_but_are_encrypted_at_rest(self):
        self.repo.updateUserSpotifyCredentials("alice", "public-client-id", "very-secret", "refresh-secret")

        self.assertEqual(self._rawColumn("spotify_client_id"), "public-client-id")
        for column, plaintext in (("spotify_client_secret", "very-secret"),
                                  ("spotify_refresh_token", "refresh-secret")):
            raw = self._rawColumn(column)
            self.assertTrue(raw.startswith(ENCRYPTED_PREFIX), f"{column} stored as plaintext")
            self.assertNotIn(plaintext, raw)

        creds = self.repo.getUserSpotifyCredentials("alice")
        self.assertEqual(creds, {
            "client_id": "public-client-id",
            "client_secret": "very-secret",
            "refresh_token": "refresh-secret",
            "needs_reauth": False,
        })

    def test_clearing_credentials_stores_none(self):
        self.repo.updateUserSpotifyCredentials("alice", "cid", "secret", "token")
        self.repo.updateUserSpotifyCredentials("alice", None, None, None)

        creds = self.repo.getUserSpotifyCredentials("alice")
        self.assertEqual(creds, {"client_id": None, "client_secret": None, "refresh_token": None, "needs_reauth": False})

    def test_legacy_plaintext_credentials_stay_readable(self):
        conn = self.repo.connection()
        with conn:
            conn.execute(
                "UPDATE users SET spotify_client_id='cid', spotify_client_secret='old-secret', "
                "spotify_refresh_token='old-token' WHERE username='alice'")

        creds = self.repo.getUserSpotifyCredentials("alice")
        self.assertEqual(creds["client_secret"], "old-secret")
        self.assertEqual(creds["refresh_token"], "old-token")


class TestSpotifyNeedsReauth(RepositorySecretsTestCase):
    def test_defaults_to_false_for_a_fresh_user(self):
        self.assertFalse(self.repo.getUserSpotifyCredentials("alice")["needs_reauth"])

    def test_set_true_then_false_round_trips(self):
        self.repo.setSpotifyNeedsReauth("alice", True)
        self.assertTrue(self.repo.getUserSpotifyCredentials("alice")["needs_reauth"])

        self.repo.setSpotifyNeedsReauth("alice", False)
        self.assertFalse(self.repo.getUserSpotifyCredentials("alice")["needs_reauth"])

    def test_only_affects_the_named_user(self):
        self.repo.upsertUser("bob", "bob@example.com")
        self.repo.setSpotifyNeedsReauth("alice", True)

        self.assertTrue(self.repo.getUserSpotifyCredentials("alice")["needs_reauth"])
        self.assertFalse(self.repo.getUserSpotifyCredentials("bob")["needs_reauth"])

    def test_standalone_getter_matches_the_credentials_dict_field(self):
        """getSpotifyNeedsReauth (the cheap, no-decryption read used by the
        topbar badge) must agree with getUserSpotifyCredentials's field."""
        self.assertFalse(self.repo.getSpotifyNeedsReauth("alice"))

        self.repo.setSpotifyNeedsReauth("alice", True)
        self.assertTrue(self.repo.getSpotifyNeedsReauth("alice"))

    def test_standalone_getter_defaults_to_false_for_unknown_user(self):
        self.assertFalse(self.repo.getSpotifyNeedsReauth("nobody"))


class TestEncryptStoredSecretsIfPlaintext(RepositorySecretsTestCase):
    def test_plaintext_rows_are_encrypted_in_place(self):
        self.repo.upsertUser("bob", "bob@example.com")
        conn = self.repo.connection()
        with conn:
            conn.execute("UPDATE users SET cookies_json='{\"sp_dc\": \"alice-cookie\"}', "
                         "spotify_client_secret='alice-secret' WHERE username='alice'")
            conn.execute("UPDATE users SET spotify_refresh_token='bob-token' WHERE username='bob'")

        updated = self.repo.encryptStoredSecretsIfPlaintext()
        self.repo.commit()

        self.assertEqual(updated, 2)
        self.assertTrue(self._rawColumn("cookies_json").startswith(ENCRYPTED_PREFIX))
        self.assertTrue(self._rawColumn("spotify_client_secret").startswith(ENCRYPTED_PREFIX))
        self.assertEqual(self.repo.getUserCookies("alice"), {"sp_dc": "alice-cookie"})
        self.assertEqual(self.repo.getUserSpotifyCredentials("alice")["client_secret"], "alice-secret")
        self.assertEqual(self.repo.getUserSpotifyCredentials("bob")["refresh_token"], "bob-token")

    def test_already_encrypted_and_empty_rows_are_untouched(self):
        self.repo.setUserCookies("alice", {"sp_dc": "cookie"})
        encryptedBefore = self._rawColumn("cookies_json")

        updated = self.repo.encryptStoredSecretsIfPlaintext()
        self.repo.commit()

        self.assertEqual(updated, 0)
        self.assertEqual(self._rawColumn("cookies_json"), encryptedBefore,
                         "an already-encrypted value must not be double-encrypted")


if __name__ == "__main__":
    unittest.main()
