"""Skip statistics as they reach the pages: the Charts payload's most-skipped
lists, and the per-entity summary on the song/artist/album detail pages.

The query semantics (rate-over-count, and the shrinkage that makes a rate
rankable at low volume) are pinned in test_skip_stats.py; these cover the
wiring only.
"""
import os
import re
import sys
import unittest
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from _app_factory import AppTestCase
from _detail_client import DetailPageClientMixin


def _skipStats(plays=10, skips=2, percent=16.7):
    return {"plays": plays, "skips": skips, "skipPercent": percent}


class SkipStatsRouteTestCase(DetailPageClientMixin, AppTestCase):
    """`_getRaw` is one request (what the /charts payload tests want);
    `_getPath` is the detail pages' shell plus its deferred body, which is
    where the per-entity summary lives now - see _detail_client.py."""

    def _makeDb(self):
        db = MagicMock()
        db.repo.getUserSettings.return_value = {"default_dashboard_window": "month", "timezone": None}
        db.getListeningTimeSeries.return_value = []
        db.getHourOfDayHeatmap.return_value = [[{"totalTimeListened": 0, "plays": 0} for _ in range(24)]
                                               for _ in range(7)]
        db.getArtistTrend.return_value = {"buckets": [], "series": []}
        db.getExplicitRatio.return_value = {"explicit": 0, "clean": 0}
        db.getReleaseDecadeDistribution.return_value = {}
        db.getCompletionStats.return_value = {"skips": 0, "completes": 0, "partials": 0}
        db.getMostSkippedSongs.return_value = []
        db.getMostSkippedArtists.return_value = []
        db.getSkipStats.return_value = _skipStats()
        db.getPlayBuckets.return_value = []
        return db


class TestChartsPayload(SkipStatsRouteTestCase):
    def test_the_ajax_payload_carries_both_lists(self):
        dash = self._makeApp()
        db = self._makeDb()
        db.getMostSkippedSongs.return_value = [
            {"id": "t1", "name": "Skipped Song", "artists": [], "skips": 8, "plays": 2,
             "encounters": 10, "skipPercent": 80.0}]
        db.getMostSkippedArtists.return_value = [
            {"id": "a1", "name": "Skipped Artist", "skips": 9, "plays": 1,
             "encounters": 10, "skipPercent": 90.0}]

        payload = self._getRaw(dash, db, "/charts?ajax=true").get_json()

        self.assertEqual(payload["mostSkippedSongs"][0]["name"], "Skipped Song")
        self.assertEqual(payload["mostSkippedSongs"][0]["skipPercent"], 80.0)
        self.assertEqual(payload["mostSkippedArtists"][0]["name"], "Skipped Artist")

    def test_the_keys_are_always_present_even_when_empty(self):
        """The client renders off these every load; an absent key would leave
        the previous range's bars on screen."""
        dash = self._makeApp()

        payload = self._getRaw(dash, self._makeDb(), "/charts?ajax=true").get_json()

        self.assertEqual(payload["mostSkippedSongs"], [])
        self.assertEqual(payload["mostSkippedArtists"], [])

    def test_the_lists_are_scoped_to_the_selected_range(self):
        dash = self._makeApp()
        db = self._makeDb()

        self._getRaw(dash, db, "/charts?ajax=true&interval=week")

        kwargs = db.getMostSkippedSongs.call_args.kwargs
        self.assertIsNotNone(kwargs["startDate"])
        self.assertIsNotNone(kwargs["endDate"])

    def test_the_limit_comes_from_config(self):
        from config import CHART_MOST_SKIPPED_LIMIT

        dash = self._makeApp()
        db = self._makeDb()

        self._getRaw(dash, db, "/charts?ajax=true")

        for call in (db.getMostSkippedSongs.call_args, db.getMostSkippedArtists.call_args):
            self.assertEqual(call.kwargs["limit"], CHART_MOST_SKIPPED_LIMIT)

    def test_the_shell_renders_the_section_canvases(self):
        dash = self._makeApp()

        body = self._getRaw(dash, self._makeDb(), "/charts").data.decode()

        self.assertIn('id="mostSkippedGrid"', body)
        self.assertIn('id="mostSkippedSongsChart"', body)
        self.assertIn('id="mostSkippedArtistsChart"', body)

    def test_the_section_follows_the_completion_donut(self):
        """"How often do I skip" then "what do I skip" - the two read as one
        thought only in that order."""
        dash = self._makeApp()

        body = self._getRaw(dash, self._makeDb(), "/charts").data.decode()

        self.assertLess(body.index('id="completionChart"'), body.index('id="mostSkippedGrid"'))


