"""The Top Songs/Artists/Albums pages' own default time window
(users.default_top_list_window, set on /profile).

Why a second setting rather than reusing default_dashboard_window: these three
pages are a career ranking and have always opened on All Time, while the
Dashboard/Insights/Compare views answer "lately". Pointing both at one column
would have moved every Top page onto whatever someone had picked for their
dashboard, which is why the new column's default is All Time - on upgrade
nobody's Top pages change at all.

The transport detail this feature forced, and the thing most likely to be
"simplified" back: All Time is now the string "all time" on the wire, not "".
Every URL these pages build drops empty values (see
PaginationMixin._buildPageUrl and _topListShell's listArgs), so while All Time
was the hardcoded default that was harmless - it was also what an absent
interval meant. Once the default is per-user, an explicitly chosen All Time has
to survive into the pagination links and the first-load URL, or page 2 silently
snaps back to the stored window.
"""
import os
import re
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from tests._app_factory import AppTestCase
from config import PAGE_SIZE, TOP_LIST_DEFAULT_WINDOW

HX_HEADERS = {"HX-Request": "true"}

TOP_LIST_PATHS = ("/top-songs", "/top-artists", "/top-albums")

#< the two ends of the fixture history: one play from 1970, one from just now,
#  so any window shorter than All Time keeps exactly one of them. Track, artist
#  and album all carry the same marker word, so one assertion covers whichever
#  of the three pages is under test.
ANCIENT_PLAYED_AT = 1000.0
ANCIENT = "Ancient"
RECENT = "Recent"

TRACK_DURATION_MS = 200000


def makeTrack(trackId, marker):
    return {
        "id": trackId,
        "name": f"{marker} Song {trackId}",
        "url": f"http://example.com/track/{trackId}",
        "artists": [{"id": f"art{trackId}", "name": f"{marker} Artist {trackId}",
                     "url": f"http://example.com/artist/art{trackId}",
                     "imageUrl": "", "imageId": f"art{trackId}"}],
        "album": {
            "id": f"alb{trackId}", "name": f"{marker} Album {trackId}",
            "url": f"http://example.com/album/alb{trackId}",
            "imageId": f"alb{trackId}", "imageUrl": "http://img.example.com/a.jpg",
            "totalTracks": 10, "releaseDate": 12345.0,
        },
        "imageUrl": "http://img.example.com/a.jpg",
        "imageId": f"alb{trackId}",
        "duration": TRACK_DURATION_MS,
        "explicit": False,
        "isrc": "US1234567890",
        "discNumber": 1,
        "trackNumber": 3,
        "releaseDate": 12345.0,
    }


class TopListWindowTestCase(AppTestCase):
    def setUp(self):
        self.dash = self._makeApp()
        self.client = self.dash.app.test_client()
        self.username = "alice"
        self.email = "alice@example.com"

        self.listener_patcher = patch("Database.database.Database.startListener")
        self.listener_patcher.start()
        self.addCleanup(self.listener_patcher.stop)

        self.dash.repo.upsertUser(self.username, self.email)
        self.dash.repo.setUserCookies(self.username, {"sp_dc": "fake_cookie"})
        self.user_db = self.dash.get_user_db(self.username, self.email)

        self._addPlay("old", ANCIENT, ANCIENT_PLAYED_AT)
        self._addPlay("new", RECENT, time.time())
        self.dash.repo.commit()

        self.logged_in_patcher = patch.object(self.dash, "is_user_logged_in", return_value=True)
        self.logged_in_patcher.start()
        self.addCleanup(self.logged_in_patcher.stop)

        with self.client.session_transaction() as sess:
            sess["email"] = self.email
            sess["username"] = self.username

    def _addPlay(self, trackId, marker, playedAt):
        self.dash.repo.upsertTrack(makeTrack(trackId, marker))
        self.dash.repo.insertPlay(self.username, trackId, playedAt, TRACK_DURATION_MS)

    def _setWindow(self, window):
        self.dash.repo.updateUserSettings(self.username, "day", None,
                                          default_top_list_window=window)

    def _list(self, path, query=""):
        """The htmx half - the rows themselves."""
        return self.client.get(f"{path}{query}", headers=HX_HEADERS).get_data(as_text=True)

    def _shell(self, path, query=""):
        return self.client.get(f"{path}{query}").get_data(as_text=True)


class TestTheStoredWindowScopesTheList(TopListWindowTestCase):
    def test_the_default_is_all_time(self):
        """Nothing changes for an account that never touches the setting - the
        whole point of defaulting the new column to All Time."""
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._list(path)

                self.assertIn(ANCIENT, body)
                self.assertIn(RECENT, body)

    def test_a_stored_window_narrows_every_top_page(self):
        self._setWindow("year")

        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                body = self._list(path)

                self.assertNotIn(ANCIENT, body)
                self.assertIn(RECENT, body)

    def test_an_explicit_interval_beats_the_stored_window(self):
        self._setWindow("year")

        self.assertIn(ANCIENT, self._list("/top-songs", "?interval=all+time"))

    def test_junk_falls_back_to_the_stored_window_not_to_all_time(self):
        """_getValidInterval's `default` is now the user's setting. Coercing junk
        to All Time instead would hand a hand-edited URL a wider view than the
        account asked for."""
        self._setWindow("year")

        self.assertNotIn(ANCIENT, self._list("/top-songs", "?interval=bogus"))

    def test_the_dashboard_window_does_not_scope_the_top_pages(self):
        """The two settings are independent - this is the regression that would
        follow from wiring the Top pages back onto default_dashboard_window."""
        self.dash.repo.updateUserSettings(self.username, "year", None)

        self.assertIn(ANCIENT, self._list("/top-songs"))


