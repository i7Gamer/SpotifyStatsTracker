"""1.44.0 -> 1.45.0: drop the cached Wrapped years so they rebuild.

The cache is judged stale by the year's max played_at and play count alone.
Three changes landed that alter a year's RESULT without touching either - the
skip-only-day streak fix, the dangling-row repair making plays joinable, and the
TZ-unset default becoming a real zone - so nothing would ever have recalculated
and every past year would have kept serving its pre-fix figures.
"""
import sys
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _migrator_case import MigratorHelpersMixin
import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_44_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


def _wrappedRow():
    return {
        "calculated_at": time.time(), "max_played_at": 1775000000,
        "total_plays": 2, "total_ms": 60000, "longest_streak": 13,
        "peak_day": "Monday", "peak_plays": 1,
        "unique_songs": 1, "unique_artists": 1,
        "discovered_songs": 1, "discovered_artists": 1,
        "time_series_day": "[]", "time_series_week": "[]", "time_series_month": "[]",
        "top_songs": "[]", "top_artists": "[]", "top_albums": "[]",
        "discovered_songs_list": "[]", "discovered_artists_list": "[]",
        "discovered_albums_list": "[]",
    }


class TestMigrate1_44_0(MigratorHelpersMixin, unittest.TestCase):
    USER = "someone"
    OTHER = "someone_else"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.45.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.44.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _seedDatabaseAt(self, version, cachedYears=(2024, 2025, 2026)):
        repo = Repository(self.dbPath)
        for username in (self.USER, self.OTHER):
            repo.upsertUser(username, f"{username}@example.com", createdAt=100.0)
            for year in cachedYears:
                repo.saveCachedWrapped(username, year, _wrappedRow())
        repo.connectionManager.close()
        dbversion.writeDbVersion(self.dbPath, version)

    def _cachedCount(self):
        return self._repo()._conn().execute(
            "SELECT COUNT(*) AS n FROM user_wrapped").fetchone()["n"]

    def _migrate(self):
        migrateModule.Migrator("1.44.0", "1.45.0").migrate()

    def test_every_cached_year_for_every_user_is_dropped(self):
        self._seedDatabaseAt("1.44.0")
        self.assertEqual(self._cachedCount(), 6)

        self._migrate()

        self.assertEqual(self._cachedCount(), 0)

    def test_the_plays_themselves_are_untouched(self):
        """It is a cache. Nothing but the cache may go."""
        self._seedDatabaseAt("1.44.0")
        repo = Repository(self.dbPath)
        before = repo._conn().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
        repo.connectionManager.close()

        self._migrate()

        self.assertEqual(
            self._repo()._conn().execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"], before)

    def test_a_database_with_no_cached_wrapped_migrates_cleanly(self):
        self._seedDatabaseAt("1.44.0", cachedYears=())

        self._migrate()

        self.assertEqual(self._cachedCount(), 0)

    def test_bumps_both_version_markers(self):
        self._seedDatabaseAt("1.44.0")

        self._migrate()

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.45.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.45.0")

    def test_rejects_a_database_not_on_the_from_version(self):
        self._seedDatabaseAt("1.43.0")
        with self.assertRaisesRegex(Exception, "does not match migrator's expected from-version"):
            self._migrate()

    def test_migration_is_idempotent_on_retry(self):
        self._seedDatabaseAt("1.44.0")
        self._migrate()

        (self.dataDir / "VERSION").write_text("1.44.0", encoding="utf-8")
        dbversion.writeDbVersion(self.dbPath, "1.44.0")
        self._migrate()   #< must not raise

        self.assertEqual(self._cachedCount(), 0)


if __name__ == "__main__":
    unittest.main()
