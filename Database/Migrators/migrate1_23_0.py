try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Relaxes share_links.year from NOT NULL to nullable so one link can
    represent "all years" (year IS NULL)."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            conn = repo.connection()

            yearColumn = next(r for r in conn.execute("PRAGMA table_info(share_links)").fetchall() if r["name"] == "year")
            if yearColumn["notnull"]:   #< idempotency guard - a bare rebuild has no natural no-op like CREATE IF NOT EXISTS does
                try:
                    conn.execute("BEGIN IMMEDIATE")   #< bare conn.execute() calls autocommit independently with no explicit BEGIN - verified
                    conn.execute("""
                        CREATE TABLE share_links_new (
                            id          INTEGER PRIMARY KEY AUTOINCREMENT,
                            token       TEXT NOT NULL UNIQUE,
                            username    TEXT NOT NULL REFERENCES users(username),
                            kind        TEXT NOT NULL CHECK (kind IN ('wrapped')),
                            year        INTEGER,
                            created_at  REAL NOT NULL,
                            expires_at  REAL
                        )
                    """)
                    # Carry the autoincrement high-water mark forward - without this,
                    # a link revoked before this migration ran has its id silently
                    # reused by the next link created after it (verified reproducible).
                    conn.execute(
                        "INSERT INTO sqlite_sequence (name, seq) "
                        "SELECT 'share_links_new', seq FROM sqlite_sequence WHERE name='share_links'"
                    )
                    conn.execute(
                        "INSERT INTO share_links_new (id, token, username, kind, year, created_at, expires_at) "
                        "SELECT id, token, username, kind, year, created_at, expires_at FROM share_links"
                    )
                    conn.execute("DROP TABLE share_links")
                    conn.execute("ALTER TABLE share_links_new RENAME TO share_links")
                    conn.execute("CREATE INDEX idx_share_links_username ON share_links(username)")
                    fkViolations = conn.execute("PRAGMA foreign_key_check").fetchall()
                    if fkViolations:
                        raise RuntimeError(f"share_links rebuild left foreign key violations: {fkViolations}")
                    conn.commit()
                except Exception:
                    conn.rollback()
                    raise
        finally:
            repo.connectionManager.close()

        print("Relaxed share_links.year to nullable (NULL = all-years link).")
        self.updateAppVersion("1.24.0")


if __name__ == "__main__":
    Migrator("1.23.0", "1.24.0").migrate()
