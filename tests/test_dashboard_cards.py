"""The dashboard's unfiltered live cards (Now Playing, Listening streak, On this
day, Discover) and their placement above the interval/date-range filter form."""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp  # noqa: F401
from _app_factory import AppTestCase


def coverageDict(song, album, artist, total=1000):
    def category(percent):
        return {"covered": int(total * percent / 100), "total": total, "percent": percent}
    return {
        "song": category(song),
        "album": category(album),
        "artist": category(artist),
        "overall": {"percent": round((song + album + artist) / 3, 1)},
    }


class DashboardCardsTestCase(AppTestCase):
    def _makeDb(self, streak=None, onThisDay=None, coverage=None, recommendations=None):
        db = MagicMock()
        db.getEntriesFromNew.return_value = []
        db.getEntriesCount.return_value = 0
        db.searchEntries.return_value = []
        db.searchEntriesCount.return_value = 0
        db.getOverallStats.return_value = {
            "currentTopSongs": [], "currentTopArtists": [],
            "totalSongsPlayed": 0, "totalDurationMs": 0,
            "previousSongsPlayed": 0, "previousDurationMs": 0,
        }
        db.getCurrentStreak.return_value = streak or {"days": 0, "activeToday": False}
        db.getOnThisDay.return_value = onThisDay or []
        db.getPlayTotals.return_value = (0, 0)   #< lifetime totals feed the Next-milestones bars
        # Empty grid by default: the calendar card only renders when weeks is
        # non-empty, so most card tests aren't perturbed by it. The dedicated
        # test below overrides this with a real built grid.
        db.getListeningCalendar.return_value = {
            "weeks": [], "monthLabels": [], "maxCount": 0, "activeDays": 0, "totalPlays": 0}
        if coverage is not None:
            db.getGenreCoverage.return_value = coverage
        if recommendations is not None:
            db.getRecommendedArtists.return_value = recommendations
        return db

    def _makeDbWithTops(self):
        """A db whose overall stats include a Top song and Top artist, both with
        deliberately long names so the full-width-title layout is exercised."""
        db = self._makeDb()
        db.getOverallStats.return_value = {
            "currentTopSongs": [{
                "id": "trk1", "name": "An Extremely Long Song Title That Overflows",
                "imageId": "img1", "totalTimeListened": 3600000,
                "firstListenedAt": 0, "plays": 5,
            }],
            "currentTopArtists": [{
                "id": "art1", "name": "An Extremely Long Artist Name That Overflows",
                "imageId": "img2", "totalTimeListened": 7200000,
                "firstListenedAt": 0, "plays": 3,
            }],
            "totalSongsPlayed": 100, "totalDurationMs": 10000000,
            "previousSongsPlayed": 0, "previousDurationMs": 0,
        }
        return db

    def _get(self, dash, db, path="/"):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(path)

    def test_now_playing_card_precedes_filter_form(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb())
        body = resp.data.decode()
        self.assertEqual(resp.status_code, 200)
        npIndex = body.find('id="nowPlayingCard"')
        formIndex = body.find('class="filter-section dashboard-filter"')
        self.assertNotEqual(npIndex, -1)
        self.assertNotEqual(formIndex, -1)
        self.assertLess(npIndex, formIndex)

    def test_live_cards_in_separate_panel_above_the_hero(self):
        dash = self._makeApp()
        body = self._get(dash, self._makeDb()).data.decode()
        liveIndex = body.find('class="dashboard-live"')
        heroIndex = body.find('<section class="hero">')
        formIndex = body.find('class="filter-section dashboard-filter"')
        self.assertNotEqual(liveIndex, -1)
        self.assertNotEqual(heroIndex, -1)
        # The live panel is its own section, before the hero (form + results).
        self.assertLess(liveIndex, heroIndex)
        self.assertLess(liveIndex, formIndex)

    def test_streak_lives_inside_the_now_playing_panel(self):
        dash = self._makeApp()
        body = self._get(dash, self._makeDb(streak={"days": 4, "activeToday": True})).data.decode()
        panelIndex = body.find('id="nowPlayingPanel"')
        streakIndex = body.find('streak-block')
        nextCardIndex = body.find('onthisday-card')
        self.assertNotEqual(panelIndex, -1)
        self.assertNotEqual(streakIndex, -1)
        # Streak markup sits after the panel opens and before the next card.
        self.assertLess(panelIndex, streakIndex)
        self.assertLess(streakIndex, nextCardIndex)

    def test_total_listen_time_hides_seconds_above_10h(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getOverallStats.return_value = {
            "currentTopSongs": [], "currentTopArtists": [],
            "totalSongsPlayed": 100,
            "totalDurationMs": (12 * 3600 + 3 * 60 + 41) * 1000,
            "previousSongsPlayed": 0, "previousDurationMs": 0,
        }
        body = self._get(dash, db).data.decode()
        self.assertIn("12h 3m", body)
        self.assertNotIn("12h 3m 41s", body)

    def test_total_listen_time_keeps_seconds_below_10h(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getOverallStats.return_value = {
            "currentTopSongs": [], "currentTopArtists": [],
            "totalSongsPlayed": 100,
            "totalDurationMs": (9 * 3600 + 5 * 60 + 7) * 1000,
            "previousSongsPlayed": 0, "previousDurationMs": 0,
        }
        body = self._get(dash, db).data.decode()
        self.assertIn("9h 5m 7s", body)

    def test_on_this_day_name_is_wrapped_for_truncation(self):
        dash = self._makeApp()
        onThisDay = [{"year": 2024, "yearsAgo": 2, "trackId": "trk1",
                      "trackName": "An Extremely Long Song Title That Would Overflow",
                      "artistName": "Some Artist", "playCount": 7}]
        body = self._get(dash, self._makeDb(onThisDay=onThisDay)).data.decode()
        self.assertIn('class="onthisday-name"', body)
        self.assertIn("An Extremely Long Song Title That Would Overflow", body)

    def test_active_streak_shows_day_count(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb(streak={"days": 5, "activeToday": True}))
        body = resp.data.decode()
        self.assertIn("🔥 5", body)
        self.assertIn("days in a row", body)
        self.assertNotIn("play today to keep it", body)

    def test_streak_alive_but_inactive_prompts_to_play(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb(streak={"days": 3, "activeToday": False}))
        self.assertIn(b"play today to keep it", resp.data)

    def test_zero_streak_prompts_to_start(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb(streak={"days": 0, "activeToday": False}))
        self.assertIn(b"start one", resp.data)

    def test_on_this_day_rows_link_to_song(self):
        dash = self._makeApp()
        onThisDay = [{"year": 2024, "yearsAgo": 2, "trackId": "trk1",
                      "trackName": "Old Favourite", "artistName": "Some Artist", "playCount": 7}]
        resp = self._get(dash, self._makeDb(onThisDay=onThisDay))
        body = resp.data.decode()
        self.assertIn("2y ago", body)
        self.assertIn("Old Favourite", body)
        self.assertIn("/song/trk1", body)

    def test_on_this_day_empty_state(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb(onThisDay=[]))
        self.assertIn(b"No past plays on today's date yet.", resp.data)

    def test_streak_calendar_renders_cells_from_calendar_data(self):
        import datetime
        from services.listening_calendar import buildListeningCalendar
        dash = self._makeApp()
        db = self._makeDb()
        # One busy day (busiest -> top level) so we can assert its cell.
        db.getListeningCalendar.return_value = buildListeningCalendar(
            {"2026-07-20": 7}, datetime.date(2026, 7, 23), weeks=6)

        resp = self._get(dash, db)

        self.assertIn(b"streak-calendar-card", resp.data)
        # The play count/date live on data-* attrs now (the JS overlay reads them);
        # the native title hint was replaced by a cursor-following tooltip.
        self.assertIn(b'data-count="7" data-date="2026-07-20"', resp.data)
        self.assertNotIn(b'title="7 plays on 2026-07-20"', resp.data)
        self.assertIn(b'data-level="4"', resp.data)   # busiest day is the top heat level

    def test_streak_calendar_absent_when_no_grid(self):
        # Empty weeks (the _makeDb default) => the card isn't rendered at all.
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb())
        self.assertNotIn(b"streak-calendar-card", resp.data)

    def test_dashboard_has_no_search_or_track_list(self):
        # The searchable play history moved to /history; the dashboard keeps
        # only the Time Period filter + stats + live cards.
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb())
        self.assertNotIn(b'id="dashboardSearch"', resp.data)
        self.assertNotIn(b'class="track-list"', resp.data)
        # The Time Period filter and stats cards do stay.
        self.assertIn(b'id="interval"', resp.data)
        self.assertIn(b"Total songs played", resp.data)

    def test_next_milestones_render_with_progress(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getPlayTotals.return_value = (820, 0)   #< 820 lifetime plays -> next is 1,000
        resp = self._get(dash, db)
        self.assertIn(b"next-milestone-card", resp.data)
        self.assertIn(b"820 / 1,000 lifetime plays", resp.data)
        db.getPlayTotals.assert_called_once()

    def test_nav_groups_analytics_and_account(self):
        # The analytics pages live under an Insights dropdown; account/management
        # pages plus Log out live under the right-aligned Account dropdown.
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb())
        body = resp.data
        self.assertIn(b"Insights", body)
        self.assertIn(b">Charts</a>", body)
        self.assertIn(b">Wrapped</a>", body)
        self.assertIn(b"nav-account-dropdown", body)
        self.assertIn(b">Profile</a>", body)
        self.assertIn(b">Import</a>", body)
        self.assertIn(b">Log out</a>", body)   #< previously only reachable from the Profile page

    def test_discover_card_placeholder_rendered_when_lastfm_enabled(self):
        # The dashboard render only emits the (empty) Discover card shell; its
        # contents are fetched by JS from /api/dashboard-discover after load,
        # so the initial page must not run the coverage/recommendation queries.
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90))
        resp = self._get(dash, db)
        self.assertIn(b'id="discoverCard"', resp.data)
        self.assertIn(b'id="discoverLoading"', resp.data)
        db.getGenreCoverage.assert_not_called()
        db.getRecommendedArtists.assert_not_called()

    def test_discover_card_hidden_when_lastfm_disabled(self):
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        db = self._makeDb(coverage=coverageDict(80, 60, 90))
        resp = self._get(dash, db)
        self.assertNotIn(b'class="summary-card discover-card"', resp.data)
        db.getGenreCoverage.assert_not_called()

    def test_discover_api_locked_when_coverage_low(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(10, 10, 10))
        resp = self._get(dash, db, path="/api/dashboard-discover")
        data = resp.get_json()
        self.assertFalse(data["unlocked"])
        self.assertEqual(data["recommendations"], [])
        db.getRecommendedArtists.assert_not_called()

    def test_discover_api_returns_recommendations_when_unlocked(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          recommendations=[{"id": "art1", "name": "Fresh Artist",
                                            "imageId": "img1", "playCount": 2,
                                            "sharedGenreCount": 2, "matchedGenres": ["rock", "indie"]}])
        resp = self._get(dash, db, path="/api/dashboard-discover")
        data = resp.get_json()
        self.assertTrue(data["unlocked"])
        self.assertEqual(data["recommendations"][0]["name"], "Fresh Artist")
        db.getRecommendedArtists.assert_called_once()

    def test_top_song_title_spans_full_width_above_detail_row(self):
        # The title is its own full-width line; the cover art and the
        # "…listened" text share the row beneath it (summary-top-detail).
        dash = self._makeApp()
        body = self._get(dash, self._makeDbWithTops()).data.decode()
        cardStart = body.find("<h2>Top song</h2>")
        self.assertNotEqual(cardStart, -1)
        titleIndex = body.find('class="summary-top-title"', cardStart)
        detailIndex = body.find('class="summary-top-detail"', cardStart)
        coverIndex = body.find('class="summary-top-cover"', cardStart)
        self.assertNotEqual(titleIndex, -1)
        self.assertNotEqual(detailIndex, -1)
        self.assertNotEqual(coverIndex, -1)
        # Title first, then the cover lives inside the detail row below it.
        self.assertLess(titleIndex, detailIndex)
        self.assertLess(detailIndex, coverIndex)
        self.assertIn("An Extremely Long Song Title That Overflows", body)
        self.assertIn("/song/trk1", body)

    def test_top_artist_title_spans_full_width_above_detail_row(self):
        dash = self._makeApp()
        body = self._get(dash, self._makeDbWithTops()).data.decode()
        cardStart = body.find("<h2>Top artist</h2>")
        self.assertNotEqual(cardStart, -1)
        titleIndex = body.find('class="summary-top-title"', cardStart)
        detailIndex = body.find('class="summary-top-detail"', cardStart)
        coverIndex = body.find('class="summary-top-cover"', cardStart)
        self.assertNotEqual(titleIndex, -1)
        self.assertNotEqual(detailIndex, -1)
        self.assertNotEqual(coverIndex, -1)
        self.assertLess(titleIndex, detailIndex)
        self.assertLess(detailIndex, coverIndex)
        self.assertIn("An Extremely Long Artist Name That Overflows", body)
        self.assertIn("/artist/art1", body)

    def test_top_cards_show_empty_state_without_data(self):
        # The else-branch still renders when there is no top song/artist.
        dash = self._makeApp()
        body = self._get(dash, self._makeDb()).data.decode()
        self.assertIn("No songs played in this period.", body)
        self.assertIn("No artists played in this period.", body)
        self.assertNotIn('class="summary-top-title"', body)

    def test_discover_api_disabled_when_lastfm_off(self):
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        db = self._makeDb(coverage=coverageDict(80, 60, 90))
        resp = self._get(dash, db, path="/api/dashboard-discover")
        data = resp.get_json()
        self.assertFalse(data["unlocked"])
        db.getGenreCoverage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