class TestTheWindowSurvivesIntoEveryUrl(TopListWindowTestCase):
    """All Time has to be expressible in a query string, because these URLs drop
    empty values - see the module docstring."""

    def test_all_time_reaches_the_first_load_url(self):
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                self.assertIn("interval=all+time", self._shell(path))

    def test_a_stored_window_reaches_the_first_load_url(self):
        self._setWindow("month")

        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                self.assertIn("interval=month", self._shell(path))

    def test_the_filter_card_shows_the_stored_window_as_selected(self):
        self._setWindow("week")

        self.assertIn('<option value="week" selected>Last Week</option>', self._shell("/top-songs"))

    def test_the_all_time_option_carries_a_non_empty_value(self):
        body = self._shell("/top-songs")

        self.assertIn(f'<option value="{TOP_LIST_DEFAULT_WINDOW}"', body)
        self.assertNotIn('<option value="">All Time</option>', body)

    def test_pagination_links_keep_an_all_time_window(self):
        """Page 2 of an All Time ranking must still be All Time. With the value
        empty it was dropped from every link, so page 2 re-resolved the default -
        harmless while that default was hardcoded, wrong once it is per-user."""
        for index in range(PAGE_SIZE):   #< one page's worth on top of setUp's two
            self._addPlay(f"filler{index}", RECENT, time.time() - index)
        self.dash.repo.commit()

        body = self._list("/top-songs")

        self.assertIn("Page 1 of 2", body)
        self.assertIn("interval=all+time", body)


class TestCustomWithoutDatesFallsBackToAllTime(TopListWindowTestCase):
    """/top-songs?interval=custom with no dates used to render a "Custom"
    selected option over data _getDateRange had already fallen back to All
    Time for (its custom branch only applies when BOTH dates parse) - the
    guard CORE-8 gives _topListFilters, mirroring the one dashboardIndex/
    historyPage/chartsPage/genresPage already carry."""

    def test_the_filter_card_shows_all_time_not_custom(self):
        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                self.assertIn('<option value="all time" selected>All Time</option>',
                             self._shell(path, "?interval=custom"))

    def test_the_list_shows_all_time_data(self):
        self.assertIn(ANCIENT, self._list("/top-songs", "?interval=custom"))

    def test_the_fallback_is_all_time_not_the_stored_window(self):
        """The two-default correction: custom-without-dates falls back to All
        Time specifically (_resolveIntervalParam's `emptyDefault`), not to
        the account's stored default_top_list_window (`absentDefault`) - a
        single-default resolver would have narrowed this to 'year' and
        dropped the Ancient play, disagreeing with what _getDateRange has
        always done for the data."""
        self._setWindow("year")

        self.assertIn(ANCIENT, self._list("/top-songs", "?interval=custom"))


class TestLegacyUrls(TopListWindowTestCase):
    def test_an_empty_interval_still_means_all_time(self):
        """?interval= is what every bookmark and shared link made before this
        change carries. It has to keep meaning All Time rather than picking up
        whatever the account has since configured."""
        self._setWindow("year")

        self.assertIn(ANCIENT, self._list("/top-songs", "?interval="))

    def test_an_empty_interval_is_normalised_before_it_reaches_a_url(self):
        """The case above is only half of it. `` is valid to _getValidInterval,
        so it used to reach the shell as-is: the card selected All Time (its
        option matches both spellings), while listUrl - which drops empty values
        - carried no interval at all, so the list request that followed
        re-resolved the stored window. The card said All Time over a
        year-scoped list.

        Normalising `` to the All Time spelling at the point it is read fixes
        both halves at once, which is why this asserts on the URL rather than on
        the selected option."""
        self._setWindow("year")

        for path in TOP_LIST_PATHS:
            with self.subTest(path=path):
                self.assertIn("interval=all+time", self._shell(path, "?interval="))

    def test_the_list_the_shell_asks_for_is_the_one_the_card_describes(self):
        """End to end, because the two halves disagreeing is the actual defect:
        follow the URL the placeholder loads and check the rows against the
        option the card marked selected."""
        self._setWindow("year")

        shell = self._shell("/top-songs", "?interval=")
        self.assertIn('<option value="all time" selected>All Time</option>', shell)

        #< the placeholder's, not the filter form's - that one is a bare
        #  request.path and carries no filters at all (see _page_card.html)
        listUrl = re.search(r'class="track-list"\s+hx-get="([^"]*)"',
                            shell).group(1).replace("&amp;", "&")
        self.assertIn(ANCIENT, self.client.get(listUrl, headers=HX_HEADERS).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
