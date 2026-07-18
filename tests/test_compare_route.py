"""GET /compare: a Compare page only visible/reachable for users with at
least one accepted mutual share - see app.py's comparePage() route.
"""
import re
import unittest
from unittest.mock import patch, MagicMock

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import app as appModule
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


def _tasteMatchFromResponse(resp):
    match = re.search(rb'taste-match-value js-taste-match">(\d+)%', resp.data)
    return match.group(1) if match else None


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

    def test_404_when_data_sharing_is_disabled(self):
        """The admin's instance-wide kill switch blocks the route outright,
        even for a user with a real accepted share already in the DB."""
        self._accept("alice", "bob")
        self.dash.repo.setDataSharingEnabled(False)
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
        """The Top Common intersection runs over the 100-deep pools, not
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
        # artist must appear in bob's column AND in Top Common Artists, but NOT
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

    def test_default_time_window_setting_is_used_when_no_interval_given(self):
        """Compare's initial view must honor the user's saved
        default_dashboard_window profile setting (like the dashboard route
        already does), not hardcode All Time."""
        self._accept("alice", "bob")
        self.dash.repo.updateUserSettings("alice", "month", None)
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'<option value="month" selected>Last Month</option>', resp.data)

    def test_explicit_interval_param_overrides_the_default_time_window_setting(self):
        self._accept("alice", "bob")
        self.dash.repo.updateUserSettings("alice", "month", None)
        client = self._loginAs("alice")

        resp = client.get("/compare?interval=week")

        self.assertIn(b'<option value="week" selected>Last Week</option>', resp.data)

    def test_default_time_window_all_time_setting_maps_to_compares_all_time_option(self):
        """The profile setting stores the literal 'all time' string, but
        Compare's own dropdown represents All Time as an empty value -
        the two conventions must be reconciled."""
        self._accept("alice", "bob")
        self.dash.repo.updateUserSettings("alice", "all time", None)
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'<option value="" selected>All Time</option>', resp.data)

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

    def test_shared_artists_ranked_by_combined_plays_not_the_viewers_own_order(self):
        """Bug: 'Top Common Artists' used to be built by walking the VIEWER's
        own pool order and slicing to the first `limit` shared matches - so
        the same mutual-share pair could see a DIFFERENT top-10 shared set
        depending on who was viewing. Fixed by ranking the shared
        intersection by shared rank-weighted score (see _sharedRankScore),
        symmetric under swapping myPool/theirPool, with combined plays as
        the first tiebreak - either way, not either side's own pool order.

        11 shared artists: in alice's OWN rank order, 'LowCombined' (her
        plays=2) would beat 'HighCombined' (her plays=1) into the top ten,
        even though HighCombined's COMBINED total (1+100=101, bob loves it)
        dwarfs LowCombined's (2+1=3, neither loves it much). The old
        viewer-order slice would cut HighCombined; the new combined-plays
        ranking must cut LowCombined instead."""
        self._accept("alice", "bob")
        filler = [_artist(f"f{i}", f"Filler{i}", plays=50) for i in range(9)]
        self.dbs["alice"].getTopArtists.return_value = filler + [
            _artist("low", "LowCombined", plays=2),
            _artist("high", "HighCombined", plays=1),
        ]
        self.dbs["bob"].getTopArtists.return_value = filler + [
            _artist("low", "LowCombined", plays=1),
            _artist("high", "HighCombined", plays=100),
        ]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        commonSection = resp.data[
            resp.data.index(b'data-category="common-top-artists"'):
            resp.data.index(b'data-category="common-top-albums"')]
        self.assertIn(b"HighCombined", commonSection)
        self.assertNotIn(b"LowCombined", commonSection)

    def test_shared_artist_overlap_is_capped_like_every_other_list(self):
        """The Top Common lists are built from the 100-deep pools but must render at
        most COMPARE_TOP_LIST_SIZE cards, matching the adjacent lists."""
        self._accept("alice", "bob")
        sharedPool = [_artist(f"s{i}", f"SharedArtist{i}", plays=12 - i) for i in range(12)]
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

    def test_peak_day_is_a_stats_table_column_not_a_separate_card(self):
        """Top Day moved out of its own card (which had no cover art and
        wasted a full grid track next to Top Song/Artist/Album) into the
        Stats Comparison table as a "Peak Day" column beside "Peak Hour"."""
        self._accept("alice", "bob")
        grid = _zeroHeatmapGrid()
        grid[2][14] = {"totalTimeListened": 999, "plays": 5}   #< Wednesday 14:00
        self.dbs["alice"].getHourOfDayHeatmap.return_value = grid
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'<th class="value">Peak Day</th>', resp.data)
        self.assertIn(b"Wednesday", resp.data)
        self.assertNotIn(b"<h3>Top Day</h3>", resp.data)

    def test_songs_and_artists_columns_drop_the_unique_qualifier(self):
        """"Unique Songs"/"Unique Artists" read as clutter in the transposed
        header - just "Songs"/"Artists" now, with the "unique/distinct count"
        detail moved to a hover hint instead of dropped outright."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")
        body = resp.data.decode()

        self.assertNotIn("Unique", body)
        self.assertIn('<th class="value" title="Number of distinct songs played">Songs</th>', body)
        self.assertIn('<th class="value" title="Number of distinct artists played">Artists</th>', body)

    def test_identical_top_song_merges_into_one_bigger_row(self):
        """Both users' #1 song is the exact same track: the Top Song card
        must collapse into one merged row (topItemMerged) instead of two
        near-identical stacked rows repeating the same cover and name."""
        self._accept("alice", "bob")
        sameSong = _song("shared1", "SharedTopSong")
        self.dbs["alice"].getTopSongs.return_value = [sameSong]
        self.dbs["bob"].getTopSongs.return_value = [dict(sameSong)]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        topSongCard = resp.data[
            resp.data.index(b"<h3>Top Song</h3>"):
            resp.data.index(b"<h3>Top Artist</h3>")]
        self.assertIn(b"compare-top-side--merged", topSongCard)
        self.assertEqual(topSongCard.count(b"SharedTopSong"), 1)
        self.assertIn(b'compare-user-mine compare-user-label js-my-username">alice</span>', topSongCard)
        self.assertIn(b'compare-user-theirs compare-user-label js-with-username">bob</span>', topSongCard)

    def test_different_top_songs_render_two_separate_rows(self):
        """The common case: different #1 songs must keep the existing
        side-by-side layout, not the merged one."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopSongs.return_value = [_song("s1", "AliceSong")]
        self.dbs["bob"].getTopSongs.return_value = [_song("s2", "BobSong")]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        topSongCard = resp.data[
            resp.data.index(b"<h3>Top Song</h3>"):
            resp.data.index(b"<h3>Top Artist</h3>")]
        self.assertNotIn(b"compare-top-side--merged", topSongCard)
        self.assertIn(b"AliceSong", topSongCard)
        self.assertIn(b"BobSong", topSongCard)

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
        # the cover next to each cell links to the same detail page (skipped
        # for keyboard/screen-reader users - the name link sits right there)
        self.assertIn(b'<a class="compare-cover-link" href="/song/s1"', resp.data)
        self.assertIn(b'<a class="compare-cover-link" href="/artist/f1"', resp.data)
        self.assertIn(b'<a class="compare-cover-link" href="/album/al1"', resp.data)

    def test_summary_rows_link_counterpart_items_to_spotify(self):
        """A counterpart summary cell links to Spotify only when the viewer
        has ZERO plays of that item (a detail link would dead-end then) -
        alice's stub db reports no plays of anything here - and only when a
        real URL exists (fabricated ids carry an empty url)."""
        self._accept("alice", "bob")
        self.dbs["bob"].getTopSongs.return_value = [
            _song("their-song-1", "TheirSong", url="https://open.spotify.com/track/xyz1")]
        self.dbs["bob"].getTopArtists.return_value = [_artist("their-artist-1", "TheirArtist", url="")]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'href="https://open.spotify.com/track/xyz1" target="_blank"', resp.data)
        self.assertNotIn(b"/song/their-song-1", resp.data)
        self.assertNotIn(b"/artist/their-artist-1", resp.data)   #< empty url: plain text, no link
        # the cover mirrors the cell: Spotify link for the song, and the
        # empty-url artist cover renders unlinked (song is the only linked one)
        self.assertIn(b'<a class="compare-cover-link" href="https://open.spotify.com/track/xyz1" target="_blank"', resp.data)
        self.assertEqual(resp.data.count(b"compare-cover-link"), 1)

    def test_counterpart_card_titles_link_to_spotify_when_url_exists(self):
        """Card-title variant of the zero-data rule above: alice's stub db
        reports no plays of bob's song, so it links out to Spotify."""
        self._accept("alice", "bob")
        self.dbs["bob"].getTopSongs.return_value = [
            _song("their-song-1", "TheirSong", url="https://open.spotify.com/track/xyz1")]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        # external cover link marker (internal covers have no target attr)
        self.assertIn(b'class="track-cover-link" target="_blank"', resp.data)
        self.assertNotIn(b"/song/their-song-1", resp.data)

    def test_counterpart_items_the_viewer_played_link_to_their_own_detail_pages(self):
        """The viewer may well have their own plays of a counterpart's top
        item without it ranking in their own displayed lists - their detail
        page then has real data to show, so the card links there like any
        own item instead of bouncing out to Spotify."""
        self._accept("alice", "bob")
        self.dbs["bob"].getTopSongs.return_value = [
            _song("known-song", "KnownSong", url="https://open.spotify.com/track/xyz1")]
        self.dbs["bob"].getTopArtists.return_value = [
            _artist("known-artist", "KnownArtist", url="https://open.spotify.com/artist/xyz2")]
        self.dbs["bob"].getTopAlbums.return_value = [
            _album("known-album", "KnownAlbum", url="https://open.spotify.com/album/xyz3")]
        self.dbs["alice"].getPlayedTrackIds.return_value = {"known-song"}
        self.dbs["alice"].getPlayedArtistIds.return_value = {"known-artist"}
        self.dbs["alice"].getPlayedAlbumIds.return_value = {"known-album"}
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b"/song/known-song", resp.data)
        self.assertIn(b"/artist/known-artist", resp.data)
        self.assertIn(b"/album/known-album", resp.data)
        #< the summary-card covers follow the same rule and link internally
        self.assertIn(b'<a class="compare-cover-link" href="/song/known-song"', resp.data)
        self.assertIn(b'<a class="compare-cover-link" href="/artist/known-artist"', resp.data)
        self.assertIn(b'<a class="compare-cover-link" href="/album/known-album"', resp.data)
        #< no external card links anywhere: every counterpart item resolves
        #  internally (the "Open in Spotify" attribute label is separate and
        #  carries class track-label, not track-cover-link)
        self.assertNotIn(b'class="track-cover-link" target="_blank"', resp.data)

    def test_played_lookup_is_batched_over_the_displayed_counterpart_ids(self):
        """One query per category over exactly the displayed items - not one
        query per card."""
        self._accept("alice", "bob")
        self.dbs["bob"].getTopSongs.return_value = [
            _song("ts1", "TheirSong1"), _song("ts2", "TheirSong2")]
        client = self._loginAs("alice")

        client.get("/compare")

        self.dbs["alice"].getPlayedTrackIds.assert_called_once_with(["ts1", "ts2"])
        self.dbs["alice"].getPlayedArtistIds.assert_called_once_with([])
        self.dbs["alice"].getPlayedAlbumIds.assert_called_once_with([])

    def test_shared_artist_cards_show_both_users_stats(self):
        """Top Common cards lead with the COMBINED plays/time (the
        per-user numbers live in the versus block below), plus a split bar
        proportioned by time."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopArtists.return_value = [
            _artist("sh1", "SharedArtist", plays=10, totalTimeListened=3_600_000, uniqueSongCount=4)]
        self.dbs["bob"].getTopArtists.return_value = [
            _artist("sh1", "SharedArtist", plays=5, totalTimeListened=1_800_000, uniqueSongCount=2)]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b"15 plays", resp.data)        #< combined, on the card's top stat line
        self.assertIn(b"1h 30m 0s", resp.data)       #< combined listening time
        self.assertIn(b"alice: 10 plays", resp.data)
        self.assertIn(b"bob: 5 plays", resp.data)
        #< no separate "Together" line in the versus block - the combined
        #  totals already lead the card's top stat line (asserted above)
        self.assertNotIn(b"Together:", resp.data)
        self.assertIn(b'style="width: 67%"', resp.data)   #< round(3.6/5.4*100)
        # The versus block must appear ONLY on the shared card - the same dict
        # also feeds alice's own Top Artists column, so it must be copied
        # before the comparison data is attached.
        self.assertEqual(resp.data.count(b"compare-split-bar\""), 1)
        #< the copy also keeps the combined totals off her own column's card
        self.assertIn(b"10 plays", resp.data)

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

        # Neither column displays it (rank 11 on both sides), so it shows in
        # the "Common Top Song" similarity cell (1 mention) plus its shared-
        # songs card (img alt + h3 = 2) - linked to the viewer's own detail
        # page, which resolves since she played it.
        self.assertEqual(resp.data.count(b"DeepSharedSong"), 3)
        self.assertIn(b"/song/shdeep", resp.data)

    def test_shared_songs_and_albums_render_with_versus_data(self):
        self._accept("alice", "bob")
        self.dbs["alice"].getTopSongs.return_value = [
            _song("sh-song", "SharedSong", plays=3, totalTimeListened=60_000)]
        self.dbs["bob"].getTopSongs.return_value = [
            _song("sh-song", "SharedSong", plays=8, totalTimeListened=30_000)]
        self.dbs["alice"].getTopAlbums.return_value = [
            _album("sh-alb", "SharedAlbum", plays=4, totalTimeListened=120_000, uniqueSongCount=2)]
        self.dbs["bob"].getTopAlbums.return_value = [
            _album("sh-alb", "SharedAlbum", plays=6, totalTimeListened=240_000, uniqueSongCount=3)]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b"alice: 3 plays", resp.data)   #< shared song, mine (versus block)
        self.assertIn(b"11 plays", resp.data)         #< shared song top line: combined plays
        self.assertIn(b"1m 30s", resp.data)           #< ...and combined time
        self.assertIn(b"10 plays", resp.data)         #< shared album combined plays
        self.assertIn(b"6m 0s", resp.data)            #< shared album combined time
        self.assertIn("alice: 4 plays · 2m 0s · 2 songs".encode("utf-8"), resp.data)   #< versus keeps song counts
        # the viewer-specific "You played N songs from X" line stays on the
        # album card in alice's own column (1 mention) but is replaced by the
        # versus block's per-user counts on the shared card (not 2)
        self.assertEqual(resp.data.count(b"You played 2 songs from SharedAlbum"), 1)
        # song cards carry no unique-song counts - the segment is omitted,
        # not rendered as "0 songs"
        self.assertNotIn(b"0 songs", resp.data)
        #< one split bar per shared card: song + album (no shared artists here)
        self.assertEqual(resp.data.count(b'class="compare-split-bar"'), 2)

    def test_shared_song_and_album_lists_are_capped(self):
        self._accept("alice", "bob")
        sharedSongs = [_song(f"s{i}", f"CapSong{i}", plays=12 - i) for i in range(12)]
        self.dbs["alice"].getTopSongs.return_value = sharedSongs
        self.dbs["bob"].getTopSongs.return_value = sharedSongs
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b"CapSong9", resp.data)
        self.assertNotIn(b"CapSong10", resp.data)
        self.assertNotIn(b"CapSong11", resp.data)

    def test_ajax_includes_shared_song_and_album_chunks(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare?ajax=true")

        data = resp.get_json()
        self.assertIn("sharedSongsHtml", data)
        self.assertIn("sharedAlbumsHtml", data)

    def test_similarities_sit_above_the_chart_and_shared_lists_join_categories(self):
        """Common Top Artist/Song/Album cards come directly above the trend
        chart; the shared lists are filterable Top Common Songs/Artists/
        Albums categories ahead of the per-user top lists."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        chartIdx = resp.data.index(b'id="comparisonTrendChart"')
        similaritiesIdx = resp.data.index(b'id="compareSimilarities"')
        commonSongsIdx = resp.data.index(b'data-category="common-top-songs"')
        commonArtistsIdx = resp.data.index(b'data-category="common-top-artists"')
        commonAlbumsIdx = resp.data.index(b'data-category="common-top-albums"')
        listsIdx = resp.data.index(b'data-category="top-songs"')
        self.assertLess(similaritiesIdx, chartIdx)
        self.assertLess(chartIdx, commonSongsIdx)
        self.assertLess(commonSongsIdx, commonArtistsIdx)
        self.assertLess(commonArtistsIdx, commonAlbumsIdx)
        self.assertLess(commonAlbumsIdx, listsIdx)

    def test_similarity_card_covers_link_to_the_viewers_detail_pages(self):
        """The Common Top cards' covers are linked just like the stats-table
        cards' - similarity items come from the viewer's own pool, so their
        detail pages always resolve."""
        self._accept("alice", "bob")
        # distinct dicts per user (like real queries return) - bob's copy gets
        # linkExternally attached in place, which must not taint alice's
        self.dbs["alice"].getTopSongs.return_value = [_song("shsong", "CommonSong")]
        self.dbs["bob"].getTopSongs.return_value = [_song("shsong", "CommonSong")]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        # once on the stats-table Top Song card (mine side), once on the
        # Common Top Song similarity card
        self.assertEqual(
            resp.data.count(b'<a class="compare-cover-link" href="/song/shsong"'), 2)

    def test_filter_badges_render_for_each_category(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        for marker in (b'data-filter="all"',
                       b'data-filter="common-top-songs"',
                       b'data-filter="common-top-artists"',
                       b'data-filter="common-top-albums"',
                       b'data-filter="top-songs"',
                       b'data-filter="top-artists"', b'data-filter="top-albums"',
                       b'data-category="common-top-songs"',
                       b'data-category="common-top-artists"',
                       b'data-category="common-top-albums"',
                       b'data-category="top-artists"', b'data-category="top-albums"'):
            self.assertIn(marker, resp.data)
        #< the combined "You Both Love" category is gone - each shared list
        #  filters on its own
        self.assertNotIn(b"you-both-love", resp.data)
        self.assertNotIn(b"You Both Love", resp.data)

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

    def test_trend_bucket_dropdown_defaults_to_auto(self):
        """The visible groupBy control: Auto (empty value, server derives the
        bucketing from the range span) is preselected unless an explicit
        groupBy was passed."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")
        respExplicit = client.get("/compare?groupBy=week")

        self.assertIn(b'id="groupBy"', resp.data)
        self.assertIn(b'<option value="" selected>Auto</option>', resp.data)
        self.assertIn(b'<option value="week" selected>Week</option>', respExplicit.data)

    def test_explicit_groupby_param_overrides_the_auto_choice(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        client.get("/compare?interval=5years&groupBy=day")

        self.assertEqual(self.dbs["alice"].getListeningTimeSeries.call_args.kwargs["groupBy"], "day")

    def test_user_identity_colors_mark_table_headers_and_column_headings(self):
        """"You" is always the accent color, "them" always --compare-theirs -
        the classes must appear on the table headers and both columns'
        headings so every section reads with the same color mapping as the
        trend chart."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'compare-user-mine compare-user-label js-my-username">alice</span>', resp.data)
        self.assertIn(b'compare-user-theirs compare-user-label js-with-username">bob</span>', resp.data)
        #< seven mine-labels: table row header + three column headings + the
        #  three Top Song/Artist/Album card sides under the stats table (Top
        #  Day moved into the stats table itself as a Peak Day column)
        self.assertEqual(resp.data.count(b"compare-user-mine"), 7)
        #< theirs additionally colors the hero name (no label dot there)
        self.assertEqual(resp.data.count(b"compare-user-theirs"), 8)

    def test_limit_param_controls_displayed_list_sizes(self):
        """The dropdown slices the displayed lists (and shared lists) deeper
        into the same 100-deep pools - the pools themselves stay fixed since
        the overlap/similarity math needs their full depth."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopArtists.return_value = [
            _artist(f"pa{i}", f"PagedArtist{i}") for i in range(30)]
        client = self._loginAs("alice")

        resp = client.get("/compare?limit=25")

        self.assertIn(b"PagedArtist24", resp.data)
        self.assertNotIn(b"PagedArtist25", resp.data)

    def test_invalid_limit_falls_back_to_the_default(self):
        self._accept("alice", "bob")
        self.dbs["alice"].getTopArtists.return_value = [
            _artist(f"pa{i}", f"PagedArtist{i}") for i in range(30)]
        client = self._loginAs("alice")

        resp = client.get("/compare?limit=13")

        self.assertIn(b"PagedArtist9", resp.data)
        self.assertNotIn(b"PagedArtist10", resp.data)

    def test_limit_applies_to_the_shared_lists_too(self):
        self._accept("alice", "bob")
        sharedSongs = [_song(f"s{i}", f"CapSong{i}", plays=30 - i) for i in range(30)]
        self.dbs["alice"].getTopSongs.return_value = sharedSongs
        self.dbs["bob"].getTopSongs.return_value = sharedSongs
        client = self._loginAs("alice")

        resp = client.get("/compare?limit=25")

        self.assertIn(b"CapSong24", resp.data)
        self.assertNotIn(b"CapSong25", resp.data)

    def test_items_per_category_dropdown_renders(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'id="limit"', resp.data)
        self.assertIn(b'<option value="10" selected>10</option>', resp.data)
        self.assertIn(b'<option value="100" >100</option>', resp.data)

    def test_default_sort_reuses_the_shared_pool_for_display_without_an_extra_query(self):
        """sortBy defaults to 'plays' - a single COMPARE_SHARED_POOL_SIZE-deep
        query per category serves BOTH the displayed my/their top lists
        (sliced to `limit`) AND taste-match's topXPool (sliced to
        COMPARE_OVERLAP_POOL_SIZE): the top 100 of a 200-row plays-ranked
        result is identical to a dedicated top-100 query, so there's no need
        to fetch it twice. Only one live query per category, same as before
        the deeper shared-pool feature existed - just at a deeper LIMIT."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        client.get("/compare")

        self.assertEqual(self.dbs["alice"].getTopSongs.call_count, 1)
        self.assertEqual(self.dbs["alice"].getTopArtists.call_count, 1)
        self.assertEqual(self.dbs["alice"].getTopAlbums.call_count, 1)
        self.assertEqual(
            self.dbs["alice"].getTopSongs.call_args.kwargs.get("limit"),
            appModule.COMPARE_SHARED_POOL_SIZE)

    def test_sort_by_param_requeries_the_individual_top_lists(self):
        """Choosing a non-default sort re-fetches the displayed my/their Top
        Songs/Artists/Albums lists at that metric (membership AND order both
        reflect it, matching Top Songs page's own behavior) - the shared/
        taste-match pool query (COMPARE_SHARED_POOL_SIZE, plays-ranked)
        stays untouched."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        client.get("/compare?sortBy=totalTimeListened")

        calls = self.dbs["alice"].getTopSongs.call_args_list
        self.assertEqual(len(calls), 2)
        poolCall = next(c for c in calls if c.kwargs.get("limit") == appModule.COMPARE_SHARED_POOL_SIZE)
        self.assertEqual(poolCall.kwargs.get("by", "plays"), "plays")
        displayCall = next(c for c in calls if c.kwargs.get("limit") == appModule.COMPARE_TOP_LIST_SIZE)
        self.assertEqual(displayCall.kwargs.get("by"), "totalTimeListened")

    def test_shared_lists_search_deeper_than_the_taste_match_pool(self):
        """Top Common Songs/Artists/Albums search COMPARE_SHARED_POOL_SIZE
        deep - wider than COMPARE_OVERLAP_POOL_SIZE, which taste-match's
        pools stay pinned to (derived as the shared pool's first
        COMPARE_OVERLAP_POOL_SIZE entries). A mutual favorite ranked beyond
        that cutoff on just ONE side used to be invisible to the
        intersection entirely - that side's 100-deep-only pool didn't have
        it at all, exact-match or not. The deeper search finds it."""
        self._accept("alice", "bob")
        mutual = _artist("mutual", "MutualFavorite")
        bobFiller = [_artist(f"bf{i}", f"BobFiller{i}") for i in range(appModule.COMPARE_OVERLAP_POOL_SIZE)]
        self.dbs["alice"].getTopArtists.return_value = [mutual]              #< alice's #1
        self.dbs["bob"].getTopArtists.return_value = bobFiller + [mutual]    #< bob's #101
        client = self._loginAs("alice")

        resp = client.get("/compare")

        commonSection = resp.data[
            resp.data.index(b'data-category="common-top-artists"'):
            resp.data.index(b'data-category="common-top-albums"')]
        self.assertIn(b"MutualFavorite", commonSection)

    def test_deeper_shared_pool_does_not_change_the_taste_match_score(self):
        """Widening the shared-item search must never move taste-match - its
        topArtistsPool is the shared pool's first COMPARE_OVERLAP_POOL_SIZE
        entries only. Compares two requests, identical except for one extra
        mutual favorite landing at rank 101 on both sides (within the Top
        Common search depth but past taste-match's cutoff) - if taste-match
        read the full shared pool, adding a brand new exact match would
        obviously move the score. It must not, even though the extra match
        DOES belong in the Top Common list."""
        self._accept("alice", "bob")
        aliceFiller = [_artist(f"af{i}", f"AliceFiller{i}") for i in range(appModule.COMPARE_OVERLAP_POOL_SIZE - 1)]
        bobFiller = [_artist(f"bf{i}", f"BobFiller{i}") for i in range(appModule.COMPARE_OVERLAP_POOL_SIZE - 1)]
        baseAlice = [_artist("top", "SharedTop")] + aliceFiller   #< 100 items
        baseBob = [_artist("top", "SharedTop")] + bobFiller       #< 100 items
        client = self._loginAs("alice")

        self.dbs["alice"].getTopArtists.return_value = baseAlice
        self.dbs["bob"].getTopArtists.return_value = baseBob
        respWithout = client.get("/compare")

        extraMatch = _artist("extra-deep-match", "ExtraDeepMatch")   #< rank 101 on both sides
        self.dbs["alice"].getTopArtists.return_value = baseAlice + [extraMatch]
        self.dbs["bob"].getTopArtists.return_value = baseBob + [extraMatch]
        respWith = client.get("/compare")

        tasteMatchWithout = _tasteMatchFromResponse(respWithout)
        tasteMatchWith = _tasteMatchFromResponse(respWith)
        self.assertIsNotNone(tasteMatchWithout)
        self.assertEqual(tasteMatchWithout, tasteMatchWith)

        commonSection = respWith.data[
            respWith.data.index(b'data-category="common-top-artists"'):
            respWith.data.index(b'data-category="common-top-albums"')]
        self.assertIn(b"ExtraDeepMatch", commonSection)

    def test_invalid_sort_by_falls_back_to_plays(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        client.get("/compare?sortBy=bogus")

        #< no extra display query at the "plays" default - the one
        #  COMPARE_SHARED_POOL_SIZE-deep query serves both taste-match's
        #  sliced topXPool and the displayed list
        self.assertEqual(self.dbs["alice"].getTopSongs.call_count, 1)

    def test_sort_by_dropdown_renders_and_preselects(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare?sortBy=totalTimeListened")

        self.assertIn(b'id="sortBy"', resp.data)
        self.assertIn(b'<option value="totalTimeListened" selected>Time Played</option>', resp.data)
        self.assertIn(b'<option value="plays" >Number of Plays</option>', resp.data)

    def test_sort_by_dropdown_defaults_to_plays(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'<option value="plays" selected>Number of Plays</option>', resp.data)

    def test_sort_by_name_is_not_offered_and_falls_back_to_plays(self):
        """Compare's dropdown doesn't offer "Name (A-Z)" (unlike Top Songs/
        Artists/Albums) - there's no sensible combined-both-users alphabetical
        ranking for the Top Common lists (see app.py's COMPARE_SORT_BY). An
        explicit ?sortBy=name must fall back to the "plays" default rather
        than reaching the DB layer with an unsupported value."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare?sortBy=name")

        self.assertNotIn(b"Name (A-Z)", resp.data)
        self.assertIn(b'<option value="plays" selected>Number of Plays</option>', resp.data)
        #< no extra display query at the "plays" default - the one
        #  COMPARE_SHARED_POOL_SIZE-deep query serves both taste-match's
        #  sliced topXPool and the displayed list
        self.assertEqual(self.dbs["alice"].getTopSongs.call_count, 1)

    def test_sort_by_does_not_reorder_the_shared_common_lists(self):
        """sortBy only reorders the individual my/their columns (see
        _gatherCompareStats) - the Top Common (shared) lists rank by a fixed
        shared-rank-weighted score (see _buildSharedItems/_sharedRankScore)
        that never reads sortBy, so picking a different metric here must
        leave their order untouched. The taste-match score stays unaffected
        too: it runs over the fixed plays-ranked overlap pool regardless of
        sortBy."""
        self._accept("alice", "bob")
        pool = [
            _artist(f"sa{i}", f"SharedA{i}", plays=10 - i, totalTimeListened=i * 100000)
            for i in range(5)
        ]
        self.dbs["alice"].getTopArtists.return_value = list(pool)
        self.dbs["bob"].getTopArtists.return_value = list(pool)
        client = self._loginAs("alice")

        byPlays = client.get("/compare")
        byTime = client.get("/compare?sortBy=totalTimeListened")

        def commonSection(resp):
            return resp.data[
                resp.data.index(b'data-category="common-top-artists"'):
                resp.data.index(b'data-category="common-top-albums"')]

        playsSection = commonSection(byPlays)
        timeSection = commonSection(byTime)
        #< SharedA0 has the most plays but the least totalTimeListened, and
        #  vice versa for SharedA4 - if sortBy leaked into the shared-list
        #  ranking, ?sortBy=totalTimeListened would flip their relative
        #  order; it must not.
        self.assertLess(playsSection.index(b"SharedA0"), playsSection.index(b"SharedA4"))
        self.assertLess(timeSection.index(b"SharedA0"), timeSection.index(b"SharedA4"))
        self.assertIn(b'class="taste-match-value js-taste-match">100%</span>', byTime.data)

    def test_shared_list_ties_break_by_combined_time_played(self):
        """Reaching the combined-time tiebreak takes a genuine tie on the
        two legs before it: Zulu is alice's #1 / bob's #2 while Alpha is
        bob's #1 / alice's #2, so both score rankWeight(1) + rankWeight(2)
        (see _sharedRankScore), and combined plays tie at 15 apiece. Zulu's
        higher combined totalTimeListened ("Time Played") must then win -
        against the later name leg (Alpha sorts first alphabetically) and
        against input-pool order (Alpha is listed first in both pools), so
        only the combined-time leg can produce this order."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopArtists.return_value = [
            _artist("aq", "Alpha", plays=5, totalTimeListened=1000),
            _artist("zt", "Zulu", plays=10, totalTimeListened=5000),
        ]
        self.dbs["bob"].getTopArtists.return_value = [
            _artist("aq", "Alpha", plays=10, totalTimeListened=1000),
            _artist("zt", "Zulu", plays=5, totalTimeListened=5000),
        ]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        section = resp.data[
            resp.data.index(b'data-category="common-top-artists"'):
            resp.data.index(b'data-category="common-top-albums"')]
        self.assertLess(section.index(b"Zulu"), section.index(b"Alpha"))

    def test_shared_list_full_ties_fall_back_to_name(self):
        """Shared artists tied through shared-rank score (cross-side #1s,
        as in the combined-time test), combined plays AND combined time
        fall back to alphabetical name. Alpha must win via its NAME alone:
        input-pool order lists Zeta first on both sides, and Zeta's id
        ("a1") sorts before Alpha's ("z9"), so neither pool order nor the
        final id leg can produce this order."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopArtists.return_value = [
            _artist("a1", "Zeta", plays=10, totalTimeListened=1000),
            _artist("z9", "Alpha", plays=5, totalTimeListened=1000),
        ]
        self.dbs["bob"].getTopArtists.return_value = [
            _artist("a1", "Zeta", plays=5, totalTimeListened=1000),
            _artist("z9", "Alpha", plays=10, totalTimeListened=1000),
        ]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        section = resp.data[
            resp.data.index(b'data-category="common-top-artists"'):
            resp.data.index(b'data-category="common-top-albums"')]
        self.assertLess(section.index(b"Alpha"), section.index(b"Zeta"))

    def test_shared_list_ranks_by_mutual_favorite_not_raw_combined_plays(self):
        """MutualFavorite: alice's #1 (plays=1000), but bob ranks it last among
        his 3 (plays=1) - rank-discount summing (see _sharedRankScore) credits
        it rankWeight(1)+rankWeight(3) = 1.5 via alice's #1. ModerateBoth: #2
        on both sides (never anyone's favorite) - 2*rankWeight(2) = ~1.26,
        even though its combined plays (1800) beat MutualFavorite's (1001),
        so the OLD combined-sum ranking would have put it first. The
        rank-weighted ranking must put MutualFavorite first instead."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopArtists.return_value = [
            _artist("mutual", "MutualFavorite", plays=1000),
            _artist("moderate", "ModerateBoth", plays=900),
        ]
        self.dbs["bob"].getTopArtists.return_value = [
            _artist("bobfav", "BobsOwnFavorite", plays=1000),   #< not shared with alice
            _artist("moderate", "ModerateBoth", plays=900),
            _artist("mutual", "MutualFavorite", plays=1),
        ]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        section = resp.data[
            resp.data.index(b'data-category="common-top-artists"'):
            resp.data.index(b'data-category="common-top-albums"')]
        self.assertLess(section.index(b"MutualFavorite"), section.index(b"ModerateBoth"))

    def test_shared_list_one_sided_favorite_loses_to_true_mutual_item(self):
        """The sharp edge a min()-based mutual score would have: OneSided is
        alice's #1 but bob's #19 (plays=1, barely in his pool) - under
        min() it would score like a true #1/#1 and outrank everything.
        _sharedRankScore SUMS both sides' discounts instead, so OneSided
        earns rankWeight(1)+rankWeight(19) = ~1.23, and TrueMutual - #2 on
        BOTH sides, 2*rankWeight(2) = ~1.26 - must rank above it: an item
        one user barely plays is not the pair's top "common" favorite."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopArtists.return_value = [
            _artist("onesided", "OneSided", plays=1000),
            _artist("truemutual", "TrueMutual", plays=900),
        ]
        bobFillers = [_artist(f"bf{i}", f"BobFiller{i}", plays=800 - i * 10) for i in range(16)]
        self.dbs["bob"].getTopArtists.return_value = [
            _artist("bobfav", "BobsOwnFavorite", plays=1000),   #< not shared with alice
            _artist("truemutual", "TrueMutual", plays=900),
            *bobFillers,                                        #< bob ranks 3-18, not shared
            _artist("onesided", "OneSided", plays=1),           #< bob rank 19
        ]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        section = resp.data[
            resp.data.index(b'data-category="common-top-artists"'):
            resp.data.index(b'data-category="common-top-albums"')]
        self.assertLess(section.index(b"TrueMutual"), section.index(b"OneSided"))

    def test_shared_list_shared_score_ties_break_by_combined_plays(self):
        """Two shared artists tie on shared-rank score when each grabs rank
        #1 on a DIFFERENT side - Alpha is alice's #1 (bob ranks it #2),
        Beta is bob's #1 (alice ranks it #2), so both score rankWeight(1) +
        rankWeight(2) (see _sharedRankScore). Must not fall back to
        input-pool order (Beta is listed first in both pools) - combined
        plays (101 vs 52) breaks the tie in Alpha's favor."""
        self._accept("alice", "bob")
        self.dbs["alice"].getTopArtists.return_value = [
            _artist("beta", "Beta", plays=2),
            _artist("alpha", "Alpha", plays=100),
        ]
        self.dbs["bob"].getTopArtists.return_value = [
            _artist("beta", "Beta", plays=50),
            _artist("alpha", "Alpha", plays=1),
        ]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        section = resp.data[
            resp.data.index(b'data-category="common-top-artists"'):
            resp.data.index(b'data-category="common-top-albums"')]
        self.assertLess(section.index(b"Alpha"), section.index(b"Beta"))

    def test_taste_match_is_full_for_identical_pools(self):
        self._accept("alice", "bob")
        pool = [_artist(f"sa{i}", f"SharedA{i}") for i in range(10)]
        self.dbs["alice"].getTopArtists.return_value = pool
        self.dbs["bob"].getTopArtists.return_value = list(pool)
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'class="taste-match-value js-taste-match">100%</span>', resp.data)

    def test_taste_match_rewards_shared_favorites_over_deep_overlap(self):
        """Rank weighting (2x the shared item's better-side rank discount,
        normalized against identical pools), then the concave display curve
        (raw**TASTE_MATCH_CURVE_EXPONENT): sharing the #1 of two-item pools
        scores 2/(2*(1+1/log2(3)))=61% raw -> round(100*0.61^0.6)=75%
        displayed; sharing only the #2 scores 39% raw either way (both sides
        rank it #2, so the better-side pairing doesn't move it) ->
        round(100*0.39^0.6)=57% displayed."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        self.dbs["alice"].getTopArtists.return_value = [_artist("top", "SharedTop"), _artist("a2", "A2")]
        self.dbs["bob"].getTopArtists.return_value = [_artist("top", "SharedTop"), _artist("b2", "B2")]
        respTop = client.get("/compare")

        self.dbs["alice"].getTopArtists.return_value = [_artist("a1", "A1"), _artist("deep", "SharedDeep")]
        self.dbs["bob"].getTopArtists.return_value = [_artist("b1", "B1"), _artist("deep", "SharedDeep")]
        respDeep = client.get("/compare")

        self.assertIn(b'class="taste-match-value js-taste-match">75%</span>', respTop.data)
        self.assertIn(b'class="taste-match-value js-taste-match">57%</span>', respDeep.data)

    def test_taste_match_mutual_favorite_credited_at_its_better_rank(self):
        """A shared artist ranked #1 by one side and #10 by the other is
        credited at 2x the BETTER (shallower) side's rank discount - not the
        sum of both sides' discounts, which would badly punish the deep
        side. actual=2*w(1)=2, ideal=sum(2*w(1..10))=9.087 -> 22% raw ->
        round(100*0.22^0.6)=40% displayed. (The old sum-of-both-discounts
        approach would have scored this pair only 14%.)"""
        self._accept("alice", "bob")
        myArtists = [_artist("mutual", "Mutual")] + [_artist(f"a{i}", f"A{i}") for i in range(9)]
        theirArtists = [_artist(f"b{i}", f"B{i}") for i in range(9)] + [_artist("mutual", "Mutual")]
        self.dbs["alice"].getTopArtists.return_value = myArtists
        self.dbs["bob"].getTopArtists.return_value = theirArtists
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'class="taste-match-value js-taste-match">40%</span>', resp.data)

    def test_taste_match_credits_a_song_by_a_shared_artist_even_without_an_exact_match(self):
        """A song that isn't itself an exact match still earns
        ARTIST_MEDIATED_CREDIT_FACTOR of its rank discount when its primary
        artist is in the counterpart's top artist pool - loving the same
        ARTIST without happening to share the exact same song is real
        overlap, not zero. Isolated to the songs category alone (artists/
        albums pools empty on both sides -> excluded) so the two requests'
        difference is purely the artist-credit mechanism: fully unrelated
        songs score 0%; when alice's song's artist is in bob's top artists,
        credit=0.4*w(1)=0.4, ideal=2*w(1)=2 -> 20% raw ->
        round(100*0.2^0.6)=38% displayed."""
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        self.dbs["alice"].getTopSongs.return_value = [_song("as1", "AS1", artists=[_artist("ax1", "Unrelated1")])]
        self.dbs["bob"].getTopSongs.return_value = [_song("bs1", "BS1", artists=[_artist("bx1", "Unrelated2")])]
        respUnrelated = client.get("/compare")

        self.dbs["alice"].getTopSongs.return_value = [
            _song("as1", "AS1", artists=[_artist("shared1", "SharedArtist")])]
        self.dbs["bob"].getTopArtists.return_value = [_artist("shared1", "SharedArtist")]
        self.dbs["bob"].getTopSongs.return_value = [_song("bs1", "BS1", artists=[_artist("bx1", "Unrelated2")])]
        respRelated = client.get("/compare")

        self.assertIn(b'class="taste-match-value js-taste-match">0%</span>', respUnrelated.data)
        self.assertIn(b'class="taste-match-value js-taste-match">38%</span>', respRelated.data)

    def test_taste_match_weights_category_overlaps(self):
        """artists identical (1.0, weight .7), albums identical (1.0, weight
        .2), songs disjoint with no artist relation (0.0, weight .1) -> 90%
        raw -> round(100*0.9^0.6)=94% displayed."""
        self._accept("alice", "bob")
        artistPool = [_artist(f"sa{i}", f"SharedA{i}") for i in range(5)]
        self.dbs["alice"].getTopArtists.return_value = artistPool
        self.dbs["bob"].getTopArtists.return_value = list(artistPool)
        albumPool = [_album(f"sal{i}", f"SharedAl{i}") for i in range(5)]
        self.dbs["alice"].getTopAlbums.return_value = albumPool
        self.dbs["bob"].getTopAlbums.return_value = list(albumPool)
        self.dbs["alice"].getTopSongs.return_value = [_song(f"as{i}", f"AS{i}") for i in range(5)]
        self.dbs["bob"].getTopSongs.return_value = [_song(f"bs{i}", f"BS{i}") for i in range(5)]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'class="taste-match-value js-taste-match">94%</span>', resp.data)

    def test_taste_match_excludes_categories_without_data_on_both_sides(self):
        """Only artists have data: 5 shared at ranks 1-5 of 10 on both sides.
        Rank-weighted: sum(w(1..5)) / sum(w(1..10)) = 2.9485/4.5436 -> 65%
        raw, with the empty song/album categories excluded instead of
        dragging the score down -> round(100*0.65^0.6)=77% displayed."""
        self._accept("alice", "bob")
        sharedArtists = [_artist(f"sa{i}", f"SharedA{i}") for i in range(5)]
        self.dbs["alice"].getTopArtists.return_value = sharedArtists + [_artist(f"a{i}", f"A{i}") for i in range(5)]
        self.dbs["bob"].getTopArtists.return_value = sharedArtists + [_artist(f"b{i}", f"B{i}") for i in range(5)]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'class="taste-match-value js-taste-match">77%</span>', resp.data)

    def test_taste_match_caps_the_ideal_at_top_taste_match_ideal_depth(self):
        """Sharing an entire top-20 (out of 100-deep pools) should score high
        even though the other 80 slots are disjoint: the ideal is capped at
        TASTE_MATCH_IDEAL_DEPTH=30 rather than the full 100-deep pool, so
        agreement on core taste isn't diluted by requiring near-total
        long-tail overlap too. sum(w(1..20))/sum(w(1..30)) -> 77% raw ->
        round(100*0.77^0.6)=85% displayed."""
        self._accept("alice", "bob")
        sharedArtists = [_artist(f"sa{i}", f"SharedA{i}") for i in range(20)]
        self.dbs["alice"].getTopArtists.return_value = sharedArtists + [_artist(f"a{i}", f"A{i}") for i in range(80)]
        self.dbs["bob"].getTopArtists.return_value = sharedArtists + [_artist(f"b{i}", f"B{i}") for i in range(80)]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'class="taste-match-value js-taste-match">85%</span>', resp.data)

    def test_taste_match_hidden_without_any_pool_data(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'id="tasteMatch" style="display: none;"', resp.data)

    def test_ajax_includes_the_taste_match(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare?ajax=true")

        data = resp.get_json()
        self.assertIn("tasteMatch", data)
        self.assertIsNone(data["tasteMatch"])   #< empty stub pools -> hidden, not 0%

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

    def test_leader_cells_carry_a_non_color_marker(self):
        """The accent color alone can't mark the leading side for color-blind
        users or screen readers - the winning cell gets a ▲ plus hidden
        '(higher)' text, and only the winning cell."""
        self._accept("alice", "bob")
        self.dbs["alice"].getPlayTotals.return_value = (10, 1000)
        self.dbs["bob"].getPlayTotals.return_value = (5, 500)
        client = self._loginAs("alice")

        resp = client.get("/compare")

        marker = ' <span class="leader-marker" aria-hidden="true">▲</span><span class="visually-hidden">(higher)</span>'.encode("utf-8")
        #< alice leads plays AND time: exactly two marked cells
        self.assertEqual(resp.data.count(marker), 2)
        self.assertIn(b'class="value leader leader-mine">10', resp.data)

    def test_leader_color_follows_the_columns_identity(self):
        """A leading counterpart cell must carry the counterpart's identity
        color class, not the viewer's accent - one leader color for both
        columns broke the you-vs-them color mapping."""
        self._accept("alice", "bob")
        self.dbs["alice"].getPlayTotals.return_value = (5, 500)
        self.dbs["bob"].getPlayTotals.return_value = (12, 1200)
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'class="value leader leader-theirs">12', resp.data)
        self.assertNotIn(b'class="value leader leader-mine"', resp.data)   #< alice leads nothing here

    def test_split_bar_carries_an_accessible_description(self):
        self._accept("alice", "bob")
        self.dbs["alice"].getTopArtists.return_value = [
            _artist("sh1", "SharedArtist", plays=10, totalTimeListened=3_600_000)]
        self.dbs["bob"].getTopArtists.return_value = [
            _artist("sh1", "SharedArtist", plays=5, totalTimeListened=1_800_000)]
        client = self._loginAs("alice")

        resp = client.get("/compare")

        #< apostrophes render autoescaped (&#39;) inside the attributes
        self.assertIn(
            b'role="img" title="67% of the combined listening time is alice&#39;s, 33% bob&#39;s" '
            b'aria-label="67% of the combined listening time is alice&#39;s, 33% bob&#39;s"',
            resp.data)

    def test_trend_canvas_has_an_accessible_label(self):
        self._accept("alice", "bob")
        client = self._loginAs("alice")

        resp = client.get("/compare")

        self.assertIn(b'id="comparisonTrendChart" role="img"', resp.data)

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
