try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Creates the share_links table backing public, tokenized read-only
    Wrapped links."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            conn = repo.connection()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS share_links (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    token       TEXT NOT NULL UNIQUE,
                    username    TEXT NOT NULL REFERENCES users(username),
                    kind        TEXT NOT NULL CHECK (kind IN ('wrapped')),
                    year        INTEGER NOT NULL,
                    created_at  REAL NOT NULL,
                    expires_at  REAL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_share_links_username ON share_links(username)")
            conn.commit()
        finally:
            repo.connectionManager.close()

        print("Created share_links table.")
        self.updateAppVersion("1.22.0")


if __name__ == "__main__":
    Migrator("1.21.0", "1.22.0").migrate()
