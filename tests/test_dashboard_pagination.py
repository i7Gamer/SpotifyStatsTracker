import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: unlike some sibling test modules, this file deliberately does NOT swap
# Database modules for MagicMocks in sys.modules. These tests only exercise the
# dashboard route with a per-test mock db (via get_user_db), so module mocks are
# unnecessary - and a module-level mock/restore here would poison the
# patch("Database.database...") targets of test files that run after this one.
import app as appModule
from app import SpotifyDashboardApp


class _ListRouteTestBase(unittest.TestCase):
    """Shared fixtures for exercising the list routes with a mocked per-user db."""

    @patch('app.SpotifyDashboardApp._get_or_create_secret_key', return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _makeDb(self, entryCount):
        db = MagicMock()
        db.getEntriesFromNew.return_value = []
        db.getEntriesCount.return_value = entryCount
        db.searchEntries.return_value = []
        db.searchEntriesCount.return_value = 0
        db.getTopSongs.return_value = []
        db.getSongsCount.return_value = 0
        db.getPlayTotals.return_value = (0, 0)
        db.getTopArtists.return_value = []
        db.getArtistsCount.return_value = 0
        db.getArtistTotals.return_value = (0, 0, 0)
        db.getOverallStats.return_value = {
            "currentTopSongs": [],
            "currentTopArtists": [],
            "totalSongsPlayed": 0,
            "totalDurationMs": 0,
            "previousSongsPlayed": 0,
            "previousDurationMs": 0,
        }
        return db

    def _getPath(self, dash, db, path):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(path)

    def _getDashboard(self, dash, db, query=""):
        return self._getPath(dash, db, f"/{query}")

    def _getTopSongs(self, dash, db, query=""):
        return self._getPath(dash, db, f"/top-songs{query}")


class TestDashboardPagination(_ListRouteTestBase):
    """Without a search query the dashboard must only materialize the page being
    shown - joining full track metadata onto every entry ever recorded on every
    request gets slow once the history grows large."""

    def test_without_search_fetches_only_one_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0)
        self.assertIn(b"Page 1 of 3", resp.data)

    def test_without_search_requests_correct_offset_for_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db, query="?page=2")

        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=appModule.PAGE_SIZE)
        self.assertIn(b"Page 2 of 3", resp.data)

    def test_without_search_clamps_page_beyond_range(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db, query="?page=99")

        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=2 * appModule.PAGE_SIZE)
        self.assertIn(b"Page 3 of 3", resp.data)

    def test_without_search_handles_empty_database(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getDashboard(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0)
        self.assertIn(b"Page 1 of 1", resp.data)

    def test_with_search_paginates_and_matches_in_sql(self):
        """Search is pushed into SQL (Repository.searchPlays) and paginated
        the same way as the non-search path - it must not fetch or count the
        unfiltered history at all."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)
        db.searchEntriesCount.return_value = 5

        resp = self._getDashboard(dash, db, query="?q=foo")

        self.assertEqual(resp.status_code, 200)
        db.searchEntriesCount.assert_called_once_with("foo")
        db.searchEntries.assert_called_once_with("foo", count=appModule.PAGE_SIZE, startIndex=0)
        db.getEntriesFromNew.assert_not_called()
        db.getEntriesCount.assert_not_called()

    def test_search_page_beyond_range_is_clamped_to_last_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.searchEntriesCount.return_value = 120

        resp = self._getDashboard(dash, db, query="?q=foo&page=9999")

        self.assertEqual(resp.status_code, 200)
        db.searchEntries.assert_called_once_with("foo", count=appModule.PAGE_SIZE, startIndex=2 * appModule.PAGE_SIZE)
        self.assertIn(b"Page 3 of 3", resp.data)


class TestTopSongsPagination(_ListRouteTestBase):
    """/top-songs must only ask the DB layer for the current page (SQL-level
    LIMIT/OFFSET, mirroring the dashboard's getEntriesCount/getEntriesFromNew
    pattern) when there's no search query - search still needs the full list
    to filter text across name/artist/album."""

    def test_without_search_fetches_only_one_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getTopSongs(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getSongsCount.assert_called_once()
        db.getTopSongs.assert_called_once()
        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["limit"], appModule.PAGE_SIZE)
        self.assertEqual(kwargs["offset"], 0)
        self.assertEqual(kwargs["by"], "totalTimeListened")   #< topSongsPage's default sortBy

    def test_without_search_requests_correct_offset_for_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getSongsCount.return_value = 120

        resp = self._getTopSongs(dash, db, query="?page=2")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["offset"], appModule.PAGE_SIZE)
        self.assertIn(b"Page 2 of 3", resp.data)

    def test_without_search_passes_requested_sort(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getTopSongs(dash, db, query="?sortBy=plays")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getTopSongs.call_args.kwargs["by"], "plays")

    def test_without_search_handles_empty_database(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getTopSongs(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Page 1 of 1", resp.data)

    def test_with_search_paginates_and_matches_in_sql(self):
        """Search is matched and paginated in SQL (Repository.getSongsPage)
        the same way as the non-search path, not by fetching everything and
        filtering in Python."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getSongsCount.return_value = 5

        resp = self._getTopSongs(dash, db, query="?q=foo")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getSongsCount.call_count, 2)
        db.getSongsCount.assert_any_call(None, None)
        db.getSongsCount.assert_any_call(None, None, searchQuery="foo")
        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["limit"], appModule.PAGE_SIZE)
        self.assertEqual(kwargs["offset"], 0)
        self.assertEqual(kwargs["searchQuery"], "foo")

    def test_totals_come_from_get_play_totals_independent_of_list(self):
        """totalPlays/totalTime must reflect the whole-range aggregate (via the
        cheap getPlayTotals call), not just whatever getTopSongs happens to
        return for the current page."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getPlayTotals.return_value = (42, 999000)

        resp = self._getTopSongs(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getPlayTotals.assert_called_once()
        self.assertIn(b'<p class="summary-value">42</p>', resp.data)

    def test_totals_are_fetched_in_search_branch_too(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getPlayTotals.return_value = (7, 1000)

        resp = self._getTopSongs(dash, db, query="?q=foo")

        self.assertEqual(resp.status_code, 200)
        db.getPlayTotals.assert_called_once()
        self.assertIn(b'<p class="summary-value">7</p>', resp.data)

    def test_unknown_sortby_falls_back_to_default_instead_of_500(self):
        """Repository.getSongsPage raises ValueError for a sortBy outside
        SONG_SORT_COLUMNS - an unvalidated query param would otherwise 500."""
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getTopSongs(dash, db, query="?sortBy=not_a_real_column")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getTopSongs.call_args.kwargs["by"], appModule.DEFAULT_SORT_BY)

    def test_page_beyond_range_is_clamped_to_last_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)
        db.getSongsCount.return_value = 120

        resp = self._getTopSongs(dash, db, query="?page=9999")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getTopSongs.call_args.kwargs
        self.assertEqual(kwargs["offset"], 2 * appModule.PAGE_SIZE)   #< last page (3) of 120/50
        self.assertIn(b"Page 3 of 3", resp.data)


class TestPageParamParsing(_ListRouteTestBase):
    """A non-numeric ?page= must not 500 any list route - it falls back to page 1."""

    def test_dashboard_survives_non_numeric_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db, query="?page=abc")

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0)
        self.assertIn(b"Page 1 of 3", resp.data)

    def test_dashboard_clamps_negative_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db, query="?page=-5")

        self.assertEqual(resp.status_code, 200)
        db.getEntriesFromNew.assert_called_once_with(count=appModule.PAGE_SIZE, startIndex=0)

    def test_top_songs_survives_non_numeric_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getPath(dash, db, "/top-songs?page=abc")

        self.assertEqual(resp.status_code, 200)

    def test_top_artists_survives_non_numeric_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getPath(dash, db, "/top-artists?page=abc")

        self.assertEqual(resp.status_code, 200)


class TestTopArtistsSortAndPageClamp(_ListRouteTestBase):
    """/top-artists is paginated in SQL (getArtistsCount()/getTopArtists()
    LIMIT+OFFSET) the same way as top-songs/top-albums, not via getPage()."""

    def _makeArtistsDb(self, artistCount=0):
        db = self._makeDb(entryCount=0)
        db.getArtistsCount.return_value = artistCount
        return db

    def test_unknown_sortby_falls_back_to_default_instead_of_500(self):
        """Repository.getArtistAggregates raises ValueError for a sortBy
        outside ARTIST_SORT_COLUMNS - an unvalidated query param would
        otherwise turn into a 500."""
        dash = self._makeApp()
        db = self._makeArtistsDb()

        resp = self._getPath(dash, db, "/top-artists?sortBy=not_a_real_column")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getTopArtists.call_args.kwargs["by"], appModule.DEFAULT_SORT_BY)

    def test_page_beyond_range_is_clamped_to_last_page(self):
        dash = self._makeApp()
        db = self._makeArtistsDb(artistCount=120)

        resp = self._getPath(dash, db, "/top-artists?page=9999")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getTopArtists.call_args.kwargs
        self.assertEqual(kwargs["offset"], 2 * appModule.PAGE_SIZE)   #< last page (3) of 120/50
        self.assertIn(b"Page 3 of 3", resp.data)

    def test_search_query_is_passed_through_to_sql(self):
        dash = self._makeApp()
        db = self._makeArtistsDb(artistCount=1)

        resp = self._getPath(dash, db, "/top-artists?q=queen")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getArtistsCount.call_args.kwargs["searchQuery"], "queen")
        self.assertEqual(db.getTopArtists.call_args.kwargs["searchQuery"], "queen")


class TestPaginationExtras(_ListRouteTestBase):
    """Page-number links, 'Showing X-Y of Z', and the jump-to-page input,
    added alongside the existing Prev/Next + 'Page N of M' pagination."""

    def test_page_number_links_are_windowed_with_ellipsis(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=500)   #< 10 pages of PAGE_SIZE=50

        resp = self._getDashboard(dash, db, query="?page=5")

        body = resp.data.decode()
        for page in (1, 3, 4, 5, 6, 7, 10):
            self.assertIn(f">{page}<", body)
        self.assertNotIn(">2<", body)   #< skipped, covered by the ellipsis instead
        self.assertNotIn(">9<", body)
        self.assertIn("&hellip;", body)

    def test_current_page_link_is_marked_active(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=500)

        resp = self._getDashboard(dash, db, query="?page=5")

        self.assertIn(b'class="pagination-page active"', resp.data)

    def test_no_ellipsis_when_all_pages_fit_in_the_window(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)   #< 3 pages, well within the window

        resp = self._getDashboard(dash, db, query="?page=2")

        self.assertNotIn(b"&hellip;", resp.data)
        for page in (1, 2, 3):
            self.assertIn(f">{page}<".encode(), resp.data)

    def test_showing_x_of_y_on_first_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db)

        self.assertIn(b"Showing 1-50 of 120", resp.data)

    def test_showing_x_of_y_on_last_page(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db, query="?page=3")

        self.assertIn(b"Showing 101-120 of 120", resp.data)

    def test_showing_x_of_y_with_no_results(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=0)

        resp = self._getDashboard(dash, db)

        self.assertIn(b"Showing 0-0 of 0", resp.data)

    def test_jump_to_page_input_max_matches_total_pages(self):
        dash = self._makeApp()
        db = self._makeDb(entryCount=120)

        resp = self._getDashboard(dash, db)

        self.assertIn(b'max="3"', resp.data)


if __name__ == "__main__":
    unittest.main()
