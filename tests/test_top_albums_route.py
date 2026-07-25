import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: like test_dashboard_pagination.py, this file deliberately does NOT swap
# Database modules for MagicMocks in sys.modules - it only exercises the route
# with a per-test mock db (via get_user_db).
import app as appModule
from app import SpotifyDashboardApp
from _app_factory import AppTestCase


class TestTopAlbumsRoute(AppTestCase):
    """/top-albums must only ask the DB layer for the current page (SQL-level
    LIMIT/OFFSET) when there's no search query, mirroring /top-songs."""

    def _makeDb(self, albumCount=0):
        db = MagicMock()
        db.getTopAlbums.return_value = []
        db.getAlbumsCount.return_value = albumCount
        db.getPlayTotals.return_value = (0, 0)
        db.repo.getUserTags.return_value = []
        return db

    def _getTopAlbums(self, dash, db, query="", ajax=True):
        # Two-phase load: the list/pagination/totals only exist behind
        # ?ajax=true; the filter card (checkbox, nav) is in the shell (ajax=False).
        path = f"/top-albums{query}"
        if ajax:
            path += ('&' if query else '?') + 'ajax=true'
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(path)

    def test_without_search_fetches_only_one_page(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getAlbumsCount.assert_called_once()
        db.getTopAlbums.assert_called_once()
        kwargs = db.getTopAlbums.call_args.kwargs
        self.assertEqual(kwargs["limit"], appModule.PAGE_SIZE)
        self.assertEqual(kwargs["offset"], 0)
        self.assertEqual(kwargs["by"], "totalTimeListened")   #< topAlbumsPage's default sortBy

    def test_without_search_requests_correct_offset_for_page(self):
        dash = self._makeApp()
        db = self._makeDb(albumCount=120)

        resp = self._getTopAlbums(dash, db, query="?page=2")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getTopAlbums.call_args.kwargs
        self.assertEqual(kwargs["offset"], appModule.PAGE_SIZE)
        self.assertIn(b"Page 2 of 3", resp.data)

    def test_without_search_passes_requested_sort(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db, query="?sortBy=plays")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getTopAlbums.call_args.kwargs["by"], "plays")

    def test_without_search_handles_empty_database(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Page 1 of 1", resp.data)

    def test_with_search_paginates_and_matches_in_sql(self):
        """Search is matched and paginated in SQL (Repository.getAlbumsPage)
        the same way as the non-search path, not by fetching everything and
        filtering in Python."""
        dash = self._makeApp()
        db = self._makeDb(albumCount=5)

        resp = self._getTopAlbums(dash, db, query="?q=foo")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getAlbumsCount.call_count, 2)
        db.getAlbumsCount.assert_any_call(None, None, fullPlaysOnly=True)
        db.getAlbumsCount.assert_any_call(None, None, searchQuery="foo", albumIds=None, fullPlaysOnly=True,
                                           sortBy=appModule.DEFAULT_SORT_BY)
        kwargs = db.getTopAlbums.call_args.kwargs
        self.assertEqual(kwargs["limit"], appModule.PAGE_SIZE)
        self.assertEqual(kwargs["offset"], 0)
        self.assertEqual(kwargs["searchQuery"], "foo")

    def test_genre_badge_renders_on_list_cards_when_data_present(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getTopAlbums.return_value = [{
            "id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
            "imageUrl": "", "totalTracks": 2, "releaseDate": 0, "artists": [],
            "plays": 5, "totalTimeListened": 50000, "firstListenedAt": 100,
        }]
        db.getGenresForAlbums.return_value = {"alb1": ["indie rock"]}

        resp = self._getTopAlbums(dash, db)

        self.assertIn('<span class="track-label genre-label">indie rock</span>',
                      resp.get_json()["resultsHtml"])
        # One batched lookup for the whole list, not one call per card.
        db.getGenresForAlbums.assert_called_once_with(["alb1"])

    def test_totals_come_from_get_play_totals(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getPlayTotals.return_value = (42, 999000)

        resp = self._getTopAlbums(dash, db)

        self.assertEqual(resp.status_code, 200)
        db.getPlayTotals.assert_called_once()
        self.assertIn('<p class="summary-value">42</p>', resp.get_json()["resultsHtml"])

    def test_full_plays_only_defaults_on(self):
        """A favorite has to have actually been heard - the filter is on by
        default, with no ?fullOnly needed, and the checkbox renders checked."""
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db)                    #< ajax: the queries
        shell = self._getTopAlbums(dash, db, ajax=False)       #< shell: the checkbox

        self.assertEqual(resp.status_code, 200)
        db.getPlayTotals.assert_called_once_with(None, None, fullPlaysOnly=True)
        db.getAlbumsCount.assert_called_once_with(None, None, fullPlaysOnly=True)
        self.assertEqual(db.getTopAlbums.call_args.kwargs["fullPlaysOnly"], True)
        self.assertIn(b'id="fullPlaysOnly"', shell.data)
        self.assertIn(b'checked', shell.data)

    def test_full_plays_only_can_be_explicitly_disabled(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db, query="?fullOnly=0")

        self.assertEqual(resp.status_code, 200)
        db.getPlayTotals.assert_called_once_with(None, None, fullPlaysOnly=False)
        db.getAlbumsCount.assert_called_once_with(None, None, fullPlaysOnly=False)
        self.assertEqual(db.getTopAlbums.call_args.kwargs["fullPlaysOnly"], False)

    def test_page_survives_non_numeric_page(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db, query="?page=abc")

        self.assertEqual(resp.status_code, 200)

    def test_unknown_sortby_falls_back_to_default_instead_of_500(self):
        """sortBy is whitelisted (VALID_SORT_BY) before reaching the DB layer -
        Repository.getAlbumsPage raises ValueError for anything outside
        ALBUM_SORT_COLUMNS, which an unvalidated query param would otherwise
        turn into a 500."""
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db, query="?sortBy=not_a_real_column")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(db.getTopAlbums.call_args.kwargs["by"], appModule.DEFAULT_SORT_BY)

    def test_page_beyond_range_is_clamped_to_last_page(self):
        dash = self._makeApp()
        db = self._makeDb(albumCount=120)

        resp = self._getTopAlbums(dash, db, query="?page=9999")

        self.assertEqual(resp.status_code, 200)
        kwargs = db.getTopAlbums.call_args.kwargs
        self.assertEqual(kwargs["offset"], 2 * appModule.PAGE_SIZE)   #< last page (3) of 120/50
        self.assertIn(b"Page 3 of 3", resp.data)

    def test_nav_link_present(self):
        dash = self._makeApp()
        db = self._makeDb()

        resp = self._getTopAlbums(dash, db, ajax=False)   #< the nav lives in the shell/layout

        self.assertIn(b'/top-albums', resp.data)


if __name__ == "__main__":
    unittest.main()
