try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds users.spotify_needs_reauth: flags an account whose stored Spotify
    refresh token was rejected by the Web API recently-played backfill for
    lacking the user-read-recently-played scope (a 403 "Insufficient client
    scope" response), previously logged forever without any user-facing
    signal that backfill was stuck. Profile now surfaces "re-authorize with
    Spotify" for accounts flagged this way."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addSpotifyNeedsReauthColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added users.spotify_needs_reauth column.")
        self.updateAppVersion("1.31.0")


if __name__ == "__main__":
    Migrator("1.30.0", "1.31.0").migrate()
