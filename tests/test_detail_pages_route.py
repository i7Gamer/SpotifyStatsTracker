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
from _detail_client import DetailPageClientMixin


def _byId(entityId, genres):
    """Return value shape of the batched genre lookups (getGenresForTracks and
    friends): {id: [genre, ...]} for every requested id."""
    return {entityId: genres}


class _DetailRouteTestBase(DetailPageClientMixin, AppTestCase):
    """`self._getPath(...)` is the shell GET plus its deferred ?ajax=page body,
    which is what a browser ends up showing - see _detail_client.py. Tests
    about the split itself live in TestDetailPageDeferredBody below and drive
    the two requests through `_getRaw` instead."""

    def _assertNoNavItemActive(self, body):
        """Detail subpages aren't the Top Songs/Artists/Albums list pages
        themselves, so nothing in the main nav should render as active/current
        while viewing one (see templates/layout.html's nav-links block)."""
        import re
        navMatch = re.search(r'<nav class="nav-links".*?</nav>', body, re.DOTALL)
        self.assertIsNotNone(navMatch, "could not find nav-links block")
        navHtml = navMatch.group(0)
        self.assertNotIn("active-parent", navHtml)
        self.assertNotIn('class="active"', navHtml)


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

    def test_no_nav_item_is_active(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self._assertNoNavItemActive(resp.data.decode())

    def test_tag_widget_shown_by_default(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'class="tag-widget"', resp.data)

    def test_tag_widget_hidden_when_admin_disables_tags(self):
        dash = self._makeApp()
        dash.repo.setTagsEnabled(False)
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'class="tag-widget"', resp.data)

    def test_tag_widget_hidden_when_user_hides_it(self):
        dash = self._makeApp()
        dash.repo.upsertUser("alice", "alice@example.com")
        dash.repo.updateUserSettings("alice", "day", None, hide_tags_panel=True)
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'class="tag-widget"', resp.data)

    def test_rendered_page_has_balanced_div_tags(self):
        """Guard against an unclosed container leaking every section below it
        into the toolbar (the tag-widget insert once dropped .detail-toolbar's
        closing </div>, which cascaded the whole subpage layout)."""
        import re
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")
        body = resp.data.decode()

        opens = len(re.findall(r"<div[\s>]", body))
        closes = body.count("</div>")
        self.assertEqual(opens, closes, f"unbalanced <div> tags: {opens} open vs {closes} close")

    def test_genre_badge_renders_when_track_has_genres(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", ["dream pop", "shoegaze"])

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'<span class="track-label genre-label">dream pop</span>', resp.data)
        self.assertIn(b'<span class="track-label genre-label">shoegaze</span>', resp.data)
        db.getGenresForTracks.assert_called_once_with(["t1"])

    def test_genre_badge_is_capped_at_track_card_genre_limit(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", ["one", "two", "three", "four"])

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
        db.getGenresForTracks.return_value = _byId("t1", ["dream pop", "shoegaze"])

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b"genre-label", resp.data)
        db.getGenresForTracks.assert_not_called()

    def test_genre_badge_absent_without_genre_data(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", [])

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
        db.getGenresForTracks.return_value = _byId("t1", ["dream pop", "shoegaze"])

        resp = self._getPath(dash, db, "/song/t1")
        body = resp.data.decode()

        attributesOpenIdx = body.index('class="track-attributes"')
        durationLabelIdx = body.index('Duration:')
        genreContainerIdx = body.index('class="genre-badges-container"')
        attributesCloseIdx = body.index('</div>', genreContainerIdx)
        self.assertLess(attributesOpenIdx, durationLabelIdx)
        self.assertLess(durationLabelIdx, genreContainerIdx)
        self.assertLess(genreContainerIdx, attributesCloseIdx)

    def test_play_now_button_renders_last_after_genre_badges(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", ["dream pop"])

        resp = self._getPath(dash, db, "/song/t1")
        body = resp.data.decode()

        self.assertIn('class="track-label track-spotify-link play-now-button"', body)
        genreContainerIdx = body.index('class="genre-badges-container"')
        buttonIdx = body.index('class="track-label track-spotify-link play-now-button"')
        self.assertLess(genreContainerIdx, buttonIdx)

    def test_play_now_button_replaces_open_in_spotify_on_the_hero_card(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'play-now-button', resp.data)
        self.assertNotIn(b'Open in Spotify', resp.data)
        self.assertIn(b'data-spotify-url="http://example.com/t1"', resp.data)
        self.assertIn(b'data-embed-type="track"', resp.data)

    def test_play_embed_container_renders_hidden_between_card_and_charts(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")
        body = resp.data.decode()

        self.assertIn('id="play-embed"', body)
        embedTag = body[body.index('id="play-embed"'):body.index('>', body.index('id="play-embed"')) + 1]
        self.assertIn('hidden', embedTag)
        trackListIdx = body.index('id="track-list"')
        embedIdx = body.index('id="play-embed"')
        chartCardIdx = body.index('chart-card')
        self.assertLess(trackListIdx, embedIdx)
        self.assertLess(embedIdx, chartCardIdx)

    def test_play_embed_renders_even_when_the_chart_section_is_absent(self):
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 1   #< single play => no Play History chart section
        db.getSong.return_value = song
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b'id="timeSeriesChart"', resp.data)
        self.assertIn(b'id="play-embed"', resp.data)

    def test_no_play_button_or_embed_for_a_fabricated_song(self):
        """A deleted/unavailable track has an empty url (a fabricated md5 id) -
        it gets neither the Spotify anchor nor a Play now button/embed, same
        guard the anchor always used."""
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["url"] = ""
        db.getSong.return_value = song
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b'play-now-button', resp.data)
        self.assertNotIn(b'track-spotify-link', resp.data)
        self.assertNotIn(b'id="play-embed"', resp.data)

    def test_play_embed_script_is_included(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'js/play-embed.js', resp.data)

    def test_csp_allows_unsafe_eval_for_the_spotify_embed(self):
        """Spotify's iFrame API bundle is a webpack eval-devtool build that runs
        in our page context, so the detail responses (and only those) must allow
        'unsafe-eval' - see DETAIL_CSP_ENDPOINTS in app.py."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn("'unsafe-eval'", resp.headers.get("Content-Security-Policy", ""))

    def test_track_card_gets_has_genres_class_only_when_genres_exist(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", ["dream pop"])

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b'class="track-card has-genres"', resp.data)

    def test_track_card_omits_has_genres_class_without_genre_data(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getGenresForTracks.return_value = _byId("t1", [])

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

    def test_play_history_panel_shown_for_a_song_that_was_only_ever_skipped(self):
        """2e4f9e3 made a skip-only song's page render, and gave the timeline a
        skips series so it would have something to show - but the canvas that
        series lands on was gated on plays > 1, and getSong's skip-only fallback
        reports plays=0 by design. So the one page the series was built for
        rendered no chart at all. The Most Skipped lists link straight here."""
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 0        #< the skip-sorted fallback's shape
        song["skips"] = 3
        db.getSong.return_value = song
        #< the route always computes this alongside the song; it is where the
        #  gate reads the skip count from, since getSongsPage has no skips column
        db.getSkipStats.return_value = {"plays": 0, "skips": 3, "skipPercent": 100.0}
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b"Play History", resp.data)
        self.assertIn(b'id="timeSeriesChart"', resp.data)

    def test_play_history_panel_shown_for_a_much_skipped_song_with_one_play(self):
        """The rule is "is there a history worth plotting", and the skips series
        is on the timeline either way - so 1 play + 40 skips must chart, exactly
        as 0 plays + 2 skips does.

        The count has to come from skipStats: getSongsPage's SELECT has no skips
        column, so song['skips'] exists ONLY on getSong's skip-only fallback
        (plays=0). Reading it off the song made the gate a no-op for every track
        that has any real play at all."""
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 1
        db.getSong.return_value = song
        db.getSkipStats.return_value = {"plays": 1, "skips": 40, "skipPercent": 97.6}
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b"Play History", resp.data)
        self.assertIn(b'id="timeSeriesChart"', resp.data)

    def test_one_play_and_no_skips_is_still_too_little_to_chart(self):
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 1
        db.getSong.return_value = song
        db.getSkipStats.return_value = {"plays": 1, "skips": 0, "skipPercent": 0.0}
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b'id="timeSeriesChart"', resp.data)

    def test_a_stubbed_skip_summary_cannot_500_the_page(self):
        """Most route tests hand the page a bare MagicMock db, so skipStats is a
        Mock rather than a dict - arithmetic on it would raise inside the
        template. The gate must degrade, not explode."""
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 4
        db.getSong.return_value = song
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b'id="timeSeriesChart"', resp.data)   #< 4 plays alone clears the bar

    def test_a_single_skip_is_still_too_little_to_chart(self):
        """Same rule as a single play: one point is not a history."""
        dash = self._makeApp()
        db = MagicMock()
        song = self._song()
        song["plays"] = 0
        song["skips"] = 1
        db.getSong.return_value = song
        db.getSkipStats.return_value = {"plays": 0, "skips": 1, "skipPercent": 100.0}
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertNotIn(b'id="timeSeriesChart"', resp.data)

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
        db.getGenresForTracks.assert_not_called()

    def test_ajax_groupby_is_passed_through_and_echoed(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/song/t1?groupBy=month&ajax=true")

        self.assertEqual(db.getListeningTimeSeries.call_args.kwargs.get("groupBy"), "month")
        self.assertEqual(resp.get_json().get("groupBy"), "month")

    def _playEntry(self, playedAtText="20 Jul 2026, 15:30", timePlayedText="3m 20s"):
        """A play entry as _embedSongsTextElements would emit it - tests stub
        the embedder to identity (the /history tests' convention), so the
        text fields are supplied directly."""
        return {"id": "t1", "name": "Song One", "playedAtText": playedAtText,
                "timePlayedText": timePlayedText, "contextName": None, "artists": []}

    def test_play_log_renders_play_rows(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getEntriesCount.return_value = 2
        db.getEntriesFromNew.return_value = [
            self._playEntry("20 Jul 2026, 15:30"),
            self._playEntry("19 Jul 2026, 09:12"),
        ]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b"Play Timeline", resp.data)
        self.assertIn(b"20 Jul 2026, 15:30", resp.data)
        self.assertIn(b"19 Jul 2026, 09:12", resp.data)
        self.assertIn(b"Time Played: 3m 20s", resp.data)

    def test_play_log_scoped_to_track_id(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        self._getPath(dash, db, "/song/t1")

        self.assertEqual(db.getEntriesCount.call_args.kwargs.get("trackId"), "t1")
        self.assertEqual(db.getEntriesFromNew.call_args.kwargs.get("trackId"), "t1")

    def test_play_log_page_2_offsets_by_page_size(self):
        from app import PAGE_SIZE
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]
        db.getEntriesCount.return_value = PAGE_SIZE * 2 + 5

        self._getPath(dash, db, "/song/t1?page=2")

        self.assertEqual(db.getEntriesFromNew.call_args.kwargs.get("startIndex"), PAGE_SIZE)
        self.assertEqual(db.getEntriesFromNew.call_args.kwargs.get("count"), PAGE_SIZE)

    def test_play_log_empty_state(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1")

        self.assertIn(b"No plays recorded yet.", resp.data)

    def test_sort_oldest_uses_entries_from_old(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1?sort=oldest")

        self.assertEqual(db.getEntriesFromOld.call_args.kwargs.get("trackId"), "t1")
        db.getEntriesFromNew.assert_not_called()
        self.assertIn("Date ↑".encode(), resp.data)   #< arrow shows the current order

    def test_sort_newest_is_the_default_and_shows_down_arrow(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]

        resp = self._getPath(dash, db, "/song/t1?sort=bogus")

        db.getEntriesFromNew.assert_called_once()
        db.getEntriesFromOld.assert_not_called()
        self.assertIn("Date ↓".encode(), resp.data)
        self.assertIn(b"sort=oldest", resp.data)   #< the toggle links to the flipped order

    def test_ajax_list_returns_results_html_and_skips_chart_work(self):
        """The sort toggle / pagination links re-fetch just the play log via
        ?ajax=list (static/js/detail-history.js) - chart and heatmap work
        must be skipped."""
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = [self._playEntry()]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            resp = self._getPath(dash, db, "/song/t1?ajax=list")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        resultsHtml = resp.get_json().get("resultsHtml", "")
        self.assertIn("20 Jul 2026", resultsHtml)
        db.getHourOfDayHeatmap.assert_not_called()

    def test_song_detail_skips_toggle_passed_to_db(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = []
        db.getEntriesCount.return_value = 2
        db.getEntriesFromNew.return_value = [self._playEntry()]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            self._getPath(dash, db, "/song/t1?skips=false")

        self.assertEqual(db.getEntriesFromNew.call_args.kwargs.get("includeSkips"), False)

    def test_song_detail_ajax_list_has_more_and_next_offset(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getEntriesCount.return_value = 120
        db.getEntriesFromNew.return_value = [self._playEntry() for _ in range(50)]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            resp = self._getPath(dash, db, "/song/t1?ajax=list&offset=0")

        payload = resp.get_json()
        self.assertTrue(payload.get("hasMore"))
        self.assertEqual(payload.get("nextOffset"), 50)
        self.assertEqual(payload.get("nextBatchSize"), 50)
        self.assertIn("20 Jul 2026", payload.get("resultsHtml", ""))
        db.getListeningTimeSeries.assert_not_called()
        db.getHourOfDayHeatmap.assert_not_called()

    def test_song_detail_next_batch_size_partial_remaining(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getSong.return_value = self._song()
        db.getEntriesCount.return_value = 62
        db.getEntriesFromNew.return_value = [self._playEntry() for _ in range(50)]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            resp = self._getPath(dash, db, "/song/t1?ajax=list&offset=0")

        payload = resp.get_json()
        self.assertTrue(payload.get("hasMore"))
        self.assertEqual(payload.get("nextOffset"), 50)
        self.assertEqual(payload.get("nextBatchSize"), 12)
        self.assertEqual(payload.get("remainingCount"), 12)
        self.assertIn("Show More Plays (12)", payload.get("resultsHtml", ""))



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

    def test_no_nav_item_is_active(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self._assertNoNavItemActive(resp.data.decode())

    def test_tag_widget_hidden_when_admin_disables_tags(self):
        dash = self._makeApp()
        dash.repo.setTagsEnabled(False)
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'class="tag-widget"', resp.data)

    def test_genre_badge_renders_when_artist_has_genres(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getGenresForArtists.return_value = _byId("a1", ["indie rock"])

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b'<span class="track-label genre-label">indie rock</span>', resp.data)
        db.getGenresForArtists.assert_called_once_with(["a1"])

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

    def test_hero_gets_play_button_but_song_sublist_keeps_spotify_anchors(self):
        """Only the hero artist card becomes a Play now button; the songs
        sub-list below (a second _track_card.html include) must keep its normal
        Open in Spotify anchors - the playNowButton flag must not leak into
        that loop's shared page context."""
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = [
            self._song("t1", "Song One", firstListenedAt=100),
            self._song("t2", "Song Two", firstListenedAt=200),
        ]
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertEqual(resp.data.count(b'play-now-button'), 1)
        self.assertIn(b'data-embed-type="artist"', resp.data)
        self.assertIn(b'Open in Spotify', resp.data)

    def test_csp_allows_unsafe_eval_for_the_spotify_embed(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn("'unsafe-eval'", resp.headers.get("Content-Security-Policy", ""))

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

    def _makeHistoryDb(self):
        db = MagicMock()
        db.getArtist.return_value = self._artist()
        db.getArtistBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        return db

    def test_history_tab_scoped_to_artist_id(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        self._getPath(dash, db, "/artist/a1")

        self.assertEqual(db.getEntriesCount.call_args.kwargs.get("artistId"), "a1")
        self.assertEqual(db.getEntriesFromNew.call_args.kwargs.get("artistId"), "a1")

    def test_default_view_activates_top_songs_tab(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/artist/a1")

        self.assertIn(b'<div data-category="top-songs" class="visible">', resp.data)
        self.assertIn(b'<div data-category="history" class="">', resp.data)

    def test_view_history_activates_history_tab(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/artist/a1?view=history")

        self.assertIn(b'<div data-category="top-songs" class="">', resp.data)
        self.assertIn(b'<div data-category="history" class="visible">', resp.data)
        self.assertIn(b"History with Artist A", resp.data)

    def test_unknown_view_falls_back_to_top_songs(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/artist/a1?view=bogus")

        self.assertIn(b'<div data-category="top-songs" class="visible">', resp.data)
        self.assertIn(b'<div data-category="history" class="">', resp.data)

    def test_history_pagination_link_carries_view_and_groupby(self):
        from app import PAGE_SIZE
        dash = self._makeApp()
        db = self._makeHistoryDb()
        db.getEntriesCount.return_value = PAGE_SIZE * 2 + 5

        resp = self._getPath(dash, db, "/artist/a1?groupBy=month")

        body = resp.data.decode()
        self.assertIn("view=history", body)
        self.assertIn("groupBy=month", body)
        self.assertIn("page=2", body)

    def test_sort_oldest_uses_entries_from_old(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/artist/a1?sort=oldest")

        self.assertEqual(db.getEntriesFromOld.call_args.kwargs.get("artistId"), "a1")
        db.getEntriesFromNew.assert_not_called()
        self.assertIn("Date ↑".encode(), resp.data)

    def test_ajax_list_skips_heavy_work(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = [
            {"id": "t1", "name": "History Song", "playedAtText": "20 Jul 2026, 15:30", "artists": []}]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs), \
             patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            resp = self._getPath(dash, db, "/artist/a1?ajax=list")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        resultsHtml = resp.get_json().get("resultsHtml", "")
        self.assertIn("History with Artist A", resultsHtml)
        self.assertIn("History Song", resultsHtml)
        self.assertIn("Played at 20 Jul 2026, 15:30", resultsHtml)
        db.getSongsStats.assert_not_called()
        db.lazyFetchArtistBio.assert_not_called()
        db.getListeningTimeSeries.assert_not_called()


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

    def test_no_nav_item_is_active(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self._assertNoNavItemActive(resp.data.decode())

    def test_tag_widget_hidden_when_admin_disables_tags(self):
        dash = self._makeApp()
        dash.repo.setTagsEnabled(False)
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b'class="tag-widget"', resp.data)

    def test_genre_badge_renders_when_album_has_genres(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getGenresForAlbums.return_value = _byId("alb1", ["indie rock"])

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(b'<span class="track-label genre-label">indie rock</span>', resp.data)
        db.getGenresForAlbums.assert_called_once_with(["alb1"])

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

    def test_hero_gets_play_button_but_song_sublist_keeps_spotify_anchors(self):
        """Only the hero album card becomes a Play now button; the tracklist
        below keeps its Open in Spotify anchors (playNowButton flag must not
        leak into that loop's shared page context)."""
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = [self._song("t1", 100), self._song("t2", 200)]
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertEqual(resp.data.count(b'play-now-button'), 1)
        self.assertIn(b'data-embed-type="album"', resp.data)
        self.assertIn(b'Open in Spotify', resp.data)

    def test_csp_allows_unsafe_eval_for_the_spotify_embed(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn("'unsafe-eval'", resp.headers.get("Content-Security-Policy", ""))

    def test_unique_song_count_card_is_shown(self):
        dash = self._makeApp()
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(b"Unique Songs Listened", resp.data)
        self.assertIn(b'<p class="summary-value">2</p>', resp.data)

    def _makeHistoryDb(self):
        db = MagicMock()
        db.getAlbum.return_value = self._album()
        db.getAlbumBio.return_value = None
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        return db

    def test_history_tab_scoped_to_album_id(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        self._getPath(dash, db, "/album/alb1")

        self.assertEqual(db.getEntriesCount.call_args.kwargs.get("albumId"), "alb1")
        self.assertEqual(db.getEntriesFromNew.call_args.kwargs.get("albumId"), "alb1")

    def test_view_history_activates_history_tab(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/album/alb1?view=history")

        self.assertIn(b'<div data-category="top-songs" class="">', resp.data)
        self.assertIn(b'<div data-category="history" class="visible">', resp.data)
        self.assertIn(b"History with Album One", resp.data)

    def test_default_view_activates_top_songs_tab(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/album/alb1")

        self.assertIn(b'<div data-category="top-songs" class="visible">', resp.data)
        self.assertIn(b'<div data-category="history" class="">', resp.data)

    def test_sort_oldest_uses_entries_from_old(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()

        resp = self._getPath(dash, db, "/album/alb1?sort=oldest")

        self.assertEqual(db.getEntriesFromOld.call_args.kwargs.get("albumId"), "alb1")
        db.getEntriesFromNew.assert_not_called()
        self.assertIn("Date ↑".encode(), resp.data)

    def test_ajax_list_skips_heavy_work(self):
        dash = self._makeApp()
        db = self._makeHistoryDb()
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = [
            {"id": "t1", "name": "History Song", "playedAtText": "20 Jul 2026, 15:30", "artists": []}]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs), \
             patch.object(dash, "_attachGenres", side_effect=lambda db_, tracks, kind: tracks):
            resp = self._getPath(dash, db, "/album/alb1?ajax=list")

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.mimetype, "application/json")
        resultsHtml = resp.get_json().get("resultsHtml", "")
        self.assertIn("History with Album One", resultsHtml)
        self.assertIn("History Song", resultsHtml)
        db.getSongsStats.assert_not_called()
        db.lazyFetchAlbumBio.assert_not_called()
        db.getListeningTimeSeries.assert_not_called()


class TestDetailPageDeferredBody(_DetailRouteTestBase):
    """The two-phase split itself: the plain GET is a shell and everything
    below the toolbar comes from ?ajax=page (see DETAIL_BODY_AJAX in
    routes/charts.py and static/js/detail-page.js). These drive the two
    requests through _getRaw rather than the composing _getPath the markup
    tests use."""

    #< every query the split moved off the first paint, per page
    DEFERRED_LOOKUPS = {
        "/song/t1": ("getListeningTimeSeries", "getHourOfDayHeatmap", "getPlayBuckets",
                     "getSkipStats", "getEntriesCount", "getEntriesFromNew", "getGenresForTracks"),
        "/artist/a1": ("getListeningTimeSeries", "getSkipStats", "getSongsStats",
                       "getEntriesCount", "lazyFetchArtistBio"),
        "/album/alb1": ("getListeningTimeSeries", "getSkipStats", "getSongsStats",
                        "getEntriesCount", "lazyFetchAlbumBio"),
    }

    def _db(self):
        db = MagicMock()
        db.getSong.return_value = {
            "id": "t1", "name": "Song One", "url": "http://example.com/t1", "imageId": "alb1",
            "duration": 200000, "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1,
            "releaseDate": 0, "artists": [{"id": "a1", "name": "Artist A", "url": "u",
                                            "imageUrl": "", "imageId": "a1"}],
            "album": {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                       "imageUrl": "", "totalTracks": 1, "releaseDate": 0},
            "plays": 5, "totalTimeListened": 50000, "firstListenedAt": 100,
        }
        db.getArtist.return_value = {"id": "a1", "name": "Artist A", "url": "u", "imageId": "a1",
                                      "imageUrl": "", "plays": 5, "totalTimeListened": 50000,
                                      "firstListenedAt": 100, "uniqueSongCount": 1}
        db.getAlbum.return_value = {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                                     "imageUrl": "", "totalTracks": 1, "releaseDate": 0, "artists": [],
                                     "plays": 5, "totalTimeListened": 50000, "firstListenedAt": 100,
                                     "uniqueSongCount": 1}
        db.getSongsStats.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getPlayBuckets.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0}
                                                for _ in range(24)] for _ in range(7)]
        db.getSkipStats.return_value = {"plays": 5, "skips": 1, "skipPercent": 16.7}
        db.getArtistBio.return_value = None
        db.getAlbumBio.return_value = None
        return db

    def test_the_shell_names_the_entity_and_holds_a_placeholder(self):
        for path, name in (("/song/t1", "Song One"), ("/artist/a1", "Artist A"),
                           ("/album/alb1", "Album One")):
            with self.subTest(path=path):
                dash = self._makeApp()

                body = self._getRaw(dash, self._db(), path).data.decode()

                self.assertIn(name, body)                    #< the hero identifies the page at once
                self.assertIn('id="detailBody"', body)
                self.assertIn('class="detail-skeleton"', body)
                self.assertIn("js/detail-page.js", body)

    def test_the_shell_leaves_every_heavy_lookup_to_the_deferred_load(self):
        """The point of the split: none of these run before the first paint."""
        for path, lookups in self.DEFERRED_LOOKUPS.items():
            with self.subTest(path=path):
                dash = self._makeApp()
                db = self._db()

                self._getRaw(dash, db, path)

                for name in lookups:
                    getattr(db, name).assert_not_called()

    def test_the_shell_disables_the_bucket_select_until_the_body_lands(self):
        """Its ?ajax=true refetch targets a chart that isn't on the page yet,
        and its result would be overwritten by the body payload in flight."""
        dash = self._makeApp()

        body = self._getRaw(dash, self._db(), "/song/t1").data.decode()

        selectTag = body[body.index('<select id="groupBy"'):]
        self.assertIn("disabled", selectTag[:selectTag.index(">")])

    def test_the_deferred_payload_carries_the_body_and_the_chart_data(self):
        for path, key in (("/song/t1", "Song One"), ("/artist/a1", "Songs by Artist A"),
                          ("/album/alb1", "Top Songs on Album One")):
            with self.subTest(path=path):
                dash = self._makeApp()

                resp = self._getRaw(dash, self._db(), f"{path}?ajax=page")

                self.assertEqual(resp.mimetype, "application/json")
                payload = resp.get_json()
                self.assertIn("track-card", payload["bodyHtml"])
                self.assertIn(key, payload["bodyHtml"])
                self.assertIn("timeSeries", payload)

    def test_only_the_song_payload_carries_a_heatmap(self):
        """Its "When You Listen" canvas is the only one on the three pages."""
        dash = self._makeApp()

        songPayload = self._getRaw(dash, self._db(), "/song/t1?ajax=page").get_json()
        artistPayload = self._getRaw(dash, self._db(), "/artist/a1?ajax=page").get_json()

        self.assertIn("heatmap", songPayload)
        self.assertNotIn("heatmap", artistPayload)

    def test_the_deferred_body_brings_the_play_log_with_it(self):
        dash = self._makeApp()
        db = self._db()
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = [
            {"id": "t1", "name": "Song One", "playedAtText": "20 Jul 2026, 15:30",
             "timePlayedText": "3m 20s", "contextName": None, "artists": []}]

        with patch.object(dash, "_embedSongsTextElements", side_effect=lambda songs: songs):
            payload = self._getRaw(dash, db, "/song/t1?ajax=page").get_json()

        self.assertIn("Play Timeline", payload["bodyHtml"])
        self.assertIn("20 Jul 2026, 15:30", payload["bodyHtml"])

    def test_the_narrower_modes_keep_their_own_branches(self):
        """?ajax=true and ?ajax=list are partial refetches OF the body this
        adds, so both must still answer with their own payload rather than
        falling through to the shell or to the whole body."""
        for path in ("/song/t1", "/artist/a1", "/album/alb1"):
            with self.subTest(path=path):
                dash = self._makeApp()

                seriesPayload = self._getRaw(dash, self._db(), f"{path}?ajax=true").get_json()
                listPayload = self._getRaw(dash, self._db(), f"{path}?ajax=list").get_json()

                self.assertEqual(sorted(seriesPayload.keys()), ["groupBy", "timeSeries"])
                self.assertIn("resultsHtml", listPayload)
                self.assertNotIn("bodyHtml", listPayload)

    def test_an_unrecognised_ajax_value_falls_back_to_the_shell(self):
        """Only the three known markers branch; anything else is a page load,
        so a stale or hand-edited value can't serve a JSON fragment as a page."""
        dash = self._makeApp()
        db = self._db()

        resp = self._getRaw(dash, db, "/song/t1?ajax=nonsense")

        self.assertEqual(resp.mimetype, "text/html")
        self.assertIn('id="detailBody"', resp.data.decode())
        db.getSkipStats.assert_not_called()

    def test_an_unknown_entity_still_redirects_before_any_of_this(self):
        for path, endpoint in (("/song/missing", "/top-songs"), ("/artist/missing", "/top-artists"),
                               ("/album/missing", "/top-albums")):
            with self.subTest(path=path):
                dash = self._makeApp()
                db = MagicMock()
                db.getSong.return_value = None
                db.getArtist.return_value = None
                db.getAlbum.return_value = None

                for suffix in ("", "?ajax=page"):
                    resp = self._getRaw(dash, db, path + suffix)
                    self.assertEqual(resp.status_code, 302)
                    self.assertIn(endpoint, resp.headers["Location"])


if __name__ == "__main__":
    unittest.main()
