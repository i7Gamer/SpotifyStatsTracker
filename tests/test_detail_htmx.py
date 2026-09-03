"""The song/artist/album detail pages' htmx contract (routes/charts.py's
songDetailPage/_entityDetailPage, templates/song_detail.html and friends,
static/js/detail-page.js, static/js/detail-history.js).

The sibling of tests/test_history_htmx.py and tests/test_top_lists_htmx.py,
which carry the fuller commentary on why the transport looks like this. What is
specific to these three pages:

- they have THREE second-request modes, and only two of them are HTML. The
  Trend-buckets select re-fetches chart DATA for a canvas (?ajax=true,
  static/js/detail-chart.js); htmx cannot swap that, so it stays a fetch() and
  stays JSON. The other two - the deferred whole body and the play log - are
  markup, and they moved.
- with two htmx modes on ONE route, the marker has to say WHICH. That is
  htmx's own HX-Target header (the id of the element being filled), not a query
  parameter: the play log's swaps carry hx-replace-url, so a marker in the query
  string would become part of the page's shareable address.
- the body payload used to be HTML *and* chart data in one JSON envelope. The
  HTML is now the response, and the chart data rides inside it as a
  <script type="application/json"> island the afterSwap handler reads.

The logged-out contract for this page is NOT here. HX-Redirect instead of a
302, an empty body, and the filters preserved through the login round-trip
are one app-wide rule with one implementation (app.py's
unauthenticatedResponse), so it is asserted once, parametrized over every
htmx page, in tests/test_ajax_unauthenticated.py. Eight copies of it lived
here and in the sibling files, and only half checked all three things.
"""
import os
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from _detail_client import (DetailPageClientMixin, HX_BODY_HEADERS, HX_LIST_HEADERS,
                            HX_MORE_HEADERS)

#< every page sharing the shell/body/play-log wiring
DETAIL_PATHS = ("/song/t1", "/artist/a1", "/album/alb1")

#< the same three, with the top list a missing entity falls back to
MISSING_PATHS = (("/song/missing", "/top-songs"), ("/artist/missing", "/top-artists"),
                 ("/album/missing", "/top-albums"))


def _playEntry():
    """One row of a play log, shaped the way the db layer emits it - the real
    _embedSongsTextElements runs over these, so the fields it reads have to be
    there (a play log row missing `duration` raises inside the view model)."""
    return {"id": "t1", "name": "Song One", "url": "http://example.com/t1",
            "imageId": "alb1", "duration": 200000, "explicit": False, "isrc": "",
            "discNumber": 1, "trackNumber": 1, "releaseDate": 0, "artists": [],
            "playedAt": 1000.0, "timePlayed": 200000,
            "album": {"id": "alb1", "name": "Album One", "url": "u", "imageId": "alb1",
                      "imageUrl": "", "totalTracks": 1, "releaseDate": 0}}


class DetailHtmxTestCase(DetailPageClientMixin, AppTestCase):
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

    def _shell(self, path, db=None):
        return self._getRaw(self._makeApp(), db or self._db(), path).get_data(as_text=True)

    def _swap(self, path, headers, db=None):
        return self._getRaw(self._makeApp(), db or self._db(), path, headers=headers)


