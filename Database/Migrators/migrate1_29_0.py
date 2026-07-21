try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Requeues artists whose Last.fm lookup was attempted before
    getArtistTopTags gained foldStylizedArtistName's fallback retry: an
    artist name using stylized Latin-Extended letters or decorative marks
    ("HUGO", "Jinka +") whose verbatim artist.gettoptags found no tags
    stayed permanently attempted (no artist_genres rows, lastfm_attempted_at
    IS NOT NULL) and never re-entered the queue on its own. Clearing
    lastfm_attempted_at re-enters only the foldable ones immediately so the
    fixed lookup retries with the folded name - the artist-genre analogue of
    migrate1_28_0 (decorated album bios)."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            requeued = repo.requeueArtistsWithFoldableNamesWithoutGenres()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Requeued {requeued} artists with foldable stylized names for the genre fallback fix.")
        self.updateAppVersion("1.30.0")


if __name__ == "__main__":
    Migrator("1.29.0", "1.30.0").migrate()
