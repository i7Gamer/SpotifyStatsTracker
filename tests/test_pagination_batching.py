"""Database._paginateEntries() must hydrate a page of play history with one
batched track-metadata fetch (Repository.getTracksByIds), not one getTrack()
call per entry - the old per-entry loop cost 3 queries per play, which meant
3x(history size) queries just to render a single dashboard page.
"""
import sys
import os
from unittest.mock import patch, MagicMock

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from conftest import DatabaseTestCase, normalizeTrackForTest
import Database.database as databaseModule


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

    def test_a_track_missing_from_the_catalog_is_dropped_without_any_live_fetch(self):
        """plays.track_id has an enforced foreign key into tracks.id, so a play
        can never be written without its track: a missing one means the track
        row was deleted afterwards, i.e. the dangling-row corruption the boot
        probe reports and migrate1_43_0 repairs.

        Rendering is not the place to fix that. This path used to call the
        listener live - a Spotify round-trip with up to five seconds of
        time.sleep in its retry ladders, plus an upsertTrack and a commit, on
        the request thread, as a side effect of a GET - and it repeated on every
        render because a failure isn't cached. It now drops the entry, which is
        what the failing fetch did anyway.

        Exercised by calling _paginateEntries() directly, since the foreign key
        makes such a row impossible to insert normally."""
        tracks, _ = self._sampleData()
        db = self._makeDb(tracks, [])
        db.listener = MagicMock()   #< a live listener is available and must still not be used

        entries = [
            {"id": "t1", "playedAt": 100, "timePlayed": 1000},
            {"id": "missing-track", "playedAt": 200, "timePlayed": 1000},
        ]

        with self.assertLogs("Database.database", level="WARNING") as logs:
            result = db._paginateEntries(entries)

        db.listener.track.assert_not_called()
        self.assertEqual([e["id"] for e in result], ["t1"])
        self.assertIn("missing-track", "".join(logs.output))

    def test_a_fully_hydrated_page_logs_nothing(self):
        """The warning above names real corruption - it must not fire on the
        ordinary path, or it becomes noise nobody reads."""
        tracks, _ = self._sampleData()
        db = self._makeDb(tracks, [])

        with patch.object(databaseModule.logger, "warning") as warned:
            db._paginateEntries([{"id": "t1", "playedAt": 100, "timePlayed": 1000}])

        warned.assert_not_called()


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


class TestPaginateEntriesKeepsSkipFlag(DatabaseTestCase):
    """Hydration must carry the read's is_skip flag onto the merged entry.

    The song detail timeline labels each play Full/Partial/Skipped off
    entry["isSkip"] (see DashboardViewModels._enrichSongTimelineEntries), and
    getPlaysNewestFirst/getPlaysOldestFirst both SELECT is_skip for it. But
    _mergeEntryWithTrack rebuilds the entry from the track row and used to copy
    only playedAt/timePlayed/playedFrom/extras across, so the flag never
    survived hydration: every skip reached the template with isSkip absent and
    rendered as "Partial - N%" instead of "Skipped"."""

    PLAY_MS = 190000
    SKIP_MS = 3000

    def _dbWithASkip(self):
        tracks = {"t1": normalizeTrackForTest({"id": "t1", "name": "Song One", "artists": []})}
        db = self._makeDb(tracks, [])
        db.repo.insertPlay(db.user, "t1", 100.0, self.PLAY_MS, is_skip=0)
        db.repo.insertPlay(db.user, "t1", 200.0, self.SKIP_MS, is_skip=1)
        db.repo.commit()
        return db

    def test_newest_first_entries_carry_is_skip(self):
        db = self._dbWithASkip()

        entries = db.getEntriesFromNew(trackId="t1", includeSkips=True)

        self.assertEqual([e["playedAt"] for e in entries], [200.0, 100.0])
        self.assertTrue(entries[0]["isSkip"])
        self.assertFalse(entries[1]["isSkip"])

    def test_oldest_first_entries_carry_is_skip(self):
        db = self._dbWithASkip()

        entries = db.getEntriesFromOld(trackId="t1", includeSkips=True)

        self.assertEqual([e["playedAt"] for e in entries], [100.0, 200.0])
        self.assertFalse(entries[0]["isSkip"])
        self.assertTrue(entries[1]["isSkip"])

    def test_skip_only_read_reports_its_rows_as_skips(self):
        """getSkipEntriesFromOld returns is_skip=1 rows by definition, but its
        SELECT omitted the column, so _playRowToEntry fell back to False and
        every one of them described itself as a real play."""
        db = self._dbWithASkip()

        entries = db.getSkipEntriesFromOld()

        self.assertEqual([e["playedAt"] for e in entries], [200.0])
        self.assertTrue(entries[0]["isSkip"])


if __name__ == "__main__":
    import unittest
    unittest.main()