class TestBodySwap(DetailHtmxTestCase):
    def test_an_hx_request_for_the_body_gets_html_not_json(self):
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                resp = self._swap(path, HX_BODY_HEADERS)

                self.assertEqual(resp.status_code, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))
                self.assertIsNone(resp.get_json(silent=True))

    def test_the_old_json_envelope_is_gone(self):
        """The key the previous fetch() layer read. Its absence is the point:
        htmx would swap the literal JSON into the page."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                self.assertNotIn("bodyHtml", self._swap(path, HX_BODY_HEADERS).get_data(as_text=True))

    def test_the_fragment_is_not_a_whole_document(self):
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                body = self._swap(path, HX_BODY_HEADERS).get_data(as_text=True)

                self.assertNotIn("<html", body.lower())
                self.assertNotIn('id="back-button"', body)   #< the toolbar stays in the shell

    def test_the_body_still_opens_with_the_hero_as_detailBodys_own_child(self):
        """The spacing rule keys on `#detailBody > .track-list`, so the hero has
        to be the fragment's first element - see TestDetailHeroSpacingCss."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                body = self._swap(path, HX_BODY_HEADERS).get_data(as_text=True)

                self.assertTrue(body.strip().startswith('<section id="track-list" class="track-list">'),
                                body[:120])

    def test_the_chart_data_rides_a_json_island_inside_the_fragment(self):
        """htmx swaps HTML, so the two chart series that used to travel beside
        bodyHtml in the JSON envelope come with the markup instead. Read by
        detail-page.js's htmx:afterSwap handler, which then renders the
        canvases - the one thing htmx has no opinion about."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                body = self._swap(path, HX_BODY_HEADERS).get_data(as_text=True)

                self.assertIn('id="detailChartData"', body)
                self.assertIn('type="application/json"', body)
                self.assertIn("timeSeries", body)

    def test_only_the_song_island_carries_a_heatmap(self):
        """Its "When You Listen" canvas is the only one on the three pages, and
        renderAllCharts skips a canvas that isn't there - so the key must be
        absent rather than an empty series."""
        songBody = self._swap("/song/t1", HX_BODY_HEADERS).get_data(as_text=True)
        artistBody = self._swap("/artist/a1", HX_BODY_HEADERS).get_data(as_text=True)

        self.assertIn("heatmap", songBody)
        self.assertNotIn("heatmap", artistBody)

    def test_ajax_page_alone_no_longer_triggers_the_fragment(self):
        """?ajax=page was the old marker. Without HX-Request it is just an
        unknown query param, and the page renders its shell."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                resp = self._getRaw(self._makeApp(), self._db(), f"{path}?ajax=page")

                self.assertIsNone(resp.get_json(silent=True))
                self.assertIn('id="detailBody"', resp.get_data(as_text=True))


class TestPlayLogSwap(DetailHtmxTestCase):
    def _db(self):
        db = super()._db()
        db.getEntriesCount.return_value = 1
        db.getEntriesFromNew.return_value = [_playEntry()]
        return db

    def test_an_hx_request_for_the_play_log_gets_html_not_json(self):
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                resp = self._swap(path, HX_LIST_HEADERS)

                self.assertEqual(resp.status_code, 200)
                self.assertIn("text/html", resp.headers.get("Content-Type", ""))
                self.assertIsNone(resp.get_json(silent=True))
                self.assertNotIn("resultsHtml", resp.get_data(as_text=True))

    def test_the_target_header_is_what_picks_the_mode(self):
        """The whole point of keying on HX-Target: one URL, two HTML fragments,
        no marker in the query string to leak into the address bar through
        hx-replace-url."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                body = self._swap(path, HX_BODY_HEADERS).get_data(as_text=True)
                log = self._swap(path, HX_LIST_HEADERS).get_data(as_text=True)

                self.assertIn("timeSeriesChart", body)      #< the chart canvas
                self.assertNotIn("timeSeriesChart", log)    #< the log alone

    def test_the_play_log_swap_skips_the_chart_work(self):
        """The sort/skips toggle and the pagination links re-fetch just the log;
        every bucketed aggregate above it is bucket-independent."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                db = self._db()

                self._swap(path, HX_LIST_HEADERS, db=db)

                db.getListeningTimeSeries.assert_not_called()
                db.getHourOfDayHeatmap.assert_not_called()
                db.getSkipStats.assert_not_called()

    def test_the_play_log_carries_its_own_controls(self):
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                log = self._swap(path, HX_LIST_HEADERS).get_data(as_text=True)

                self.assertIn("sort-toggle", log)

    def test_the_play_log_swap_replaces_the_url_and_never_pushes_it(self):
        """A sort or page change must not stack a history entry, so Back leaves
        the detail page instead of stepping through its list states."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                body = self._swap(path, HX_BODY_HEADERS).get_data(as_text=True)

                self.assertIn('hx-replace-url="true"', body)
                self.assertNotIn("hx-push-url", body)

    def test_a_superseded_play_log_request_is_aborted(self):
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                body = self._swap(path, HX_BODY_HEADERS).get_data(as_text=True)

                self.assertIn("hx-sync=", body)
                self.assertIn(":replace", body)

    def test_the_detail_links_in_the_list_are_not_boosted(self):
        """hx-boost is scoped to the wrappers around the toggle and the
        pagination strip. Boosting the whole container would capture every
        track/artist/album link in the list and swap a detail PAGE into the log."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                log = self._swap(path, HX_LIST_HEADERS).get_data(as_text=True)

                self.assertNotIn('id="detailHistoryResults" hx-boost', log)


