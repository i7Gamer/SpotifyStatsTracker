"""Startup's key-ownership count includes raw SMTP credentials."""
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app import SpotifyDashboardApp
from Database.repository import Repository
from Database.secret_store import encryptSecret, keyFingerprint, ENCRYPTION_KEY_ENV_VAR


SMTP_PASSWORD = "test-smtp-password"
FOREIGN_KEY = "test-key-from-another-instance"


@pytest.fixture
def secretRepo(tmp_path):
    repo = Repository(tmp_path / "secrets.db")
    yield repo
    repo.connectionManager.close()


def foreignCiphertext(monkeypatch):
    with monkeypatch.context() as context:
        context.setenv(ENCRYPTION_KEY_ENV_VAR, FOREIGN_KEY)
        return encryptSecret(SMTP_PASSWORD)


def test_smtp_only_foreign_secret_counted_without_modification(secretRepo, monkeypatch):
    stored = foreignCiphertext(monkeypatch)
    secretRepo.setAppSetting("smtp_password", stored)
    conn = secretRepo.connection()
    before = conn.total_changes
    with patch("Database.queries.users.keyFingerprint", wraps=keyFingerprint) as fingerprint:
        assert secretRepo.countSecretsUnderAnotherKey() == 1
    fingerprint.assert_called_once_with()
    assert conn.total_changes == before
    assert secretRepo.getAppSetting("smtp_password") == stored


def test_smtp_and_user_secret_counts_are_added(secretRepo, monkeypatch):
    secretRepo.upsertUser("alice", "alice@example.com")
    stored = foreignCiphertext(monkeypatch)
    secretRepo.setAppSetting("smtp_password", stored)
    conn = secretRepo.connection()
    with conn:
        conn.execute("UPDATE users SET spotify_client_secret=? WHERE username='alice'", (stored,))
    expectedSecrets = 2
    assert secretRepo.countSecretsUnderAnotherKey() == expectedSecrets


@pytest.mark.parametrize("value", [None, "", "plaintext", "enc:v1:old-format", "enc:v2:missing-separator"])
def test_unclassifiable_or_absent_smtp_is_not_counted(secretRepo, value):
    if value is not None:
        secretRepo.setAppSetting("smtp_password", value)
    assert secretRepo.countSecretsUnderAnotherKey() == 0


def test_current_key_smtp_not_counted(secretRepo):
    secretRepo.setAppSetting("smtp_password", encryptSecret(SMTP_PASSWORD))
    assert secretRepo.countSecretsUnderAnotherKey() == 0


def test_startup_warning_covers_smtp_without_logging_secret(secretRepo, monkeypatch, caplog):
    stored = foreignCiphertext(monkeypatch)
    secretRepo.setAppSetting("smtp_password", stored)
    # Invoke just the diagnostic with an isolated repository, never app startup.
    SpotifyDashboardApp._logIntegrityProbe(SimpleNamespace(repo=secretRepo))
    assert "SMTP" in caplog.text
    assert "DIFFERENT key" in caplog.text
    assert "restore secrets/data_encryption_key.txt" in caplog.text
    assert SMTP_PASSWORD not in caplog.text
    assert stored not in caplog.text
