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
from unittest.mock import patch
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.logging_config import (
    configureLogging, InstanceZoneFormatter, LOG_FILE_NAME, LOG_FORMAT,
)


class TestLogTimestampZone(unittest.TestCase):
    """The stamp on every line comes from the instance's configured zone, not
    the C runtime's idea of local time.

    logging.Formatter renders %(asctime)s through time.localtime, and on Windows
    the UCRT reads TZ itself and parses it as a POSIX spec - so an IANA name is
    neither understood nor ignored. TZ=Europe/Zurich silently resolved to a
    fixed UTC+1 with no DST, putting every log line an hour behind the wall
    clock for half the year, while the app's own date handling (which goes
    through ZoneInfo) stayed right. TZ=America/Los_Angeles lands on the same
    wrong answer, which is how you can tell it is reading letters rather than
    looking anything up."""

    #< a fixed instant - 2026-08-07 10:14:13 in Europe/Zurich (CEST, +02:00),
    #  08:14:13 UTC - so the assertion does not depend on when the suite runs,
    #  or on which side of a DST change it runs
    FIXED_EPOCH = 1786090453.0

    def _line(self, zone):
        record = logging.LogRecord("probe", logging.INFO, __file__, 1, "hello", None, None)
        #< set after construction: LogRecord derives msecs from its own
        #  time.time() call, so both have to be pinned or the two disagree
        record.created = self.FIXED_EPOCH
        record.msecs = 95.0
        with patch("Database.utils.tz", ZoneInfo(zone)):
            return InstanceZoneFormatter(LOG_FORMAT).format(record)

    def test_a_record_is_stamped_in_the_configured_zone(self):
        self.assertTrue(self._line("Europe/Zurich").startswith("2026-08-07 10:14:13,095+0200"),
                        self._line("Europe/Zurich"))

    def test_a_different_zone_moves_the_stamp(self):
        """Proves it reads the configured zone rather than happening to agree
        with the machine's - the same instant, two hours apart."""
        self.assertTrue(self._line("UTC").startswith("2026-08-07 08:14:13,095+0000"),
                        self._line("UTC"))

    def test_the_offset_is_part_of_the_stamp(self):
        """A bare local time in a file that outlives a DST change cannot be read
        back unambiguously - and this log is read months later, by hand, next to
        an incident. Two lines an hour apart across the October change are
        otherwise indistinguishable from two an hour apart."""
        for zone, offset in (("Europe/Zurich", "+0200"), ("America/Los_Angeles", "-0700")):
            with self.subTest(zone=zone):
                self.assertIn(offset, self._line(zone).split()[1])

    def test_configure_logging_installs_it_on_every_handler(self):
        root = logging.getLogger()
        original = list(root.handlers)
        tmpdir = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(tmpdir, ignore_errors=True))
        try:
            configureLogging(tmpdir)
            added = [h for h in root.handlers if h not in original]
            self.assertTrue(added)
            for handler in added:
                self.assertIsInstance(handler.formatter, InstanceZoneFormatter)
        finally:
            for handler in list(root.handlers):
                if handler not in original:
                    root.removeHandler(handler)
                    handler.close()


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
