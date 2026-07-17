try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Clears every artist image marked 'failed': they were all marked that way by
    scraping open.spotify.com's public artist page for an og:image meta tag, which
    stopped working for every artist (not just ones that genuinely lack a picture)
    once Spotify moved artist pages to a client-rendered SPA shell with no
    server-rendered metadata. lazyFetchArtistImage now fetches via the Web API /
    SpotipyFree instead and treats 'failed' as permanent, so without this cleanup
    every artist caught by the old bug would stay image-less forever."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            cleared = repo.deleteFailedArtistImages()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Cleared {cleared} wrongly-permanent-failed artist image(s) for retry under the new fetch path.")
        self.updateAppVersion("1.21.0")


if __name__ == "__main__":
    Migrator("1.20.0", "1.21.0").migrate()
