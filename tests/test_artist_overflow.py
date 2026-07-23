"""Long artist lists (_artist_links.html): lists longer than
MAX_INLINE_ARTISTS collapse behind a "+N more" toggle button, but only when
at least MIN_HIDDEN_ARTISTS names would be hidden - a "+1 more" button takes
about as much space as the single name it hides.

Covered render sites: the song/album detail heroes and _track_card.html's two
artist loops (song cards and album cards), which every list page (dashboard,
top lists, Wrapped, Compare) includes.
"""
import re
import unittest
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp, MAX_INLINE_ARTISTS, MIN_HIDDEN_ARTISTS
from _app_factory import AppTestCase

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'

# Smallest list length that collapses.
COLLAPSING_COUNT = MAX_INLINE_ARTISTS + MIN_HIDDEN_ARTISTS

OVERFLOW_SPAN_RE = re.compile(r'<span class="artist-overflow" hidden>(.*?)</span>', re.S)
TOGGLE_BUTTON = 'class="artist-toggle"'


def _artists(count):
    return [{"id": f"a{i}", "name": f"Artist {i:02d}", "url": f"http://example.com/a{i}",
             "imageUrl": "", "imageId": f"a{i}"} for i in range(1, count + 1)]


class _ArtistOverflowTestBase(AppTestCase):
    def _getPath(self, dash, db, path):
        # The detail routes unconditionally fetch a page of play history (see
        # _detailHistoryContext) - default it to "no history", same as
        # test_detail_pages_route.py's _DetailRouteTestBase.
        if not isinstance(db.getEntriesCount.return_value, int):
            db.getEntriesCount.return_value = 0
        if not isinstance(db.getEntriesFromNew.return_value, list):
            db.getEntriesFromNew.return_value = []
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(path)


class TestSongPagesArtistOverflow(_ArtistOverflowTestBase):
    """Song detail renders the artist list twice (hero + track card), so it
    exercises both the hero markup and _track_card.html's song-card loop."""

    def _song(self, artistCount):
        return {
            "id": "t1", "name": "Song One", "url": "http://example.com/t1",
            "imageId": "alb1", "duration": 200000, "explicit": False, "isrc": "",
            "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album One", "url": "http://example.com/alb1",
                      "imageId": "alb1", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": _artists(artistCount),
            "plays": 5, "totalTimeListened": 50000, "firstListenedAt": 100,
        }

    def _renderSongDetail(self, artistCount):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song(artistCount)
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        resp = self._getPath(dash, db, "/song/t1")
        self.assertEqual(resp.status_code, 200)
        return resp.data.decode()

    def test_at_threshold_shows_all_artists_without_toggle(self):
        data = self._renderSongDetail(MAX_INLINE_ARTISTS)

        self.assertNotIn(TOGGLE_BUTTON, data)
        self.assertNotIn('artist-overflow', data)
        for artist in _artists(MAX_INLINE_ARTISTS):
            self.assertIn(artist["name"], data)

    def test_one_over_threshold_still_shows_all_artists_without_toggle(self):
        # Hiding a single name behind a "+1 more" button saves no space.
        data = self._renderSongDetail(MAX_INLINE_ARTISTS + MIN_HIDDEN_ARTISTS - 1)

        self.assertNotIn(TOGGLE_BUTTON, data)
        self.assertNotIn('artist-overflow', data)
        for artist in _artists(MAX_INLINE_ARTISTS + MIN_HIDDEN_ARTISTS - 1):
            self.assertIn(artist["name"], data)

    def test_collapses_when_enough_artists_are_hidden(self):
        data = self._renderSongDetail(COLLAPSING_COUNT)

        # Hero + track card each render one collapsed list.
        self.assertEqual(data.count(TOGGLE_BUTTON), 2)
        self.assertIn(f'+{MIN_HIDDEN_ARTISTS} more', data)
        self.assertIn('aria-expanded="false"', data)
        self.assertIn(f'data-hidden-count="{MIN_HIDDEN_ARTISTS}"', data)

        overflows = OVERFLOW_SPAN_RE.findall(data)
        self.assertEqual(len(overflows), 2)
        visibleNames = [a["name"] for a in _artists(MAX_INLINE_ARTISTS)]
        hiddenNames = [a["name"] for a in _artists(COLLAPSING_COUNT)[MAX_INLINE_ARTISTS:]]
        for overflow in overflows:
            for name in hiddenNames:
                self.assertIn(name, overflow)
            for name in visibleNames:
                self.assertNotIn(name, overflow)

    def test_hidden_artists_keep_their_detail_links(self):
        data = self._renderSongDetail(COLLAPSING_COUNT)

        overflow = OVERFLOW_SPAN_RE.search(data).group(1)
        self.assertIn(f'/artist/a{COLLAPSING_COUNT}', overflow)

    def test_visible_artists_render_before_the_overflow_span(self):
        data = self._renderSongDetail(COLLAPSING_COUNT)

        lastVisible = _artists(MAX_INLINE_ARTISTS)[-1]["name"]
        self.assertLess(data.index(lastVisible), data.index('artist-overflow'))


