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
        if coverage is not None:
            db.getGenreCoverage.return_value = coverage
        if recommendations is not None:
            db.getRecommendedArtists.return_value = recommendations
        return db

    def _get(self, dash, db):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get("/")

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

    def test_discover_locked_message_when_coverage_low(self):
        dash = self._makeApp()
        resp = self._get(dash, self._makeDb(coverage=coverageDict(10, 10, 10)))
        body = resp.data.decode()
        self.assertIn("Discover", body)
        self.assertIn("Unlock artist recommendations", body)

    def test_discover_shows_recommendations_when_unlocked(self):
        dash = self._makeApp()
        db = self._makeDb(coverage=coverageDict(80, 60, 90),
                          recommendations=[{"id": "art1", "name": "Fresh Artist",
                                            "imageId": "img1", "playCount": 2,
                                            "sharedGenreCount": 2, "matchedGenres": ["rock", "indie"]}])
        resp = self._get(dash, db)
        body = resp.data.decode()
        self.assertIn("Fresh Artist", body)
        self.assertIn("/artist/art1", body)
        self.assertIn("rock", body)
        db.getRecommendedArtists.assert_called_once()

    def test_discover_card_hidden_when_lastfm_disabled(self):
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        db = self._makeDb(coverage=coverageDict(80, 60, 90))
        resp = self._get(dash, db)
        self.assertNotIn(b'class="summary-card discover-card"', resp.data)
        db.getGenreCoverage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
