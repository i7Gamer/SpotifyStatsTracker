# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later
"""SECONDS_PER_DAY has one home, Database/utils.py.

It was defined four times (database.py, queries/_base.py, queries/trends.py -
which shadowed the name it already star-imported from _base - and utils.py),
so a change to the day convention was a four-place edit with nothing keeping
the copies equal. The other three now import it; the ratchet below is a
source-shape assertion because "the same value" is exactly what a
re-introduced copy would also have, and a behavioural test could not tell
them apart.
"""
import os
import pathlib
import re
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

import Database.utils
import Database.database
import Database.queries._base
import Database.queries.trends

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_HOME = REPO_ROOT / "Database" / "utils.py"
_DEFINITION = re.compile(r"^SECONDS_PER_DAY\s*=", re.MULTILINE)


class TestSecondsPerDaySingleHome(unittest.TestCase):
    def _definingFiles(self):
        candidates = list((REPO_ROOT / "Database").rglob("*.py")) + [REPO_ROOT / "config.py"]
        return sorted(path for path in candidates
                      if _DEFINITION.search(path.read_text(encoding="utf-8")))

    def test_the_constant_is_defined_exactly_once_in_utils(self):
        self.assertEqual(self._definingFiles(), [_HOME])

    def test_the_former_homes_still_expose_the_one_value(self):
        """database.py's own consumers and the query mixins (which reach it via
        `from Database.queries._base import *`) read the same object utils
        defines - the import, not a re-typed literal."""
        for module in (Database.database, Database.queries._base, Database.queries.trends):
            with self.subTest(module=module.__name__):
                self.assertIs(module.SECONDS_PER_DAY, Database.utils.SECONDS_PER_DAY)


if __name__ == "__main__":
    unittest.main()
