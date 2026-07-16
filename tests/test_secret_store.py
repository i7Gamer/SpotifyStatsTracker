"""Database/secret_store.py - encryption-at-rest for stored secrets.

Spotify session cookies, API client secrets and refresh tokens live in the
shared SQLite file, which the README tells users to back up and copy around;
stored as plaintext, one leaked backup handed out every user's live Spotify
session. Values are Fernet-encrypted with a key that never lives inside the
database file itself (env var, or a file under secrets/).
"""
import os
import sys
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
