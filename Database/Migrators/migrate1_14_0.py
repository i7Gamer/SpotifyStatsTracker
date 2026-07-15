try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Creates the user_shares table backing mutual data-sharing requests."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            conn = repo.connection()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_shares (
                    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                    requester_username  TEXT NOT NULL REFERENCES users(username),
                    recipient_username  TEXT NOT NULL REFERENCES users(username),
                    status              TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'accepted')),
                    created_at          REAL NOT NULL,
                    responded_at        REAL,
                    UNIQUE (requester_username, recipient_username),
                    CHECK (requester_username != recipient_username)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user_shares_recipient ON user_shares(recipient_username, status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_user_shares_requester ON user_shares(requester_username, status)")
            conn.commit()
        finally:
            repo.connectionManager.close()

        print("Created user_shares table.")
        self.updateAppVersion("1.15.0")


if __name__ == "__main__":
    Migrator("1.14.0", "1.15.0").migrate()
