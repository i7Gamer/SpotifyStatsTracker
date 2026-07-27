try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """1.45.0 -> 1.46.0: adds users.display_name, the editable label shown
    wherever a person is named (topbar, share lists, Compare headings, public
    Wrapped pages, the admin table).

    It exists because `username` cannot change: it is the primary key that
    eight tables reference by foreign key - plays, user_tags, user_shares,
    share_links, user_milestones, user_wrapped, imported_files,
    import_progress - none declaring ON UPDATE CASCADE, while every connection
    runs with foreign_keys=ON. Rewriting it would also have to re-key the live
    per-user Database/listener maps, the autoImport/<username>/ drop folder and
    a possibly-running import. A nullable second column carries the same user
    benefit and touches none of that.

    Nothing to backfill: NULL means "display as the username", which is exactly
    what every existing account should keep showing, so the ALTER's own default
    is the migration's whole data story."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            repo.addDisplayNameColumnIfMissing()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print("Added users.display_name column.")
        self.updateAppVersion("1.46.0")


if __name__ == "__main__":
    Migrator("1.45.0", "1.46.0").migrate()
