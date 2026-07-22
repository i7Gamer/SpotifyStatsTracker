try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Requeues artists whose Last.fm lookup was attempted before
    getArtistTopTags/getArtistInfo gained normalizeArtistLookupName's
    slash/plus/credit-joiner retry: an artist name like "Axwell /\\ Ingrosso"
    or "Florence + The Machine" whose verbatim lookup found no tags stayed
    permanently attempted (no artist_genres rows, lastfm_attempted_at IS NOT
    NULL) and never re-entered the queue on its own. Clearing
    lastfm_attempted_at re-enters only the transformable ones immediately so
    the fixed lookup retries with the transformed name - the artist-genre
    analogue of migrate1_29_0 (foldable stylized names), one release later
    for the slash/plus/credit-joiner transforms."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            requeued = repo.requeueArtistsWithTransformedNamesWithoutGenres()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Requeued {requeued} artists with transformable names for the genre fallback fix.")
        self.updateAppVersion("1.32.0")


if __name__ == "__main__":
    Migrator("1.31.0", "1.32.0").migrate()
