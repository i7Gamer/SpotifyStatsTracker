import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# NOTE: like test_dashboard_pagination.py / test_top_albums_route.py, this file
# deliberately does NOT swap Database modules for MagicMocks in sys.modules -
# it only exercises the routes with a per-test mock db (via get_user_db).
from app import SpotifyDashboardApp
from _app_factory import AppTestCase


class _DetailRouteTestBase(AppTestCase):
    def _getPath(self, dash, db, path):
        client = dash.app.test_client()
        with patch.object(dash, 'is_user_logged_in', return_value=True), \
             patch.object(dash, 'get_username_for_email', return_value='alice'), \
             patch.object(dash, 'get_user_db', return_value=db):
            with client.session_transaction() as sess:
                sess['email'] = 'alice@example.com'
            return client.get(path)


class TestSongDetailRoute(_DetailRouteTestBase):
    def _song(self):
        return {
            "id": "t1", "name": "Song One", "url": "http://example.com/t1",
            "imageId": "alb1", "duration": 200000, "explicit": False, "isrc": "",
            "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
            "album": {"id": "alb1", "name": "Album One", "url": "http://example.com/alb1",
                      "imageId": "alb1", "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "artists": [{"id": "a1", "name": "Artist A", "url": "u", "imageUrl": "", "imageId": "a1"}],
            "plays": 5, "totalTimeListened": 50000, "firstListenedAt": 100,
        }

    def test_known_song_renders(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Song One", resp.data)
        db.getSong.assert_called_once_with("t1")
        db.getListeningTimeSeries.assert_called_once()
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("trackId"), "t1")
        self.assertEqual(db.getHourOfDayHeatmap.call_args.kwargs.get("trackId"), "t1")

    def test_genre_badge_renders_when_track_has_genres(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTrack.return_value = ["dream pop", "shoegaze"]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'<span class="track-label genre-label">dream pop</span>', resp.data)
        self.assertIn(b'<span class="track-label genre-label">shoegaze</span>', resp.data)
        db.getGenresForTrack.assert_called_once_with("t1")

    def test_genre_badge_is_capped_at_track_card_genre_limit(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTrack.return_value = ["one", "two", "three", "four"]

        resp = self._getPath(dash, db, "/song/t1")

        from app import TRACK_CARD_GENRE_LIMIT
        self.assertEqual(TRACK_CARD_GENRE_LIMIT, 3)
        for genre in ("one", "two", "three"):
            self.assertIn(f'<span class="track-label genre-label">{genre}</span>'.encode(), resp.data)
        self.assertNotIn(b"genre-label\">four<", resp.data)

    def test_genre_badge_hides_when_the_admin_disables_lastfm_backfill(self):
        """Per-track badges normally show regardless of the aggregate
        charts/wrapped/compare unlock threshold - but the admin's instance-
        wide kill switch still applies, same as every other genre surface."""
        dash = self._makeApp()
        dash.repo.setLastfmGenreBackfillEnabled(False)
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTrack.return_value = ["dream pop", "shoegaze"]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b"genre-label", resp.data)
        db.getGenresForTrack.assert_not_called()

    def test_genre_badge_absent_without_genre_data(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTrack.return_value = []

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b"genre-label", resp.data)

    def test_genre_badges_render_inside_track_attributes_after_other_labels(self):
        """Genre badges are nested inside .track-attributes, after the other
        label spans, so that .genre-badges-container's display:contents lets
        them join .track-attributes' own flex row on mobile/tablet - same
        row, same size, just a different color. Desktop promotes the
        container to an absolutely positioned overlay instead - see the
        has-genres media query in style.css."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTrack.return_value = ["dream pop", "shoegaze"]

        resp = self._getPath(dash, db, "/song/t1")
        body = resp.data.decode()

        attributesOpenIdx = body.index('class="track-attributes"')
        durationLabelIdx = body.index('Duration:')
        genreContainerIdx = body.index('class="genre-badges-container"')
        attributesCloseIdx = body.index('</div>', genreContainerIdx)
        self.assertLess(attributesOpenIdx, durationLabelIdx)
        self.assertLess(durationLabelIdx, genreContainerIdx)
        self.assertLess(genreContainerIdx, attributesCloseIdx)

    def test_spotify_link_renders_last_after_genre_badges(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTrack.return_value = ["dream pop"]

        resp = self._getPath(dash, db, "/song/t1")
        body = resp.data.decode()

        self.assertIn('class="track-label track-spotify-link"', body)
        genreContainerIdx = body.index('class="genre-badges-container"')
        spotifyLinkIdx = body.index('class="track-label track-spotify-link"')
        self.assertLess(genreContainerIdx, spotifyLinkIdx)

    def test_track_card_gets_has_genres_class_only_when_genres_exist(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTrack.return_value = ["dream pop"]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'class="track-card has-genres"', resp.data)

    def test_track_card_omits_has_genres_class_without_genre_data(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTrack.return_value = []

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'class="track-card"', resp.data)
        self.assertNotIn(b'has-genres', resp.data)

    def test_play_history_panel_hidden_for_single_play_song(self):
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 1
        db.getSong.return_value = song
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b"Play History", resp.data)
        self.assertNotIn(b'id="timeSeriesChart"', resp.data)
        self.assertIn(b"When You Listen to This Song", resp.data)

    def test_play_history_panel_shown_for_multiple_plays(self):
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 2
        db.getSong.return_value = song
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b"Play History", resp.data)
        self.assertIn(b'id="timeSeriesChart"', resp.data)

    def test_unknown_song_redirects_to_top_songs(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = None

        resp = self._getPath(dash, db, "/song/missing")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/top-songs", resp.headers["Location"])

    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1?groupBy=month")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_invalid_groupby_resolves_like_auto(self):
        # Junk goes through the same span-derived resolution as the Auto
        # option (see _resolveGroupBy) - with no recorded plays the span is
        # empty, which resolves to day.
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        self._getPath(dash, db, "/song/t1?groupBy=nonsense")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "day")

    def test_ajax_returns_only_the_time_series_json_and_skips_heavy_work(self):
        """The Trend-buckets select re-fetches just the play-history series via
        ?ajax=true (static/js/detail-chart.js); the heavy per-page work (heatmap,
        genres) must be deferred - see the branch in songDetailPage."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = [{"label": "2026-07-01", "totalTimeListened": 1000, "plays": 1}]

        resp = self._getPath(dash, db, "/song/t1?ajax=true")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertEqual(sorted(payload.keys()), ["groupBy", "timeSeries"])
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("trackId"), "t1")
        db.getHourOfDayHeatmap.assert_not_called()
        db.getGenresForTrack.assert_not_called()

    def test_ajax_groupby_is_passed_through_and_echoed(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/song/t1?groupBy=month&ajax=true")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertEqual(resp.get_json().get("groupBy"), "month")


class TestArtistDetailRoute(_DetailRouteTestBase):
    def _artist(self):
        return {"id": "a1", "name": "Artist A", "url": "http://example.com/a1", "imageUrl": "",
                "imageId": "a1", "plays": 5, "totalTimeListened": 50000, "uniqueSongCount": 2,
                "firstListenedAt": 100}

    def _song(self, trackId, name, firstListenedAt):
        return {
            "id": trackId, "name": name, "url": "u", "imageId": "alb1",
            "duration": 200000, "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1,
            "releaseDate": 0, "album": {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                                        "imageUrl": "", "totalTracks": 2, "releaseDate": 0},
            "artists": [], "plays": 3, "totalTimeListened": 30000, "firstListenedAt": firstListenedAt,
        }

    def test_known_artist_renders_with_their_songs(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Artist A", resp.data)
        db.getArtist.assert_called_once_with("a1")
        self.assertEqual(db.getSongsStats.call_args.kwargs.get("artistId"), "a1")
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("artistId"), "a1")

    def test_genre_badge_renders_when_artist_has_genres(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getGenresForArtist.return_value = ["indie rock"]

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b'<span class="track-label genre-label">indie rock</span>', resp.data)
        db.getGenresForArtist.assert_called_once_with("a1")

    def test_biography_renders_when_present(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getArtistBio.return_value = "A great band from somewhere."

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b"Biography", resp.data)
        self.assertIn(b"A great band from somewhere.", resp.data)
        self.assertIn(b"Biography via Last.fm", resp.data)
        db.lazyFetchArtistBio.assert_called_once_with("a1", "Artist A")
        db.getArtistBio.assert_called_once_with("a1")

    def test_biography_section_absent_without_a_bio(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getArtistBio.return_value = None

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertNotIn(b"Biography", resp.data)

    def test_biography_hides_when_the_admin_disables_the_feature(self):
        """Same contract as the genre badge's kill switch: disabled hides
        the section even for an artist whose bio was already fetched and
        stored - db.getArtistBio isn't even consulted for display."""
        dash = self._makeApp()
        dash.repo.setArtistBioEnabled(False)
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getArtistBio.return_value = "A great band from somewhere."

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertNotIn(b"Biography", resp.data)
        db.getArtistBio.assert_not_called()

    def test_biography_text_is_html_escaped(self):
        """Last.fm bio text must never be rendered as raw HTML (defense in
        depth alongside the tag-stripping already done in
        Database.lastfm._extractArtistBio)."""
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getArtistBio.return_value = "<script>alert('xss')</script>"

        resp = self._getPath(dash, db, "/artist/a1")

        #< the raw payload must never appear unescaped (the page legitimately
        #  contains other <script> tags from layout.html/charts.js, so check
        #  the specific string instead of a bare "<script>" substring)
        self.assertNotIn(b"<script>alert", resp.data)
        self.assertIn(b"&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;", resp.data)

    def test_unknown_artist_redirects_to_top_artists(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = None

        resp = self._getPath(dash, db, "/artist/missing")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/top-artists", resp.headers["Location"])

    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1?groupBy=month")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_ajax_returns_only_the_time_series_json_and_skips_heavy_work(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getListeningTimeSeries.return_value = [{"label": "2026-07-01", "totalTimeListened": 1000, "plays": 1}]

        resp = self._getPath(dash, db, "/artist/a1?ajax=true")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertEqual(sorted(payload.keys()), ["groupBy", "timeSeries"])
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("artistId"), "a1")
        db.getSongsStats.assert_not_called()
        db.lazyFetchArtistBio.assert_not_called()
        db.getArtistBio.assert_not_called()

    def test_first_song_you_listened_to_is_shown(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = [
            self._song("t1", "Later Song", firstListenedAt=200),
            self._song("t2", "Earliest Song", firstListenedAt=100),
        ]
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b"First Song You Listened To", resp.data)
        self.assertIn(b"Earliest Song", resp.data)

    def test_unique_song_count_card_is_shown(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b"Unique Songs Listened", resp.data)
        self.assertIn(b'<p class="summary-value">2</p>', resp.data)


class TestAlbumDetailRoute(_DetailRouteTestBase):
    def _album(self):
        return {"id": "alb1", "name": "Album One", "url": "http://example.com/alb1", "imageId": "alb1",
                "imageUrl": "", "totalTracks": 2, "releaseDate": 0, "artists": [],
                "plays": 5, "totalTimeListened": 50000, "uniqueSongCount": 2, "firstListenedAt": 100}

    def _song(self, trackId, firstListenedAt):
        return {
            "id": trackId, "name": f"Song {trackId}", "url": "u", "imageId": "alb1",
            "duration": 200000, "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1,
            "releaseDate": 0, "album": {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                                        "imageUrl": "", "totalTracks": 2, "releaseDate": 0},
            "artists": [], "plays": 3, "totalTimeListened": 30000, "firstListenedAt": firstListenedAt,
        }

    def test_known_album_renders_with_its_songs(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = [self._song("t1", 200), self._song("t2", 100)]
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"Album One", resp.data)
        db.getAlbum.assert_called_once_with("alb1")
        self.assertEqual(db.getSongsStats.call_args.kwargs.get("albumId"), "alb1")
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("albumId"), "alb1")

    def test_genre_badge_renders_when_album_has_genres(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getGenresForAlbum.return_value = ["indie rock"]

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(b'<span class="track-label genre-label">indie rock</span>', resp.data)
        db.getGenresForAlbum.assert_called_once_with("alb1")

    def _albumWithArtist(self):
        album = self._album()
        album["artists"] = [{"id": "a1", "name": "Artist A", "url": "u", "imageUrl": "", "imageId": "a1"}]
        return album

    def test_biography_renders_when_present(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._albumWithArtist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getAlbumBio.return_value = "A landmark album from somewhere."

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(b"Biography", resp.data)
        self.assertIn(b"A landmark album from somewhere.", resp.data)
        self.assertIn(b"Biography via Last.fm", resp.data)
        db.lazyFetchAlbumBio.assert_called_once_with("alb1", "Album One", "Artist A")
        db.getAlbumBio.assert_called_once_with("alb1")

    def test_biography_section_absent_without_a_bio(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._albumWithArtist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getAlbumBio.return_value = None

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertNotIn(b"Biography", resp.data)

    def test_biography_hides_when_the_admin_disables_the_feature(self):
        """Same contract as the artist bio's kill switch: disabled hides the
        section even for an album whose bio was already fetched and stored -
        db.getAlbumBio isn't even consulted for display."""
        dash = self._makeApp()
        dash.repo.setAlbumBioEnabled(False)
        db = MagicMock()
        db.getAlbum.return_value = self._albumWithArtist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getAlbumBio.return_value = "A landmark album from somewhere."

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertNotIn(b"Biography", resp.data)
        db.getAlbumBio.assert_not_called()

    def test_biography_text_is_html_escaped(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._albumWithArtist()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getAlbumBio.return_value = "<script>alert('xss')</script>"

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertNotIn(b"<script>alert", resp.data)
        self.assertIn(b"&lt;script&gt;alert(&#39;xss&#39;)&lt;/script&gt;", resp.data)

    def test_no_lazy_fetch_without_a_resolvable_primary_artist(self):
        """_album() (no artists) can't be looked up via album.getinfo, which
        needs an artist name - the route must skip the fetch, not crash."""
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getAlbumBio.return_value = None

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertEqual(resp.status_code, 200)
        db.lazyFetchAlbumBio.assert_not_called()

    def test_unknown_album_redirects_to_top_albums(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = None

        resp = self._getPath(dash, db, "/album/missing")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/top-albums", resp.headers["Location"])

    def test_month_groupby_is_passed_through_and_selected(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1?groupBy=month")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertIn(b'<option value="month" selected>Month</option>', resp.data)

    def test_ajax_returns_only_the_time_series_json_and_skips_heavy_work(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getListeningTimeSeries.return_value = [{"label": "2026-07-01", "totalTimeListened": 1000, "plays": 1}]

        resp = self._getPath(dash, db, "/album/alb1?ajax=true")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        payload = resp.get_json()
        self.assertEqual(sorted(payload.keys()), ["groupBy", "timeSeries"])
        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("albumId"), "alb1")
        db.getSongsStats.assert_not_called()
        db.lazyFetchAlbumBio.assert_not_called()
        db.getAlbumBio.assert_not_called()

    def test_unique_song_count_card_is_shown(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(b"Unique Songs Listened", resp.data)
        self.assertIn(b'<p class="summary-value">2</p>', resp.data)


if __name__ == "__main__":
    unittest.main()
