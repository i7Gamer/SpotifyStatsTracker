try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds user_shares.requester_seen_accepted, backing the "your share
    request was accepted" topbar notification's dismissal state."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addRequesterSeenAcceptedColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added user_shares.requester_seen_accepted column.")
        self.updateAppVersion("1.16.0")


if __name__ == "__main__":
    Migrator("1.15.0", "1.16.0").migrate()
