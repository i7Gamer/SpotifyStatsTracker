"""The dashboard's unfiltered live cards (Now Playing, Listening streak, On this
day, Discover) and their placement above the interval/date-range filter form."""
import datetime
import json
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp  # noqa: F401
from _app_factory import AppTestCase
from conftest import makeDashboardDbMock


def coverageDict(song, album, artist, total=1000):
    def category(percent):
        return {"covered": int(total * percent / 100), "total": total, "percent": percent}
    return {
        "song": category(song),
        "album": category(album),
        "artist": category(artist),
        "overall": {"percent": round((song + album + artist) / 3, 1)},
    }


class _DashboardHelpers:
    """Shared fixtures for exercising the dashboard route with a mocked
    per-user db - split out so DashboardAjaxFilterTestCase doesn't have to
    inherit from (and re-run) DashboardCardsTestCase's own tests."""

    def _makeDb(self, streak=None, onThisDay=None, coverage=None, recommendations=None):
        #< the dashboard route's baseline (the six queries whose shapes matter)
        #  comes from conftest; this only layers on what these tests vary
        db = makeDashboardDbMock()
        db.getEntriesFromNew.return_value = []
        db.getEntriesCount.return_value = 0
        db.searchEntries.return_value = []
        db.searchEntriesCount.return_value = 0
        if streak is not None:
            db.getCurrentStreak.return_value = streak
        if onThisDay is not None:
            db.getOnThisDay.return_value = onThisDay
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


class DashboardCardsTestCase(_DashboardHelpers, AppTestCase):

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
        #< gated with the earned-milestones card beside it, see
        #  DashboardMilestonesCardTestCase.test_kill_switch_hides_both_cards

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
        self.assertIn(b">Import &amp; Export</a>", body)
        # Log out is a POST form (state-changing), not a plain link.
        self.assertIn(b"dropdown-logout-form", body)
        self.assertIn(b">Log out</button>", body)

    def test_nav_dropdown_triggers_are_keyboard_focusable(self):
        # The dropdown triggers must be focusable (tabindex) so keyboard users
        # can reach the submenu links, which open via CSS :focus-within.
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb())
        body = resp.data
        self.assertIn(b'class="dropdown-trigger" tabindex="0"', body)
        self.assertIn(b'aria-haspopup="true"', body)

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


class DashboardIntervalResolutionTestCase(_DashboardHelpers, AppTestCase):
    """The dashboard has to resolve ?interval= the way /charts and /genres do.

    It was reading the param straight out of the query string with no
    _getValidInterval, then handing default="day" to both _getDateRange and
    _getIntervalLabel - so an unrecognised value fell back to the literal "day"
    rather than the window the user configured, and the heading confidently
    named it."""

    def _settings(self, window):
        db = self._makeDb()
        db.repo.getUserSettings.return_value = {"default_dashboard_window": window, "timezone": None}
        return db

    def test_the_saved_window_is_used_when_no_interval_is_given(self):
        resp = self._get(self._makeApp(), self._settings("month"))

        self.assertIn(b'<option value="month" selected>', resp.data)

    def test_an_unrecognised_interval_falls_back_to_the_saved_window(self):
        """A stale or hand-edited URL, or one truncated by a chat client."""
        resp = self._get(self._makeApp(), self._settings("month"), path="/?interval=bogus")

        self.assertIn(b'<option value="month" selected>', resp.data)
        self.assertNotIn(b'<option value="day" selected>', resp.data)

    def test_an_empty_interval_falls_back_to_the_saved_window(self):
        resp = self._get(self._makeApp(), self._settings("week"), path="/?interval=")

        self.assertIn(b'<option value="week" selected>', resp.data)

    def test_a_valid_interval_still_wins_over_the_saved_window(self):
        resp = self._get(self._makeApp(), self._settings("month"), path="/?interval=year")

        self.assertIn(b'<option value="year" selected>', resp.data)

    def test_custom_without_both_dates_falls_back_to_the_saved_window(self):
        resp = self._get(self._makeApp(), self._settings("month"), path="/?interval=custom")

        self.assertIn(b'<option value="month" selected>', resp.data)


