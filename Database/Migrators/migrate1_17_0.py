try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Adds users.is_admin (backing admin-only surfaces like /overview's
    per-user table) and promotes the earliest-created user - whoever set the
    instance up - when no admin exists yet. The ADMIN_EMAIL env var (see
    app.py) is the explicit override/recovery path if that guess is wrong."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addUserIsAdminColumnIfMissing()
            promoted = repo.promoteEarliestUserToAdminIfNoneExists()
            repo.commit()
        finally:
            repo.connectionManager.close()

        if promoted:
            print(f"Added users.is_admin column; promoted earliest user '{promoted}' to admin.")
        else:
            print("Added users.is_admin column; admin unchanged.")
        self.updateAppVersion("1.18.0")


if __name__ == "__main__":
    Migrator("1.17.0", "1.18.0").migrate()
