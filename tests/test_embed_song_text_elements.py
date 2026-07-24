import unittest
from unittest.mock import patch
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from app import SpotifyDashboardApp
from _app_factory import AppTestCase


class TestEmbedSongTextElementsMissingAlbum(AppTestCase):
    """_embedSongTextElements() must not assume song['album'] is always a dict -
    Repository._songRowToDict() can legitimately return album=None when the
    LEFT JOIN to albums finds no matching row (see
    tests/test_repository.py::test_missing_album_row_falls_back_like_get_track).
    A song in that state must still render instead of crashing every page that
    lists it (dashboard, top songs, song/artist/album detail, wrapped, ...)."""

    def _song(self, album):
        return {
            "id": "t1", "name": "Song One", "duration": 200000,
            "artists": [{"name": "Artist A"}], "album": album,
        }

    def test_missing_album_does_not_crash(self):
        dash = self._makeApp()
        song = self._song(album=None)

        result = dash._embedSongTextElements(song)

        self.assertEqual(result["releaseDateText"], "")
        self.assertEqual(result["artistsText"], "Artist A")
        self.assertIsNone(result["album"])

    def test_missing_album_in_a_list_does_not_crash(self):
        dash = self._makeApp()
        songs = [self._song(album=None), self._song(album={"releaseDate": 0})]

        result = dash._embedSongsTextElements(songs)

        self.assertEqual(len(result), 2)

    def test_present_album_with_unknown_release_date_gets_blank_text(self):
        """releaseDate=0 is the app-wide sentinel for "unknown" (used by
        synthetic tracks and albums the metadata backfiller hasn't reached
        yet - see Repository.upsertTrack/_createSyntheticTrack) - it must
        render as blank, not as the Unix epoch date."""
        dash = self._makeApp()
        song = self._song(album={"releaseDate": 0})

        result = dash._embedSongTextElements(song)

        self.assertIn("releaseDateText", result["album"])
        self.assertEqual(result["releaseDateText"], "")

    def test_present_album_with_known_release_date_gets_release_date_text(self):
        dash = self._makeApp()
        song = self._song(album={"releaseDate": 946684800})   #< 2000-01-01

        result = dash._embedSongTextElements(song)

        self.assertNotEqual(result["releaseDateText"], "")


class TestEmbedAlbumTextElementsReleaseDate(AppTestCase):
    """_embedAlbumTextElements() backs the Top Albums, Wrapped, and
    album-detail pages - same unknown-release-date sentinel (releaseDate=0,
    the value Repository.upsertTrack/_createSyntheticTrack use for an album
    with no known release date) as _embedSongTextElements()."""

    def _album(self, **overrides):
        album = {"id": "alb1", "name": "Album One", "artists": []}
        album.update(overrides)
        return album

    def test_unknown_release_date_gets_blank_text(self):
        dash = self._makeApp()
        with dash.app.app_context():
            result = dash._embedAlbumTextElements(self._album(releaseDate=0))

        self.assertEqual(result["releaseDateText"], "")

    def test_missing_release_date_key_gets_blank_text(self):
        dash = self._makeApp()
        with dash.app.app_context():
            result = dash._embedAlbumTextElements(self._album())

        self.assertEqual(result["releaseDateText"], "")

    def test_known_release_date_gets_release_date_text(self):
        dash = self._makeApp()
        with dash.app.app_context():
            result = dash._embedAlbumTextElements(self._album(releaseDate=946684800))   #< 2000-01-01

        self.assertNotEqual(result["releaseDateText"], "")