class TestAlbumPagesArtistOverflow(_ArtistOverflowTestBase):
    """Album detail renders the album's artist list twice (hero + album card),
    exercising _track_card.html's separate album-card artist loop."""

    def _album(self, artistCount):
        return {"id": "alb1", "name": "Album One", "url": "http://example.com/alb1", "imageId": "alb1",
                "imageUrl": "", "totalTracks": 2, "releaseDate": 0, "artists": _artists(artistCount),
                "plays": 5, "totalTimeListened": 50000, "uniqueSongCount": 2, "firstListenedAt": 100}

    def _renderAlbumDetail(self, artistCount):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album(artistCount)
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        resp = self._getPath(dash, db, "/album/alb1")
        self.assertEqual(resp.status_code, 200)
        return resp.data.decode()

    def test_at_threshold_shows_all_artists_without_toggle(self):
        data = self._renderAlbumDetail(MAX_INLINE_ARTISTS)

        self.assertNotIn(TOGGLE_BUTTON, data)
        self.assertNotIn('artist-overflow', data)

    def test_collapses_when_enough_artists_are_hidden(self):
        data = self._renderAlbumDetail(COLLAPSING_COUNT)

        self.assertEqual(data.count(TOGGLE_BUTTON), 2)
        overflows = OVERFLOW_SPAN_RE.findall(data)
        self.assertEqual(len(overflows), 2)
        hiddenNames = [a["name"] for a in _artists(COLLAPSING_COUNT)[MAX_INLINE_ARTISTS:]]
        for overflow in overflows:
            for name in hiddenNames:
                self.assertIn(name, overflow)


class TestCompareArtistOverflow(AppTestCase):
    """The Compare page renders the same track three ways: the viewer's own
    column (detail links), the counterpart's column (suppressDetailLinks -
    Spotify links only), and the shared Top Common card (detail links).
    Hidden overflow artists must follow the same link rules as visible ones."""

    def _song(self):
        return {"id": "t1", "name": "Crowded Song", "artists": _artists(COLLAPSING_COUNT),
                "duration": 60000, "plays": 3, "totalTimeListened": 30000,
                "url": "https://open.spotify.com/track/t1"}

    def _makeStubDb(self):
        db = MagicMock()
        db.tz = None
        db.getPlayTotals.return_value = (0, 0)
        db.getTopSongs.return_value = [self._song()]
        db.getTopArtists.return_value = []
        db.getTopAlbums.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getSongsCount.return_value = 0
        db.getArtistsCount.return_value = 0
        db.getCompletionStats.return_value = {"skips": 0, "completes": 0, "partials": 0}
        db.getExplicitRatio.return_value = {"explicit": 0, "clean": 0}
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.readProgress.return_value = {"status": "idle", "current": 0, "total": 0, "percentage": 0, "message": "", "error": False}
        return db

    def setUp(self):
        self.dash = self._makeApp()
        for username in ("alice", "bob"):
            self.dash.repo.upsertUser(username, f"{username}@example.com")
            self.dash.repo.setUserCookies(username, {"sp_dc": "test"})
        self.dash.repo.createShareRequest("alice", "bob")
        shareId = self.dash.repo.getPendingIncomingShares("bob")[0]["id"]
        self.dash.repo.respondToShareRequest(shareId, "bob", accept=True)
        self.dbs = {u: self._makeStubDb() for u in ("alice", "bob")}

    def _loginAs(self, username):
        patch.object(self.dash, 'is_user_logged_in', return_value=True).start()
        patch.object(self.dash, 'get_username_for_email', return_value=username).start()
        patch.object(self.dash, 'get_user_db', side_effect=lambda u, e: self.dbs[u]).start()
        self.addCleanup(patch.stopall)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = f"{username}@example.com"
            sess['username'] = username
        return client

    def test_all_three_render_contexts_collapse(self):
        client = self._loginAs("alice")

        resp = client.get("/compare?ajax=true")

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        # Shared card + my column + their column - the three fragments that
        # each render the same track (see the class docstring).
        data = payload["sharedSongsHtml"] + payload["myTopSongsHtml"] + payload["theirTopSongsHtml"]
        self.assertEqual(data.count(TOGGLE_BUTTON), 3)

    def test_counterpart_overflow_links_to_spotify_not_detail_pages(self):
        client = self._loginAs("alice")

        resp = client.get("/compare?ajax=true")

        payload = resp.get_json()
        # Page order: shared Top Common card, then my column, then theirs.
        data = payload["sharedSongsHtml"] + payload["myTopSongsHtml"] + payload["theirTopSongsHtml"]
        overflows = OVERFLOW_SPAN_RE.findall(data)
        self.assertEqual(len(overflows), 3)
        sharedOverflow, myOverflow, theirOverflow = overflows
        self.assertIn('/artist/', sharedOverflow)
        self.assertIn('/artist/', myOverflow)
        self.assertNotIn('/artist/', theirOverflow)
        self.assertIn('target="_blank"', theirOverflow)

    def test_ajax_partials_carry_the_collapsed_markup(self):
        # Compare's filter controls swap card lists via innerHTML with these
        # server-rendered partials - they must collapse exactly like the
        # initial page render.
        client = self._loginAs("alice")

        resp = client.get("/compare?ajax=true")

        self.assertEqual(resp.status_code, 200)
        payload = resp.get_json()
        self.assertIn(TOGGLE_BUTTON, payload["myTopSongsHtml"])
        self.assertIn(TOGGLE_BUTTON, payload["theirTopSongsHtml"])
        self.assertIn(TOGGLE_BUTTON, payload["sharedSongsHtml"])


if __name__ == "__main__":
    unittest.main()
