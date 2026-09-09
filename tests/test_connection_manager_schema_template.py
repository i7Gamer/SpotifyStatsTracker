"""ConnectionManager._newConnection() copies a cached in-memory schema
template via sqlite3.Connection.backup() for brand-new database files instead
of re-running the full DDL script, since backup() is markedly cheaper and
this constructor runs on essentially every test in the suite. These tests
guard the two ways that optimization could go wrong: the copied schema
silently drifting from the real DDL, and the "existing file" fallback path
(which must NOT use backup(), since backup() overwrites its destination)
losing data on reopen.
"""
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import Database.db as dbModule
from Database.db import ConnectionManager, SCHEMA
import sqlite3

DDL_RACE_JOIN_TIMEOUT_SECONDS = 5.0
DDL_FIRST_CALL = 1
DDL_SECOND_CALL = 2


def _schemaDump(conn: sqlite3.Connection) -> list[tuple]:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master WHERE type IN ('table', 'index') ORDER BY name"
    ).fetchall()
    return [tuple(row) for row in rows]


class _ExecutescriptCountingConnection(sqlite3.Connection):
    """sqlite3.Connection is a C type - its methods can't be patched with
    unittest.mock.patch.object, so counting executescript() calls needs a
    real subclass installed via sqlite3.connect(factory=...) instead."""
    call_count = 0

    def executescript(self, *args, **kwargs):
        type(self).call_count += 1
        return super().executescript(*args, **kwargs)


_realSqliteConnect = sqlite3.connect   #< captured before any test patches sqlite3.connect


def _connectCountingExecutescript(dbPath, **kwargs):
    return _realSqliteConnect(dbPath, factory=_ExecutescriptCountingConnection, **kwargs)


class _BlockingExecutescriptConnection(sqlite3.Connection):
    """Coordinates concurrent schema execution without depending on SQLite timeouts."""

    call_count = 0
    first_entered = threading.Event()
    second_settled = threading.Event()
    release = threading.Event()
    lock = threading.Lock()

    @classmethod
    def reset(cls):
        cls.call_count = 0
        cls.first_entered.clear()
        cls.second_settled.clear()
        cls.release.clear()

    def executescript(self, script):
        if script == dbModule.SCHEMA:
            with type(self).lock:
                type(self).call_count += 1
                if type(self).call_count == DDL_FIRST_CALL:
                    type(self).first_entered.set()
                elif type(self).call_count == DDL_SECOND_CALL:
                    type(self).second_settled.set()
            if not type(self).release.wait(DDL_RACE_JOIN_TIMEOUT_SECONDS):
                raise TimeoutError("test did not release schema execution")
        return super().executescript(script)


def _connectBlockingExecutescript(dbPath, **kwargs):
    return _realSqliteConnect(dbPath, factory=_BlockingExecutescriptConnection, **kwargs)


class _ObservedStampLock:
    """Notice a contending opener without waiting for a forbidden second DDL."""

    def __init__(self):
        self.lock = threading.Lock()

    def __enter__(self):
        if not self.lock.acquire(blocking=False):
            _BlockingExecutescriptConnection.second_settled.set()
            if not self.lock.acquire(timeout=DDL_RACE_JOIN_TIMEOUT_SECONDS):
                raise TimeoutError("test schema lock remained held")
        return self

    def __exit__(self, *exc):
        self.lock.release()


class _FailingThenCountingExecutescriptConnection(sqlite3.Connection):
    call_count = 0
    failures_remaining = 1
    lock = threading.Lock()

    @classmethod
    def reset(cls):
        cls.call_count = 0
        cls.failures_remaining = 1

    def executescript(self, script):
        if script == dbModule.SCHEMA:
            with type(self).lock:
                type(self).call_count += 1
                should_fail = type(self).failures_remaining > 0
                if should_fail:
                    type(self).failures_remaining -= 1
            if should_fail:
                self.close()
                raise sqlite3.OperationalError("schema stamp failed")
        return super().executescript(script)


def _connectFailingThenCountingExecutescript(dbPath, **kwargs):
    return _realSqliteConnect(dbPath, factory=_FailingThenCountingExecutescriptConnection, **kwargs)


