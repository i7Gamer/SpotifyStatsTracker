try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds the users.password_hash column so existing accounts (previously
    cookies-only) can opt into a password login without needing to re-paste
    their cookies every time. New installs already get this column from
    db.py's SCHEMA; this migration brings pre-existing databases in line."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addUserPasswordHashColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added users.password_hash column.")
        self.updateAppVersion("1.9.0")


if __name__ == "__main__":
    Migrator("1.8.0", "1.9.0").migrate()
