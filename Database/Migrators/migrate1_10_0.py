try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds created_at and created_reason columns to tracks table for tracking
    when and why tracks were added to the catalog (listener fetch, history import, etc)."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addTrackMetadataColumnsIfMissing()
            repo.addSpotifyApiColumnsToUsersIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added tracks.created_at/created_reason columns and users Spotify API columns.")
        self.updateAppVersion("1.11.0")


if __name__ == "__main__":
    Migrator("1.10.0", "1.11.0").migrate()
