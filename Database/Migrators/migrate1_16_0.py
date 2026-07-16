try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository


class Migrator(BaseMigrator):
    """Encrypts the users table's stored secrets (Spotify session cookies,
    API client secret, refresh token) at rest - values written by older
    versions are plaintext in the database file, so any copied-around backup
    handed out every user's live Spotify session. See
    Database/secret_store.py for the key resolution (DATA_ENCRYPTION_KEY /
    FLASK_SECRET_KEY env, falling back to secrets/data_encryption_key.txt)."""

    def migrate(self):
        self.checkPreconditions()

        repo = Repository(resolveRuntimeDir(self.baseDir) / "spotify_stats.db")
        try:
            updated = repo.encryptStoredSecretsIfPlaintext()
            repo.commit()
        finally:
            repo.connectionManager.close()

        print(f"Encrypted stored secrets for {updated} user(s).")
        self.updateAppVersion("1.17.0")


if __name__ == "__main__":
    Migrator("1.16.0", "1.17.0").migrate()
