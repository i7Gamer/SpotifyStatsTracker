"""The whole migrator chain, run end to end against a throwaway database.

Every migrator has its own focused test, but nothing ever ran them in sequence:
each one is verified against a database shaped exactly the way that test sets it
up, never against the output of the 31 migrators before it. The failure modes
that only appear in sequence - a step that crashes on a database an earlier step
left in a slightly different shape, a version bump with no migrator file behind
it, a precondition that no longer matches the version the previous step wrote -
had no coverage at all. The stale-VERSION-file incident was exactly this class.

Scope: 1.7.0 onward, the first version where spotify_stats.db exists. The
migrators before it convert per-user JSON files and address the real Users/
directory through `self.baseDir` rather than resolveRuntimeDir(), so they can't
be redirected at a temp directory - and an install still on them has no database
to chain-migrate in the first place.

SAFETY: this repository's Database/Data/ holds real listening history. Every
test here redirects resolveRuntimeDir (in both modules that call it, since each
migrator binds base's copy when it is loaded) at a temp directory, and
test_the_real_runtime_directory_is_never_touched pins that redirection.
"""
import contextlib
import shutil
import sqlite3
import sys
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.Migrators.base as migratorBase
import Database.Migrators.migrate as migrateModule
from Database.Migrators import dbversion
from Database.Migrators.base import BaseMigrator
from Database.repository import Repository

MIGRATORS_DIR = Path(migrateModule.__file__).resolve().parent
APP_VERSION = (MIGRATORS_DIR / ".." / "VERSION").read_text().strip()

# The first version whose runtime data is a database rather than JSON files.
OLDEST_DB_ERA_VERSION = "1.7.0"


def _versionsInChain(fromVersion: str, toVersion: str) -> list[str]:
    """Every version a database passes through, excluding the destination."""
    major, minor = BaseMigrator.getMajorMinor(fromVersion)
    endMajor, endMinor = BaseMigrator.getMajorMinor(toVersion)
    assert major == endMajor, "this helper only walks minor versions"
    return [f"{major}.{m}.0" for m in range(minor, endMinor)]


class MigrationChainTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.runtimeDir = Path(self._tmpdir.name) / "Data"
        self.runtimeDir.mkdir(parents=True)
        self.dbPath = self.runtimeDir / "spotify_stats.db"

    @contextlib.contextmanager
    def _redirectedRuntimeDir(self):
        """Point every runtime-dir lookup at the temp directory.

        Both modules need it: migrateIfNeeded() calls its own imported copy, and
        each migrator binds base's copy at load time (they're loaded fresh via
        importlib per step, so patching base reaches them)."""
        with patch.object(migratorBase, "resolveRuntimeDir", return_value=self.runtimeDir), \
             patch.object(migrateModule, "resolveRuntimeDir", return_value=self.runtimeDir):
            yield

    def _seedDatabase(self, version=OLDEST_DB_ERA_VERSION):
        """A database with a little real data, stamped at `version`.

        The tables come from the app's current schema because ConnectionManager
        stamps it on every connect - which is also what happens on a real
        install the moment the new app opens an old file, so the migrators'
        actual job is the column ALTERs and data rewrites, not table creation.
        """
        repo = Repository(self.dbPath)
        try:
            repo.upsertUser("alice", "alice@example.com")
            repo.upsertTrack({
                "id": "t1", "name": "Song One", "url": "http://example.com/track/t1",
                "artists": [{"id": "art1", "name": "Artist One", "url": "", "imageUrl": "", "imageId": "art1"}],
                "album": {"id": "alb1", "name": "Album One", "url": "", "imageId": "alb1",
                           "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
                "imageUrl": "", "imageId": "alb1", "duration": 200000, "explicit": False,
                "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            })
            repo.insertPlay("alice", "t1", 1000.0, 200000)
            repo.commit()
        finally:
            repo.connectionManager.close()

        dbversion.writeDbVersion(self.dbPath, version)
        (self.runtimeDir / "VERSION").write_text(version)

    def _runChain(self):
        with self._redirectedRuntimeDir():
            migrateModule.migrateIfNeeded()

    def _columns(self, table):
        conn = sqlite3.connect(self.dbPath)
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        finally:
            conn.close()

    def _playCount(self):
        conn = sqlite3.connect(self.dbPath)
        try:
            return conn.execute("SELECT COUNT(*) FROM plays").fetchone()[0]
        finally:
            conn.close()


class TestFullChain(MigrationChainTestCase):
    def test_a_db_era_database_migrates_all_the_way_to_the_current_version(self):
        """One scenario, three independent postconditions.

        These were three test methods that each re-ran the identical
        seed-then-migrate scenario - ~330 ms of real chain work per run, for
        assertions that all describe the same outcome. subTest (used elsewhere
        in this file) keeps them reporting and failing independently while the
        chain runs once."""
        self._seedDatabase()

        self._runChain()

        with self.subTest("the database's own version marker is current"):
            self.assertEqual(dbversion.readDbVersion(self.dbPath), APP_VERSION)

        with self.subTest("the sibling VERSION file ends up in step"):
            self.assertEqual((self.runtimeDir / "VERSION").read_text().strip(), APP_VERSION)

        with self.subTest("existing data survives the whole chain"):
            self.assertEqual(self._playCount(), 1)
            conn = sqlite3.connect(self.dbPath)
            try:
                self.assertIsNotNone(
                    conn.execute("SELECT 1 FROM users WHERE username='alice'").fetchone())
                self.assertIsNotNone(
                    conn.execute("SELECT 1 FROM tracks WHERE id='t1'").fetchone())
            finally:
                conn.close()

    def test_a_column_a_migrator_adds_is_present_afterwards(self):
        """Proves the ALTER path actually ran rather than every step no-opping:
        the column is removed before migrating and must come back."""
        self._seedDatabase()
        conn = sqlite3.connect(self.dbPath)
        try:
            conn.execute("ALTER TABLE users DROP COLUMN hide_tags_panel")
            conn.commit()
        finally:
            conn.close()
        self.assertNotIn("hide_tags_panel", self._columns("users"))

        self._runChain()

        self.assertIn("hide_tags_panel", self._columns("users"))

    def test_running_the_chain_again_is_a_no_op(self):
        """Migrations run at every app boot, so a second start right after an
        upgrade must not re-run anything or trip a precondition."""
        self._seedDatabase()
        self._runChain()

        self._runChain()   #< must not raise

        self.assertEqual(dbversion.readDbVersion(self.dbPath), APP_VERSION)
        self.assertEqual(self._playCount(), 1)

    def test_an_already_current_database_is_left_alone(self):
        self._seedDatabase(version=APP_VERSION)

        self._runChain()

        self.assertEqual(dbversion.readDbVersion(self.dbPath), APP_VERSION)
        self.assertEqual(self._playCount(), 1)


class TestChainCompleteness(MigrationChainTestCase):
    def test_every_version_between_the_db_era_and_now_has_a_migrator(self):
        """Bumping VERSION without adding the matching migrator file leaves the
        chain unable to reach the new version - migrateIfNeeded would loop
        looking for a file that doesn't exist."""
        missing = [
            version for version in _versionsInChain(OLDEST_DB_ERA_VERSION, APP_VERSION)
            if not (MIGRATORS_DIR / f"migrate{version.replace('.0', '', 1).replace('.', '_')}_0.py").exists()
        ]

        self.assertEqual(missing, [])

    def test_each_migrator_hands_off_to_the_version_the_next_one_expects(self):
        """A gap or overlap here would make the chain stall at that step: each
        module's __main__ block declares its own from/to pair."""
        versions = _versionsInChain(OLDEST_DB_ERA_VERSION, APP_VERSION)
        for version in versions:
            major, minor = BaseMigrator.getMajorMinor(version)
            path = MIGRATORS_DIR / f"migrate{major}_{minor}_0.py"
            with self.subTest(version=version):
                source = path.read_text(encoding="utf-8")
                self.assertIn(f'Migrator("{major}.{minor}.0", "{major}.{minor + 1}.0")', source)
                self.assertIn(f'self.updateAppVersion("{major}.{minor + 1}.0")', source)


class TestVersionMarkerSurvivesACopy(MigrationChainTestCase):
    """The reason the marker moved inside the database: a sibling VERSION file
    stays behind when the .db is copied, so a restored backup used to be
    migrated against whatever version the directory happened to claim."""

    def test_a_copied_database_carries_its_own_version(self):
        self._seedDatabase()
        self._runChain()

        copied = Path(self._tmpdir.name) / "restored.db"
        shutil.copy2(self.dbPath, copied)

        self.assertEqual(dbversion.readDbVersion(copied), APP_VERSION)

    def test_a_copy_restored_without_a_version_file_still_migrates(self):
        self._seedDatabase()
        restoreDir = Path(self._tmpdir.name) / "restored"
        restoreDir.mkdir()
        shutil.copy2(self.dbPath, restoreDir / "spotify_stats.db")   #< no sibling VERSION
        self.runtimeDir = restoreDir
        self.dbPath = restoreDir / "spotify_stats.db"

        self._runChain()

        self.assertEqual(dbversion.readDbVersion(self.dbPath), APP_VERSION)


class TestPreMigrationSnapshot(MigrationChainTestCase):
    """Migrations run automatically at every boot, and the backup worker's own
    scheduled snapshot deliberately waits out a startup delay so it doesn't race
    them - which means that without this one-off snapshot, the newest recovery
    point available to someone whose upgrade went wrong could be a full
    BACKUP_INTERVAL_HOURS (24 by default) old.

    It is the only thing standing between a buggy migrator and unrecoverable
    data loss, and nothing exercised it. Worth being explicit about what it is
    NOT: no integrity probe can detect a migrator that drops a table or deletes
    rows - PRAGMA quick_check reports a perfectly healthy file afterwards,
    because a smaller database is not a damaged one. Restoring the snapshot is
    the whole recovery story, so the snapshot has to be there.
    """

    def _snapshotWith(self, backupWorker):
        with patch("Database.backup.BackupWorker", backupWorker):
            migrateModule._snapshotBeforeMigrating(self.runtimeDir)

    def test_an_existing_database_is_snapshotted(self):
        self._seedDatabase()
        worker = MagicMock()

        self._snapshotWith(worker)

        worker.assert_called_once_with(dbPath=self.dbPath)
        worker.return_value.runBackup.assert_called_once()

    def test_a_fresh_install_with_no_database_is_a_silent_no_op(self):
        """Nothing to protect yet (a fresh install, or the pre-1.7.0 JSON era).
        Constructing a BackupWorker against a path with no file behind it would
        create an empty Backups/ directory and log a failure on every first
        boot, for a database that does not exist."""
        worker = MagicMock()

        self._snapshotWith(worker)   #< no _seedDatabase call

        worker.assert_not_called()

    def test_a_failed_snapshot_does_not_block_startup(self):
        """Best-effort on purpose: migrating with no extra safety net still
        beats an instance that refuses to boot because its disk is full. The
        alternative is an upgrade that bricks the app rather than the data."""
        self._seedDatabase()
        worker = MagicMock()
        worker.return_value.runBackup.side_effect = OSError("No space left on device")

        self._snapshotWith(worker)   # must not raise

        worker.return_value.runBackup.assert_called_once()

    def test_the_snapshot_is_taken_before_the_first_migrator_runs(self):
        """The ordering IS the feature - a snapshot taken after the chain has
        started captures a half-migrated database, which is worse than useless
        as a recovery point. Pinned by recording the version marker at the
        moment the snapshot is asked for: it must still read the seeded
        version, not anything the chain went on to write."""
        self._seedDatabase()
        versionAtSnapshotTime = []

        def recordingSnapshot(runtimeDir):
            versionAtSnapshotTime.append(dbversion.readDbVersion(self.dbPath))

        with patch.object(migrateModule, "_snapshotBeforeMigrating", recordingSnapshot):
            self._runChain()

        self.assertEqual(versionAtSnapshotTime, [OLDEST_DB_ERA_VERSION])
        self.assertEqual(dbversion.readDbVersion(self.dbPath), APP_VERSION)   #< the chain still ran

    def test_no_snapshot_is_taken_when_there_is_nothing_to_migrate(self):
        """An up-to-date database boots many times a day; a snapshot on every
        one of them would rotate the genuinely useful ones out of retention."""
        self._seedDatabase(version=APP_VERSION)

        with patch.object(migrateModule, "_snapshotBeforeMigrating") as mockSnapshot:
            self._runChain()

        mockSnapshot.assert_not_called()


class TestTheRealRuntimeDirectoryIsNeverTouched(MigrationChainTestCase):
    """Guards the guard: this repository's Database/Data/ holds real listening
    history, so a redirection that silently stopped working would run 32
    migrations against it."""

    def test_the_runtime_dir_lookup_is_redirected_for_both_callers(self):
        with self._redirectedRuntimeDir():
            self.assertEqual(migratorBase.resolveRuntimeDir(MIGRATORS_DIR), self.runtimeDir)
            self.assertEqual(migrateModule.resolveRuntimeDir(MIGRATORS_DIR), self.runtimeDir)

    def test_a_migrator_instance_resolves_paths_inside_the_temp_dir(self):
        self._seedDatabase()
        with self._redirectedRuntimeDir():
            migrator = BaseMigrator(OLDEST_DB_ERA_VERSION, "1.8.0")

            self.assertEqual(migrator.dbPath, self.dbPath)
            self.assertEqual(migrator.databaseVersionFile, self.runtimeDir / "VERSION")


if __name__ == "__main__":
    unittest.main()
