try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds tracks.availability_reason (Spotify playability restriction) and
    albums.backfill_attempted_at (metadata backfill retry rate-limiting), and
    clears cached Wrapped years so they recalculate with the new badge fields."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addAvailabilityColumnsIfMissing()
            # Cached Wrapped payloads were serialized before created_reason/
            # availability_reason existed in track dicts, so their track cards
            # could never show the Deleted/Unavailable badges - drop them and
            # let each year recalculate on next view.
            repo.connection().execute("DELETE FROM user_wrapped")
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added tracks.availability_reason and albums.backfill_attempted_at columns; cleared cached Wrapped years.")
        self.updateAppVersion("1.14.0")


if __name__ == "__main__":
    Migrator("1.13.0", "1.14.0").migrate()
