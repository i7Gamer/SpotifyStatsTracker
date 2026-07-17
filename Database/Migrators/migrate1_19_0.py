try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Requeues the Last.fm genre backlog: entities that never got OWN
    (non-inherited) genre rows have their lastfm_attempted_at cleared so the
    1.20.0 lookup improvements (tag aliases, cleaned-name retry, album-first
    inheritance for tracks, repaired track artists) re-run across them
    immediately instead of after the 30-day retry window. Entities holding
    own tags keep their stamp, and existing inherited rows stay in place so
    genre stats keep working until the re-run replaces them."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            cleared = repo.requeueLastfmEntitiesWithoutOwnGenres()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Requeued {cleared} entities without own Last.fm tags for the improved genre backfill.")
        self.updateAppVersion("1.20.0")


if __name__ == "__main__":
    Migrator("1.19.0", "1.20.0").migrate()
