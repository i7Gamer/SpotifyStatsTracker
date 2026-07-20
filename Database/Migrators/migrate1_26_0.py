try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Requeues corrupted artist biographies: bios fetched before the
    bio.content + sentence-boundary-truncation fix are stuck mid-sentence
    forever (bio.summary, Last.fm's own truncated excerpt, cuts off at a
    fixed character budget with no regard for sentence boundaries - bio
    IS NOT NULL, so they'd never re-enter the fetch queue on their own).
    Clearing bio and bio_attempted_at re-enters them immediately instead of
    after the 30-day retry window."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            cleared = repo.requeueCorruptedBiographies()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Requeued {cleared} corrupted artist biographies for the improved extraction.")
        self.updateAppVersion("1.27.0")


if __name__ == "__main__":
    Migrator("1.26.0", "1.27.0").migrate()
