"""Tests for Database.appendTrackData's insert-time dedup guard.

A wide, defense-in-depth guard (duration + BACKFILL_INSERT_GUARD_EXTRA_SECONDS)
applied ONLY to Web API backfill-sourced inserts (source="web_api_backfill"),
never to the live listener's own inserts (source="listener") - see
appendTrackData's inline comment for why. This is symmetric and catches a
duplicate regardless of whether Spotify reported an entry's played_at as a
start or end time (spotify/web-api#1083 - the field is documented as
inconsistent about this).
"""
import sys
import os
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

if isinstance(sys.modules.get("Database.database"), MagicMock):
    del sys.modules["Database.database"]

from Database.database import Database


def _bareDatabase():
    db = Database.__new__(Database)
    db.user = "alice"
    db.repo = MagicMock()
    db.appendMetadata = MagicMock(return_value=True)
    return db


TRACK = {"id": "t1", "name": "Song One", "duration_ms": 180000}


class TestAppendTrackDataDedupGuard(unittest.TestCase):
    @patch("Database.database.Client")
    def test_backfill_with_nearby_play_is_skipped(self, mock_client):
        mock_client.formatTrack.return_value = {"id": "t1", "playedAt": 1000.0}
        db = _bareDatabase()
        db.repo.hasPlayNearTime.return_value = True

        result = db.appendTrackData("2026-07-13T10:00:00Z", TRACK, 180000, source="web_api_backfill")

        self.assertFalse(result)
        db.appendMetadata.assert_not_called()

    @patch("Database.database.Client")
    def test_backfill_with_no_nearby_play_is_inserted(self, mock_client):
        mock_client.formatTrack.return_value = {"id": "t1", "playedAt": 1000.0}
        db = _bareDatabase()
        db.repo.hasPlayNearTime.return_value = False

        result = db.appendTrackData("2026-07-13T10:00:00Z", TRACK, 180000, source="web_api_backfill")

        self.assertTrue(result)
        db.appendMetadata.assert_called_once()

    @patch("Database.database.Client")
    def test_listener_source_with_nearby_play_is_still_inserted(self, mock_client):
        """Locks in the design decision: the guard must never apply to the
        live listener's own insert path. A genuine short-track replay within
        duration+60s is normal listener behavior and must not be dropped."""
        mock_client.formatTrack.return_value = {"id": "t1", "playedAt": 1000.0}
        db = _bareDatabase()
        db.repo.hasPlayNearTime.return_value = True  # even if a "nearby" play exists

        result = db.appendTrackData("2026-07-13T10:00:00Z", TRACK, 180000, source="listener")

        self.assertTrue(result)
        db.appendMetadata.assert_called_once()
        db.repo.hasPlayNearTime.assert_not_called()  # guard isn't even consulted for listener source

    @patch("Database.database.Client")
    def test_backfill_guard_uses_duration_plus_extra_seconds_tolerance(self, mock_client):
        mock_client.formatTrack.return_value = {"id": "t1", "playedAt": 1000.0}
        db = _bareDatabase()
        db.repo.hasPlayNearTime.return_value = False

        db.appendTrackData("2026-07-13T10:00:00Z", TRACK, 180000, source="web_api_backfill")

        db.repo.hasPlayNearTime.assert_called_once_with(
            "alice", "t1", 1000.0, 180 + Database.BACKFILL_INSERT_GUARD_EXTRA_SECONDS
        )

    @patch("Database.database.Client")
    def test_backfill_guard_handles_missing_duration(self, mock_client):
        """A track dict with no duration_ms must not crash the guard - falls
        back to just the extra-seconds margin."""
        mock_client.formatTrack.return_value = {"id": "t1", "playedAt": 1000.0}
        db = _bareDatabase()
        db.repo.hasPlayNearTime.return_value = False

        db.appendTrackData("2026-07-13T10:00:00Z", {"id": "t1", "name": "No Duration"}, 0, source="web_api_backfill")

        db.repo.hasPlayNearTime.assert_called_once_with(
            "alice", "t1", 1000.0, Database.BACKFILL_INSERT_GUARD_EXTRA_SECONDS
        )


if __name__ == "__main__":
    unittest.main()
