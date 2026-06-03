from pathlib import Path

class BaseMigrator:
    def __init__(self, *args, **kwargs):
        self.baseDir = Path(__file__).resolve().parent
        databaseVersionFile = self.baseDir / ".." / "Users" / "VERSION"
        self.databaseVersion = databaseVersionFile.read_text().strip()
        appVersionFile = self.baseDir / ".." / "VERSION"
        self.appVersion = appVersionFile.read_text().strip()

    def getMiddleVersion(self, version):
        return version.split(".")[1]

    def checkPreconditions(self):
        if self.getMiddleVersion(self.databaseVersion)+1 != self.getMiddleVersion(self.appVersion):
            raise Exception("Database and app versions are not compatible. Please run the migrator for the correct version first.")

    def migrate(self):
        self.checkPreconditions()
