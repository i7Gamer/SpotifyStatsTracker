"""Automatic scheduled backups of the shared SQLite database.

Snapshots are taken with SQLite's online backup API - safe against a live,
WAL-mode database (the README explains why a raw file copy is not) - into
Database/Data/Backups/, which the standard Docker volume mount already
persists. Old snapshots are rotated out. Restart-safe: whether a backup is
due is judged from the newest existing backup file's mtime, not from process
start time, so a daily-restarting container doesn't back up on every boot.

Backups protect against app/database corruption and accidental deletion on
the same disk - copy them elsewhere for real disaster protection. Stored
secrets inside a backup are encrypted (see secret_store.py); keep the
encryption key alongside the backups, and treat backup+key together as
sensitive.
"""
import datetime
import logging
import os
import random
import sqlite3
import threading
from pathlib import Path

try:
    import Database.db as db
except ModuleNotFoundError:
    import db

logger = logging.getLogger(__name__)

BACKUP_INTERVAL_ENV_VAR = "BACKUP_INTERVAL_HOURS"
BACKUP_RETENTION_ENV_VAR = "BACKUP_RETENTION_COUNT"
DEFAULT_BACKUP_INTERVAL_HOURS = 24
DEFAULT_BACKUP_RETENTION_COUNT = 7
BACKUP_DIR_NAME = "Backups"                 #< created next to the database file, inside the persisted Data/ volume
BACKUP_FILENAME_PREFIX = "spotify_stats_backup_"
BACKUP_STARTUP_MIN_DELAY_SECONDS = 60       #< random startup-offset bounds: don't race app startup (migrations
BACKUP_STARTUP_MAX_DELAY_SECONDS = 300      #  just ran, listeners are spinning up), and stagger against the other
                                            #  periodic workers instead of all firing at the same instant
BACKUP_CHECK_INTERVAL_SECONDS = 15 * 60     #< how often the worker re-checks whether a backup is due
BACKUP_TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"   #< lexicographic order == chronological order, which rotation relies on


def _envInt(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        logger.warning("Ignoring non-numeric %s=%r, using default %d", name, raw, default)
        return default


class BackupWorker:
    """One per process (the database is shared across every user)."""

    def __init__(self, dbPath: Path | None = None, backupDir: Path | None = None,
                 intervalHours: int | None = None, retentionCount: int | None = None):
        # Resolved at call time (not as a default argument) so tests that
        # monkeypatch db.DEFAULT_DB_PATH are honored - same pattern as
        # Repository.__init__.
        self.dbPath = Path(dbPath if dbPath is not None else db.DEFAULT_DB_PATH)
        self.backupDir = Path(backupDir) if backupDir is not None else self.dbPath.parent / BACKUP_DIR_NAME
        self.intervalHours = intervalHours if intervalHours is not None else _envInt(
            BACKUP_INTERVAL_ENV_VAR, DEFAULT_BACKUP_INTERVAL_HOURS)
        self.retentionCount = retentionCount if retentionCount is not None else _envInt(
            BACKUP_RETENTION_ENV_VAR, DEFAULT_BACKUP_RETENTION_COUNT)
        self.thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def isEnabled(self) -> bool:
        return self.intervalHours > 0 and self.retentionCount > 0

    def _backupFiles(self) -> list[Path]:
        """Existing snapshots, oldest first (timestamped names sort
        chronologically). Only files this worker created are considered -
        a user's own manual copies in the same folder are never touched."""
        if not self.backupDir.exists():
            return []
        return sorted(p for p in self.backupDir.iterdir()
                      if p.is_file() and p.name.startswith(BACKUP_FILENAME_PREFIX) and p.suffix == ".db")

    def newestBackupTime(self) -> float | None:
        files = self._backupFiles()
        return files[-1].stat().st_mtime if files else None

    def isDue(self) -> bool:
        if not self.isEnabled():
            return False
        newest = self.newestBackupTime()
        if newest is None:
            return True
        import time
        return time.time() - newest >= self.intervalHours * 3600

    def runBackup(self) -> Path:
        """Snapshot the database and rotate old snapshots. Writes to a
        .partial file first and renames only on success, so a crash mid-backup
        can't leave a truncated file that looks like a valid snapshot."""
        self.backupDir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.datetime.now().strftime(BACKUP_TIMESTAMP_FORMAT)
        finalPath = self.backupDir / f"{BACKUP_FILENAME_PREFIX}{stamp}.db"
        partialPath = finalPath.with_suffix(".partial")

        source = sqlite3.connect(self.dbPath)
        try:
            destination = sqlite3.connect(partialPath)
            try:
                source.backup(destination)
            finally:
                destination.close()
        finally:
            source.close()

        try:
            os.replace(partialPath, finalPath)
        except Exception:
            partialPath.unlink(missing_ok=True)
            raise
        logger.info("Database backed up to %s", finalPath)
        self._rotate()
        return finalPath

    def _rotate(self) -> None:
        files = self._backupFiles()
        for stale in files[:-self.retentionCount] if self.retentionCount else files:
            try:
                stale.unlink()
                logger.info("Rotated out old backup %s", stale.name)
            except OSError as e:
                logger.warning("Could not delete old backup %s: %s", stale, e)

    def _loop(self) -> None:
        if self._stop_event.wait(random.randint(BACKUP_STARTUP_MIN_DELAY_SECONDS,
                                                BACKUP_STARTUP_MAX_DELAY_SECONDS)):
            return
        while not self._stop_event.is_set():
            try:
                if self.isDue():
                    self.runBackup()
            except Exception as e:
                logger.error("Scheduled backup failed: %s", e)
            if self._stop_event.wait(BACKUP_CHECK_INTERVAL_SECONDS):
                return

    def start(self) -> None:
        if not self.isEnabled():
            logger.info("Scheduled backups disabled (%s=%d, %s=%d)",
                        BACKUP_INTERVAL_ENV_VAR, self.intervalHours,
                        BACKUP_RETENTION_ENV_VAR, self.retentionCount)
            return
        if self.thread is not None and self.thread.is_alive():
            return
        self._stop_event.clear()
        self.thread = threading.Thread(target=self._loop, name="backup-worker", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=5)
