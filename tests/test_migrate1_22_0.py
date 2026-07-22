"""1.22.0 -> 1.23.0: behavioral play metadata.

Adds the 8 nullable behavioral columns (platform, conn_country, reason_start,
reason_end, shuffle, skipped, offline, incognito) to a pre-existing plays
table. Existing play rows must survive untouched. (This migration historically
also created a separate play_skips table via SCHEMA; that table was later
merged back into plays and removed from SCHEMA, so it's no longer created here -
see migrate1_32_0.)
"""
import sqlite3
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as baseModule
from Database.db import BEHAVIORAL_COLUMNS
from Database.repository import Repository

# The plays table exactly as it existed in 1.22.0 - the migration must ALTER
# this shape, not rely on SCHEMA (CREATE TABLE IF NOT EXISTS is a no-op here).
OLD_PLAYS_TABLE_SQL = """
CREATE TABLE plays (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT NOT NULL,
    track_id        TEXT NOT NULL,
    played_at       REAL NOT NULL,
    time_played     INTEGER NOT NULL CHECK (time_played >= 1000),
    played_from     TEXT,
    created_at      REAL,
    created_reason  TEXT,
    UNIQUE (username, track_id, played_at)
)
"""


class TestMigrate1_22_0(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.23.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.22.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"
        self._seedOldShapeDb()

    def _seedOldShapeDb(self):
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute(OLD_PLAYS_TABLE_SQL)
            conn.execute(
                "INSERT INTO plays (username, track_id, played_at, time_played) VALUES ('u1', 't1', 1000.0, 60000)"
            )
        conn.close()

    def _migrate(self):
        import Database.Migrators.migrate1_22_0 as migrateModule
        migrateModule.Migrator("1.22.0", "1.23.0").migrate()

    def _columns(self, conn, table):
        return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def test_adds_behavioral_columns(self):
        self._migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            playColumns = self._columns(conn, "plays")
            for column in BEHAVIORAL_COLUMNS:
                self.assertIn(column, playColumns)

            # Existing play rows survive the ALTERs
            row = conn.execute("SELECT username, track_id, time_played FROM plays").fetchone()
            self.assertEqual(row, ("u1", "t1", 60000))

            # play_skips is retired: no longer in SCHEMA, so this migration no
            # longer creates it (migrate1_32_0 folds one in if present).
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            self.assertNotIn("play_skips", tables)
        finally:
            conn.close()

        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.23.0")

    def test_column_helper_is_idempotent(self):
        # Fresh DBs get the columns from SCHEMA already - the guarded helper
        # must be a no-op there, not an error.
        repo = Repository(self.root / "fresh.db")
        self.addCleanup(repo.connectionManager.close)
        repo.addPlayBehavioralColumnsIfMissing()
        repo.addPlayBehavioralColumnsIfMissing()
        columns = {row["name"] for row in repo._conn().execute("PRAGMA table_info(plays)").fetchall()}
        for column in BEHAVIORAL_COLUMNS:
            self.assertIn(column, columns)


if __name__ == "__main__":
    unittest.main()
