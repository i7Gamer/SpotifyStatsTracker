# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""services.genre_gate.userHasLastfmKey - whether THIS user has a Last.fm API
key configured, as opposed to whether the admin's instance-wide genre
backfill toggle is on. The four genre-gate include sites (Charts, Genres,
Wrapped incl. its public /shared view, Overview) used to pass the instance
toggle to _genre_progress.html's `lastfmConfigured` flag, which pitched
adding a key to a user who already had one and just picked an empty range,
or (this function's own bug) never distinguished a truly keyless user from
one with a working key at all.

2026-09-02 review, UT-12 follow-up."""
import os
import sys
import unittest
from unittest.mock import MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from services.genre_gate import userHasLastfmKey


def _repo(enabled=True):
    repo = MagicMock()
    repo.isLastfmGenreBackfillEnabled.return_value = enabled
    return repo


class UserHasLastfmKeyTestCase(unittest.TestCase):
    def test_true_when_the_worker_reports_configured(self):
        db = MagicMock()
        db.getLastfmWorkerStatus.return_value = {"configured": True, "running": False}
        self.assertTrue(userHasLastfmKey(_repo(), db))

    def test_false_when_the_worker_reports_unconfigured(self):
        db = MagicMock()
        db.getLastfmWorkerStatus.return_value = {"configured": False, "running": False}
        self.assertFalse(userHasLastfmKey(_repo(), db))

    def test_false_when_db_is_none(self):
        self.assertFalse(userHasLastfmKey(_repo(), None))

    def test_false_when_the_instance_wide_toggle_is_off_even_with_a_key(self):
        """The kill switch means the worker never runs, so "configured" is
        moot - and every caller already hides the whole genre section in
        that case."""
        db = MagicMock()
        db.getLastfmWorkerStatus.return_value = {"configured": True, "running": False}
        self.assertFalse(userHasLastfmKey(_repo(enabled=False), db))
        db.getLastfmWorkerStatus.assert_not_called()

    def test_false_on_an_unstubbed_magicmock_status(self):
        """A bare MagicMock db (most existing route tests) answers
        getLastfmWorkerStatus() with a MagicMock, not a dict - must degrade
        to False rather than a truthy MagicMock.get(...) accident."""
        db = MagicMock()
        self.assertFalse(userHasLastfmKey(_repo(), db))

    def test_false_when_the_lookup_raises(self):
        db = MagicMock()
        db.getLastfmWorkerStatus.side_effect = RuntimeError("boom")
        self.assertFalse(userHasLastfmKey(_repo(), db))


if __name__ == "__main__":
    unittest.main()
