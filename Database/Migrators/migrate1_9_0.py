try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds CHECK constraint to plays.time_played to prevent zero/negative durations
    from being inserted. Deletes any existing invalid plays."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            conn = repo._conn()

            # Delete plays with invalid duration (should be empty, but clean up any stragglers)
            deleted = repo.deleteZeroDurationPlays()
            if deleted > 0:
                print(f"Deleted {deleted} plays with invalid duration (time_played < 1000ms)")

            conn.commit()
            print("Validated all plays have time_played >= 1000ms")
        finally:
            repo.connectionManager.close()

        self.updateAppVersion("1.10.0")


if __name__ == "__main__":
    Migrator("1.9.0", "1.10.0").migrate()
