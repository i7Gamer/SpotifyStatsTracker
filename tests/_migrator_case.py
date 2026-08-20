# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The two helpers every migrator test writes out for itself.

A migrator test seeds a legacy database, runs one step, and inspects the
result. The seeding differs per migrator - that is the whole point of the file -
so there is no shared ``setUp`` to be had here and none is offered. What IS
shared is how you get a Repository and how you ask what columns a table has,
and eighteen files had each written those out identically.

Worth centralizing rather than left alone because both encode a rule about
CLOSING, not just a shortcut:

- ``_repo`` has to register the close as a cleanup, because it hands the
  connection back open. A migrator test that forgets leaves a handle on the
  temp database, and on Windows that makes ``TemporaryDirectory.cleanup``
  fail - as a confusing PermissionError in teardown, attributed to whichever
  test happened to run last.
- ``_columnNames`` does not hand anything back, so it closes immediately in a
  ``finally``. Eight files used ``addCleanup`` here instead, which keeps a
  connection open for the remainder of the test and opens another on every
  call. Consolidating on the ``finally`` form is the reason this file is not
  merely tidying.

Imported by bare module name (``from _migrator_case import ...``) like the
suite's other shared helpers, since tests/ is on sys.path with no package
``__init__``.
"""
import sqlite3

from Database.repository import Repository


class MigratorHelpersMixin:
    """Requires ``self.dbPath``, which each migrator test's own setUp defines."""

    def _repo(self):
        """A Repository on the test database, closed at teardown.

        Cleanup rather than a ``finally``: the caller gets the connection open
        and goes on using it."""
        repo = Repository(self.dbPath)
        self.addCleanup(repo.connectionManager.close)
        return repo

    def _columnNames(self, table):
        """The column names of `table`, with nothing left open.

        Closed here rather than at teardown: this returns a set of strings, so
        there is no reason to hold the connection, and a test that asks about
        three tables would otherwise be holding three."""
        conn = sqlite3.connect(self.dbPath)
        try:
            return {row[1] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        finally:
            conn.close()
