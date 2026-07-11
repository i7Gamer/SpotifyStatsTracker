try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Removes existing zero (or negative) duration plays from the shared
    database - skip/error events from Spotify's exported history that older
    versions of the importer recorded as real plays. The importer now filters
    these out at import time (see StreamingHistoryImporter.MIN_TIME_PLAYED_MS);
    this migration is a one-time cleanup of rows already imported before that
    fix landed."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            removedCount = repo.deleteZeroDurationPlays()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Removed {removedCount} zero-duration play(s).")
        self.updateAppVersion("1.8.0")


if __name__ == "__main__":
    Migrator().migrate()
