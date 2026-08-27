import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _migrator_case import MigratorHelpersMixin
import Database.db as dbModule
import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_51_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_51_0(MigratorHelpersMixin, unittest.TestCase):
    """1.51.0 -> 1.52.0 adds albums.artist_repair_done_at: the stamp that lets
    the artistless-track repair queue terminate.

    That queue selects albums holding a track with no artist links, and its
    exit condition was the absence of such tracks - which an album can never
    reach when Spotify credits nobody on one of them. Each pass repaired
    nothing and the album returned a retry window later, forever, occupying a
    slot in every batch.

    It arrives NULL everywhere, which reads as "never walked", so the upgrade
    queues exactly what it queued before. Nothing is backfilled and nothing
    could be: before this release no code walked an album's complete track
    list, so no existing row can honestly claim one."""

    COLUMN_SQL = "artist_repair_done_at REAL"
    COLUMN_COMMENT_START = "-- When this album's COMPLETE track list was last walked"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.52.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.51.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preMigrationSchema(self):
        """SCHEMA with the new column stripped, standing in for a pre-1.52.0
        database - without it a fresh Repository() connection would create the
        albums table WITH the column and the migration would have nothing to
        prove.

        artist_repair_done_at is the LAST column of albums, so removing it also
        has to take the comma that ended the line before it, or what is left is
        not valid SQL and the fixture fails in a way that looks like a schema
        bug. Its comment block goes with it, so the fixture reads as a
        believable older file rather than one documenting a column it does not
        have."""
        schema = dbModule.SCHEMA
        self.assertIn(self.COLUMN_SQL, schema)

        start = schema.index(self.COLUMN_COMMENT_START)
        head = schema[:start].rstrip()
        self.assertTrue(head.endswith(","), "expected artist_repair_done_at to follow another column")
        return head[:-1] + "\n" + schema[schema.index(self.COLUMN_SQL) + len(self.COLUMN_SQL):]

    #< closed before returning, not via addCleanup: an inspection connection
    #  left open across the migration blocks its database snapshot on Windows,
    #  which reads like a migrator bug and is not one
    def _seedOldDatabase(self):
        with patch.object(dbModule, "SCHEMA", self._preMigrationSchema()):
            repo = Repository(self.dbPath)
            repo._conn()   #< Repository connects lazily, and connecting is what stamps SCHEMA
            repo._conn().execute(
                "INSERT INTO albums (id, name, url) VALUES ('alb1', 'Album 1', '')")
            repo.commit()
            repo.connectionManager.close()

    def test_adds_the_column_and_bumps_the_version(self):
        self._seedOldDatabase()
        self.assertNotIn("artist_repair_done_at", self._columnNames("albums"))

        migrateModule.Migrator("1.51.0", "1.52.0").migrate()

        self.assertIn("artist_repair_done_at", self._columnNames("albums"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.52.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.52.0")

    def test_an_existing_album_arrives_unwalked(self):
        """NULL, not a timestamp: nothing before this release walked an
        album's complete track list, so no existing row may claim one - and a
        row that did would be excluded from the repair queue for
        ALBUM_ARTIST_REPAIR_RETRY_SECONDS on the strength of work never done."""
        self._seedOldDatabase()

        migrateModule.Migrator("1.51.0", "1.52.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            row = conn.execute("SELECT id, artist_repair_done_at FROM albums").fetchone()
            self.assertEqual(row[0], "alb1")
            self.assertIsNone(row[1])
        finally:
            conn.close()

    def test_a_fresh_database_needs_no_migration(self):
        """SCHEMA carries the column, so a new install is born at 1.52.0's
        shape - the two DDL sites have to agree or an upgraded database and a
        fresh one end up different."""
        repo = Repository(self.dbPath)
        repo._conn()   #< see _seedOldDatabase: connecting is what stamps SCHEMA
        repo.connectionManager.close()

        self.assertIn("artist_repair_done_at", self._columnNames("albums"))

    def test_migration_is_idempotent(self):
        self._seedOldDatabase()
        migrateModule.Migrator("1.51.0", "1.52.0").migrate()

        (self.dataDir / "VERSION").write_text("1.51.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.51.0")
        migrateModule.Migrator("1.51.0", "1.52.0").migrate()   #< must not raise

        self.assertIn("artist_repair_done_at", self._columnNames("albums"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.52.0")

    def test_a_rerun_does_not_clear_a_walk_already_recorded(self):
        """A retry must not re-queue albums a cycle has since finished with."""
        self._seedOldDatabase()
        migrateModule.Migrator("1.51.0", "1.52.0").migrate()
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("UPDATE albums SET artist_repair_done_at = 1700000000.0 WHERE id = 'alb1'")
        conn.close()

        (self.dataDir / "VERSION").write_text("1.51.0", encoding="utf-8")
        dbversion.writeDbVersion(self.dbPath, "1.51.0")
        migrateModule.Migrator("1.51.0", "1.52.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            stamp = conn.execute(
                "SELECT artist_repair_done_at FROM albums WHERE id='alb1'").fetchone()[0]
            self.assertEqual(stamp, 1700000000.0)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
