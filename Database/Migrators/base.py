# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

try:
    from Database.Migrators import dbversion
except ModuleNotFoundError:
    import dbversion

from pathlib import Path


def resolveRuntimeDir(baseDir: Path) -> Path:
    """The runtime-data directory (VERSION marker, and historically every
    per-user JSON file) used to be named Users/; migrate1_6_0 renames it to
    Data/ once everything has moved into the shared database. Prefer Data/ if
    it already exists (an already-migrated install), fall back to Users/ for
    anyone still mid-upgrade, and default to Data/ (the current naming) if
    neither exists yet - a fresh install."""
    dataDir = baseDir / ".." / "Data"
    if dataDir.exists():
        return dataDir
    legacyUsersDir = baseDir / ".." / "Users"
    if legacyUsersDir.exists():
        return legacyUsersDir
    return dataDir


class BaseMigrator:  #< one step in the chain migrate.py drives
    """Subclasses override migrate() (calling super() so checkPreconditions
    runs) and finish by calling updateAppVersion()."""

    def __init__(self, fromVersion: str, toVersion: str, *args, **kwargs):
        self.fromVersion = fromVersion
        self.toVersion = toVersion
        self.baseDir: Path = Path(__file__).resolve().parent
        self._bindRuntimePaths()
        self.databaseVersion = self._readVersion()

    def _bindRuntimePaths(self) -> None:
        """Point dbPath/databaseVersionFile at wherever the runtime dir IS
        right now - resolved again by updateAppVersion because a migrator can
        move the directory (Users/ -> Data/) or create spotify_stats.db for
        the first time mid-step (migrate1_6_0 did both)."""
        runtimeDir = resolveRuntimeDir(self.baseDir)
        self.dbPath = runtimeDir / "spotify_stats.db"
        self.databaseVersionFile = runtimeDir / "VERSION"

    def _readVersion(self) -> str:
        """The version lives inside the .db file itself (schema_version
        table) once one exists - it then survives a raw file copy/backup,
        unlike the sibling VERSION file. Pre-1.7.0 migrators run before
        spotify_stats.db exists at all (the JSON-history era), and any
        database that predates the schema_version table falls back to the
        sibling file too."""
        if self.dbPath.exists():
            dbVersion = dbversion.readDbVersion(self.dbPath)
            if dbVersion is not None:
                return dbVersion
        if self.databaseVersionFile.exists():
            return self.databaseVersionFile.read_text().strip()
        return "1.0.0"

    @staticmethod
    def getMajorMinor(version):
        """(major, minor) as ints - comparisons must use both components, not
        just minor, so e.g. database "1.7.0" vs app "2.7.0" (same minor,
        different major) isn't mistaken for a version match."""
        major, minor = version.split(".")[:2]
        return int(major), int(minor)

    def checkPreconditions(self) -> None:
        # (major, minor), not the full string: the chain steps at minor
        # granularity, and a fresh install born on a patch release stamps its
        # FULL version (migrateIfNeeded writes appVersion verbatim) - so a
        # "1.46.1" database IS the state migrate1_46_0 expects, and
        # full-string equality bricked every such install at its next minor
        # upgrade. A marker that cannot even parse still gets the named
        # mismatch below rather than a bare ValueError out of int().
        try:
            matches = self.getMajorMinor(self.databaseVersion) == self.getMajorMinor(self.fromVersion)
        except ValueError:
            matches = False
        # The message text is pinned by tests/test_migrator_base.py - it is
        # what an operator sees when a chain is run out of order.
        if not matches:
            raise Exception(
                f"Database version {self.databaseVersion} does not match "
                f"migrator's expected from-version {self.fromVersion}.")

    def updateAppVersion(self, newVersion: str) -> None:
        self._bindRuntimePaths()   #< the step may have moved the runtime dir - see _bindRuntimePaths
        self.databaseVersion = newVersion   #< keep the in-memory view in step with what is persisted below

        self.databaseVersionFile.write_text(newVersion, encoding="utf-8")
        # Kept as a safety-net/rollback path alongside the in-db marker
        # (cheap to write, lets an older app build still find a version).
        if self.dbPath.exists():
            dbversion.writeDbVersion(self.dbPath, newVersion)

    def migrate(self) -> None:
        self.checkPreconditions()   #< every subclass's super().migrate() lands here
