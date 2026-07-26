"""Database/secret_store.py - encryption-at-rest for stored secrets.

Spotify session cookies, API client secrets and refresh tokens live in the
shared SQLite file, which the README tells users to back up and copy around;
stored as plaintext, one leaked backup handed out every user's live Spotify
session. Values are Fernet-encrypted with a key that never lives inside the
database file itself (env var, or a file under secrets/).
"""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.secret_store as secretStore
from Database.secret_store import encryptSecret, decryptSecret, isEncrypted, ENCRYPTED_PREFIX


class TestRoundTrip(unittest.TestCase):
    def test_encrypt_decrypt_round_trip(self):
        self.assertEqual(decryptSecret(encryptSecret("hello secret")), "hello secret")

    def test_encrypted_form_is_marked_and_unreadable(self):
        stored = encryptSecret('{"sp_dc": "super-secret-cookie"}')
        self.assertTrue(stored.startswith(ENCRYPTED_PREFIX))
        self.assertNotIn("super-secret-cookie", stored)
        self.assertTrue(isEncrypted(stored))

    def test_unicode_survives_the_round_trip(self):
        self.assertEqual(decryptSecret(encryptSecret("pässwörd-日本語")), "pässwörd-日本語")


class TestLegacyAndEdgeCases(unittest.TestCase):
    def test_plaintext_passes_through_unchanged(self):
        """Values written before encryption existed have no prefix and must
        keep reading back as-is."""
        legacy = '{"sp_dc": "legacy-cookie"}'
        self.assertEqual(decryptSecret(legacy), legacy)
        self.assertFalse(isEncrypted(legacy))

    def test_none_reads_as_none(self):
        self.assertIsNone(decryptSecret(None))

    def test_undecryptable_value_reads_as_missing(self):
        """A prefixed value that can't be decrypted (garbage, or a rotated/
        lost key) must read as missing - routing the user through re-login -
        rather than raising or leaking the raw token."""
        self.assertIsNone(decryptSecret(ENCRYPTED_PREFIX + "not-a-real-token"))

    def test_value_encrypted_under_a_different_key_reads_as_missing(self):
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-one"}):
            stored = encryptSecret("secret")
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "key-two"}):
            self.assertIsNone(decryptSecret(stored))


class TestAnEmptyKeyFileFailsLoudly(unittest.TestCase):
    """An existing-but-empty key file used to be treated as "no key yet": the
    resolver fell through and MINTED A NEW ONE, over the top of the old path.
    Every enc:v1: value in the database then failed to decrypt, and decryptSecret
    returns None on failure, which callers read as "no cookies / no credentials
    stored" - so every user was silently bounced to re-login and their stored
    Spotify refresh tokens became unrecoverable, with nothing louder than a
    per-read warning.

    The state is reachable: the file is written with a plain truncate-then-write,
    so a crash or a full disk between the two leaves it empty.

    Refusing to start is the only safe answer - the same call the app already
    makes for the placeholder FLASK_SECRET_KEY. Restoring the file from a backup
    recovers everything; minting a key cannot be undone."""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.keyPath = pathlib.Path(self._tmpdir.name) / "key.txt"
        patcher = patch.object(secretStore, "DEFAULT_KEY_PATH", self.keyPath)
        patcher.start()
        self.addCleanup(patcher.stop)
        self._envPatcher = patch.dict(os.environ, {}, clear=False)
        self._envPatcher.start()
        self.addCleanup(self._envPatcher.stop)
        os.environ.pop(secretStore.ENCRYPTION_KEY_ENV_VAR, None)
        os.environ.pop(secretStore.FLASK_SECRET_KEY_ENV_VAR, None)

    def test_an_empty_key_file_raises_instead_of_minting_a_new_key(self):
        self.keyPath.write_text("", encoding="utf-8")

        with self.assertRaises(RuntimeError) as ctx:
            secretStore._keyMaterial()

        self.assertIn(str(self.keyPath), str(ctx.exception))
        self.assertEqual(self.keyPath.read_text(encoding="utf-8"), "",
                         "the empty file must be left alone, not overwritten with a new key")

    def test_a_whitespace_only_key_file_is_treated_the_same(self):
        self.keyPath.write_text("   \n", encoding="utf-8")

        with self.assertRaises(RuntimeError):
            secretStore._keyMaterial()

    def test_a_missing_key_file_still_mints_one(self):
        """First run on a fresh install must keep working."""
        minted = secretStore._keyMaterial()

        self.assertTrue(minted)
        self.assertEqual(self.keyPath.read_text(encoding="utf-8").strip(), minted)

    def test_a_populated_key_file_is_returned_unchanged(self):
        self.keyPath.write_text("an-existing-key", encoding="utf-8")

        self.assertEqual(secretStore._keyMaterial(), "an-existing-key")

    def test_the_key_file_is_written_atomically(self):
        """The truncate-then-write is what creates the empty file above."""
        secretStore._keyMaterial()

        leftovers = [p.name for p in self.keyPath.parent.iterdir() if p != self.keyPath]
        self.assertEqual(leftovers, [], f"temporary artefacts left behind: {leftovers}")


class TestKeyResolution(unittest.TestCase):
    def test_env_key_takes_precedence_over_key_file(self):
        stored = encryptSecret("file-key-secret")   #< under the (test-isolated) key file
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "some-env-key"}):
            self.assertIsNone(decryptSecret(stored), "env key must win over the key file")

    def test_data_encryption_key_takes_precedence_over_flask_secret_key(self):
        env = {
            secretStore.ENCRYPTION_KEY_ENV_VAR: "dedicated-key",
            secretStore.FLASK_SECRET_KEY_ENV_VAR: "flask-key",
        }
        with patch.dict(os.environ, env):
            stored = encryptSecret("secret")
        with patch.dict(os.environ, {secretStore.ENCRYPTION_KEY_ENV_VAR: "dedicated-key"}):
            self.assertEqual(decryptSecret(stored), "secret")
        with patch.dict(os.environ, {secretStore.FLASK_SECRET_KEY_ENV_VAR: "flask-key"}):
            self.assertIsNone(decryptSecret(stored))

    def test_flask_secret_key_is_used_when_no_dedicated_key(self):
        """Docker deployments already set FLASK_SECRET_KEY (the README example
        includes it) - reusing it means zero new configuration for them."""
        with patch.dict(os.environ, {secretStore.FLASK_SECRET_KEY_ENV_VAR: "flask-key"}):
            stored = encryptSecret("secret")
            self.assertEqual(decryptSecret(stored), "secret")

    def test_key_file_is_created_once_and_reused(self):
        self.assertFalse(secretStore.DEFAULT_KEY_PATH.exists())

        stored = encryptSecret("secret")

        self.assertTrue(secretStore.DEFAULT_KEY_PATH.exists())
        keyMaterial = secretStore.DEFAULT_KEY_PATH.read_text(encoding="utf-8").strip()
        self.assertTrue(keyMaterial)
        self.assertEqual(decryptSecret(stored), "secret", "a later call must reuse the same key file")


if __name__ == "__main__":
    unittest.main()