class TestSchemaTemplateMatchesDirectExecutescript(unittest.TestCase):
    def test_backup_copied_schema_matches_executescript(self):
        """A brand-new file's schema (via the backup() fast path) must be
        byte-for-byte identical to running the DDL script directly - the two
        paths must never be allowed to drift apart."""
        with tempfile.TemporaryDirectory() as tmpDir:
            manager = ConnectionManager(Path(tmpDir) / "viaBackup.db")
            viaBackup = manager.connection()
            try:
                direct = sqlite3.connect(":memory:")
                direct.executescript(SCHEMA)

                self.assertEqual(_schemaDump(viaBackup), _schemaDump(direct))
            finally:
                manager.close()


class TestExistingFileIsNeverOverwritten(unittest.TestCase):
    def test_reopening_an_existing_populated_file_preserves_its_data(self):
        """The backup() fast path only applies to a file this connection is
        the first to create - reopening an existing file (a second
        ConnectionManager against the same path, simulating a restart or a
        second thread) must take the executescript fallback and must never
        wipe what's already there."""
        with tempfile.TemporaryDirectory() as tmpDir:
            dbPath = Path(tmpDir) / "existing.db"

            first = ConnectionManager(dbPath)
            first.connection().execute(
                "INSERT INTO artists (id, name, url) VALUES ('a1', 'Artist', '')"
            )
            first.connection().commit()
            first.close()

            second = ConnectionManager(dbPath)
            try:
                count = second.connection().execute(
                    "SELECT COUNT(*) FROM artists WHERE id='a1'"
                ).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                second.close()

    def test_a_second_connection_racing_the_first_new_file_also_preserves_data(self):
        """Simulates two threads racing to create the SAME brand-new file:
        by the time the second ConnectionManager opens it, the file already
        exists (created by the first), so it must fall back to executescript
        rather than calling backup() and overwriting the first connection's
        already-committed row."""
        with tempfile.TemporaryDirectory() as tmpDir:
            dbPath = Path(tmpDir) / "race.db"

            first = ConnectionManager(dbPath)
            first.connection().execute(
                "INSERT INTO artists (id, name, url) VALUES ('a1', 'Artist', '')"
            )
            first.connection().commit()

            second = ConnectionManager(dbPath)
            try:
                count = second.connection().execute(
                    "SELECT COUNT(*) FROM artists WHERE id='a1'"
                ).fetchone()[0]
                self.assertEqual(count, 1)
            finally:
                first.close()
                second.close()


