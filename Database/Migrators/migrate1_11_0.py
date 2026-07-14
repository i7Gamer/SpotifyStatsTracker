try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds default_dashboard_window and timezone columns to the users table."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addUserSettingsColumnsIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added users.default_dashboard_window and users.timezone columns.")
        self.updateAppVersion("1.12.0")


if __name__ == "__main__":
    Migrator("1.11.0", "1.12.0").migrate()
