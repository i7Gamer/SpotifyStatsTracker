"""window.PLACEHOLDER_IMG (the inline-SVG fallback that <img onerror>
handlers across the templates swap in for missing covers) must be defined
in <head>, before any <img> is parsed.

It used to be defined in the script block at the bottom of layout.html:
an image whose error event fires while the document is still streaming in
(most reliably the dashboard's now-playing cover, whose src="" fails
instantly at parse time) ran its onerror before that script executed, so
`this.src = window.PLACEHOLDER_IMG` assigned the *string* "undefined" and
the browser requested GET /undefined -> 404.
"""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from _app_factory import AppTestCase


class TestPlaceholderImgDefinition(AppTestCase):
    def _makeDb(self):
        db = MagicMock()
        db.getEntriesFromNew.return_value = []
        db.getEntriesCount.return_value = 0
        db.searchEntries.return_value = []
        db.searchEntriesCount.return_value = 0
        db.getOverallStats.return_value = {
            "currentTopSongs": [],
            "currentTopArtists": [],
            "totalSongsPlayed": 0,
            "totalDurationMs": 0,
            "previousSongsPlayed": 0,
            "previousDurationMs": 0,
        }
        db.getCurrentStreak.return_value = {"days": 0, "activeToday": False}
        db.getOnThisDay.return_value = []
        db.getListeningCalendar.return_value = {
            "weeks": [], "monthLabels": [], "maxCount": 0, "activeDays": 0, "totalPlays": 0}
        return db

    def _getDashboardHtml(self, dash):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=self._makeDb()):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            resp = client.get("/")
        self.assertEqual(resp.status_code, 200)
        return resp.data.decode("utf-8")

    def test_placeholder_img_is_defined_before_body(self):
        """The definition must precede <body> (i.e. live in <head>), so no
        img error handler - however early it fires - can see it undefined."""
        html = self._getDashboardHtml(self._makeApp())

        definitionIndex = html.find("window.PLACEHOLDER_IMG")
        bodyIndex = html.find("<body")

        self.assertNotEqual(definitionIndex, -1, "window.PLACEHOLDER_IMG is never defined")
        self.assertNotEqual(bodyIndex, -1)
        self.assertLess(definitionIndex, bodyIndex,
                        "window.PLACEHOLDER_IMG must be defined in <head>, before any <img> can parse")

    def test_now_playing_cover_has_no_empty_src(self):
        """src="" fails at parse time, which both races the placeholder
        definition and burns the one-shot onerror fallback (this.onerror=null)
        before the now-playing JS ever assigns a real cover URL - a later
        genuine 404 then renders as a broken-image icon instead of the
        placeholder. The img must start with no src attribute at all (an
        <img> without src fires no error and requests nothing)."""
        html = self._getDashboardHtml(self._makeApp())

        tagStart = html.find('id="nowPlayingCover"')
        self.assertNotEqual(tagStart, -1)
        tag = html[html.rfind("<img", 0, tagStart):html.find(">", tagStart) + 1]

        self.assertNotIn('src=""', tag)
        self.assertNotIn("src=''", tag)

    def test_now_playing_name_links_to_spotify_not_internal_song_page(self):
        """A currently-playing track usually has no completed play logged
        yet, so /song/<id> would find nothing and silently redirect to Top
        Songs. The now-playing name must link straight to the real Spotify
        track page instead."""
        html = self._getDashboardHtml(self._makeApp())

        self.assertIn("nameEl.href = 'https://open.spotify.com/track/' + encodeURIComponent(np.trackId);", html)
        self.assertNotIn("nameEl.href = '/song/' + encodeURIComponent(np.trackId);", html)


if __name__ == "__main__":
    unittest.main()
