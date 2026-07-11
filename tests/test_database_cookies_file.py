import json
import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase


class TestMaterializeCookiesFile(DatabaseTestCase):
    def test_writes_cookies_from_database_in_savesession_shape(self):
        db = self._makeDb({}, [], username="alice")
        db.email = "alice@example.com"
        db.repo.setUserCookies("alice", {"sp_dc": "abc123"})

        tmpPath = db._materializeCookiesFile()
        try:
            data = json.loads(tmpPath.read_text(encoding="utf-8"))
            self.assertEqual(data, [{"identifier": "alice@example.com", "cookies": {"sp_dc": "abc123"}}])
        finally:
            tmpPath.unlink(missing_ok=True)

    def test_no_stored_cookies_writes_empty_cookies_dict(self):
        db = self._makeDb({}, [], username="alice")
        db.email = "alice@example.com"

        tmpPath = db._materializeCookiesFile()
        try:
            data = json.loads(tmpPath.read_text(encoding="utf-8"))
            self.assertEqual(data, [{"identifier": "alice@example.com", "cookies": {}}])
        finally:
            tmpPath.unlink(missing_ok=True)


class TestWithCookiesFile(DatabaseTestCase):
    def test_uses_explicit_cookies_file_without_touching_database(self):
        db = self._makeDb({}, [], username="alice")
        db.cookiesFile = "explicit/path/cookies.json"

        seenPaths = []
        result = db._withCookiesFile(lambda cf: seenPaths.append(cf) or "client")

        self.assertEqual(seenPaths, ["explicit/path/cookies.json"])
        self.assertEqual(result, "client")

    def test_materializes_and_deletes_temp_file_when_no_explicit_path(self):
        db = self._makeDb({}, [], username="alice")
        db.cookiesFile = None
        db.email = "alice@example.com"

        seenPaths = []

        def factory(cf):
            seenPaths.append(cf)
            self.assertTrue(Path(cf).exists())  #< file must exist while the factory runs
            return "client"

        result = db._withCookiesFile(factory)

        self.assertEqual(result, "client")
        self.assertFalse(Path(seenPaths[0]).exists(), "temp cookies file must be deleted after use")

    def test_temp_file_is_deleted_even_if_factory_raises(self):
        db = self._makeDb({}, [], username="alice")
        db.cookiesFile = None

        seenPaths = []

        def factory(cf):
            seenPaths.append(cf)
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            db._withCookiesFile(factory)

        self.assertFalse(Path(seenPaths[0]).exists())


class TestStartListenerAndImportHistoryUseDatabaseCookies(DatabaseTestCase):
    def test_start_listener_passes_materialized_file_to_listener(self):
        db = self._makeDb({}, [], username="alice")
        db.cookiesFile = None
        db.email = "alice@example.com"
        db.repo.setUserCookies("alice", {"sp_dc": "abc"})

        with patch("Database.database.Listener") as mockListenerClass:
            mockListener = MagicMock()
            mockListenerClass.return_value = mockListener

            db.startListener(email="alice@example.com")

            calledPath, calledKwargs = mockListenerClass.call_args
            self.assertTrue(Path(calledPath[0]).name.startswith("cookies_alice_"))
            self.assertEqual(calledKwargs["email"], "alice@example.com")
            mockListener.startListener_thread.assert_called_once()


if __name__ == "__main__":
    import unittest
    unittest.main()
