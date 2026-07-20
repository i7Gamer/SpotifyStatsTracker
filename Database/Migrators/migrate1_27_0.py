try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds albums.bio and albums.bio_attempted_at for the album-bio
    feature (lazily fetched from Last.fm's album.getinfo wiki field, one
    call per album, cached forever like artist bios - see migrate1_25_0)."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addAlbumBioColumnsIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added albums.bio and albums.bio_attempted_at columns.")
        self.updateAppVersion("1.28.0")


if __name__ == "__main__":
    Migrator("1.27.0", "1.28.0").migrate()