class TestExistingSchemaDdlRunsOnceProcessWide(unittest.TestCase):
    """A second (or third, or Nth) ConnectionManager opened against a path
    this process has already stamped with the current SCHEMA must not
    re-run executescript(SCHEMA) - even though every statement in it is a
    no-op once its table/index exists, SQLite still takes a write lock to
    check, which can collide with a concurrent writer and raise "database
    is locked" (see Database/db.py's ConnectionManager._newConnection)."""

    def setUp(self):
        _ExecutescriptCountingConnection.call_count = 0

    def test_reopening_an_already_stamped_path_skips_the_ddl_rerun(self):
        with tempfile.TemporaryDirectory() as tmpDir:
            dbPath = Path(tmpDir) / "reused.db"

            first = ConnectionManager(dbPath)
            first.connection()   #< brand-new file: stamps the path via backup()
            first.close()

            with patch.object(dbModule.sqlite3, "connect", side_effect=_connectCountingExecutescript):
                second = ConnectionManager(dbPath)
                try:
                    second.connection()
                finally:
                    second.close()

            self.assertEqual(_ExecutescriptCountingConnection.call_count, 0)

    def test_a_third_connection_manager_also_skips_the_rerun(self):
        """Not just the second opener - every later one in this process."""
        with tempfile.TemporaryDirectory() as tmpDir:
            dbPath = Path(tmpDir) / "reused2.db"

            first = ConnectionManager(dbPath)
            first.connection()
            first.close()

            second = ConnectionManager(dbPath)
            second.connection()
            second.close()

            with patch.object(dbModule.sqlite3, "connect", side_effect=_connectCountingExecutescript):
                third = ConnectionManager(dbPath)
                try:
                    third.connection()
                finally:
                    third.close()

            self.assertEqual(_ExecutescriptCountingConnection.call_count, 0)

    def test_a_schema_change_for_the_same_path_still_forces_a_rerun(self):
        """The skip must never suppress a real lazy-migration catch-up: if
        SCHEMA gains a table after a path was stamped (e.g. a future
        migrator that only adds CREATE TABLE IF NOT EXISTS, relying on it
        appearing on the next connection), reopening that same path with
        the new SCHEMA value must still create it."""
        with tempfile.TemporaryDirectory() as tmpDir:
            dbPath = Path(tmpDir) / "legacyThenCurrent.db"
            removedBlock = (
                "CREATE TABLE IF NOT EXISTS app_settings (\n"
                "    key     TEXT PRIMARY KEY,\n"
                "    value   TEXT NOT NULL\n"
                ");"
            )
            self.assertIn(removedBlock, SCHEMA)   #< sanity: the block we strip really exists
            legacySchema = SCHEMA.replace(removedBlock, "")

            with patch.object(dbModule, "SCHEMA", legacySchema):
                legacyManager = ConnectionManager(dbPath)
                legacyManager.connection()
                legacyManager.close()

            def tableNames():
                conn = sqlite3.connect(dbPath)
                try:
                    return {row[0] for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                finally:
                    conn.close()

            self.assertNotIn("app_settings", tableNames())

            currentManager = ConnectionManager(dbPath)
            try:
                currentManager.connection()
            finally:
                currentManager.close()

            self.assertIn("app_settings", tableNames())

    def test_overlapping_first_existing_file_stamps_run_ddl_once(self):
        """Only the first unstamped opener should run the current DDL."""
        with tempfile.TemporaryDirectory() as tmpDir:
            dbPath = Path(tmpDir) / "overlappingExisting.db"
            first = ConnectionManager(dbPath)
            first.connection().execute(
                "INSERT INTO artists (id, name, url) VALUES ('a1', 'Artist', '')"
            )
            first.connection().commit()
            first.close()

            with dbModule._stampedSchemaLock:
                dbModule._stampedSchemaByPath.pop(dbPath.resolve(), None)

            _BlockingExecutescriptConnection.reset()
            errors = []

            def opener():
                manager = ConnectionManager(dbPath)
                try:
                    manager.connection()
                except Exception as exc:
                    errors.append(repr(exc))
                finally:
                    manager.close()

            with patch.object(dbModule.sqlite3, "connect", side_effect=_connectBlockingExecutescript), \
                 patch.object(dbModule, "_stampedSchemaLock", _ObservedStampLock()):
                first_thread = threading.Thread(target=opener)
                second_thread = threading.Thread(target=opener)
                first_thread.start()
                try:
                    self.assertTrue(_BlockingExecutescriptConnection.first_entered.wait(
                        DDL_RACE_JOIN_TIMEOUT_SECONDS))
                    second_thread.start()
                    # Fixed code blocks at the held stamp lock; old code enters
                    # DDL again. Either signal proves the second opener got there.
                    self.assertTrue(_BlockingExecutescriptConnection.second_settled.wait(
                        DDL_RACE_JOIN_TIMEOUT_SECONDS))
                finally:
                    _BlockingExecutescriptConnection.release.set()
                    first_thread.join(DDL_RACE_JOIN_TIMEOUT_SECONDS)
                    if second_thread.ident is not None:
                        second_thread.join(DDL_RACE_JOIN_TIMEOUT_SECONDS)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(_BlockingExecutescriptConnection.call_count, 1)

    def test_failed_existing_file_stamp_is_retryable(self):
        with tempfile.TemporaryDirectory() as tmpDir:
            dbPath = Path(tmpDir) / "retryAfterFailure.db"
            first = ConnectionManager(dbPath)
            first.connection().execute(
                "INSERT INTO artists (id, name, url) VALUES ('a1', 'Artist', '')"
            )
            first.connection().commit()
            first.close()

            with dbModule._stampedSchemaLock:
                dbModule._stampedSchemaByPath.pop(dbPath.resolve(), None)
            _FailingThenCountingExecutescriptConnection.reset()

            with patch.object(dbModule.sqlite3, "connect",
                              side_effect=_connectFailingThenCountingExecutescript):
                failing = ConnectionManager(dbPath)
                try:
                    with self.assertRaisesRegex(sqlite3.OperationalError,
                                                "schema stamp failed"):
                        failing.connection()
                finally:
                    failing.close()

                with dbModule._stampedSchemaLock:
                    self.assertIsNone(dbModule._stampedSchemaByPath.get(dbPath.resolve()))

                retrying = ConnectionManager(dbPath)
                try:
                    retrying.connection()
                finally:
                    retrying.close()

            self.assertEqual(_FailingThenCountingExecutescriptConnection.call_count, 2)
            with dbModule._stampedSchemaLock:
                self.assertEqual(dbModule._stampedSchemaByPath.get(dbPath.resolve()), SCHEMA)


class TestSchemaTemplateBackupIsThreadSafe(unittest.TestCase):
    """The cached :memory: schema template is the backup() SOURCE for every
    thread that opens a brand-new db file, but it is built by whichever thread
    first needs it - not necessarily the thread that later opens a fresh file.
    With the source created check_same_thread=True (the default), that
    cross-thread backup() raised sqlite3.ProgrammingError. Regression guard.
    """

    def test_a_worker_thread_can_open_a_fresh_db_using_a_main_thread_template(self):
        # Force a clean template built in THIS (main) thread, so the worker
        # below exercises genuine cross-thread use of the source connection.
        with dbModule._schemaTemplateLock:
            dbModule._schemaTemplateConn = None
            dbModule._schemaTemplateSchema = None
        dbModule._getSchemaTemplate()

        errors = []
        with tempfile.TemporaryDirectory() as tmpDir:
            def worker():
                try:
                    manager = ConnectionManager(Path(tmpDir) / "fromWorker.db")
                    try:
                        names = {
                            row[0] for row in manager.connection().execute(
                                "SELECT name FROM sqlite_master WHERE type='table'")
                        }
                        self.assertIn("plays", names)
                    finally:
                        manager.close()
                except Exception as exc:   # report any error to the assertion below
                    errors.append(repr(exc))

            worker_thread = threading.Thread(target=worker)
            worker_thread.start()
            worker_thread.join()

        self.assertEqual(errors, [], f"cross-thread schema init failed: {errors}")


class TestSchemaTemplateRespectsAPatchedSchema(unittest.TestCase):
    """Migration tests (e.g. test_migrate1_23_0.py) patch.object(dbModule,
    "SCHEMA", legacySchema) to build a pre-migration database, relying on a
    brand-new file's executescript(SCHEMA) picking up the patched value. The
    cached template must never let a stale, pre-patch schema leak through."""

    def test_a_schema_patched_after_the_template_was_cached_is_still_honored(self):
        with tempfile.TemporaryDirectory() as tmpDir:
            # Prime the cache with the real SCHEMA first (mirrors most tests
            # in the suite running before a migration test does).
            warmupManager = ConnectionManager(Path(tmpDir) / "warmup.db")
            warmupManager.connection()
            warmupManager.close()

            legacySchema = SCHEMA.replace(
                "is_admin              INTEGER NOT NULL DEFAULT 0,", ""
            )
            self.assertNotEqual(legacySchema, SCHEMA)   #< sanity: the replace matched something

            with patch.object(dbModule, "SCHEMA", legacySchema):
                legacyManager = ConnectionManager(Path(tmpDir) / "legacy.db")
                try:
                    columns = {
                        row["name"]
                        for row in legacyManager.connection().execute("PRAGMA table_info(users)")
                    }
                    self.assertNotIn("is_admin", columns)
                finally:
                    legacyManager.close()

            # Un-patched SCHEMA must still produce the real, current schema
            # for a later, ordinary test.
            afterManager = ConnectionManager(Path(tmpDir) / "after.db")
            try:
                columns = {
                    row["name"]
                    for row in afterManager.connection().execute("PRAGMA table_info(users)")
                }
                self.assertIn("is_admin", columns)
            finally:
                afterManager.close()


if __name__ == "__main__":
    unittest.main()
