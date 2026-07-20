try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds artists.bio and artists.bio_attempted_at for the artist-bio
    feature (lazily fetched from Last.fm's artist.getinfo, one call per
    artist, cached forever like artist images)."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addArtistBioColumnsIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added artists.bio and artists.bio_attempted_at columns.")
        self.updateAppVersion("1.26.0")


if __name__ == "__main__":
    Migrator("1.25.0", "1.26.0").migrate()