class DashboardAjaxFilterTestCase(_DashboardHelpers, AppTestCase):
    """The Time Period filter (interval/date range) now updates via ?ajax=true
    instead of a full page reload - static/js in tracks.html swaps the
    rendered partial into #dashboardSummary, same fade-and-swap pattern as
    compare.html/genres.html. Only the four Time-Period-scoped cards
    (_dashboard_summary.html) are part of that payload - the live cards
    (streak, on this day, discover, calendar) and next-milestones are
    unfiltered and must not be recomputed on an ajax filter change."""

    def test_wrapper_div_present_for_the_ajax_swap_target(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb())

        self.assertIn(b'id="dashboardSummary"', resp.data)

    def test_ajax_returns_a_json_partial_not_a_full_page(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getOverallStats.return_value = {
            "currentTopSongs": [], "currentTopArtists": [],
            "totalSongsPlayed": 42, "totalDurationMs": 0,
            "previousSongsPlayed": 0, "previousDurationMs": 0,
        }

        resp = self._get(dash, db, path="/?ajax=true")

        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertIn("summaryHtml", data)
        self.assertIn('<p class="summary-value">42</p>', data["summaryHtml"])
        self.assertNotIn("<html", data["summaryHtml"].lower())
        self.assertNotIn("dashboard-live", data["summaryHtml"])   #< the unfiltered panel isn't part of this chunk

    def test_ajax_scopes_stats_by_the_requested_interval(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._get(dash, db, path="/?ajax=true&interval=week")

        self.assertEqual(db.getOverallStats.call_count, 1)   #< scoped by the request's own date range

    def test_ajax_skips_the_unfiltered_live_queries(self):
        """The interval/date-range filter never affects streak, on-this-day,
        the listening calendar, or lifetime totals - an ajax filter change
        must not pay for any of that."""
        dash = self._makeApp()
        db = self._makeDb()

        self._get(dash, db, path="/?ajax=true")

        db.getCurrentStreak.assert_not_called()
        db.getOnThisDay.assert_not_called()
        db.getListeningCalendar.assert_not_called()
        db.getPlayTotals.assert_not_called()

    def test_non_ajax_request_still_renders_everything(self):
        dash = self._makeApp()
        db = self._makeDb(streak={"days": 2, "activeToday": True})

        resp = self._get(dash, db)

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"streak-block", resp.data)
        db.getCurrentStreak.assert_called_once()


class DashboardMilestonesCardTestCase(_DashboardHelpers, AppTestCase):
    """Achievements earned, shown next to the progress bars toward the next
    ones. This list used to live on /profile, which is a settings page - and
    it meant the topbar badge sent you somewhere with no other reason to go.

    Both cards are gated on the same admin kill switch: an instance with
    milestones switched off used to keep showing progress toward them."""

    def _record(self, dash, username="alice", kind="plays", threshold=1000,
                detail=None, achievedAt=1609459200.0, seen=True):
        dash.repo.upsertUser(username, f"{username}@example.com")
        dash.repo.recordMilestone(username, kind, threshold, detail, achievedAt, seen=seen)

    def _earnedList(self, body):
        """Just the earned-milestones <ul>.

        The two cards share vocabulary - "1,000 lifetime plays" is also how the
        Next-milestones bar beside it labels its target - so an unscoped
        assertIn passes whether or not the earned card rendered at all."""
        start = body.find(b'class="milestone-list"')
        self.assertNotEqual(start, -1, "earned-milestones list not rendered")
        return body[start:body.index(b"</ul>", start)]

    def test_card_renders_in_the_same_row_as_next_milestones(self):
        dash = self._makeApp()
        self._record(dash)
        db = self._makeDb()
        db.getPlayTotals.return_value = (820, 0)

        body = self._get(dash, db).data

        self.assertIn(b"milestone-row", body)
        self.assertIn(b"milestone-card", body)
        self.assertIn(b"next-milestone-card", body)
        #< earned on the left, progress toward the next on the right
        self.assertLess(body.index(b'class="summary-card milestone-card"'),
                        body.index(b'class="summary-card next-milestone-card"'))

    def test_lists_the_achievement(self):
        dash = self._makeApp()
        self._record(dash)

        earned = self._earnedList(self._get(dash, self._makeDb()).data)

        self.assertIn(b"1,000 lifetime plays", earned)

    def test_orders_newest_first(self):
        dash = self._makeApp()
        self._record(dash, kind="plays", threshold=1000, achievedAt=1609459200.0)
        self._record(dash, kind="streak", threshold=7, achievedAt=1612137600.0)

        earned = self._earnedList(self._get(dash, self._makeDb()).data)

        self.assertLess(earned.index(b"7-day listening streak"),
                        earned.index(b"1,000 lifetime plays"))

    def test_shows_the_date_in_the_users_timezone(self):
        """Pinned to a zone whose local date DIFFERS from both UTC and the
        server's own - otherwise the assertion passes even if the route stops
        passing tz at all, since every fallback lands on the same day."""
        dash = self._makeApp()
        self._record(dash, achievedAt=1609459200.0)   #< 2021-01-01 00:00 UTC
        db = self._makeDb()
        db.tz = datetime.timezone(datetime.timedelta(hours=-8))   #< 2020-12-31 local

        earned = self._earnedList(self._get(dash, db).data)

        self.assertIn(b"2020-12-31", earned)
        self.assertNotIn(b"2021-01-01", earned)

    def test_top_artist_milestone_links_to_the_artist_page(self):
        dash = self._makeApp()
        self._record(dash, kind="top_artist", threshold=0,
                     detail=json.dumps({"id": "art9", "name": "Boards of Canada"}))

        earned = self._earnedList(self._get(dash, self._makeDb()).data)

        self.assertIn(b"New #1 artist: Boards of Canada", earned)
        self.assertIn(b"/artist/art9", earned)

    def test_visiting_the_dashboard_clears_the_badge(self):
        dash = self._makeApp()
        self._record(dash, seen=False)
        self.assertEqual(dash.repo.getUnseenMilestoneCount("alice"), 1)

        self._get(dash, self._makeDb())

        self.assertEqual(dash.repo.getUnseenMilestoneCount("alice"), 0)

    def test_badge_still_renders_on_the_render_that_clears_it(self):
        """`/` is where login drops you, and it both shows the milestones and
        acknowledges them. Context processors run after the view, so without
        priming (see primeMilestoneBadge) the badge would count what was just
        cleared - zero - and a user landing here would never see the
        notification at all."""
        dash = self._makeApp()
        self._record(dash, seen=False)

        first = self._get(dash, self._makeDb()).data

        self.assertIn(b'class="milestone-badge"', first)
        self.assertIn(b"1 new milestone", first)

    def test_badge_is_gone_on_the_next_load(self):
        dash = self._makeApp()
        self._record(dash, seen=False)

        self._get(dash, self._makeDb())
        second = self._get(dash, self._makeDb()).data

        self.assertNotIn(b"milestone-badge", second)

    def test_empty_state_when_nothing_earned_yet(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")

        body = self._get(dash, self._makeDb()).data

        self.assertIn(b"No milestones yet", body)

    def test_extras_collapse_behind_the_show_more_button(self):
        """Three stay visible so the card roughly matches the height of the
        three progress bars beside it; milestone-more.js reveals the rest."""
        dash = self._makeApp()
        for i in range(5):
            self._record(dash, kind="plays", threshold=1000 * (i + 1),
                         achievedAt=1609459200.0 + i)

        body = self._get(dash, self._makeDb()).data

        #< every milestone is in the DOM; the surplus ones just carry `hidden`,
        #  so the class substring matches those too - count them separately
        total = body.count(b'class="milestone-item"')
        hidden = body.count(b'class="milestone-item" hidden')
        self.assertEqual(total, 5)
        self.assertEqual(hidden, 2)
        self.assertEqual(total - hidden, 3)   #< visible
        self.assertIn(b"data-milestone-more", body)
        self.assertIn(b'data-chunk-size="5"', body)
        self.assertIn(b"Show 2 more", body)
        #< the button is inert without its script, and the suite would not
        #  otherwise notice the tag going missing
        self.assertIn(b"js/milestone-more.js", body)

    def test_no_show_more_button_at_exactly_the_visible_limit(self):
        """The boundary itself: 3 earned, 3 visible, nothing left to reveal."""
        dash = self._makeApp()
        for i in range(3):
            self._record(dash, threshold=1000 * (i + 1), achievedAt=1609459200.0 + i)

        body = self._get(dash, self._makeDb()).data

        self.assertEqual(body.count(b'class="milestone-item"'), 3)
        self.assertNotIn(b"data-milestone-more", body)
        self.assertNotIn(b'class="milestone-item" hidden', body)

    def test_show_more_button_appears_one_past_the_limit(self):
        dash = self._makeApp()
        for i in range(4):
            self._record(dash, threshold=1000 * (i + 1), achievedAt=1609459200.0 + i)

        body = self._get(dash, self._makeDb()).data

        self.assertIn(b"data-milestone-more", body)
        self.assertIn(b"Show 1 more", body)   #< not the full chunk size
        self.assertEqual(body.count(b'class="milestone-item" hidden'), 1)

    def test_kill_switch_hides_both_cards(self):
        dash = self._makeApp()
        self._record(dash)
        dash.repo.setMilestonesEnabled(False)
        db = self._makeDb()
        db.getPlayTotals.return_value = (820, 0)

        body = self._get(dash, db).data

        self.assertNotIn(b"milestone-row", body)
        self.assertNotIn(b"1,000 lifetime plays", body)
        #< the progress bars go too: an instance with the feature off should
        #  not advertise progress toward milestones it will never record
        self.assertNotIn(b"next-milestone-card", body)
        self.assertNotIn(b"820 / 1,000 lifetime plays", body)

    def test_kill_switch_skips_the_lifetime_totals_query(self):
        """Nothing renders from it, so nothing should pay for it."""
        dash = self._makeApp()
        dash.repo.setMilestonesEnabled(False)
        db = self._makeDb()

        self._get(dash, db)

        db.getPlayTotals.assert_not_called()

    def test_kill_switch_does_not_clear_the_badge(self):
        """Same contract as the topbar badge: the switch hides the rows, it
        does not silently acknowledge them."""
        dash = self._makeApp()
        self._record(dash, seen=False)
        dash.repo.setMilestonesEnabled(False)

        self._get(dash, self._makeDb())

        self.assertEqual(dash.repo.getMilestonesForUser("alice")[0]["seen"], 0)

    def test_ajax_filter_change_does_not_rebuild_the_cards(self):
        """The milestone cards are unfiltered, like the streak and calendar."""
        dash = self._makeApp()
        self._record(dash, seen=False)
        db = self._makeDb()

        self._get(dash, db, path="/?ajax=true")

        self.assertEqual(dash.repo.getUnseenMilestoneCount("alice"), 1)


if __name__ == "__main__":
    unittest.main()
