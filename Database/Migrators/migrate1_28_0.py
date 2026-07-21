try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Requeues albums whose Last.fm bio was attempted before the album-bio
    lookup gained cleanLookupName's decoration-stripping retry: a decorated
    title ("(Deluxe Edition)", "- Remastered", ...) whose verbatim
    album.getinfo found no bio stayed permanently attempted (bio IS NULL,
    bio_attempted_at IS NOT NULL) and never re-entered the queue on its own.
    Clearing bio_attempted_at re-enters only the decorated ones immediately
    so the fixed lookup retries with the undecorated title - the album-bio
    analogue of migrate1_26_0 (corrupted artist bios)."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            requeued = repo.requeueDecoratedAlbumsWithoutBios()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Requeued {requeued} decorated albums for the album-bio deluxe-edition fallback.")
        self.updateAppVersion("1.29.0")


if __name__ == "__main__":
    Migrator("1.28.0", "1.29.0").migrate()
