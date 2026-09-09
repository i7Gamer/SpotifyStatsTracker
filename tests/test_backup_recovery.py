# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Offline restore drills with synthetic keys; no application workers start.

Fingerprint diagnostics are a lower bound: pre-fingerprint ciphertext must
actually be decrypted to establish coverage. These tests inventory raw fields
so an unreadable encrypted value cannot be mistaken for an unset credential.
"""
import shutil

import pytest

from Database.backup import BackupWorker
from Database.repository import Repository
from Database import secret_store as secrets


ORIGINAL_KEY = "synthetic-original-recovery-key"
LATER_KEY = "synthetic-later-recovery-key"
WRONG_KEY = "synthetic-unrelated-key"
USERNAME = "recovery-user"
UNSET_USERNAME = "no-credentials"
TRACK_ID = "recovery-track"
PLAYED_AT = 1_700_000_000
PLAY_TIME_MS = 180_000
TRACK_NUMBER = 1
DISC_NUMBER = 1
SMTP_FIELD = "smtp_password"
SECRET_FIELDS = (*Repository.SECRET_COLUMNS, SMTP_FIELD)
PLAINTEXT = {field: f"synthetic-{field}" for field in SECRET_FIELDS}
MIXED_KEYS = {
    "cookies_json": ORIGINAL_KEY,
    "spotify_client_secret": LATER_KEY,
    "spotify_refresh_token": ORIGINAL_KEY,
    "lastfm_api_key": LATER_KEY,
    SMTP_FIELD: ORIGINAL_KEY,
}
LEGACY_FIELDS = frozenset(("spotify_refresh_token", "lastfm_api_key"))


@pytest.fixture
def restoredRepo(tmp_path, monkeypatch, request):
    """Commit live WAL data, snapshot it, then restore the completed file."""
    keyByField, legacyFields = request.param
    sourcePath = tmp_path / "source.db"
    source = Repository(sourcePath)
    try:
        source.upsertUser(USERNAME, "recovery@example.com")
        source.upsertUser(UNSET_USERNAME, "unset@example.com")
        source.upsertTrack({
            "id": TRACK_ID, "name": "Recovery track", "url": "", "imageId": TRACK_ID,
            "duration": PLAY_TIME_MS, "isrc": "", "discNumber": DISC_NUMBER,
            "trackNumber": TRACK_NUMBER,
        })
        source.insertPlay(USERNAME, TRACK_ID, PLAYED_AT, PLAY_TIME_MS)
        stored = {}
        for field in SECRET_FIELDS:
            monkeypatch.setenv(secrets.ENCRYPTION_KEY_ENV_VAR, keyByField[field])
            encrypted = secrets.encryptSecret(PLAINTEXT[field])
            if field in legacyFields:
                # Same Fernet payload as historical enc:v1 values, without a fingerprint.
                token = encrypted.removeprefix(secrets.ENCRYPTED_PREFIX_V2).partition(":")[2]
                encrypted = secrets.ENCRYPTED_PREFIX + token
            stored[field] = encrypted
        assignments = ", ".join(f"{field}=?" for field in source.SECRET_COLUMNS)
        source.connection().execute(
            f"UPDATE users SET {assignments} WHERE username=?",
            (*[stored[field] for field in source.SECRET_COLUMNS], USERNAME),
        )
        source.setAppSetting(SMTP_FIELD, stored[SMTP_FIELD])
        source.commit()
        snapshot = BackupWorker(dbPath=sourcePath, backupDir=tmp_path / "snapshots").runBackup()
        restoredPath = tmp_path / "restored.db"
        shutil.copy2(snapshot, restoredPath)
    finally:
        source.connectionManager.close()
    restored = Repository(restoredPath)
    try:
        yield restored, stored, keyByField
    finally:
        restored.connectionManager.close()


SAME_KEY_FIXTURE = ({field: ORIGINAL_KEY for field in SECRET_FIELDS}, frozenset())
MIXED_KEY_FIXTURE = (MIXED_KEYS, LEGACY_FIELDS)
LEGACY_FIXTURE = ({field: ORIGINAL_KEY for field in SECRET_FIELDS}, frozenset(SECRET_FIELDS))


@pytest.mark.parametrize("restoredRepo", [SAME_KEY_FIXTURE, MIXED_KEY_FIXTURE, LEGACY_FIXTURE], indirect=True)
@pytest.mark.parametrize("recoveryKey", [ORIGINAL_KEY, LATER_KEY, WRONG_KEY])
def test_restored_inventory_counts_readable_unreadable_and_unset_fields(restoredRepo, monkeypatch, recoveryKey):
    repo, stored, keyByField = restoredRepo
    monkeypatch.setenv(secrets.ENCRYPTION_KEY_ENV_VAR, recoveryKey)
    conn = repo.connection()
    row = conn.execute("SELECT * FROM users WHERE username=?", (USERNAME,)).fetchone()
    raw = {field: row[field] for field in repo.SECRET_COLUMNS}
    raw[SMTP_FIELD] = repo.getAppSetting(SMTP_FIELD)
    assert raw == stored
    decrypted = {field: secrets.decryptSecret(value) for field, value in raw.items()}
    expectedReadable = {field for field in SECRET_FIELDS if keyByField[field] == recoveryKey}
    assert {field for field, value in decrypted.items() if value is not None} == expectedReadable
    for field in expectedReadable:
        assert decrypted[field] == PLAINTEXT[field]
    unreadable = sum(secrets.isEncrypted(raw[field]) and value is None for field, value in decrypted.items())
    assert unreadable == len(SECRET_FIELDS) - len(expectedReadable)
    foreign = sum(secrets.isForeignKeyed(value) for value in raw.values())
    assert repo.countSecretsUnderAnotherKey() == foreign
    if all(value.startswith(secrets.ENCRYPTED_PREFIX) for value in raw.values()) and not expectedReadable:
        assert foreign == 0
        assert unreadable == len(SECRET_FIELDS), "zero fingerprint mismatches cannot certify legacy recovery"
    unset = conn.execute("SELECT * FROM users WHERE username=?", (UNSET_USERNAME,)).fetchone()
    assert all(unset[field] is None for field in repo.SECRET_COLUMNS)
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert [tuple(play) for play in conn.execute(
        "SELECT username, track_id, played_at, time_played FROM plays"
    )] == [(USERNAME, TRACK_ID, PLAYED_AT, PLAY_TIME_MS)]
    assert {field: conn.execute(f"SELECT {field} FROM users WHERE username=?", (USERNAME,)).fetchone()[0]
            for field in repo.SECRET_COLUMNS} == {field: stored[field] for field in repo.SECRET_COLUMNS}
    assert repo.getAppSetting(SMTP_FIELD) == stored[SMTP_FIELD]


@pytest.mark.parametrize("restoredRepo", [SAME_KEY_FIXTURE], indirect=True)
@pytest.mark.parametrize("keySource", ["dedicated", "flask", "file", "missing", "empty"])
def test_restore_key_precedence_and_missing_key_failure_preserve_history(restoredRepo, monkeypatch, tmp_path, keySource):
    repo, stored, _ = restoredRepo
    monkeypatch.delenv(secrets.ENCRYPTION_KEY_ENV_VAR, raising=False)
    monkeypatch.delenv(secrets.FLASK_SECRET_KEY_ENV_VAR, raising=False)
    keyPath = tmp_path / "isolated-recovery-key.txt"
    monkeypatch.setattr(secrets, "DEFAULT_KEY_PATH", keyPath)
    if keySource != "missing":
        keyPath.write_text("" if keySource == "empty" else WRONG_KEY, encoding="utf-8")
    if keySource == "dedicated":
        monkeypatch.setenv(secrets.ENCRYPTION_KEY_ENV_VAR, ORIGINAL_KEY)
        monkeypatch.setenv(secrets.FLASK_SECRET_KEY_ENV_VAR, WRONG_KEY)
    elif keySource == "flask":
        monkeypatch.setenv(secrets.FLASK_SECRET_KEY_ENV_VAR, ORIGINAL_KEY)
    elif keySource == "file":
        keyPath.write_text(ORIGINAL_KEY, encoding="utf-8")
    if keySource == "empty":
        with pytest.raises(RuntimeError, match="exists but is empty"):
            secrets.decryptSecret(stored[SMTP_FIELD])
        assert keyPath.read_text(encoding="utf-8") == ""
    else:
        decrypted = {field: secrets.decryptSecret(value) for field, value in stored.items()}
        if keySource == "missing":
            assert all(value is None for value in decrypted.values())
            assert keyPath.exists(), "fresh-install key creation cannot recover the original credentials"
        else:
            assert decrypted == PLAINTEXT
    conn = repo.connection()
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert [tuple(row) for row in conn.execute(
        "SELECT username, track_id, played_at, time_played FROM plays"
    )] == [(USERNAME, TRACK_ID, PLAYED_AT, PLAY_TIME_MS)]
