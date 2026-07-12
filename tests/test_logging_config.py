"""Tests for Database/logging_config.py.

Every diagnostic in this codebase used to go through bare print() - invisible
the moment the console it ran in is gone (which is exactly what happened when
the listener's feed silently died - see test_listener_reconnect.py). This
verifies logging is actually routed to a persistent file instead.
"""
import logging
import shutil
import sys
import os
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.logging_config import configureLogging, LOG_FILE_NAME


class TestConfigureLogging(unittest.TestCase):
    def setUp(self):
        self._root = logging.getLogger()
        self._originalHandlers = list(self._root.handlers)
        self._originalLevel = self._root.level

    def tearDown(self):
        # Close every handler this test added before anything tries to delete
        # the temp directory backing it - Windows keeps an open log file locked.
        for handler in list(self._root.handlers):
            self._root.removeHandler(handler)
            if handler not in self._originalHandlers:
                handler.close()
        for handler in self._originalHandlers:
            self._root.addHandler(handler)
        self._root.setLevel(self._originalLevel)

    def _tmpdir(self) -> str:
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
        return tmpdir

    def test_creates_log_file_in_given_directory(self):
        logDir = Path(self._tmpdir()) / "nested"
        logFile = configureLogging(logDir)

        self.assertEqual(logFile, logDir / LOG_FILE_NAME)
        self.assertTrue(logDir.exists())

    def test_a_logger_message_is_written_to_the_file(self):
        logFile = configureLogging(self._tmpdir())

        logger = logging.getLogger("Database.someModule")
        logger.info("hello from the listener")
        for handler in logging.getLogger().handlers:
            handler.flush()

        content = logFile.read_text(encoding="utf-8")
        self.assertIn("hello from the listener", content)
        self.assertIn("Database.someModule", content)

    def test_calling_twice_does_not_duplicate_handlers(self):
        tmpdir = self._tmpdir()
        configureLogging(tmpdir)
        handlerCountAfterFirst = len(logging.getLogger().handlers)

        configureLogging(tmpdir)
        handlerCountAfterSecond = len(logging.getLogger().handlers)

        self.assertEqual(handlerCountAfterFirst, handlerCountAfterSecond)

    def test_defaults_to_default_db_path_parent(self):
        import Database.db as dbModule

        fakeDbPath = Path(self._tmpdir()) / "fake.db"
        original = dbModule.DEFAULT_DB_PATH
        dbModule.DEFAULT_DB_PATH = fakeDbPath
        try:
            logFile = configureLogging()
        finally:
            dbModule.DEFAULT_DB_PATH = original

        self.assertEqual(logFile, fakeDbPath.parent / LOG_FILE_NAME)


if __name__ == "__main__":
    unittest.main()
