try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Requeues albums without own Last.fm tags: album.gettoptags was found
    to miss real tag data for ~46% of tag-less albums that album.getinfo has
    (a persistent Last.fm-side inconsistency between the two endpoints, not
    a caching/autocorrect artifact) - getAlbumTopTags now falls back to
    album.getinfo on an empty gettoptags result. Clearing lastfm_attempted_at
    for already-attempted, still-tagless albums lets the fix take effect
    immediately instead of after the 30-day retry window. Scoped to albums
    only: artists and tracks showed no such divergence in testing (0/30 and
    0/70 respectively), so requeuing them would just re-run unchanged
    results for no benefit. Existing inherited rows stay in place so genre
    stats keep working until the re-run replaces them."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            cleared = repo.requeueAlbumsLastfmWithoutOwnGenres()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Requeued {cleared} albums without own Last.fm tags for the album.getinfo fallback fix.")
        self.updateAppVersion("1.25.0")


if __name__ == "__main__":
    Migrator("1.24.0", "1.25.0").migrate()