def _heroCard(body):
    """The detail page's hero card markup.

    `<section id="track-list">` wraps exactly the one card the page is about;
    the artist/album pages' song lists sit in their own (id-less) sections
    below, so slicing to the next </section> isolates the hero.
    """
    start = body.index('<section id="track-list"')
    return body[start:body.index("</section>", start)]


class TestDetailPageSkipStat(SkipStatsRouteTestCase):
    # The card's own plays aggregate and getSkipStats' `plays` are the same
    # figure computed two ways (both count non-skip plays), so the fixtures keep
    # them equal - that equality is exactly why the summary must not restate it.
    DEFAULT_PLAYS = 54

    def _detailDb(self, plays=DEFAULT_PLAYS):
        db = self._makeDb()
        song = {"id": "t1", "name": "Song", "url": "u", "imageId": "i", "duration": 200000,
                "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
                "album": {"id": "alb1", "name": "Album", "url": "u", "imageId": "i", "imageUrl": "",
                           "totalTracks": 1, "releaseDate": 0},
                "artists": [], "plays": plays, "totalTimeListened": 5000, "firstListenedAt": 0}
        db.getSong.return_value = song
        db.getArtist.return_value = {"id": "a1", "name": "Artist", "url": "u", "imageId": "i",
                                      "imageUrl": "", "plays": plays, "totalTimeListened": 5000,
                                      "firstListenedAt": 0, "uniqueSongCount": 1}
        db.getAlbum.return_value = {"id": "alb1", "name": "Album", "url": "u", "imageId": "i",
                                     "imageUrl": "", "totalTracks": 1, "releaseDate": 0, "artists": [],
                                     "plays": plays, "totalTimeListened": 5000, "firstListenedAt": 0,
                                     "uniqueSongCount": 1}
        db.getSongsStats.return_value = []
        db.getAlbumsStats.return_value = []
        db.getEntriesFromNew.return_value = []
        db.getEntriesCount.return_value = 0
        db.getArtistBio.return_value = None
        db.getAlbumBio.return_value = None
        return db

    def test_the_song_page_shows_the_skip_summary(self):
        dash = self._makeApp()
        db = self._detailDb()
        db.getSkipStats.return_value = _skipStats(plays=54, skips=12, percent=18.2)

        body = self._getPath(dash, db, "/song/t1").data.decode()

        self.assertIn("12 skips", body)
        self.assertIn("18.2% skipped", body)

    def test_each_detail_page_asks_for_its_own_entity(self):
        for path, kwarg, value in (("/song/t1", "trackId", "t1"),
                                   ("/artist/a1", "artistId", "a1"),
                                   ("/album/alb1", "albumId", "alb1")):
            with self.subTest(path=path):
                dash = self._makeApp()
                db = self._detailDb()

                self._getPath(dash, db, path)

                self.assertEqual(db.getSkipStats.call_args.kwargs, {kwarg: value})

    def test_the_summary_joins_the_hero_cards_meta_line(self):
        """It used to be a standalone <p> below the card. It belongs on the
        stat line the card already has, so all of one entity's numbers read as
        one sentence."""
        for path in ("/song/t1", "/artist/a1", "/album/alb1"):
            with self.subTest(path=path):
                dash = self._makeApp()
                db = self._detailDb()
                db.getSkipStats.return_value = _skipStats(plays=54, skips=12, percent=18.2)

                card = _heroCard(self._getPath(dash, db, path).data.decode())

                self.assertLess(card.index('class="track-meta small"'), card.index("detail-skip-stat"))
                #< .track-attributes opens right after the last .track-meta block,
                #  so landing before it means the summary is inside the meta line
                self.assertLess(card.index("detail-skip-stat"), card.index('class="track-attributes"'))

    def test_the_plays_count_is_not_repeated(self):
        """The meta line already opens with the entity's play count, so the
        summary contributes only the half the line lacked."""
        for path in ("/song/t1", "/artist/a1", "/album/alb1"):
            with self.subTest(path=path):
                dash = self._makeApp()
                db = self._detailDb()
                db.getSkipStats.return_value = _skipStats(plays=54, skips=12, percent=18.2)

                card = _heroCard(self._getPath(dash, db, path).data.decode())

                self.assertEqual(re.findall(r"\d+ plays", card), ["54 plays"])

    def test_the_sub_lists_do_not_inherit_the_heros_summary(self):
        """`skipStats` is a page-level variable, so every song card in the
        artist page's list would otherwise restate the artist's own figures."""
        dash = self._makeApp()
        db = self._detailDb()
        db.getSkipStats.return_value = _skipStats(plays=54, skips=12, percent=18.2)
        db.getSongsStats.return_value = [
            {"id": "t1", "name": "Song One", "url": "u", "imageId": "i", "duration": 200000,
             "explicit": False, "isrc": "", "discNumber": 1, "trackNumber": 1, "releaseDate": 0,
             "album": {"id": "alb1", "name": "Album", "url": "u", "imageId": "i", "imageUrl": "",
                        "totalTracks": 1, "releaseDate": 0},
             "artists": [], "plays": 3, "totalTimeListened": 5000, "firstListenedAt": 0},
        ]

        body = self._getPath(dash, db, "/artist/a1").data.decode()

        self.assertEqual(body.count("detail-skip-stat"), 1)

    def test_a_never_skipped_entity_says_so(self):
        dash = self._makeApp()
        db = self._detailDb()
        db.getSkipStats.return_value = _skipStats(plays=20, skips=0, percent=0.0)

        body = self._getPath(dash, db, "/song/t1").data.decode()

        self.assertIn("Never skipped", body)
        self.assertNotIn("% skipped", body)

    def test_an_unheard_entity_renders_no_line_at_all(self):
        dash = self._makeApp()
        db = self._detailDb()
        db.getSkipStats.return_value = _skipStats(plays=0, skips=0, percent=0.0)

        body = self._getPath(dash, db, "/song/t1").data.decode()

        self.assertNotIn("detail-skip-stat", body)

    def test_a_skip_only_entity_shows_its_skips_without_a_stray_plays_figure(self):
        """Every play was a skip (getSong's skip-sorted fallback): the card
        reports no plays, so the line must carry the skips alone."""
        dash = self._makeApp()
        db = self._detailDb(plays=0)
        db.getSkipStats.return_value = _skipStats(plays=0, skips=4, percent=100.0)

        card = _heroCard(self._getPath(dash, db, "/song/t1").data.decode())

        self.assertIn("4 skips", card)
        self.assertIn("100.0% skipped", card)
        self.assertEqual(re.findall(r"\d+ plays", card), [])

    def test_the_skip_sorted_label_gives_way_to_the_summary(self):
        """A skip-only song's card carries `skips` of its own (the fallback
        query attaches it for the skip-ranked Top pages' label). With the
        summary on the meta line that label would say the same thing twice."""
        dash = self._makeApp()
        db = self._detailDb(plays=0)
        db.getSong.return_value = {**db.getSong.return_value, "skips": 4, "skipPercent": 100.0}
        db.getSkipStats.return_value = _skipStats(plays=0, skips=4, percent=100.0)

        card = _heroCard(self._getPath(dash, db, "/song/t1").data.decode())

        self.assertNotIn("skip-label", card)
        self.assertEqual(card.count("4 skips"), 1)

    def test_singular_wording_for_a_single_skip(self):
        dash = self._makeApp()
        db = self._detailDb(plays=1)
        db.getSkipStats.return_value = _skipStats(plays=1, skips=1, percent=50.0)

        body = self._getPath(dash, db, "/song/t1").data.decode()

        # Matched on the word boundary rather than on the surrounding markup:
        # the count no longer has an element of its own (see
        # tests/test_skip_count_text_colour.py), and the wording is the point.
        self.assertRegex(body, r"1 skip\b")
        self.assertNotIn("1 skips", body)


if __name__ == "__main__":
    unittest.main()
