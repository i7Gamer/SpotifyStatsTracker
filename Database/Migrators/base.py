from pathlib import Path

try:
    from Database.Migrators import dbversion
except ModuleNotFoundError:
    import dbversion


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


class BaseMigrator:
    def __init__(self, fromVersion: str, toVersion: str, *args, **kwargs):
        self.baseDir = Path(__file__).resolve().parent
        runtimeDir = resolveRuntimeDir(self.baseDir)
        self.dbPath = runtimeDir / "spotify_stats.db"
        self.databaseVersionFile = runtimeDir / "VERSION"
        self.databaseVersion = self._readVersion()
        self.fromVersion = fromVersion
        self.toVersion = toVersion

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
        return self.databaseVersionFile.read_text().strip()

    @staticmethod
    def getMajorMinor(version):
        """(major, minor) as ints - comparisons must use both components, not
        just minor, so e.g. database "1.7.0" vs app "2.7.0" (same minor,
        different major) isn't mistaken for a version match."""
        major, minor = version.split(".")[:2]
        return int(major), int(minor)

    def checkPreconditions(self):
        if self.databaseVersion != self.fromVersion:
            raise Exception(f"Database version {self.databaseVersion} does not match migrator's expected from-version {self.fromVersion}.")

    def updateAppVersion(self, newVersion):
        self.databaseVersion = newVersion
        # Re-resolved fresh (not just self.dbPath/self.databaseVersionFile)
        # because a migrator can move the runtime dir (Users/ -> Data/) or
        # create spotify_stats.db for the first time before calling this -
        # see migrate1_6_0, which does both.
        runtimeDir = resolveRuntimeDir(self.baseDir)
        self.databaseVersionFile = runtimeDir / "VERSION"
        self.dbPath = runtimeDir / "spotify_stats.db"

        self.databaseVersionFile.write_text(newVersion)
        # Kept as a safety-net/rollback path alongside the in-db marker
        # (cheap to write, lets an older app build still find a version).
        if self.dbPath.exists():
            dbversion.writeDbVersion(self.dbPath, newVersion)

    def migrate(self):
        self.checkPreconditions()