class TestShowMoreSwap(DetailHtmxTestCase):
    """The song page's "Show more" appends a batch rather than replacing the
    list, so it is its own swap target: the control at the end of the timeline,
    replaced by the next batch plus the next control."""

    def _db(self, total=120, rows=50):
        db = super()._db()
        db.getEntriesCount.return_value = total
        db.getEntriesFromNew.return_value = [_playEntry() for _ in range(rows)]
        return db

    def test_a_batch_is_rows_plus_the_next_control_and_nothing_else(self):
        resp = self._swap("/song/t1", HX_MORE_HEADERS, db=self._db())
        body = resp.get_data(as_text=True)

        self.assertEqual(resp.status_code, 200)
        self.assertIn("timeline-item", body)
        self.assertIn('id="timelineActions"', body)
        self.assertNotIn("Play Timeline", body)      #< the log's header stays put
        self.assertNotIn('id="timelineItems"', body)  #< appended INTO it, not around it

    def test_the_last_batch_drops_the_control(self):
        """outerHTML-swapping the control with a response that has none is how
        the button disappears - no client-side "remove the actions div" branch."""
        body = self._swap("/song/t1", HX_MORE_HEADERS,
                          db=self._db(total=50, rows=50)).get_data(as_text=True)

        self.assertNotIn('id="timelineActions"', body)

    def test_the_control_asks_for_the_batch_after_the_rows_on_screen(self):
        body = self._swap("/song/t1", HX_BODY_HEADERS, db=self._db()).get_data(as_text=True)

        self.assertIn('id="timelineActions"', body)
        self.assertIn("offset=50", body)


class TestChartDataStaysJson(DetailHtmxTestCase):
    """?ajax=true is DATA for a canvas, not markup. htmx cannot swap it, so the
    Trend-buckets select keeps its own fetch() and this branch keeps its JSON."""

    def test_the_bucket_select_still_gets_json(self):
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                resp = self._getRaw(self._makeApp(), self._db(), f"{path}?ajax=true")

                self.assertEqual(resp.mimetype, "application/json")
                self.assertEqual(sorted(resp.get_json().keys()), ["groupBy", "timeSeries"])

    def test_the_shell_still_loads_the_fetch_based_chart_module(self):
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                self.assertIn("js/detail-chart.js", self._shell(path))


