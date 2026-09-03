# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

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
import Database.Migrators.migrate1_52_0 as migrateModule
from Database.Migrators import dbversion
from Database.repository import Repository


class TestMigrate1_52_0(MigratorHelpersMixin, unittest.TestCase):
    """1.52.0 -> 1.53.0 does two things, both about track merging and the genre
    backfill disagreeing over what a SONG is.

    It adds track_merge_decisions.carried_canonical_id - where the MATCHER
    moved a person's verdict, so canonical_id can go on naming where the person
    PUT it and the toggle's off edge has something to restore. It arrives NULL
    everywhere, the correct reading of every existing row; verdicts already
    re-homed before the column existed are unrecoverable, because the target
    was overwritten in place and that table has one row per track.

    And it requeues merge-group canonicals holding no own genre rows. Their
    songs' genres were written to a member, which no read resolves to - the
    canonical was never looked up and nothing would ever requeue it. Members
    keep their rows and their stamps: nothing is copied or deleted, so
    unmerging stays lossless.
    """

    COLUMN_SQL = "carried_canonical_id TEXT REFERENCES tracks(id)"
    COLUMN_COMMENT_START = "-- carried_canonical_id is where the MATCHER moved"
    CREATE_START = "CREATE TABLE IF NOT EXISTS track_merge_decisions"

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)

        self.migratorsDir = self.root / "Database" / "Migrators"
        self.migratorsDir.mkdir(parents=True)
        self.dataDir = self.root / "Database" / "Data"
        self.dataDir.mkdir(parents=True)

        (self.root / "Database" / "VERSION").write_text("1.53.0", encoding="utf-8")
        (self.dataDir / "VERSION").write_text("1.52.0", encoding="utf-8")

        self._filePatcher = patch.object(baseModule, "__file__", str(self.migratorsDir / "base.py"))
        self._filePatcher.start()
        self.addCleanup(self._filePatcher.stop)

        self.dbPath = self.dataDir / "spotify_stats.db"

    def _preMigrationSchema(self):
        """SCHEMA with the new column stripped, standing in for a pre-1.53.0
        database - without it a fresh Repository() connection would create the
        table WITH the column and the migration would have nothing to prove.

        carried_canonical_id is the LAST column of track_merge_decisions, so
        removing it also has to take the comma that ended the line before it,
        or what is left is not valid SQL and the fixture fails in a way that
        looks like a schema bug. Unlike migrate1_51_0's column, its
        documentation is a block ABOVE the CREATE rather than beside the
        column, so the two are stripped separately - it still goes, so the
        fixture reads as a believable older file rather than one documenting a
        column it does not have."""
        schema = dbModule.SCHEMA
        columnLine = ",\n    " + self.COLUMN_SQL
        self.assertIn(columnLine, schema,
                      "expected carried_canonical_id to be the last column, after a comma")
        schema = schema.replace(columnLine, "", 1)

        start = schema.index(self.COLUMN_COMMENT_START)
        end = schema.index(self.CREATE_START, start)
        return schema[:start] + schema[end:]

    #< closed before returning, not via addCleanup: an inspection connection
    #  left open across the migration blocks its database snapshot on Windows
    def _seedOldDatabase(self, merged=True, canonicalHasOwnGenres=False):
        """A merged pair as a pre-1.53.0 library holds it: the member carries
        the genre row and the attempted stamp, the canonical carries neither."""
        with patch.object(dbModule, "SCHEMA", self._preMigrationSchema()):
            repo = Repository(self.dbPath)
            conn = repo._conn()   #< Repository connects lazily; connecting stamps SCHEMA
            with conn:
                conn.execute("INSERT INTO albums (id, name, url) VALUES ('alb1', 'A', '')")
                for trackId in ("member", "canon"):
                    conn.execute(
                        "INSERT INTO tracks (id, name, url, album_id, lastfm_attempted_at) "
                        "VALUES (?, 'Song', '', 'alb1', 1700000000.0)", (trackId,))
                conn.execute("INSERT INTO track_genres (track_id, genre, position, inherited) "
                             "VALUES ('member', 'rock', 0, 0)")
                if canonicalHasOwnGenres:
                    conn.execute("INSERT INTO track_genres (track_id, genre, position, inherited) "
                                 "VALUES ('canon', 'jazz', 0, 0)")
                if merged:
                    conn.execute("UPDATE tracks SET canonical_id='canon' WHERE id='member'")
                conn.execute(
                    "INSERT INTO track_merge_decisions (track_id, canonical_id, reason, "
                    "decided_at, decided_by) VALUES ('member', 'canon', 'manual-merge', 0, 'tim')")
            repo.commit()
            repo.connectionManager.close()

    def _attempted(self, trackId):
        conn = sqlite3.connect(self.dbPath)
        try:
            return conn.execute("SELECT lastfm_attempted_at FROM tracks WHERE id=?",
                                (trackId,)).fetchone()[0]
        finally:
            conn.close()

    def test_adds_the_column_and_bumps_the_version(self):
        self._seedOldDatabase()
        self.assertNotIn("carried_canonical_id", self._columnNames("track_merge_decisions"))

        migrateModule.Migrator("1.52.0", "1.53.0").migrate()

        self.assertIn("carried_canonical_id", self._columnNames("track_merge_decisions"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.53.0")
        self.assertEqual(dbversion.readDbVersion(self.dbPath), "1.53.0")

    def test_an_existing_verdict_arrives_uncarried(self):
        """NULL reads as "still where the person put it". A verdict the matcher
        had already re-homed cannot be recovered - the target was overwritten
        in place - and inventing one would name a release nobody chose."""
        self._seedOldDatabase()

        migrateModule.Migrator("1.52.0", "1.53.0").migrate()

        conn = sqlite3.connect(self.dbPath)
        try:
            row = conn.execute("SELECT canonical_id, carried_canonical_id "
                               "FROM track_merge_decisions WHERE track_id='member'").fetchone()
            self.assertEqual(row[0], "canon")
            self.assertIsNone(row[1])
        finally:
            conn.close()

    def test_it_requeues_the_canonical_and_leaves_the_member_alone(self):
        self._seedOldDatabase()

        migrateModule.Migrator("1.52.0", "1.53.0").migrate()

        self.assertIsNone(self._attempted("canon"))
        #< the member keeps its stamp AND its genre row: unmerging stays lossless
        self.assertEqual(self._attempted("member"), 1700000000.0)
        conn = sqlite3.connect(self.dbPath)
        try:
            self.assertEqual(
                conn.execute("SELECT COUNT(*) FROM track_genres "
                             "WHERE track_id='member'").fetchone()[0], 1)
        finally:
            conn.close()

    def test_a_canonical_that_already_has_its_own_genres_is_left_alone(self):
        self._seedOldDatabase(canonicalHasOwnGenres=True)

        migrateModule.Migrator("1.52.0", "1.53.0").migrate()

        self.assertEqual(self._attempted("canon"), 1700000000.0)

    def test_an_unmerged_library_is_untouched(self):
        """Scoped to canonicals of real groups: a plain track that came back
        genuinely tag-less is not this lever's business, and requeuing it would
        re-spend Last.fm quota on an answer that has not changed."""
        self._seedOldDatabase(merged=False)

        migrateModule.Migrator("1.52.0", "1.53.0").migrate()

        self.assertEqual(self._attempted("canon"), 1700000000.0)
        self.assertEqual(self._attempted("member"), 1700000000.0)

    def test_a_fresh_database_needs_no_migration(self):
        """SCHEMA carries the column, so a new install is born at 1.53.0's
        shape - the two DDL sites have to agree or an upgraded database and a
        fresh one end up different."""
        repo = Repository(self.dbPath)
        repo._conn()   #< see _seedOldDatabase: connecting is what stamps SCHEMA
        repo.connectionManager.close()

        self.assertIn("carried_canonical_id", self._columnNames("track_merge_decisions"))

    def test_migration_is_idempotent(self):
        self._seedOldDatabase()
        migrateModule.Migrator("1.52.0", "1.53.0").migrate()

        (self.dataDir / "VERSION").write_text("1.52.0", encoding="utf-8")   #< simulate a retry
        dbversion.writeDbVersion(self.dbPath, "1.52.0")
        migrateModule.Migrator("1.52.0", "1.53.0").migrate()   #< must not raise

        self.assertIn("carried_canonical_id", self._columnNames("track_merge_decisions"))
        self.assertEqual((self.dataDir / "VERSION").read_text(encoding="utf-8").strip(), "1.53.0")

    def test_a_rerun_does_not_requeue_a_canonical_already_looked_up(self):
        """The retry must not undo a backfill cycle that has since answered -
        the requeue only matches rows that still carry a stamp and still hold
        no own genre rows."""
        self._seedOldDatabase()
        migrateModule.Migrator("1.52.0", "1.53.0").migrate()
        conn = sqlite3.connect(self.dbPath)
        with conn:
            conn.execute("UPDATE tracks SET lastfm_attempted_at=1800000000.0 WHERE id='canon'")
            conn.execute("INSERT INTO track_genres (track_id, genre, position, inherited) "
                         "VALUES ('canon', 'jazz', 0, 0)")
        conn.close()

        (self.dataDir / "VERSION").write_text("1.52.0", encoding="utf-8")
        dbversion.writeDbVersion(self.dbPath, "1.52.0")
        migrateModule.Migrator("1.52.0", "1.53.0").migrate()

        self.assertEqual(self._attempted("canon"), 1800000000.0)


if __name__ == "__main__":
    unittest.main()
