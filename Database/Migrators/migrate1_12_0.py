try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Creates the user_wrapped table for caching precalculated wrapped stats."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            conn = repo.connection()
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_wrapped (
                    username        TEXT NOT NULL REFERENCES users(username),
                    year            INTEGER NOT NULL,
                    calculated_at   REAL NOT NULL,
                    max_played_at   REAL NOT NULL,
                    total_plays     INTEGER NOT NULL,
                    total_ms        INTEGER NOT NULL,
                    longest_streak  INTEGER NOT NULL,
                    peak_day        TEXT,
                    peak_plays      INTEGER,
                    unique_songs    INTEGER NOT NULL,
                    unique_artists  INTEGER NOT NULL,
                    discovered_songs INTEGER NOT NULL,
                    discovered_artists INTEGER NOT NULL,
                    time_series_day   TEXT NOT NULL,
                    time_series_week  TEXT NOT NULL,
                    time_series_month TEXT NOT NULL,
                    top_songs        TEXT NOT NULL,
                    top_artists      TEXT NOT NULL,
                    top_albums       TEXT NOT NULL,
                    discovered_songs_list TEXT NOT NULL,
                    discovered_artists_list TEXT NOT NULL,
                    discovered_albums_list TEXT NOT NULL,
                    PRIMARY KEY (username, year)
                )
            """)
            conn.commit()
        finally:
            repo.connectionManager.close()

        print("Created user_wrapped table.")
        self.updateAppVersion("1.13.0")


if __name__ == "__main__":
    Migrator("1.12.0", "1.13.0").migrate()