class TestShell(DetailHtmxTestCase):
    def test_the_shell_does_not_contain_the_body(self):
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                body = self._shell(path)

                self.assertIn('id="detailBody"', body)
                self.assertIn('class="detail-skeleton"', body)
                self.assertNotIn("timeSeriesChart", body)

    def test_the_shell_serves_htmx_from_this_origin(self):
        """config.py's Content-Security-Policy allows script-src 'self' only, so
        a CDN tag would be blocked and the page would never load its body."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                self.assertIn("js/vendor/htmx.min.js", self._shell(path))

    def test_the_shell_also_serves_the_boosted_link_modifier_fix(self):
        """htmx-filters.js is not optional on a page carrying an hx-boost, even
        though nothing here calls into it: it installs the capture listener that
        hands shift-click and alt-click on a boosted link back to the browser.
        htmx's own boost exemption covers ctrl and meta ONLY, while the
        delegated click handler hx-boost replaced exempted all four - so without
        this, opening a play-log page in a new WINDOW swaps it into the list."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                self.assertIn("js/htmx-filters.js", self._shell(path))

    def test_the_placeholder_triggers_the_first_load(self):
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                self.assertIn('hx-trigger="load"', self._shell(path))

    def test_the_body_load_url_holds_no_unvalidated_input(self):
        """Same rule the pagination links follow: junk is coerced for the query
        itself, so reflecting it into the markup would assert it again in the one
        place a reader would trust."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                body = self._shell(f"{path}?groupBy=bogus&sort=junk&view=nonsense&page=notanumber")

                self.assertNotIn("bogus", body)
                self.assertNotIn("junk", body)
                self.assertNotIn("nonsense", body)
                self.assertNotIn("notanumber", body)

    def test_the_body_load_carries_the_state_from_a_shared_url(self):
        """A shared or bookmarked link carries its bucket, sort, tab and page,
        and the deferred body is what renders all four."""
        body = self._shell("/artist/a1?groupBy=month&view=history&sort=oldest&page=2")

        for expected in ("groupBy=month", "view=history", "sort=oldest", "page=2"):
            with self.subTest(param=expected):
                self.assertIn(expected, body)

    def test_the_marker_never_reaches_the_markup(self):
        """The two HTML modes are told apart by a header, so neither spelling
        belongs in a URL the page prints - hx-replace-url would put it in the
        address bar."""
        for path in DETAIL_PATHS:
            with self.subTest(path=path):
                body = self._shell(path)

                self.assertNotIn("ajax=page", body)
                self.assertNotIn("ajax=list", body)

    def test_the_shell_disables_the_bucket_select_until_the_body_lands(self):
        """Its ?ajax=true refetch targets a chart that isn't on the page yet,
        and its result would be overwritten by the body swap in flight."""
        body = self._shell("/song/t1")

        selectTag = body[body.index('<select id="groupBy"'):]
        self.assertIn("disabled", selectTag[:selectTag.index(">")])


class TestMissingEntitySwap(DetailHtmxTestCase):
    """An entity an overwrite import removed between the shell request and the
    body request. Without an htmx branch the swap follows the 302 to the top
    list and injects a whole PAGE into #detailBody."""

    def _db(self):
        db = MagicMock()
        db.getSong.return_value = None
        db.getArtist.return_value = None
        db.getAlbum.return_value = None
        return db

    def test_an_hx_request_gets_hx_redirect_rather_than_a_302(self):
        for path, endpoint in MISSING_PATHS:
            for headers in (HX_BODY_HEADERS, HX_LIST_HEADERS):
                with self.subTest(path=path, target=headers["HX-Target"]):
                    resp = self._swap(path, headers)

                    self.assertNotIn(resp.status_code, (301, 302, 303, 307, 308))
                    self.assertIn(endpoint, resp.headers.get("HX-Redirect", ""))
                    self.assertEqual(resp.get_data(as_text=True), "")

    def test_a_plain_get_still_redirects(self):
        for path, endpoint in MISSING_PATHS:
            with self.subTest(path=path):
                resp = self._getRaw(self._makeApp(), self._db(), path)

                self.assertEqual(resp.status_code, 302)
                self.assertIn(endpoint, resp.headers["Location"])

    def test_the_fetch_based_chart_mode_keeps_its_json_404(self):
        """detail-chart.js is still a fetch(), which follows a 302 into the top
        list's 200 HTML and then throws parsing it as JSON - so that mode keeps
        the explicit "go here instead" body it has always had."""
        for path, endpoint in MISSING_PATHS:
            with self.subTest(path=path):
                resp = self._getRaw(self._makeApp(), self._db(), f"{path}?ajax=true")

                self.assertEqual(resp.status_code, 404)
                self.assertEqual(resp.mimetype, "application/json")
                self.assertIn(endpoint, resp.get_json()["redirectUrl"])


if __name__ == "__main__":
    unittest.main()


