import sys
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.db as dbModule
import Database.Migrators.base as baseModule
import Database.Migrators.migrate1_49_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_49_0(unittest.TestCase):
    """1.49.0 -> 1.50.0 adds track_merge_decisions.against_id: the release a
    "not the same recording" was ruled AGAINST.

    The verdict itself does not change - the track still leaves the review
    queue for good, whatever it was compared with - so this is an audit
    column, and it arrives NULL. That is the honest answer for every rejection
    recorded before it existed: nothing knows what those were ruled against,
    and backfilling a guess would put a name on a decision nobody made."""

    COLUMN_SQL = "against_id   TEXT REFERENCES tracks(id)"
    COLUMN_COMMENT_START = "-- against_id is the other half of a rejection"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.50.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.49.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preMigrationSchema(self):
        """SCHEMA with the new column stripped out, standing in for a
        pre-1.50.0 database - without it a fresh Repository() connection would
        create the table WITH the column before the migration ever runs.

        against_id is the last column of track_merge_decisions, so removing it
        also has to take the comma that ended the line before it, or what is
        left is not valid SQL and the fixture fails in a way that looks like a
        schema bug."""
        schema = dbModule.SCHEMA
        self.assertIn(self.COLUMN_SQL, schema)

        start = schema.index(self.COLUMN_SQL)
        head = schema[:start].rstrip()
        self.assertTrue(head.endswith(","), "expected against_id to follow another column")
        schema = head[:-1] + "\n" + schema[start + len(self.COLUMN_SQL):]
        #< the comment block explaining it goes too, so the fixture is a
        #  believable older file rather than one documenting a missing column
        commentStart = schema.index(self.COLUMN_COMMENT_START)
        return schema[:commentStart] + schema[schema.index("CREATE TABLE IF NOT EXISTS "
                                                           "track_merge_decisions"):]

    #< closed before returning, not via addCleanup: an inspection connection
    #  left open across the migration blocks its database snapshot on Windows,
    #  which reads like a migrator bug and is not one
    def _columnNames(self, table):
        conn = sqlite3.connect(self.dbPath)
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        finally:
            conn.close()

    def _seedOldDatabase(self, withRejection=False):
        with patch.object(dbModule, "SCHEMA", self._preMigrationSchema()):
            repo = Repository(self.dbPath)
            repo._conn()   #< Repository connects lazily, and connecting is what stamps SCHEMA
            repo.connectionManager.close()
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Album 1', '')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id) "
                         "VALUES ('3xMBguKPth2j8YPuhmJHSO', 'Song', '', 'alb1')")
            if withRejection:
                conn.execute(
                    "INSERT INTO track_merge_decisions "
                    "(track_id, canonical_id, reason, evidence, decided_at, decided_by) "
                    "VALUES ('3xMBguKPth2j8YPuhmJHSO', NULL, 'manual-reject', NULL, "
                    "1786000000.0, 'timorzipa')")
        conn.close()

    def test_adds_the_column_and_bumps_the_version(self):
        self._seedOldDatabase()
        self.assertNotIn("against_id", self._columnNames("track_merge_decisions"))

        migrateModule.Migrator("1.49.0", "1.50.0").migrate()

        self.assertIn("against_id", self._columnNames("track_merge_decisions"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.50.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.50.0")

    def test_an_existing_rejection_survives_and_names_nothing(self):
        """The verdict is untouched - it still keeps the pair out of the queue
        - and its new column is NULL rather than a guess at what it was
        compared with."""
        self._seedOldDatabase(withRejection=True)

        migrateModule.Migrator("1.49.0", "1.50.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            row = conn.execute("SELECT reason, decided_by, against_id "
                               "FROM track_merge_decisions").fetchone()
            self.assertEqual(row[0], "manual-reject")
            self.assertEqual(row[1], "timorzipa")
            self.assertIsNone(row[2])
        finally:
            conn.close()

    def test_a_fresh_database_needs_no_migration(self):
        """SCHEMA carries the column, so a new install is born at 1.50.0's
        shape - the two DDL sites have to agree or an upgraded database and a
        fresh one end up different."""
        repo = Repository(self.dbPath)
        repo._conn()   #< see _seedOldDatabase: connecting is what stamps SCHEMA
        repo.connectionManager.close()

        self.assertIn("against_id", self._columnNames("track_merge_decisions"))

    def test_migration_is_idempotent(self):
        self._seedOldDatabase()
        migrateModule.Migrator("1.49.0", "1.50.0").migrate()

        (self.dataDir / "VERSION").write_text("1.49.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.49.0")
        migrateModule.Migrator("1.49.0", "1.50.0").migrate()   #< must not raise

        self.assertIn("against_id", self._columnNames("track_merge_decisions"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.50.0")

    def test_a_rejection_recorded_before_a_rerun_survives_it(self):
        """A retry must not clobber what the first run made reachable."""
        self._seedOldDatabase()
        migrateModule.Migrator("1.49.0", "1.50.0").migrate()
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("INSERT INTO tracks (id, name, url, album_id) "
                         "VALUES ('6YcwCi4Guhw3TEfnSH9ROX', 'Song', '', 'alb1')")
            conn.execute(
                "INSERT INTO track_merge_decisions (track_id, canonical_id, reason, "
                "evidence, decided_at, decided_by, against_id) "
                "VALUES ('6YcwCi4Guhw3TEfnSH9ROX', NULL, 'manual-reject', NULL, "
                "1786000000.0, 'timorzipa', '3xMBguKPth2j8YPuhmJHSO')")
        conn.close()

        (self.dataDir / "VERSION").write_text("1.49.0", encoding="utf-8")
        dbversion.writeDbVersion(self.dbPath, "1.49.0")
        migrateModule.Migrator("1.49.0", "1.50.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            self.assertEqual(
                conn.execute("SELECT against_id FROM track_merge_decisions "
                             "WHERE track_id='6YcwCi4Guhw3TEfnSH9ROX'").fetchone()[0],
                "3xMBguKPth2j8YPuhmJHSO")
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