class TestEnrichSongTimelineEntries(AppTestCase):
    """_enrichSongTimelineEntries must add playType, timePassedText, and monthYearHeader tokens."""

    def test_play_type_classification(self):
        dash = self._makeApp()
        track_duration = 200000  # 200s
        plays = [
            {"id": "t1", "playedAt": 1784560000, "timePlayed": 190000, "isSkip": False},  # 190s / 200s = 95% -> full
            {"id": "t1", "playedAt": 1784540000, "timePlayed": 100000, "isSkip": False},  # 100s / 200s = 50% -> partial
            {"id": "t1", "playedAt": 1784520000, "timePlayed": 5000, "isSkip": True},     # skip
        ]

        enriched = dash._enrichSongTimelineEntries(plays, trackDurationMs=track_duration)

        self.assertEqual(enriched[0]["playType"], "full")
        self.assertEqual(enriched[1]["playType"], "partial")
        self.assertEqual(enriched[2]["playType"], "skip")

    def test_time_passed_between_plays_newest_first(self):
        # The gap badge renders in the template directly above each play's card
        # (i.e. between the previous list entry and this one), so it must be
        # attached to the *later* list entry regardless of sort direction -
        # otherwise it renders one card too high in the timeline.
        dash = self._makeApp()
        # Newest first: 15:00, then 12:00 (3 hours difference)
        ts_newer = 1784560000       # 2026-07-20 15:06:40 UTC
        ts_older = ts_newer - 10800 # 3 hours earlier
        plays = [
            {"id": "t1", "playedAt": ts_newer, "timePlayed": 180000, "isSkip": False},
            {"id": "t1", "playedAt": ts_older, "timePlayed": 180000, "isSkip": False},
        ]

        enriched = dash._enrichSongTimelineEntries(plays, trackDurationMs=200000)

        self.assertIsNone(enriched[0].get("timePassedText"))
        self.assertIn("timePassedText", enriched[1])
        self.assertEqual(enriched[1]["timePassedText"], "3 hours later")

    def test_time_passed_between_plays_oldest_first(self):
        # Same plays in ascending order - the gap badge must still land on the
        # second list entry, since that is always the one whose card it precedes.
        dash = self._makeApp()
        ts_older = 1784560000
        ts_newer = ts_older + 10800  # 3 hours later
        plays = [
            {"id": "t1", "playedAt": ts_older, "timePlayed": 180000, "isSkip": False},
            {"id": "t1", "playedAt": ts_newer, "timePlayed": 180000, "isSkip": False},
        ]

        enriched = dash._enrichSongTimelineEntries(plays, trackDurationMs=200000)

        self.assertIsNone(enriched[0].get("timePassedText"))
        self.assertIn("timePassedText", enriched[1])
        self.assertEqual(enriched[1]["timePassedText"], "3 hours later")

    def test_play_type_respects_explicit_threshold(self):
        # The full-vs-partial cutoff is a parameter, not a hardcoded 80: a 90%
        # play is "partial" under a 95% bar and "full" under an 80% bar.
        dash = self._makeApp()
        play = {"id": "t1", "playedAt": 1784560000, "timePlayed": 180000, "isSkip": False}  # 90%

        strict = dash._enrichSongTimelineEntries([dict(play)], trackDurationMs=200000,
                                                 completePercentThreshold=95)
        self.assertEqual(strict[0]["playType"], "partial")

        lenient = dash._enrichSongTimelineEntries([dict(play)], trackDurationMs=200000,
                                                  completePercentThreshold=80)
        self.assertEqual(lenient[0]["playType"], "full")

    def test_play_type_defaults_to_admin_completion_percent(self):
        # With no explicit threshold the cutoff comes from the admin's
        # instance-wide completion-complete percent, not a hardcoded 80 - so a
        # 90% play reads as "partial" once the admin raises the bar to 95%,
        # keeping the timeline label consistent with Top Songs' "Full plays
        # only" filter and the Forgotten Favorite trend.
        dash = self._makeApp()
        play = {"id": "t1", "playedAt": 1784560000, "timePlayed": 180000, "isSkip": False}  # 90%

        with patch.object(dash.repo, "getCompletionCompletePercent", return_value=95):
            enriched = dash._enrichSongTimelineEntries([dict(play)], trackDurationMs=200000)

        self.assertEqual(enriched[0]["playType"], "partial")

    def test_month_year_headers(self):
        dash = self._makeApp()
        import datetime
        dt1 = datetime.datetime(2026, 7, 20, 15, 0, tzinfo=datetime.timezone.utc).timestamp()
        dt2 = datetime.datetime(2026, 6, 15, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
        dt3 = datetime.datetime(2026, 6, 10, 10, 0, tzinfo=datetime.timezone.utc).timestamp()
        plays = [
            {"id": "t1", "playedAt": dt1, "timePlayed": 180000, "isSkip": False},
            {"id": "t1", "playedAt": dt2, "timePlayed": 180000, "isSkip": False},
            {"id": "t1", "playedAt": dt3, "timePlayed": 180000, "isSkip": False},
        ]

        enriched = dash._enrichSongTimelineEntries(plays, trackDurationMs=200000)

        self.assertEqual(enriched[0].get("monthYearHeader"), "July 2026")
        self.assertEqual(enriched[1].get("monthYearHeader"), "June 2026")
        self.assertIsNone(enriched[2].get("monthYearHeader"))


if __name__ == "__main__":
    unittest.main()