class TestPlayLogPagingParamsAreBounded(DetailHtmxTestCase):
    """?offset= and ?page= on the song play log were parsed with a bare int()
    and bound straight into LIMIT ? OFFSET ?; sqlite3 refuses anything above
    2**63-1 with OverflowError and the route had no handler for it, so
    /song/<id>?offset=99999999999999999999 (or the same ?page=) answered the
    play-log swap with a 500. The sibling ?limit= got its ceiling
    (MAX_DETAIL_HISTORY_PAGES) for exactly this footgun; these two were left
    behind.

    The first fix (?offset= clamped to totalCount) traded that 500 for a
    quieter bug (UT-2, 2026-09-02 review): totalCount is a row COUNT, not a
    valid OFFSET into the rows, so clamping to it landed one past the last
    row and rendered "No plays recorded yet." beside a card that says the
    song has plays - ?page=999999 or ?page=3 of 2 on any song did this, not
    only an absurd offset. /history and the artist/album views never had
    this: dashboard/pagination.py's _calculatePagination clamps `page`
    itself to the last page, not the offset it produces to the count.

    Now the offset is clamped to the last BATCH boundary - the largest
    multiple of PAGE_SIZE below totalCount, or 0 when there are no rows -
    so an out-of-range page renders the final batch instead of nothing, and
    the page goes through _positivePageArg's digit cap before any
    arithmetic."""

    def _db(self):
        db = super()._db()
        db.getEntriesCount.return_value = 3
        db.getEntriesFromNew.return_value = []
        return db

    def _startIndex(self, db):
        #< the BATCH call, which is always the first: an offset past row 0
        #  also fetches the one row before it, to seed the appended batch's
        #  month header and gap badge (see _songHistoryContext)
        return db.getEntriesFromNew.call_args_list[0].kwargs["startIndex"]

    def _seedIndex(self, db):
        """The seed call's startIndex, or None when the batch opens the list."""
        calls = db.getEntriesFromNew.call_args_list
        return calls[1].kwargs["startIndex"] if len(calls) > 1 else None

    def test_an_absurd_offset_is_clamped_to_the_last_batch_boundary(self):
        from routes.charts import PAGE_SIZE

        #< 2 full batches plus a partial third: the last batch starts at
        #  2 * PAGE_SIZE, a value that differs from both 0 and totalCount, so
        #  clamping to totalCount (the old bug) or to always-0 would both
        #  fail this instead of passing by coincidence.
        totalCount = 2 * PAGE_SIZE + 7
        for raw in ("99999999999999999999", "9223372036854775808"):
            with self.subTest(offset=raw):
                db = self._db()
                db.getEntriesCount.return_value = totalCount
                resp = self._swap("/song/t1?offset=" + raw, HX_LIST_HEADERS, db=db)

                self.assertEqual(resp.status_code, 200)
                self.assertEqual(self._startIndex(db), 2 * PAGE_SIZE)
                self.assertEqual(self._seedIndex(db), 2 * PAGE_SIZE - 1,
                                 "and the seed is the row directly above it")

    def test_an_absurd_page_starts_at_the_first_page(self):
        db = self._db()
        resp = self._swap("/song/t1?page=99999999999999999999", HX_LIST_HEADERS, db=db)

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._startIndex(db), 0)
        self.assertIsNone(self._seedIndex(db), "row 0 opens the list; there is nothing above it")

    def test_a_real_offset_and_page_still_pass_through(self):
        from routes.charts import PAGE_SIZE

        db = self._db()
        resp = self._swap("/song/t1?offset=2", HX_LIST_HEADERS, db=db)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._startIndex(db), 2)

        db = self._db()
        db.getEntriesCount.return_value = 10 * PAGE_SIZE
        self._swap("/song/t1?page=3", HX_LIST_HEADERS, db=db)
        self.assertEqual(self._startIndex(db), 2 * PAGE_SIZE)

    def test_an_out_of_range_page_renders_the_last_batch_not_the_empty_state(self):
        """UT-2's actual symptom: ?page=999999 on a song WITH plays used to
        show "No plays recorded yet." next to a card that says otherwise."""
        from routes.charts import PAGE_SIZE

        db = self._db()
        db.getEntriesCount.return_value = 2 * PAGE_SIZE + 7
        db.getEntriesFromNew.return_value = [_playEntry()]

        for query in ("?page=999999", "?offset=99999"):
            with self.subTest(query=query):
                db.getEntriesFromNew.reset_mock()
                body = self._swap("/song/t1" + query, HX_LIST_HEADERS, db=db).get_data(as_text=True)

                self.assertEqual(self._startIndex(db), 2 * PAGE_SIZE)
                self.assertNotIn("No plays recorded yet.", body)
                self.assertIn('class="timeline-item', body)

    def test_a_song_with_zero_plays_still_renders_the_empty_state(self):
        db = self._db()
        db.getEntriesCount.return_value = 0
        db.getEntriesFromNew.return_value = []

        body = self._swap("/song/t1?page=999999", HX_LIST_HEADERS, db=db).get_data(as_text=True)

        self.assertEqual(self._startIndex(db), 0)
        self.assertIn("No plays recorded yet.", body)
