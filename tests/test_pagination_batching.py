"""Database._paginateEntries() must hydrate a page of play history with one
batched track-metadata fetch (Repository.getTracksByIds), not one getTrack()
call per entry - the old per-entry loop cost 3 queries per play, which meant
3x(history size) queries just to render a single dashboard page.
"""
import sys
import os
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest


class TestPaginateEntriesBatching(DatabaseTestCase):
    def _sampleData(self):
        tracks = {
            "t1": normalizeTrackForTest({"id": "t1", "name": "Song One", "artists": []}),
            "t2": normalizeTrackForTest({"id": "t2", "name": "Song Two", "artists": []}),
        }
        entries = [
            {"id": "t1", "playedAt": 100, "timePlayed": 1000},
            {"id": "t2", "playedAt": 200, "timePlayed": 1000},
            {"id": "t1", "playedAt": 300, "timePlayed": 1000},
        ]
        return tracks, entries

    def test_fetches_track_metadata_in_a_single_batched_call(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        with patch.object(db.repo, "getTracksByIds", wraps=db.repo.getTracksByIds) as batchSpy, \
             patch.object(db.repo, "getTrack", wraps=db.repo.getTrack) as singleSpy:
            result = db.getEntriesFromNew()

        batchSpy.assert_called_once()
        singleSpy.assert_not_called()  #< the old per-entry lookup must not run at all when everything is in the batch
        self.assertEqual(len(result), 3)

    def test_batched_id_list_has_no_duplicates(self):
        tracks, entries = self._sampleData()  # t1 appears twice in entries
        db = self._makeDb(tracks, entries)

        with patch.object(db.repo, "getTracksByIds", wraps=db.repo.getTracksByIds) as batchSpy:
            db.getEntriesFromNew()

        requestedIds = batchSpy.call_args.args[0]
        self.assertEqual(sorted(set(requestedIds)), sorted(requestedIds))  #< no repeats

    def test_results_are_hydrated_and_ordered_newest_first(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        result = db.getEntriesFromNew()

        self.assertEqual([e["playedAt"] for e in result], [300, 200, 100])
        self.assertEqual([e["id"] for e in result], ["t1", "t2", "t1"])
        self.assertEqual(result[0]["name"], "Song One")
        self.assertEqual(result[1]["name"], "Song Two")

    def test_track_missing_from_catalog_falls_back_to_single_lookup_and_is_dropped_without_a_listener(self):
        """A play whose track isn't in the catalog (rare - e.g. a partially
        failed import) must still be handled via the existing single-track
        fallback path (which would normally re-fetch it live from the
        listener), not silently corrupt the batched result. plays.track_id has
        a foreign key into tracks.id, so this is exercised by calling
        _paginateEntries() directly with a synthetic entry rather than trying
        to get such a row into the real plays table."""
        tracks, _ = self._sampleData()
        db = self._makeDb(tracks, [])

        entries = [
            {"id": "t1", "playedAt": 100, "timePlayed": 1000},
            {"id": "missing-track", "playedAt": 200, "timePlayed": 1000},
        ]

        with patch.object(db, "_ensureTrackMetadata", wraps=db._ensureTrackMetadata) as fallbackSpy:
            result = db._paginateEntries(entries)

        fallbackSpy.assert_called_once_with("missing-track")
        # db.listener is None in this test, so the fallback can't actually
        # fetch it live - that entry is dropped, the other is unaffected.
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "t1")


class TestSearchEntries(DatabaseTestCase):
    """Database.searchEntries()/searchEntriesCount() delegate matching and
    pagination to Repository.searchPlays()/searchPlaysCount() (SQL LIKE +
    LIMIT/OFFSET), then hydrate the matched page's track metadata the same
    batched way as the non-search path."""

    def _sampleData(self):
        tracks = {
            "t1": normalizeTrackForTest({"id": "t1", "name": "Bohemian Rhapsody", "artists": []}),
            "t2": normalizeTrackForTest({"id": "t2", "name": "Unrelated Song", "artists": []}),
        }
        entries = [
            {"id": "t1", "playedAt": 100, "timePlayed": 1000},
            {"id": "t2", "playedAt": 200, "timePlayed": 1000},
        ]
        return tracks, entries

    def test_search_entries_returns_hydrated_matches_only(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        result = db.searchEntries("bohemian")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "Bohemian Rhapsody")

    def test_search_entries_count_matches_result_length_across_pages(self):
        tracks, entries = self._sampleData()
        db = self._makeDb(tracks, entries)

        self.assertEqual(db.searchEntriesCount("bohemian"), 1)
        self.assertEqual(db.searchEntriesCount("song"), 1)
        self.assertEqual(db.searchEntriesCount("nonexistent"), 0)

    def test_search_entries_respects_count_and_start_index(self):
        tracks, _ = self._sampleData()
        entries = [{"id": "t1", "playedAt": i, "timePlayed": 1000} for i in range(5)]
        db = self._makeDb(tracks, entries)

        page = db.searchEntries("bohemian", count=2, startIndex=1)

        self.assertEqual([e["playedAt"] for e in page], [3, 2])


if __name__ == "__main__":
    import unittest
    unittest.main()
