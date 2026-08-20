import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _migrator_case import MigratorHelpersMixin
import Database.db as dbModule
import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_47_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository

REAL_ID = "3xMBguKPth2j8YPuhmJHSO"


class TestMigrate1_47_0(MigratorHelpersMixin, unittest.TestCase):
    """1.47.0 -> 1.48.0 adds tracks.isrc_attempted_at, the retry stamp the
    Web-API ISRC backfiller needs so tracks Spotify has no ISRC for stop
    re-entering the queue every cycle.

    Nothing to backfill: NULL means "never asked", which is true of every
    existing row - tracks.isrc is empty catalog-wide because no ingest path has
    ever been able to fill it."""

    #< carries a trailing comma since 1.49.0 added canonical_id after it. Every
    #  new last column shifts the previous one's comma, so the fixture of the
    #  migration BEFORE it needs this line updated - expect to be here again.
    COLUMN_LINE = "    isrc_attempted_at REAL,\n"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.48.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.47.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preColumnSchema(self):
        """SCHEMA with the new column stripped out, simulating a pre-1.48.0
        database - without this, a fresh Repository() connection would create the
        column via SCHEMA's own CREATE TABLE before the ALTER ever runs. The
        preceding column's trailing comma goes too, or the CREATE TABLE is left
        ending in ",\n)" and won't parse."""
        self.assertIn(self.COLUMN_LINE, dbModule.SCHEMA)
        return dbModule.SCHEMA.replace(
            "    availability_reason TEXT,\n" + self.COLUMN_LINE,
            "    availability_reason TEXT\n")

    def _seedOldDatabase(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            repo = Repository(self.dbPath)
            conn = repo._conn()
            with conn:
                conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Album 1', '')")
                conn.execute(
                    "INSERT INTO tracks (id, name, url, album_id, isrc) VALUES (?, 'Song', '', 'alb1', '')",
                    (REAL_ID,))
            repo.connectionManager.close()

    def test_adds_the_column_and_bumps_the_version(self):
        self._seedOldDatabase()
        self.assertNotIn("isrc_attempted_at", self._columnNames("tracks"))

        migrateModule.Migrator("1.47.0", "1.48.0").migrate()

        self.assertIn("isrc_attempted_at", self._columnNames("tracks"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.48.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.48.0")

    def test_every_existing_track_is_queued_after_the_upgrade(self):
        """The no-backfill contract: a NULL stamp has to read as "never asked",
        so the whole catalog enters the ISRC queue on upgrade rather than
        sitting out the first retry window."""
        self._seedOldDatabase()

        migrateModule.Migrator("1.47.0", "1.48.0").migrate()

        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        self.assertEqual(repo.getTracksMissingIsrc(10), [REAL_ID])

    def test_migration_is_idempotent(self):
        self._seedOldDatabase()
        migrateModule.Migrator("1.47.0", "1.48.0").migrate()

        (self.dataDir / "VERSION").write_text("1.47.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.47.0")
        migrateModule.Migrator("1.47.0", "1.48.0").migrate()   #< must not raise

        self.assertIn("isrc_attempted_at", self._columnNames("tracks"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.48.0")

    def test_a_stamp_set_before_a_rerun_survives_it(self):
        """A retry must not clobber data the first run made reachable - the
        ALTER is guarded, so the second pass has to leave the column alone."""
        self._seedOldDatabase()
        migrateModule.Migrator("1.47.0", "1.48.0").migrate()
        repo = Repository(self.dbPath)
        repo.markTracksIsrcAttempted([REAL_ID])
        repo.connectionManager.close()

        (self.dataDir / "VERSION").write_text("1.47.0", encoding="utf-8")
        dbversion.writeDbVersion(self.dbPath, "1.47.0")
        migrateModule.Migrator("1.47.0", "1.48.0").migrate()

        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        self.assertEqual(repo.getTracksMissingIsrc(10), [])

    def test_empty_database_migrates_cleanly(self):
        preSchema = self._preColumnSchema()
        with patch.object(dbModule, "SCHEMA", preSchema):
            Repository(self.dbPath).connectionManager.close()   #< schema only, no tracks

        migrateModule.Migrator("1.47.0", "1.48.0").migrate()   #< must not raise

        self.assertIn("isrc_attempted_at", self._columnNames("tracks"))


if __name__ == "__main__":
    unittest.main()
