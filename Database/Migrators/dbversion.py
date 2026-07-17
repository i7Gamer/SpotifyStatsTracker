"""The database's schema version, stored inside the sqlite file itself rather
than in a sibling VERSION text file. A raw file copy (a backup, a manual
`cp`) carries this along automatically - SQLite's own backup API (used by
Database/backup.py) copies every table, schema_version included - whereas a
sibling file left in the original directory silently desyncs from a restored
copy. Deliberately independent of Repository/ConnectionManager/db.SCHEMA:
those run the app's full *current* schema on every connection, which would
stamp every current table onto an old database before its true version was
ever read.
"""
import sqlite3
import time
from pathlib import Path

SCHEMA_VERSION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version     TEXT NOT NULL,
    applied_at  REAL NOT NULL
);
"""


def readDbVersion(dbPath: Path) -> str | None:
    """The most recently recorded version, or None if this database predates
    the schema_version table (or the table exists but is empty - same
    meaning, just written by a prior readDbVersion() call)."""
    conn = sqlite3.connect(dbPath)
    try:
        conn.execute(SCHEMA_VERSION_TABLE_SQL)
        row = conn.execute(
            "SELECT version FROM schema_version ORDER BY applied_at DESC, rowid DESC LIMIT 1"
        ).fetchone()
        return row[0] if row is not None else None
    finally:
        conn.close()


def writeDbVersion(dbPath: Path, version: str) -> None:
    """Appends a new current-version row rather than overwriting the last one
    - a cheap audit trail, and it means this can never lose a prior write."""
    conn = sqlite3.connect(dbPath)
    try:
        conn.execute(SCHEMA_VERSION_TABLE_SQL)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def hasAnyData(dbPath: Path) -> bool:
    """Whether any table other than schema_version itself has at least one
    row - distinguishes a genuinely fresh/empty database (safe to stamp with
    the current version, no migration needed) from a legacy database that has
    real data but was never given a version marker."""
    conn = sqlite3.connect(dbPath)
    try:
        tables = [row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()]
        for table in tables:
            if table == "schema_version":
                continue
            count = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            if count > 0:
                return True
        return False
    finally:
        conn.close()
