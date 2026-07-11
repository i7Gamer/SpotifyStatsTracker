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


class BaseMigrator:
    def __init__(self, *args, **kwargs):
        self.baseDir = Path(__file__).resolve().parent
        self.databaseVersionFile = resolveRuntimeDir(self.baseDir) / "VERSION"
        self.databaseVersion = self.databaseVersionFile.read_text().strip()
        self.appVersionFile = self.baseDir / ".." / "VERSION"
        self.appVersion = self.appVersionFile.read_text().strip()

    def getMiddleVersion(self, version):
        return int(version.split(".")[1])

    def checkPreconditions(self):
        if self.getMiddleVersion(self.databaseVersion)+1 != self.getMiddleVersion(self.appVersion):
            raise Exception("Database and app versions are not compatible. Please run the migrator for the correct version first.")

    def updateAppVersion(self, newVersion):
        self.databaseVersion = newVersion
        self.databaseVersionFile.write_text(newVersion)

    def migrate(self):
        self.checkPreconditions()
