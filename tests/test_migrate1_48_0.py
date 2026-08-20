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
import Database.Migrators.migrate1_48_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_48_0(MigratorHelpersMixin, unittest.TestCase):
    """1.48.0 -> 1.49.0 adds tracks.canonical_id and the track_merge_decisions
    table: the storage for merging a song that appears on Spotify more than once
    (a single, an album, a compilation) into one entry.

    Nothing is merged by this migration and nothing reads canonical_id yet. That
    is deliberate - the column arrives inert so the schema can ship on its own,
    ahead of the matcher that fills it and well ahead of the read paths that
    would honour it. NULL means "not merged", which is the correct state for
    every existing row."""

    COLUMN_SQL = "canonical_id    TEXT REFERENCES tracks(id)"
    COLUMN_COMMENT_START = "    -- The track this one has been merged INTO"
    TABLE_SQL = "CREATE TABLE IF NOT EXISTS track_merge_decisions"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.49.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.48.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preMigrationSchema(self):
        """SCHEMA with the new column and table stripped out, standing in for a
        pre-1.49.0 database - without this a fresh Repository() connection would
        create both via SCHEMA before the migration ever runs.

        canonical_id is the LAST column in tracks, so removing it also has to
        take the comma that ended the line before it, or what is left is not
        valid SQL and the fixture fails in a way that looks like a schema bug."""
        schema = dbModule.SCHEMA
        self.assertIn(self.COLUMN_SQL, schema)
        self.assertIn(self.TABLE_SQL, schema)

        start = schema.index(self.COLUMN_COMMENT_START)
        end = schema.index(self.COLUMN_SQL) + len(self.COLUMN_SQL)
        head = schema[:start].rstrip()
        self.assertTrue(head.endswith(","), "expected canonical_id to follow another column")
        schema = head[:-1] + "\n" + schema[end:]

        start = schema.index(self.TABLE_SQL)
        return schema[:start] + schema[schema.index(";", start) + 1:]

    #< closed before returning, not via addCleanup: an inspection connection
    #  left open across the migration blocks its database snapshot on Windows
    #  ("the process cannot access the file because it is being used by another
    #  process"), which reads like a migrator bug and is not one
    def _tables(self):
        conn = sqlite3.connect(self.dbPath)
        try:
            return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()

    def _seedOldDatabase(self):
        with patch.object(dbModule, "SCHEMA", self._preMigrationSchema()):
            repo = Repository(self.dbPath)
            repo._conn()   #< Repository connects lazily, and connecting is what stamps SCHEMA
            repo.connectionManager.close()
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'Album 1', '')")
            conn.execute("INSERT INTO tracks (id, name, url, album_id, isrc) "
                         "VALUES ('3xMBguKPth2j8YPuhmJHSO', 'Song', '', 'alb1', 'USRC12345678')")
        conn.close()

    def test_adds_the_column_and_the_table_and_bumps_the_version(self):
        self._seedOldDatabase()
        self.assertNotIn("canonical_id", self._columnNames("tracks"))
        self.assertNotIn("track_merge_decisions", self._tables())

        migrateModule.Migrator("1.48.0", "1.49.0").migrate()

        self.assertIn("canonical_id", self._columnNames("tracks"))
        self.assertIn("track_merge_decisions", self._tables())
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.49.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.49.0")

    def test_nothing_is_merged_by_the_migration(self):
        """The column arrives inert. A row whose canonical_id is NULL is its own
        canonical track, which is what every existing row must remain until a
        matcher says otherwise - an upgrade that silently moved someone's play
        counts would be a merge nobody asked for."""
        self._seedOldDatabase()

        migrateModule.Migrator("1.48.0", "1.49.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            self.assertIsNone(conn.execute("SELECT canonical_id FROM tracks").fetchone()[0])
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM track_merge_decisions").fetchone()[0], 0)
            #< and the row it was attached to is untouched
            self.assertEqual(conn.execute("SELECT isrc FROM tracks").fetchone()[0], "USRC12345678")
        finally:
            conn.close()

    def test_the_decisions_table_records_who_decided_and_why(self):
        """Reversibility is the point of the table: a merge has to be auditable
        and undoable, and a manual verdict has to outrank a later automatic
        pass. A NULL canonical_id there is a deliberate "these are NOT the same
        song", which is a different statement from having no row at all."""
        self._seedOldDatabase()
        migrateModule.Migrator("1.48.0", "1.49.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        with conn:
            conn.execute(
                "INSERT INTO track_merge_decisions "
                "(track_id, canonical_id, reason, evidence, decided_at, decided_by) "
                "VALUES ('3xMBguKPth2j8YPuhmJHSO', NULL, 'manual-split', 'not the same recording', "
                "1786000000.0, 'timorzipa')")
        row = conn.execute("SELECT canonical_id, reason, decided_by FROM track_merge_decisions").fetchone()
        self.assertIsNone(row[0])
        self.assertEqual(row[1], "manual-split")
        self.assertEqual(row[2], "timorzipa")

    def test_a_fresh_database_needs_no_migration(self):
        """SCHEMA carries both, so a new install is born at 1.49.0's shape."""
        repo = Repository(self.dbPath)
        repo._conn()   #< see _seedOldDatabase: connecting is what stamps SCHEMA
        repo.connectionManager.close()

        self.assertIn("canonical_id", self._columnNames("tracks"))
        self.assertIn("track_merge_decisions", self._tables())

    def test_migration_is_idempotent(self):
        self._seedOldDatabase()
        migrateModule.Migrator("1.48.0", "1.49.0").migrate()

        (self.dataDir / "VERSION").write_text("1.48.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.48.0")
        migrateModule.Migrator("1.48.0", "1.49.0").migrate()   #< must not raise

        self.assertIn("canonical_id", self._columnNames("tracks"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.49.0")

    def test_a_merge_recorded_before_a_rerun_survives_it(self):
        """A retry must not clobber what the first run made reachable."""
        self._seedOldDatabase()
        migrateModule.Migrator("1.48.0", "1.49.0").migrate()
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("INSERT INTO tracks (id, name, url, album_id) "
                         "VALUES ('6YcwCi4Guhw3TEfnSH9ROX', 'Song', '', 'alb1')")
            conn.execute("UPDATE tracks SET canonical_id='3xMBguKPth2j8YPuhmJHSO' "
                         "WHERE id='6YcwCi4Guhw3TEfnSH9ROX'")
            conn.execute("INSERT INTO track_merge_decisions "
                         "(track_id, canonical_id, reason, evidence, decided_at) "
                         "VALUES ('6YcwCi4Guhw3TEfnSH9ROX', '3xMBguKPth2j8YPuhmJHSO', 'isrc', "
                         "'USRC12345678', 1786000000.0)")
        conn.close()

        (self.dataDir / "VERSION").write_text("1.48.0", encoding="utf-8")
        dbversion.writeDbVersion(self.dbPath, "1.48.0")
        migrateModule.Migrator("1.48.0", "1.49.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            self.assertEqual(
                conn.execute("SELECT canonical_id FROM tracks WHERE id='6YcwCi4Guhw3TEfnSH9ROX'").fetchone()[0],
                "3xMBguKPth2j8YPuhmJHSO")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM track_merge_decisions").fetchone()[0], 1)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
