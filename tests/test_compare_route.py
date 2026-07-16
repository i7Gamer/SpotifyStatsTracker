"""GET /compare: a Compare page only visible/reachable for users with at
least one accepted mutual share - see app.py's comparePage() route.
"""
import unittest
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp

_SECRET_KEY_PATCH = 'app.SpotifyDashboardApp._get_or_create_secret_key'


def _artist(artistId, name, **extra):
    return {"id": artistId, "name": name, **extra}


def _song(trackId, name, **extra):
    # duration/artists are required by _embedSongTextElements (direct key
    # access), everything else the card template reads via .get().
    return {"id": trackId, "name": name, "artists": [], "duration": 60000, **extra}


def _album(albumId, name, **extra):
    return {"id": albumId, "name": name, "artists": [], **extra}


def _zeroHeatmapGrid():
    return [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)] for _ in range(7)]


class TestCompareRoute(unittest.TestCase):
    @patch(_SECRET_KEY_PATCH, return_value='test-secret-key')
    @patch('app.SpotifyDashboardApp.startVersionCheck_thread')
    @patch('app.SpotifyDashboardApp.checkLogin_thread')
    @patch('app.migrateIfNeeded')
    @patch('app.Path.exists')
    def _makeApp(self, mock_exists, mock_migrate, mock_check, mock_version, mock_secret):
        mock_exists.return_value = False
        return SpotifyDashboardApp()

    def _makeStubDb(self, tz=None):
        db = MagicMock()
        db.tz = tz
        db.getPlayTotals.return_value = (0, 0)
        db.getTopSongs.return_value = []
        db.getTopArtists.return_value = []
        db.getTopAlbums.return_value = []
        db.getListeningTimeSeries.return_value = []
        db.getSongsCount.return_value = 0
        db.getArtistsCount.return_value = 0
        db.getCompletionStats.return_value = {"skips": 0, "completes": 0, "partials": 0}
        db.getExplicitRatio.return_value = {"explicit": 0, "clean": 0}
        db.getHourOfDayHeatmap.return_value = _zeroHeatmapGrid()
        db.readProgress.return_value = {"status": "idle", "current": 0, "total": 0, "percentage": 0, "message": "", "error": False}
        return db

    def setUp(self):
        self.dash = self._makeApp()
        # dave deliberately gets no cookies - the "registered but never
        # synced" case comparePage must not try to load a live Database for.
        for username in ("alice", "bob", "carol"):
            self.dash.repo.upsertUser(username, f"{username}@example.com")
            self.dash.repo.setUserCookies(username, {"sp_dc": "test"})
        self.dash.repo.upsertUser("dave", "dave@example.com")
        self.dbs = {u: self._makeStubDb() for u in ("alice", "bob", "carol", "dave")}

    def _loginAs(self, username):
        patch.object(self.dash, 'is_user_logged_in', return_value=True).start()
        patch.object(self.dash, 'get_username_for_email', return_value=username).start()
        self.get_user_db_mock = patch.object(self.dash, 'get_user_db', side_effect=lambda u, e: self.dbs[u]).start()
        self.addCleanup(patch.stopall)

        client = self.dash.app.test_client()
        with client.session_transaction() as sess:
            sess['email'] = f"{username}@example.com"
            sess['username'] = username
        return client

    def _accept(self, requester, recipient):
        self.dash.repo.createShareRequest(requester, recipient)
        shareId = self.dash.repo.getPendingIncomingShares(recipient)[0]["id"]
        self.dash.repo.respondToShareRequest(shareId, recipient, accept=True)

    def test_404_with_no_accepted_shares(self):
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 404)

    def test_anonymous_request_redirects_to_login(self):
        client = self.dash.app.test_client()   #< no session at all

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 302)
        self.assertIn("/login", resp.headers["Location"])
        self.assertIn("compare", resp.headers["Location"])   #< next= preserves the destination

    def test_renders_with_one_accepted_share(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"bob", resp.data)

    def test_with_param_selects_among_multiple_accepted_shares(self):
        self._accept("alice", "bob")
        self._accept("alice", "carol")
        client = self._loginAs("alice")

        resp = client.get("/compare?with=carol")

        self.assertEqual(resp.status_code, 200)
        self.dbs["carol"].getPlayTotals.assert_called()

    def test_with_param_pointing_at_a_non_shared_user_is_ignored(self):
        """The critical authorization boundary: ?with= is untrusted input and
        must never select a user's data the session user hasn't mutually
        accepted a share with, even if that user exists and has shared with
        someone else entirely."""
        self._accept("alice", "bob")
        self._accept("bob", "carol")   #< carol shares with bob, NOT with alice
        client = self._loginAs("alice")

        resp = client.get("/compare?with=carol")

        self.assertEqual(resp.status_code, 200)
        self.dbs["carol"].getPlayTotals.assert_not_called()
        self.dbs["bob"].getPlayTotals.assert_called()
        self.assertNotIn(b"carol", resp.data)

    def test_default_with_is_the_only_accepted_share(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        client.get("/compare")

        self.dbs["bob"].getPlayTotals.assert_called()

    def test_counterpart_without_cookies_is_not_loaded(self):
        """A share row can point at a user with no stored cookies (only
        creatable by seeding the DB - the UI can't accept a share while
        logged out). get_user_db would start a live listener against that
        empty session and crash, so such counterparts must be skipped
        entirely, mirroring /overview's cookies_json guard."""
        self._accept("alice", "dave")   #< dave has no cookies (see setUp)
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 404)
        calledUsernames = [call.args[0] for call in self.get_user_db_mock.call_args_list]
        self.assertNotIn("dave", calledUsernames)

    def test_counterpart_without_cookies_is_excluded_from_the_picker(self):
        self._accept("alice", "bob")
        self._accept("alice", "dave")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(b"dave", resp.data)

    def test_single_day_interval_buckets_by_hour(self):
        """chartsPage switches to hour bucketing for today/day (its
        isSingleDayView) - without the same switch here the whole trend
        collapses into one 'day' bucket, a single point."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        client.get("/compare?interval=today")

        self.assertEqual(self.dbs["alice"].getListeningTimeSeries.call_args.kwargs["groupBy"], "hour")
        self.assertEqual(self.dbs["bob"].getListeningTimeSeries.call_args.kwargs["groupBy"], "hour")

    def test_all_time_trend_uses_the_combined_play_range(self):
        """With no explicit range, getListeningTimeSeries gap-fills each user
        only across their own first-to-last play - two users with disjoint
        listening eras would union into an axis with the years between them
        missing. The route must pin both series to one combined range."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        playRanges = {"alice": (1000.0, 2000.0), "bob": (500000.0, 600000.0)}
        with patch.object(self.dash.repo, 'getPlayTimeRange', side_effect=lambda u: playRanges[u]):
            client.get("/compare")

        for stub in (self.dbs["alice"], self.dbs["bob"]):
            trendStart, trendEnd = stub.getListeningTimeSeries.call_args.args
            self.assertIsNotNone(trendStart)
            self.assertIsNotNone(trendEnd)
            self.assertLess(trendStart, trendEnd)

    def test_trend_buckets_are_the_union_of_both_users_labels_with_zero_fill(self):
        self._accept("alice", "bob")
        self.dbs["alice"].getListeningTimeSeries.return_value = [
            {"label": "2026-01-01", "totalTimeListened": 100, "plays": 1},
        ]
        self.dbs["bob"].getListeningTimeSeries.return_value = [
            {"label": "2026-01-02", "totalTimeListened": 200, "plays": 2},
        ]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'"buckets": ["2026-01-01", "2026-01-02"]', resp.data)
        self.assertIn(b"[100, 0]", resp.data)   #< alice zero-filled on bob's day
        self.assertIn(b"[0, 200]", resp.data)   #< bob zero-filled on alice's day

    def test_overlap_includes_shared_artists_beyond_the_displayed_top_ten(self):
        """The 'You Both Love' intersection runs over the 100-deep pools, not
        the displayed top 10 - and the displayed lists are the pools' first
        10 entries (one aggregation per user, not two)."""
        self._accept("alice", "bob")
        aliceArtists = [_artist(f"a{i}", f"AliceArtist{i}") for i in range(10)]
        shared = _artist("shared11", "OverlapOnlyArtist")
        self.dbs["alice"].getTopArtists.return_value = aliceArtists + [shared]   #< 11th: sliced out of her list
        # bob's own #1 differs so the shared artist doesn't also occupy his
        # summary "Top Artist" cell - keeps the occurrence count card-only.
        self.dbs["bob"].getTopArtists.return_value = [_artist("b0", "BobTopArtist"), shared]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 200)
        # Each rendered card mentions the name twice (img alt= + <h3>). The
        # artist must appear in bob's column AND in "You Both Love", but NOT
        # in alice's top-ten column (it's her #11) - exactly 2 cards, plus one
        # mention as the "Common Top Artist" similarity cell.
        self.assertEqual(resp.data.count(b"OverlapOnlyArtist"), 5)
        self.assertIn(b"AliceArtist9", resp.data)   #< her actual top ten still renders

    def test_summary_row_derives_from_the_same_lists_the_page_shows(self):
        """The 'Top Song'/'Top Artist' summary cells must agree with the #1
        entries of the lists below them - previously the summary used
        getOverallStats' by-time ranking while the artist list used by-plays,
        so one page contradicted itself."""
        self._accept("alice", "bob")
        self.dbs["alice"].getPlayTotals.return_value = (1234, 3_600_000)
        self.dbs["alice"].getTopSongs.return_value = [_song("s1", "AliceTopSong")]
        self.dbs["alice"].getTopArtists.return_value = [_artist("f1", "AliceFavArtist")]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"1,234", resp.data)
        self.assertIn(b"AliceTopSong", resp.data)
        self.assertIn(b"AliceFavArtist", resp.data)
        self.dbs["alice"].getOverallStats.assert_not_called()

    def test_cards_carry_the_embedded_stat_text_like_other_pages(self):
        """Every other page feeding _track_card.html runs the _embed*Text
        helpers first (totalTimeListenedText etc.) - compare must too, or the
        cards render with blank stat lines."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopSongs.return_value = [
            {**_song("s1", "AliceTopSong"), "plays": 7, "totalTimeListened": 3_600_000},
        ]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b"7 plays", resp.data)
        self.assertIn(b"1h 0m 0s", resp.data)   #< msToString(3600000), only present when embedded

    def test_counterpart_cards_are_not_linked_to_detail_pages(self):
        """Detail pages resolve against the VIEWER's own db, so linking the
        counterpart's items either dead-ends (never played -> bounced to the
        viewer's top list) or silently shows the viewer's stats under the
        counterpart's numbers. Their cards must render unlinked; the viewer's
        own cards keep their links."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopSongs.return_value = [_song("my-song-1", "MySong")]
        self.dbs["bob"].getTopSongs.return_value = [_song("their-song-1", "TheirSong")]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b"/song/my-song-1", resp.data)
        self.assertIn(b"TheirSong", resp.data)
        self.assertNotIn(b"/song/their-song-1", resp.data)

    def test_date_range_query_params_are_accepted(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare?interval=month")

        self.assertEqual(resp.status_code, 200)

    def test_custom_date_range_query_params_narrow_the_query(self):
        """_getDateRange prioritizes startDate/endDate over the interval
        string whenever both are present - comparePage already threads them
        through, the UI just needed the option to set them."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare?interval=custom&startDate=2026-01-01&endDate=2026-01-31")

        self.assertEqual(resp.status_code, 200)
        startDate, endDate = self.dbs["alice"].getPlayTotals.call_args.args
        self.assertEqual(startDate.strftime("%Y-%m-%d"), "2026-01-01")
        self.assertEqual(endDate.strftime("%Y-%m-%d"), "2026-02-01")   #< _getDateRange's exclusive end

    def test_custom_date_range_control_is_prefilled_from_query_params(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare?interval=custom&startDate=2026-01-01&endDate=2026-01-31")

        self.assertIn(b'value="2026-01-01"', resp.data)
        self.assertIn(b'value="2026-01-31"', resp.data)
        self.assertIn(b'<option value="custom" selected>Custom Date Range</option>', resp.data)

    def test_custom_date_inputs_are_hidden_by_default(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'id="compareCustomDates" style="display: none;"', resp.data)

    def test_shared_artist_overlap_is_capped_like_every_other_list(self):
        """"You Both Love" is built from the 100-deep pools but must render at
        most COMPARE_TOP_LIST_SIZE cards, matching the adjacent lists."""
        self._accept("alice", "bob")
        sharedPool = [_artist(f"s{i}", f"SharedArtist{i}") for i in range(12)]
        self.dbs["alice"].getTopArtists.return_value = sharedPool
        self.dbs["bob"].getTopArtists.return_value = sharedPool
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"SharedArtist9", resp.data)      #< 10th kept everywhere
        self.assertNotIn(b"SharedArtist10", resp.data)  #< 11th/12th sliced from every list
        self.assertNotIn(b"SharedArtist11", resp.data)

    def test_nav_link_appears_only_once_a_share_is_accepted(self):
        client = self._loginAs("alice")

        respBefore = client.get("/import")
        self._accept("alice", "bob")
        respAfter = client.get("/import")

        self.assertNotIn(b'href="/compare"', respBefore.data)
        self.assertIn(b'href="/compare"', respAfter.data)

    def test_nav_link_stays_hidden_when_the_only_share_is_with_a_cookie_less_user(self):
        """/compare filters cookie-less counterparts and 404s when none remain,
        so the nav link must apply the same filter - otherwise it would point
        at a page that always 404s."""
        self._accept("alice", "dave")   #< dave has no cookies (see setUp)
        client = self._loginAs("alice")

        resp = client.get("/import")

        self.assertNotIn(b'href="/compare"', resp.data)

    def test_stats_table_shows_listening_style_rows(self):
        """The 'differences in listening style' rows: unique songs/artists,
        skip rate, explicit share, and peak hour/day derived from the
        heatmap grid."""
        self._accept("alice", "bob")
        self.dbs["alice"].getSongsCount.return_value = 1111
        self.dbs["alice"].getArtistsCount.return_value = 222
        self.dbs["alice"].getCompletionStats.return_value = {"skips": 1, "completes": 3, "partials": 0}
        self.dbs["alice"].getExplicitRatio.return_value = {"explicit": 1, "clean": 3}
        grid = _zeroHeatmapGrid()
        grid[2][14] = {"totalTimeListened": 999, "plays": 5}   #< Wednesday 14:00
        self.dbs["alice"].getHourOfDayHeatmap.return_value = grid
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 200)
        self.assertIn(b"1,111", resp.data)       #< unique songs, thousands-formatted
        self.assertIn(b"222", resp.data)         #< unique artists
        self.assertIn(b"25%", resp.data)         #< skip rate AND explicit share: 1 of 4
        self.assertIn(b"14:00", resp.data)       #< peak listening hour
        self.assertIn(b"Wednesday", resp.data)   #< most active weekday

    def test_style_rows_show_placeholder_without_any_plays(self):
        """Zero plays must render placeholders, not divide by zero."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertEqual(resp.status_code, 200)
        self.assertIn("—".encode("utf-8"), resp.data)

    def test_summary_rows_link_own_items_to_detail_pages(self):
        self._accept("alice", "bob")
        self.dbs["alice"].getTopSongs.return_value = [_song("s1", "AliceTopSong")]
        self.dbs["alice"].getTopArtists.return_value = [_artist("f1", "AliceFavArtist")]
        self.dbs["alice"].getTopAlbums.return_value = [_album("al1", "AliceTopAlbum")]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'<a class="compare-cell-link" href="/song/s1">', resp.data)
        self.assertIn(b'<a class="compare-cell-link" href="/artist/f1">', resp.data)
        self.assertIn(b'<a class="compare-cell-link" href="/album/al1">', resp.data)

    def test_summary_rows_link_counterpart_items_to_spotify(self):
        """The counterpart's summary cells can't use detail links (they resolve
        against the viewer's db) - they link to Spotify instead, and only when
        a real URL exists (fabricated ids carry an empty url)."""
        self._accept("alice", "bob")
        self.dbs["bob"].getTopSongs.return_value = [
            _song("their-song-1", "TheirSong", url="https://open.spotify.com/track/xyz1")]
        self.dbs["bob"].getTopArtists.return_value = [_artist("their-artist-1", "TheirArtist", url="")]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'href="https://open.spotify.com/track/xyz1" target="_blank"', resp.data)
        self.assertNotIn(b"/song/their-song-1", resp.data)
        self.assertNotIn(b"/artist/their-artist-1", resp.data)   #< empty url: plain text, no link

    def test_counterpart_card_titles_link_to_spotify_when_url_exists(self):
        self._accept("alice", "bob")
        self.dbs["bob"].getTopSongs.return_value = [
            _song("their-song-1", "TheirSong", url="https://open.spotify.com/track/xyz1")]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        # external cover link marker (internal covers have no target attr)
        self.assertIn(b'class="track-cover-link" target="_blank"', resp.data)
        self.assertNotIn(b"/song/their-song-1", resp.data)

    def test_shared_artist_cards_show_both_users_stats(self):
        """Item on 'You Both Love' cards: both users' plays/time/unique songs,
        the combined listening time, and a split bar proportioned by time."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopArtists.return_value = [
            _artist("sh1", "SharedArtist", plays=10, totalTimeListened=3_600_000, uniqueSongCount=4)]
        self.dbs["bob"].getTopArtists.return_value = [
            _artist("sh1", "SharedArtist", plays=5, totalTimeListened=1_800_000, uniqueSongCount=2)]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b"alice: 10 plays", resp.data)
        self.assertIn(b"bob: 5 plays", resp.data)
        self.assertIn(b"Together: 1h 30m 0s", resp.data)
        self.assertIn(b'style="width: 67%"', resp.data)   #< round(3.6/5.4*100)
        # The versus block must appear ONLY on the shared card - the same dict
        # also feeds alice's own Top Artists column, so it must be copied
        # before the comparison data is attached.
        self.assertEqual(resp.data.count(b"compare-split-bar\""), 1)

    def test_similarities_come_from_the_deep_pools(self):
        """Common top song/album cells run over the 100-deep pools, not the
        displayed top ten."""
        self._accept("alice", "bob")
        aliceSongs = [_song(f"a{i}", f"AliceSong{i}") for i in range(10)]
        bobSongs = [_song(f"b{i}", f"BobSong{i}") for i in range(10)]
        shared = _song("shdeep", "DeepSharedSong")
        self.dbs["alice"].getTopSongs.return_value = aliceSongs + [shared]   #< her #11
        self.dbs["bob"].getTopSongs.return_value = bobSongs + [shared]       #< his #11
        client = self._loginAs("alice")

        resp = client.get("/compare")

        # Neither column displays it (rank 11 on both sides), so the only
        # mention is the "Common Top Song" similarity cell - linked to the
        # viewer's own detail page, which resolves since she played it.
        self.assertEqual(resp.data.count(b"DeepSharedSong"), 1)
        self.assertIn(b"/song/shdeep", resp.data)

    def test_you_both_love_sits_between_chart_and_category_lists(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        chartIdx = resp.data.index(b'id="comparisonTrendChart"')
        sharedIdx = resp.data.index(b"You Both Love")
        listsIdx = resp.data.index(b'data-category="top-songs"')
        self.assertLess(chartIdx, sharedIdx)
        self.assertLess(sharedIdx, listsIdx)

    def test_filter_badges_render_for_each_category(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        for marker in (b'data-filter="all"', b'data-filter="top-songs"',
                       b'data-filter="top-artists"', b'data-filter="top-albums"',
                       b'data-category="top-artists"', b'data-category="top-albums"'):
            self.assertIn(marker, resp.data)

    def test_stats_table_styling_is_class_based(self):
        """Row borders moved from inline styles to .compare-table so the last
        row's bottom border can be dropped via :last-child."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b"compare-table", resp.data)
        self.assertNotIn(b'<tr style="border-bottom', resp.data)

    def test_track_cards_wrap_number_and_cover_in_a_media_column(self):
        self._accept("alice", "bob")
        self.dbs["alice"].getTopSongs.return_value = [_song("s1", "AliceTopSong")]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'class="track-media"', resp.data)

    def test_trend_groupby_auto_coarsens_with_the_range_span(self):
        """Without an explicit groupBy, day buckets over a 5-year range mean
        ~1800 sub-pixel points - the trend picks day/week/month from the span."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        for interval, expected in (("month", "day"), ("year", "week"), ("5years", "month")):
            client.get(f"/compare?interval={interval}")
            self.assertEqual(
                self.dbs["alice"].getListeningTimeSeries.call_args.kwargs["groupBy"],
                expected, f"interval={interval}")

    def test_explicit_groupby_param_overrides_the_auto_choice(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        client.get("/compare?interval=5years&groupBy=day")

        self.assertEqual(self.dbs["alice"].getListeningTimeSeries.call_args.kwargs["groupBy"], "day")

    def test_ajax_returns_partial_chunks_not_a_full_page(self):
        """The filter controls swap regions in place via ?ajax=true, mirroring
        the Wrapped page's fade-and-swap pattern."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopSongs.return_value = [_song("s1", "MyAjaxSong")]
        client = self._loginAs("alice")

        resp = client.get("/compare?ajax=true")

        data = resp.get_json()
        for key in ("withUsername", "statsTableHtml", "similaritiesHtml", "sharedArtistsHtml",
                    "myTopSongsHtml", "theirTopSongsHtml", "myTopArtistsHtml",
                    "theirTopArtistsHtml", "myTopAlbumsHtml", "theirTopAlbumsHtml",
                    "comparisonTrend"):
            self.assertIn(key, data)
        self.assertEqual(data["withUsername"], "bob")
        self.assertIn("MyAjaxSong", data["myTopSongsHtml"])
        self.assertIn("compare-table", data["statsTableHtml"])
        self.assertNotIn("<html", data["statsTableHtml"].lower())   #< chunks, not a page

    def test_ajax_counterpart_lists_stay_unlinked_from_detail_pages(self):
        self._accept("alice", "bob")
        self.dbs["bob"].getTopSongs.return_value = [_song("their-song-1", "TheirSong")]
        client = self._loginAs("alice")

        resp = client.get("/compare?ajax=true")

        data = resp.get_json()
        self.assertIn("TheirSong", data["theirTopSongsHtml"])
        self.assertNotIn("/song/their-song-1", data["theirTopSongsHtml"])

    def test_ajax_requires_an_accepted_share_like_the_full_page(self):
        client = self._loginAs("alice")   #< no accepted shares

        resp = client.get("/compare?ajax=true")

        self.assertEqual(resp.status_code, 404)

    def test_share_status_is_computed_once_per_request(self):
        """One request can render several templates (the Wrapped AJAX endpoint
        renders six partials) and every render re-runs all context processors -
        the share-existence query must be memoized on flask.g."""
        with patch.object(self.dash.repo, 'hasAnyAcceptedShare', return_value=True) as mock_check:
            with self.dash.app.test_request_context("/"):
                from flask import session as flaskSession
                flaskSession["username"] = "alice"
                processors = self.dash.app.template_context_processors[None]
                for _ in range(2):   #< two simulated render_template calls
                    for processor in processors:
                        processor()

        self.assertEqual(mock_check.call_count, 1)


if __name__ == "__main__":
    unittest.main()
