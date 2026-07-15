try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds tracks.availability_reason (Spotify playability restriction) and
    albums.backfill_attempted_at (metadata backfill retry rate-limiting)."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addAvailabilityColumnsIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added tracks.availability_reason and albums.backfill_attempted_at columns.")
        self.updateAppVersion("1.14.0")


if __name__ == "__main__":
    Migrator("1.13.0", "1.14.0").migrate()
