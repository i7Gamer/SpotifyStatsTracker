import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_16_0 as migrateModule
from Database.repository import Repository
from Database.secret_store import ENCRYPTED_PREFIX


class TestMigrate1_16_0(unittest.TestCase):
    """1.16.0 -> 1.17.0 encrypts the users table's stored secrets (session
    cookies, API client secret, refresh token) in place - values written by
    older versions are plaintext."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.17.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.16.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedPlaintextUser(self):
        repo = Repository(self.dbPath)
        repo.upsertUser("alice", "alice@example.com")
        conn = repo.connection()
        with conn:
            # Written raw, as a pre-encryption version would have stored them.
            conn.execute(
                "UPDATE users SET cookies_json='{\"sp_dc\": \"plain-cookie\"}', "
                "spotify_client_id='cid', spotify_client_secret='plain-secret', "
                "spotify_refresh_token='plain-token' WHERE username='alice'")
        repo.connectionManager.close()

    def _rawUserRow(self):
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT cookies_json, spotify_client_id, spotify_client_secret, spotify_refresh_token "
            "FROM users WHERE username='alice'").fetchone()

    def test_encrypts_stored_secrets_and_bumps_version(self):
        self._seedPlaintextUser()

        migrateModule.Migrator("1.16.0", "1.17.0").migrate()

        row = self._rawUserRow()
        self.assertTrue(row["cookies_json"].startswith(ENCRYPTED_PREFIX))
        self.assertTrue(row["spotify_client_secret"].startswith(ENCRYPTED_PREFIX))
        self.assertTrue(row["spotify_refresh_token"].startswith(ENCRYPTED_PREFIX))
        self.assertNotIn("plain-cookie", row["cookies_json"])
        self.assertEqual(row["spotify_client_id"], "cid")   #< not a secret - stays readable

        # And they read back correctly through the Repository.
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        self.assertEqual(repo.getUserCookies("alice"), {"sp_dc": "plain-cookie"})
        self.assertEqual(repo.getUserSpotifyCredentials("alice")["client_secret"], "plain-secret")

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.17.0")

    def test_migration_is_idempotent(self):
        """A retried/interrupted upgrade must not double-encrypt anything."""
        self._seedPlaintextUser()

        migrateModule.Migrator("1.16.0", "1.17.0").migrate()
        rowAfterFirst = dict(self._rawUserRow())

        (self.dataDir / "VERSION").write_text("1.16.0", encoding="utf-8")   #< simulate a retry
        migrateModule.Migrator("1.16.0", "1.17.0").migrate()   #< must not raise

        self.assertEqual(dict(self._rawUserRow()), rowAfterFirst)
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.17.0")

    def test_users_without_secrets_are_untouched(self):
        repo = Repository(self.dbPath)
        repo.upsertUser("empty", "empty@example.com")
        repo.connectionManager.close()

        migrateModule.Migrator("1.16.0", "1.17.0").migrate()   #< must not raise

        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        row = conn.execute("SELECT cookies_json FROM users WHERE username='empty'").fetchone()
        self.assertIsNone(row[0])


if __name__ == "__main__":
    unittest.main()
