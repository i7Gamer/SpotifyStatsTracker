try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds the Last.fm genre-backfill columns: users.lastfm_api_key (the
    per-user key the genre backfiller runs on, encrypted at rest) plus
    lastfm_attempted_at on artists/albums/tracks (queue retry stamps). The
    genre join tables and app_settings are CREATE TABLE IF NOT EXISTS in
    SCHEMA, so they appear on the next connection without a migration."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addLastfmColumnsIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added Last.fm columns (users.lastfm_api_key, artists/albums/tracks.lastfm_attempted_at).")
        self.updateAppVersion("1.19.0")


if __name__ == "__main__":
    Migrator("1.18.0", "1.19.0").migrate()
