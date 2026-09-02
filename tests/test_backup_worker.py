"""Automatic scheduled backups of the shared SQLite database.

The README's manual `docker compose exec ... backup` command protects nobody
who doesn't run it. The BackupWorker snapshots the database on a schedule
using SQLite's online backup API (safe against a live WAL database), rotates
old snapshots, and is restart-safe: whether a backup is due is judged from
the newest existing backup file, not from process start time.
"""
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.backup as backupModule
from Database.backup import BackupWorker


def _makeSourceDb(path: Path):
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE plays (id INTEGER PRIMARY KEY, note TEXT)")
    conn.execute("INSERT INTO plays (note) VALUES ('keep me safe')")
    conn.commit()
    conn.close()


# Bound for waits that should return immediately in a correct run - they exist
# to turn a hang into a failure, not to give slow machines "enough" time. A
# blocked worker is held on an unbounded Event released in a finally, so no
# hold can expire early under load (the suite runs under xdist by default).
HANG_TIMEOUT_SECONDS = 10


class BackupWorkerTestCase(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.root = Path(self._tmpdir.name)
        self.dbPath = self.root / "spotify_stats.db"
        _makeSourceDb(self.dbPath)

    def _makeWorker(self, **kwargs):
        return BackupWorker(dbPath=self.dbPath, **kwargs)


class TestRunBackup(BackupWorkerTestCase):
    def test_backup_creates_a_readable_snapshot(self):
        worker = self._makeWorker()

        backupPath = worker.runBackup()

        self.assertTrue(backupPath.exists())
        self.assertEqual(backupPath.parent, self.root / "Backups")
        self.assertTrue(backupPath.name.startswith(backupModule.BACKUP_FILENAME_PREFIX))
        conn = sqlite3.connect(backupPath)
        self.addCleanup(conn.close)
        rows = conn.execute("SELECT note FROM plays").fetchall()
        self.assertEqual(rows, [("keep me safe",)])

    def test_the_source_connection_waits_out_a_lock(self):
        """3101c8d's fix, which had no test.

        Every ConnectionManager connection waits out a lock; this one is opened
        raw with sqlite3.connect and so did not, meaning a snapshot starting
        while a checkpoint or VACUUM held the file raised "database is locked"
        instantly. The scheduled loop swallows that into a skipped backup and the
        admin button reports an error - for a wait the rest of the app makes
        happily. Dropping the pragma leaves every other test in this file green.

        Asserted through the trace hook: the pragma's effect (a 5s wait) is not
        something a test should sit through, and reading it back would need the
        same connection runBackup owns and closes."""
        statements = []
        realConnect = sqlite3.connect

        def tracingConnect(path, *args, **kwargs):
            conn = realConnect(path, *args, **kwargs)
            if str(path) == str(self.dbPath):
                conn.set_trace_callback(statements.append)
            return conn

        with patch.object(backupModule.sqlite3, "connect", tracingConnect):
            self._makeWorker().runBackup()

        self.assertIn(f"PRAGMA busy_timeout = {backupModule.BACKUP_BUSY_TIMEOUT_MS}", statements)

    def test_the_backup_timeout_matches_the_apps_own(self):
        """The constant is deliberately duplicated rather than imported (see its
        comment), so nothing else keeps the two in step."""
        from Database.db import SQLITE_BUSY_TIMEOUT_MS

        self.assertEqual(backupModule.BACKUP_BUSY_TIMEOUT_MS, SQLITE_BUSY_TIMEOUT_MS)

    def test_backup_of_a_missing_source_raises_instead_of_an_empty_snapshot(self):
        """sqlite3.connect() would CREATE a missing source, so a misconfigured
        path used to produce a valid-looking but EMPTY snapshot. It must raise."""
        self.dbPath.unlink()
        worker = self._makeWorker()
        with self.assertRaises(FileNotFoundError):
            worker.runBackup()

    def test_runbackup_sweeps_stale_partial_files(self):
        """A crash mid-backup leaves a .partial; rotation only looks at *.db, so
        without a sweep they accumulate forever (each a full copy)."""
        backupDir = self.root / "Backups"
        backupDir.mkdir(parents=True, exist_ok=True)
        stale = backupDir / f"{backupModule.BACKUP_FILENAME_PREFIX}20200101_000000.partial"
        stale.write_bytes(b"leftover from a crashed backup")

        self._makeWorker().runBackup()

        self.assertFalse(stale.exists())

    def test_backup_captures_committed_but_uncheckpointed_wal_data(self):
        """Guards the exact risk runBackup() exists to avoid: a raw copy of
        just spotify_stats.db would miss rows sitting in the -wal file that
        haven't been checkpointed into the main file yet. sqlite3.Connection.backup()
        is WAL-aware and must capture them regardless of checkpoint state."""
        conn = sqlite3.connect(self.dbPath)
        self.addCleanup(conn.close)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("INSERT INTO plays (note) VALUES ('still in the wal')")
        conn.commit()
        # Sanity check the scenario is real: the write actually landed in the
        # WAL file rather than being auto-checkpointed into the main file.
        self.assertTrue((self.root / "spotify_stats.db-wal").exists())

        worker = self._makeWorker()
        backupPath = worker.runBackup()

        snapshot = sqlite3.connect(backupPath)
        self.addCleanup(snapshot.close)
        notes = {row[0] for row in snapshot.execute("SELECT note FROM plays")}
        self.assertEqual(notes, {"keep me safe", "still in the wal"})

    def test_a_failed_copy_does_not_leave_its_partial_behind(self):
        """The sweep at the top of the next run collects it eventually, but
        "eventually" is the next scheduled backup - a full-size copy of the
        database sits in Backups/ until then, and on a disk-full failure that
        is the one thing that must not happen."""
        worker = self._makeWorker()
        realConnect = sqlite3.connect

        class _SourceWhoseCopyFails:
            """sqlite3.Connection is an immutable C type, so the failure is
            injected by wrapping the SOURCE connection - the destination stays
            real, because the .partial file it creates is the point."""
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def backup(self, *args, **kwargs):
                raise sqlite3.OperationalError("disk I/O error")

        def connectFailingSource(path, *args, **kwargs):
            conn = realConnect(path, *args, **kwargs)
            return _SourceWhoseCopyFails(conn) if Path(path) == self.dbPath else conn

        with patch.object(backupModule.sqlite3, "connect", side_effect=connectFailingSource):
            with self.assertRaises(sqlite3.OperationalError):
                worker.runBackup()

        leftovers = sorted(p.name for p in (self.root / "Backups").iterdir())
        self.assertEqual(leftovers, [])

    def test_a_cleanup_that_itself_fails_does_not_hide_why_the_copy_failed(self):
        """The whole point of the cleanup is a disk-full copy, and on Windows a
        scanner holding the freshly-written .partial for a moment is exactly
        when the unlink loses. Letting that PermissionError replace the
        OperationalError would answer "why did my backup fail?" with the
        janitor's problem instead of the disk's."""
        worker = self._makeWorker()
        realConnect = sqlite3.connect
        copyFailure = sqlite3.OperationalError("database or disk is full")

        class _SourceWhoseCopyFails:
            def __init__(self, conn):
                self._conn = conn

            def __getattr__(self, name):
                return getattr(self._conn, name)

            def backup(self, *args, **kwargs):
                raise copyFailure

        def connectFailingSource(path, *args, **kwargs):
            conn = realConnect(path, *args, **kwargs)
            return _SourceWhoseCopyFails(conn) if Path(path) == self.dbPath else conn

        realUnlink = Path.unlink
        ourBackupDir = self.root / "Backups"

        def unlinkThatLoses(path, missing_ok=False):
            # Only this test's own .partial: patch.object on Path.unlink is
            # process-wide, ".partial" is a suffix three subsystems use, and the
            # suite runs worker threads from other tests in the same process.
            # Everything else gets the real one.
            if path.suffix == ".partial" and path.parent == ourBackupDir:
                raise PermissionError("[WinError 32] used by another process")
            return realUnlink(path, missing_ok=missing_ok)

        with patch.object(backupModule.sqlite3, "connect", side_effect=connectFailingSource):
            with patch.object(Path, "unlink", unlinkThatLoses):
                with self.assertRaises(sqlite3.OperationalError) as raised:
                    worker.runBackup()

        self.assertIs(raised.exception, copyFailure)
        # And the file is simply left for the next run's sweep - no retry, no
        # rename. (That the cleanup is ATTEMPTED at all is the sibling test
        # above; this one is about what happens when it loses.)
        leftovers = sorted(p.name for p in (self.root / "Backups").iterdir())
        self.assertEqual(len(leftovers), 1)
        self.assertTrue(leftovers[0].endswith(".partial"))

    def test_no_partial_files_left_behind(self):
        worker = self._makeWorker()

        worker.runBackup()

        leftovers = [p for p in (self.root / "Backups").iterdir() if p.suffix != ".db"]
        self.assertEqual(leftovers, [])

    def test_rotation_keeps_only_the_newest_snapshots(self):
        worker = self._makeWorker(retentionCount=2)
        backupDir = self.root / "Backups"
        backupDir.mkdir()
        # Pre-existing older snapshots (timestamped names sort chronologically).
        for stamp in ("20250101_000000", "20250102_000000", "20250103_000000"):
            (backupDir / f"{backupModule.BACKUP_FILENAME_PREFIX}{stamp}.db").write_bytes(b"old")

        newest = worker.runBackup()

        remaining = sorted(p.name for p in backupDir.iterdir())
        self.assertEqual(len(remaining), 2)
        self.assertIn(newest.name, remaining)
        self.assertIn(f"{backupModule.BACKUP_FILENAME_PREFIX}20250103_000000.db", remaining)

    def test_rotation_ignores_unrelated_files(self):
        worker = self._makeWorker(retentionCount=1)
        backupDir = self.root / "Backups"
        backupDir.mkdir()
        unrelated = backupDir / "my-manual-copy.db"
        unrelated.write_bytes(b"mine")

        worker.runBackup()

        self.assertTrue(unrelated.exists())

    def test_zero_retention_rotates_nothing(self):
        """retentionCount=0 disables scheduled backups (isEnabled is False), so
        a DIRECT runBackup() call must rotate nothing - not read 0 as "keep
        zero snapshots" and delete every existing snapshot including the one
        runBackup() itself just wrote."""
        worker = self._makeWorker(retentionCount=0)
        backupDir = self.root / "Backups"
        backupDir.mkdir()
        preexisting = backupDir / f"{backupModule.BACKUP_FILENAME_PREFIX}20250101_000000.db"
        preexisting.write_bytes(b"old")

        newest = worker.runBackup()

        self.assertTrue(newest.exists())
        self.assertTrue(preexisting.exists())


class TestConcurrentBackups(BackupWorkerTestCase):
    """The scheduled loop and the admin's Create Backup Now button are two
    threads calling the same code. Overlapping runs swept each other's
    .partial file mid-write (a PermissionError on Windows, where an open
    handle can't be unlinked) and, starting in the same second, wrote the same
    path from two connections - so os.replace could promote an interleaved
    copy to a valid-looking snapshot."""

    def test_a_second_backup_waits_instead_of_overlapping(self):
        """The invariant is the interleaving, not any duration: whatever the
        scheduler does, one snapshot must fully finish before the next starts.

        The recorded start/end sequence proves that on its own - an overlap
        would show up as start/start. There is deliberately no "is the second
        thread still alive after N ms" check: that asserted a timing symptom of
        blocking rather than the property itself, and it could not tell a
        thread waiting on the lock apart from one the OS simply hadn't
        scheduled yet."""
        worker = self._makeWorker()
        overlapped = []
        inFlight = threading.Event()
        release = threading.Event()
        realRun = worker._runBackupLocked

        def slowRun():
            overlapped.append("start")
            inFlight.set()
            release.wait()   #< unbounded; released in the finally below
            try:
                return realRun()
            finally:
                overlapped.append("end")

        try:
            with patch.object(worker, "_runBackupLocked", slowRun):
                first = threading.Thread(target=worker.runBackup, daemon=True)
                first.start()
                #< a hang detector, not a race: slowRun sets this immediately
                self.assertTrue(inFlight.wait(HANG_TIMEOUT_SECONDS))

                second = threading.Thread(target=worker.runBackup, daemon=True)
                second.start()

                release.set()
                first.join(timeout=HANG_TIMEOUT_SECONDS)
                second.join(timeout=HANG_TIMEOUT_SECONDS)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
        finally:
            release.set()

        self.assertEqual(overlapped, ["start", "end", "start", "end"])

    def test_two_separate_workers_are_serialized_against_each_other(self):
        """The lock has to be process-wide, not per instance.

        app.py holds one BackupWorker, but Migrators/migrate.py builds its own
        ad-hoc one for the pre-migration snapshot - so a per-instance lock left
        exactly those two free to overlap while runBackup's docstring promised
        they could not. Unreachable in today's startup order (migrations finish
        before backupWorker.start()), but the promise should hold for the next
        caller rather than for the current call graph.

        Same interleaving assertion as above, across two instances."""
        scheduled = self._makeWorker()
        adHoc = self._makeWorker()      #< what migrate.py constructs
        overlapped = []
        inFlight = threading.Event()
        release = threading.Event()
        realRun = scheduled._runBackupLocked

        def slowRun():
            overlapped.append("start")
            inFlight.set()
            release.wait()   #< unbounded; released in the finally below
            try:
                return realRun()
            finally:
                overlapped.append("end")

        try:
            with patch.object(scheduled, "_runBackupLocked", slowRun):
                first = threading.Thread(target=scheduled.runBackup, daemon=True)
                first.start()
                self.assertTrue(inFlight.wait(HANG_TIMEOUT_SECONDS))

                #< the OTHER instance, which used to hold its own lock
                second = threading.Thread(target=adHoc.runBackup, daemon=True)
                second.start()
                self.assertTrue(adHoc.isBackupRunning(), "isBackupRunning must see the whole process")

                release.set()
                first.join(timeout=HANG_TIMEOUT_SECONDS)
                second.join(timeout=HANG_TIMEOUT_SECONDS)
                self.assertFalse(first.is_alive())
                self.assertFalse(second.is_alive())
        finally:
            release.set()

        self.assertEqual(overlapped, ["start", "end"])   #< only the patched one records

    def test_is_backup_running_reports_an_in_flight_snapshot(self):
        worker = self._makeWorker()
        inFlight = threading.Event()
        release = threading.Event()

        def slowRun():
            inFlight.set()
            release.wait()   #< unbounded; released in the finally below
            return self.root / "Backups" / "fake.db"

        self.assertFalse(worker.isBackupRunning())
        try:
            with patch.object(worker, "_runBackupLocked", slowRun):
                thread = threading.Thread(target=worker.runBackup, daemon=True)
                thread.start()
                #< a hang detector, not a race: slowRun sets this immediately
                self.assertTrue(inFlight.wait(HANG_TIMEOUT_SECONDS))

                self.assertTrue(worker.isBackupRunning())

                release.set()
                thread.join(timeout=HANG_TIMEOUT_SECONDS)
                self.assertFalse(thread.is_alive())
        finally:
            release.set()
        self.assertFalse(worker.isBackupRunning())


class TestIsDue(BackupWorkerTestCase):
    def test_due_when_no_backup_exists_yet(self):
        self.assertTrue(self._makeWorker().isDue())

    def test_not_due_right_after_a_backup(self):
        worker = self._makeWorker()
        worker.runBackup()
        self.assertFalse(worker.isDue())

    def test_due_again_once_the_newest_backup_is_older_than_the_interval(self):
        worker = self._makeWorker(intervalHours=1)
        backupPath = worker.runBackup()
        oldTime = time.time() - 2 * 3600
        os.utime(backupPath, (oldTime, oldTime))

        self.assertTrue(worker.isDue())

    def test_due_when_the_newest_backup_is_ahead_of_the_system_clock(self):
        """A clock that stepped backward (or a file with a bogus future mtime)
        must not permanently wedge isDue() into "never due again" - the naive
        `time.time() - newest` comparison never crosses the interval threshold
        again once the delta goes negative, so a skewed clock would silently
        stop scheduled backups forever."""
        worker = self._makeWorker(intervalHours=1)
        backupPath = worker.runBackup()
        futureTime = time.time() + 24 * 3600
        os.utime(backupPath, (futureTime, futureTime))

        with self.assertLogs(backupModule.logger, level="WARNING") as logs:
            self.assertTrue(worker.isDue())

        self.assertTrue(any(str(backupPath) in message for message in logs.output),
                        f"expected a warning naming {backupPath} in {logs.output}")


class TestConfiguration(BackupWorkerTestCase):
    def test_disabled_via_zero_interval(self):
        worker = self._makeWorker(intervalHours=0)
        self.assertFalse(worker.isEnabled())

    def test_disabled_via_zero_retention(self):
        worker = self._makeWorker(retentionCount=0)
        self.assertFalse(worker.isEnabled())

    def test_env_vars_override_defaults(self):
        env = {
            backupModule.BACKUP_INTERVAL_ENV_VAR: "6",
            backupModule.BACKUP_RETENTION_ENV_VAR: "3",
        }
        with patch.dict(os.environ, env):
            worker = self._makeWorker()
        self.assertEqual(worker.intervalHours, 6)
        self.assertEqual(worker.retentionCount, 3)

    def test_the_backup_directory_can_be_pointed_off_the_data_disk(self):
        """Snapshots default to Backups/ INSIDE the data directory, which means
        they share a disk with the database they exist to survive. One failure
        takes the live file and every snapshot of it together.

        Interval and retention were already env-configurable; the destination
        was the one knob that needed a code change, so nobody could move it
        without one. An operator can now point it at another disk, or at a
        mount of somewhere else entirely."""
        target = self.root / "elsewhere" / "snapshots"

        with patch.dict(os.environ, {backupModule.BACKUP_DIR_ENV_VAR: str(target)}):
            worker = self._makeWorker()

        self.assertEqual(target, worker.backupDir)

    def test_the_configured_directory_is_where_a_snapshot_actually_lands(self):
        """Not just the attribute: the directory has to be created and written
        to, including when it does not exist yet - the whole point is a path on
        a disk this app has never touched."""
        target = self.root / "elsewhere" / "snapshots"

        with patch.dict(os.environ, {backupModule.BACKUP_DIR_ENV_VAR: str(target)}):
            backupPath = self._makeWorker().runBackup()

        self.assertEqual(target, backupPath.parent)
        self.assertTrue(backupPath.exists())
        #< and nothing was written to the default location beside the database
        self.assertFalse((self.root / "Backups").exists())

    def test_an_explicit_argument_still_wins_over_the_variable(self):
        """The argument is the test seam - no production caller passes it (the
        pre-migration snapshot deliberately does NOT, so BACKUP_DIR redirects
        it too; test_migration_chain pins that call shape). It has to beat the
        variable so a test can pin a location the environment can't move."""
        explicit = self.root / "explicit"

        with patch.dict(os.environ, {backupModule.BACKUP_DIR_ENV_VAR: str(self.root / "from-env")}):
            worker = self._makeWorker(backupDir=explicit)

        self.assertEqual(explicit, worker.backupDir)

    def test_a_blank_variable_is_the_default_not_the_working_directory(self):
        """`BACKUP_DIR=` in a compose file is "I did not set this", not "write
        snapshots to the process's cwd" - which for the container is /app, a
        layer that does not survive `docker compose pull`."""
        with patch.dict(os.environ, {backupModule.BACKUP_DIR_ENV_VAR: "   "}):
            worker = self._makeWorker()

        self.assertEqual(self.root / "Backups", worker.backupDir)

    def test_junk_env_values_fall_back_to_defaults(self):
        env = {
            backupModule.BACKUP_INTERVAL_ENV_VAR: "banana",
            backupModule.BACKUP_RETENTION_ENV_VAR: "",
        }
        with patch.dict(os.environ, env):
            worker = self._makeWorker()
        self.assertEqual(worker.intervalHours, backupModule.DEFAULT_BACKUP_INTERVAL_HOURS)
        self.assertEqual(worker.retentionCount, backupModule.DEFAULT_BACKUP_RETENTION_COUNT)

    def test_a_negative_env_value_disables_rather_than_reverting_to_the_default(self):
        """Deliberately NOT the junk path above. A negative number is a parseable
        intent to switch backups off, so clamping it to 0 (which isEnabled reads
        as disabled) honours it, while falling back to the 24h default would
        quietly keep writing snapshots for someone who asked for none.

        Clamping is also what keeps a negative value from meaning "always due":
        isDue compares against intervalHours * 3600, so a raw -1 would make
        every check due and back the database up every 15 minutes forever."""
        for raw in ("-1", "-24"):
            with self.subTest(raw=raw):
                with patch.dict(os.environ, {backupModule.BACKUP_INTERVAL_ENV_VAR: raw}):
                    worker = self._makeWorker()

                self.assertEqual(worker.intervalHours, 0)
                self.assertFalse(worker.isEnabled())


class TestBackupTelemetry(BackupWorkerTestCase):
    """A scheduled backup that fails is logged and retried on the next check -
    which is correct, and was also the whole story: nothing counted the
    failures, so /admin's Worker Health card reported the service as RUNNING
    whether it had been succeeding or failing every 15 minutes for a month.
    The per-user workers already record cycle outcomes (WorkerTelemetryMixin);
    this one now does too, through the same mixin and the same FAILING
    threshold."""

    def _runCycles(self, worker, outcomes):
        """Drive one loop pass per entry in `outcomes` - an Exception to raise,
        anything else for a clean backup - then stop. No clock involved: the
        check interval is patched to 0 and the loop exits on the pass after the
        last outcome."""
        stopEvent = threading.Event()
        remaining = list(outcomes)

        def fakeIsDue():
            #< nothing due on the pass after the last outcome, so the wind-down
            #  records no cycle of its own and can't reset what was counted
            if not remaining:
                stopEvent.set()
                return False
            return True

        def fakeBackup():
            outcome = remaining.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return self.root / "backup.db"

        with patch.object(worker, "isDue", side_effect=fakeIsDue), \
             patch.object(worker, "runBackup", side_effect=fakeBackup), \
             patch("Database.backup.random.randint", return_value=0), \
             patch.object(backupModule, "BACKUP_CHECK_INTERVAL_SECONDS", 0):
            worker._loop(stopEvent)

    def test_a_failing_backup_is_counted_with_its_error(self):
        worker = self._makeWorker()

        self._runCycles(worker, [sqlite3.OperationalError("database is locked")])

        summary = worker.getSummary()
        self.assertEqual(summary["consecutive_failures"], 1)
        self.assertEqual(summary["failure_rate"], 1.0)
        self.assertIn("database is locked", summary["last_error"])

    def test_consecutive_failures_accumulate(self):
        worker = self._makeWorker()

        self._runCycles(worker, [OSError("disk full")] * 3)

        self.assertEqual(worker.getSummary()["consecutive_failures"], 3)

    def test_a_success_clears_the_consecutive_count_but_not_the_rate(self):
        """The rate is what still says "this has been unhealthy" after one
        lucky cycle."""
        worker = self._makeWorker()

        self._runCycles(worker, [OSError("disk full"), None])

        summary = worker.getSummary()
        self.assertEqual(summary["consecutive_failures"], 0)
        self.assertEqual(summary["failure_rate"], 0.5)

    def test_a_cycle_with_nothing_due_records_nothing(self):
        """The mixin counts cycles that actually ran - an idle worker between
        daily backups must not dilute the failure rate towards zero."""
        worker = self._makeWorker()
        stopEvent = threading.Event()
        checks = []

        def notDue():
            checks.append(1)
            if len(checks) >= 3:
                stopEvent.set()
            return False

        with patch.object(worker, "isDue", side_effect=notDue), \
             patch("Database.backup.random.randint", return_value=0), \
             patch.object(backupModule, "BACKUP_CHECK_INTERVAL_SECONDS", 0):
            worker._loop(stopEvent)

        summary = worker.getSummary()
        self.assertEqual(summary["consecutive_failures"], 0)
        self.assertEqual(summary["failure_rate"], 0.0)
        self.assertIsNone(summary["last_error"])

    def test_a_failing_due_check_counts_too(self):
        """isDue() reads the backup directory - if that raises (an unreadable
        or vanished mount), no backup can be taken either."""
        worker = self._makeWorker()
        stopEvent = threading.Event()
        calls = []

        def brokenIsDue():
            calls.append(1)
            if len(calls) >= 2:
                stopEvent.set()
            raise OSError("backup volume is gone")

        with patch.object(worker, "isDue", side_effect=brokenIsDue), \
             patch("Database.backup.random.randint", return_value=0), \
             patch.object(backupModule, "BACKUP_CHECK_INTERVAL_SECONDS", 0):
            worker._loop(stopEvent)

        self.assertEqual(worker.getSummary()["consecutive_failures"], 2)

    def test_a_worker_that_never_ran_reports_zeros(self):
        summary = self._makeWorker().getSummary()

        self.assertEqual(summary["status"], "INACTIVE")
        self.assertEqual(summary["consecutive_failures"], 0)
        self.assertIsNone(summary["last_error"])

    def test_the_summary_reports_the_running_thread(self):
        worker = self._makeWorker()
        worker.start()
        self.addCleanup(worker.stop)

        self.assertEqual(worker.getSummary()["status"], "RUNNING")


class TestWorkerThread(BackupWorkerTestCase):
    def test_start_and_stop_cleanly_without_backing_up_immediately(self):
        """The thread waits out a startup delay before its first due-check, so
        app construction (and every app test) doesn't race a backup write."""
        worker = self._makeWorker()
        worker.start()
        self.assertTrue(worker.thread.is_alive())

        worker.stop()

        self.assertFalse(worker.thread.is_alive())
        self.assertFalse((self.root / "Backups").exists())

    def test_disabled_worker_does_not_start_a_thread(self):
        worker = self._makeWorker(intervalHours=0)
        worker.start()
        self.assertIsNone(worker.thread)


if __name__ == "__main__":
    unittest.main()


class TestBackupStampUsesTheInstanceZone(unittest.TestCase):
    """The filename stamp comes from the instance's configured zone, not the C
    runtime's idea of local time.

    Same defect the log had (see Database/logging_config.py's
    InstanceZoneFormatter): on Windows the UCRT parses TZ as a POSIX spec, so
    TZ=Europe/Zurich silently resolves to fixed UTC+1 and every backup filename
    ran an hour behind the wall clock for half the year - which matters here
    because the name is the only thing an operator has when picking a snapshot
    to restore."""

    def test_the_stamp_matches_the_configured_zone(self):
        import datetime as dt
        from unittest.mock import patch
        from zoneinfo import ZoneInfo
        import Database.utils as utilsModule
        from Database.backup import BackupWorker, BACKUP_TIMESTAMP_FORMAT

        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            dbPath = Path(tmp) / "spotify_stats.db"
            seed = sqlite3.connect(dbPath)
            with seed:
                seed.execute("CREATE TABLE t (x)")
            seed.close()   #< Windows keeps an open db file locked; the temp dir must delete
            worker = BackupWorker(dbPath=dbPath, backupDir=Path(tmp) / "Backups",
                                  intervalHours=24, retentionCount=7)
            with patch.object(utilsModule, "tz", ZoneInfo("UTC")):
                before = dt.datetime.now(tz=ZoneInfo("UTC"))
                made = worker.runBackup()
                after = dt.datetime.now(tz=ZoneInfo("UTC"))

        stampText = made.stem.rsplit("backup_", 1)[-1]
        stamped = dt.datetime.strptime(stampText, BACKUP_TIMESTAMP_FORMAT).replace(tzinfo=ZoneInfo("UTC"))
        self.assertLessEqual(before.replace(microsecond=0), stamped)
        self.assertLessEqual(stamped, after)
