import datetime
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.Importers.StreamingHistoryImporter import Importer
from Database.db import SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON, SKIP_THRESHOLD_MS
from Database.utils import convertToDatetime, getTimezone


def _appTzYear(timestamp):
    """coverage() buckets years in the app timezone (matching dashboard
    day/year bucketing), not convertToDatetime's UTC default."""
    return convertToDatetime(timestamp, getTimezone()).year
import Database.utils as utilsModule

MUSICOLET_CSV = (
    "FILE_PATH,TITLE,ARTIST,ALBUM,ALBUM_ARTIST,COMPOSER,GENRE,YEAR,DURATION_MS,PLAY_COUNT\n"
    "/music/song.mp3,Song One,Artist One,Album One,Artist One,,Pop,2020,200000,1\n"
)

FAKE_TRACK = {
    "id": "track123",
    "name": "Song One",
    "external_urls": {"spotify": "https://open.spotify.com/track/track123"},
    "duration_ms": 200000,
    "explicit": False,
    "disc_number": 1,
    "track_number": 1,
    "external_ids": {"isrc": "ABC123"},
    "album": {
        "id": "album123",
        "name": "Album One",
        "external_urls": {"spotify": "https://open.spotify.com/album/album123"},
        "images": [{"url": "https://example.com/img.jpg"}],
        "total_tracks": 10,
        "release_date": "2020-01-01",
        "artists": [{
            "name": "Artist One",
            "id": "artist123",
            "external_urls": {"spotify": "https://open.spotify.com/artist/artist123"},
        }],
    },
}


class TestMusicoletImport(unittest.TestCase):
    def _mockedImporter(self):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.search.return_value = {"tracks": {"items": [FAKE_TRACK]}}
        return importer

    def test_import_history_dispatches_musicolet_without_error(self):
        """importHistory() must accept a progressCallback for every export type,
        including musicoletPremium, without raising a TypeError."""
        importer = self._mockedImporter()
        parsedHistory, exportType = importer._convertToList(MUSICOLET_CSV)
        self.assertEqual(exportType, "musicoletPremium")

        progressCalls = []

        def progressCallback(status, current, total, message):
            progressCalls.append((status, current, total, message))

        result = importer.importHistory(parsedHistory, known=[], exportType=exportType, progressCallback=progressCallback)
        tracks = list(result)

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["name"], "Song One")

    def test_import_musicolet_csv_export_accepts_progressCallback_directly(self):
        importer = self._mockedImporter()
        gen = importer.importMusicoletCSVExport(MUSICOLET_CSV.splitlines()[1:], known=[], progressCallback=lambda *a: None)
        tracks = list(gen)
        self.assertEqual(len(tracks), 1)

    def test_prefetch_progress_callback_is_monotonic(self):
        importer = self._mockedImporter()
        
        # We need two missing tracks to show multiple pre-fetch progress steps
        history = [
            ("Track A", "Artist A", "2023-01-01 00:00:00", 180000, "uriA"),
            ("Track B", "Artist B", "2023-01-01 00:03:00", 180000, "uriB"),
        ]
        
        # Mock SpotipyFree search/track to return valid items
        importer.sp.track.side_effect = [
            {"id": "uriA", "name": "Track A", "external_urls": {"spotify": "http://a"}, "duration_ms": 180000, "album": {"images": []}},
            {"id": "uriB", "name": "Track B", "external_urls": {"spotify": "http://b"}, "duration_ms": 180000, "album": {"images": []}},
        ]
        
        progressCalls = []
        def progressCallback(status, current, total, message):
            progressCalls.append((status, current, total, message))
            
        def dummyDataFunction(item):
            return item
            
        # Run import generator to trigger pre-fetch and yielding
        tracks = list(importer._import(dummyDataFunction, history, known=[], progressCallback=progressCallback))
        
        # Filter progress calls that are pre-fetching
        prefetchCalls = [c for c in progressCalls if "Pre-fetching" in c[3]]
        
        # We expect 2 calls, with current values: 1 and 2
        self.assertEqual(len(prefetchCalls), 2)
        self.assertEqual(prefetchCalls[0][1], 1)
        self.assertEqual(prefetchCalls[1][1], 2)


class TestImportTimePlayedNotCapped(unittest.TestCase):
    """Export ms_played is authoritative (it comes from Spotify's own records).
    When a play is mapped to a different version of the song - name+artist
    catalog match or Spotify track relinking - whose duration is shorter, the
    play time must NOT be capped at that version's duration; the cap only
    exists to guard against corrupt live-listener values."""

    CATALOG_DURATION_MS = 200000
    PLAYED_MS = 250000  #< longer than the catalog version's duration

    def test_fetched_track_play_keeps_full_ms_played(self):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.track.return_value = FAKE_TRACK  #< duration_ms=200000
        history = [{
            "ts": "2023-01-01T00:05:00Z", "ms_played": self.PLAYED_MS,
            "master_metadata_track_name": "Song One",
            "master_metadata_album_artist_name": "Artist One",
            "spotify_track_uri": "spotify:track:track123",
        }]
        tracks = list(importer.importExtendedHistory(history, known=[], progressCallback=None))
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["timePlayed"], self.PLAYED_MS)

    def test_known_catalog_track_play_keeps_full_ms_played(self):
        importer = Importer()
        importer.sp = MagicMock()
        known = [{
            "id": "canonical1",
            "name": "Song One",
            "artists": [{"name": "Artist One", "id": "a1"}],
            "album": {"id": "alb1", "name": "Album One"},
            "duration": self.CATALOG_DURATION_MS,
        }]
        history = [{
            "ts": "2023-01-01T00:05:00Z", "ms_played": self.PLAYED_MS,
            "master_metadata_track_name": "Song One",
            "master_metadata_album_artist_name": "Artist One",
            "spotify_track_uri": None,
        }]
        tracks = list(importer.importExtendedHistory(history, known=known, progressCallback=None))
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["timePlayed"], self.PLAYED_MS)
        importer.sp.track.assert_not_called()


class TestSkipThresholdRouting(unittest.TestCase):
    """Entries shorter than SKIP_THRESHOLD_MS still flow through the same
    track resolution but come out tagged isSkip=True - the DB writer routes
    them to play_skips instead of plays. Only negative durations are dropped
    outright."""

    def _importer(self):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.search.return_value = {"tracks": {"items": [FAKE_TRACK]}}
        importer.sp.track.return_value = FAKE_TRACK
        return importer

    def _extendedEntry(self, msPlayed, minute=0):
        return {
            "ts": f"2023-01-01T00:{minute:02d}:00Z", "ms_played": msPlayed,
            "master_metadata_track_name": "Song One",
            "master_metadata_album_artist_name": "Artist One",
            "spotify_track_uri": "spotify:track:track123",
        }

    def test_parse_history_keeps_zero_played_items(self):
        importer = self._importer()
        history = [
            ("Song A", "Artist A", 100, 0, None),
            ("Song B", "Artist B", 200, 5000, None),
        ]
        parsed = importer._parseHistory(lambda item: item, history)
        self.assertEqual([name for name, *_ in parsed], ["Song A", "Song B"])

    def test_parse_history_drops_negative_played_items(self):
        importer = self._importer()
        history = [("Song A", "Artist A", 100, -5, None)]
        parsed = importer._parseHistory(lambda item: item, history)
        self.assertEqual(parsed, [])

    def test_extended_history_tags_sub_threshold_entries_as_skips(self):
        importer = self._importer()
        history = [
            self._extendedEntry(0, minute=0),
            self._extendedEntry(400, minute=1),
            self._extendedEntry(SKIP_THRESHOLD_MS - 1, minute=2),
            self._extendedEntry(SKIP_THRESHOLD_MS, minute=3),
        ]
        metas = list(importer.importExtendedHistory(history, known=[], progressCallback=None))
        self.assertEqual(len(metas), 4)
        self.assertEqual([m["isSkip"] for m in metas], [True, True, True, False])
        self.assertEqual([m["timePlayed"] for m in metas], [0, 400, SKIP_THRESHOLD_MS - 1, SKIP_THRESHOLD_MS])
        # Skips still resolve to the real track (the FK into tracks must hold)
        self.assertEqual(metas[0]["id"], "track123")

    def test_account_history_tags_sub_threshold_entries_as_skips(self):
        importer = self._importer()
        history = [
            {"endTime": "2023-01-01 00:00:00", "msPlayed": 0, "trackName": "Song One", "artistName": "Artist One"},
            {"endTime": "2023-01-01 00:05:00", "msPlayed": 5000, "trackName": "Song One", "artistName": "Artist One"},
        ]
        metas = list(importer.importAcountHistory(history, known=[], progressCallback=None))
        self.assertEqual(len(metas), 2)
        self.assertEqual([m["isSkip"] for m in metas], [True, False])

    def test_musicolet_short_track_rows_become_skips(self):
        """Musicolet rows use the track duration as play time - a sub-threshold
        duration therefore lands as a skip event; zero-duration rows collapse
        into one skip via the (track, played_at) UNIQUE dedup downstream."""
        csvData = (
            "FILE_PATH,TITLE,ARTIST,ALBUM,ALBUM_ARTIST,COMPOSER,GENRE,YEAR,DURATION_MS,PLAY_COUNT\n"
            "/music/zero.mp3,Zero Song,Artist One,Album One,Artist One,,Pop,2020,0,1\n"
            "/music/song.mp3,Song One,Artist One,Album One,Artist One,,Pop,2020,200000,1\n"
        )
        importer = self._importer()
        rows = csvData.splitlines()[1:]
        metas = list(importer.importMusicoletCSVExport(rows, known=[], progressCallback=None))
        self.assertEqual(len(metas), 2)
        byTime = sorted(metas, key=lambda m: m["timePlayed"])
        self.assertTrue(byTime[0]["isSkip"])
        self.assertEqual(byTime[0]["timePlayed"], 0)
        self.assertFalse(byTime[1]["isSkip"])
        self.assertEqual(byTime[1]["timePlayed"], 200000)

    def test_process_play_updates_synthetic_track_duration_in_cache(self):
        from Database.db import SYNTHETIC_FALLBACK_REASON
        importer = self._importer()
        
        # Setup cached synthetic track with 10s duration
        known = {
            "track_x": {
                "id": "track_x",
                "name": "Song X",
                "artists": [{"id": "artist_x", "name": "Artist X"}],
                "duration": 10000,
                "created_reason": SYNTHETIC_FALLBACK_REASON
            }
        }
        
        # Process a play with 240s duration
        item = ("Song X", "Artist X", 1000, 240000, "track_x", "Album X")
        meta = importer._processPlay(item, known)
        
        # Verify the cached track duration was updated in place
        self.assertEqual(known["track_x"]["duration"], 240000)

    def test_process_play_repairs_missing_album_from_spotify_or_import_data(self):
        importer = self._importer()
        
        known = {
            "track_real": {
                "id": "track_real",
                "name": "Song Real",
                "artists": [{"id": "artist_real", "name": "Artist Real"}],
                "duration": 240000,
                "album": None
            }
        }
        
        # 1. Test repairing via Spotify API (mocked)
        with patch.object(importer.sp, "track") as mock_track:
            mock_track.return_value = {
                "id": "track_real",
                "name": "Song Real",
                "duration_ms": 240000,
                "external_urls": {"spotify": "https://open.spotify.com/track/track_real"},
                "album": {
                    "id": "album_real",
                    "name": "Album Real Refetched",
                    "external_urls": {"spotify": "https://open.spotify.com/album/album_real"},
                    "images": [{"url": "http://img.url"}],
                    "total_tracks": 10,
                    "release_date": "2026-01-01"
                },
                "artists": [{"id": "artist_real", "name": "Artist Real"}]
            }
            
            item = ("Song Real", "Artist Real", 1000, 240000, "track_real", "Album Real Exported")
            importer._processPlay(item, known)
            
            self.assertIsNotNone(known["track_real"]["album"])
            self.assertEqual(known["track_real"]["album"]["name"], "Album Real Refetched")
            self.assertEqual(known["track_real"]["album"]["id"], "album_real")
            
        # 2. Test repairing via export data fallback when Spotify API fails
        known["track_real"]["album"] = None  # reset
        with patch.object(importer.sp, "track", side_effect=Exception("API failure")):
            item = ("Song Real", "Artist Real", 1000, 240000, "track_real", "Album Real Exported")
            importer._processPlay(item, known)
            
            self.assertIsNotNone(known["track_real"]["album"])
            self.assertEqual(known["track_real"]["album"]["name"], "Album Real Exported")
            self.assertTrue(known["track_real"]["album"]["id"].startswith("album_"))


class TestOfflineTimestampCorrection(unittest.TestCase):
    """For offline plays, `ts` is the SYNC time (whole sessions share one
    stamp, sometimes days late) - offline_timestamp holds the true start.
    It's used only when offline is truthy, normalized from ms when needed,
    and sanity-guarded (must be >= 2006-01-01 and <= ts)."""

    TS = "2023-01-01T12:00:00Z"
    END_TS = int(datetime.datetime(2023, 1, 1, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
    MS_PLAYED = 180000
    TRUE_START = END_TS - 7200  #< two hours before the sync stamp

    def _run(self, **overrides):
        entry = {
            "ts": self.TS, "ms_played": self.MS_PLAYED,
            "master_metadata_track_name": "Song One",
            "master_metadata_album_artist_name": "Artist One",
            "spotify_track_uri": "spotify:track:track123",
            **overrides,
        }
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.track.return_value = FAKE_TRACK
        metas = list(importer.importExtendedHistory([entry], known=[], progressCallback=None))
        self.assertEqual(len(metas), 1)
        return metas[0]

    def test_offline_play_uses_offline_timestamp_in_seconds(self):
        meta = self._run(offline=True, offline_timestamp=self.TRUE_START)
        self.assertEqual(meta["playedAt"], self.TRUE_START)

    def test_offline_play_normalizes_millisecond_offline_timestamp(self):
        meta = self._run(offline=True, offline_timestamp=self.TRUE_START * 1000)
        self.assertEqual(meta["playedAt"], self.TRUE_START)

    def test_online_play_ignores_offline_timestamp(self):
        meta = self._run(offline=False, offline_timestamp=self.TRUE_START)
        self.assertEqual(meta["playedAt"], self.END_TS - self.MS_PLAYED // 1000)

    def test_implausibly_old_offline_timestamp_falls_back_to_ts(self):
        meta = self._run(offline=True, offline_timestamp=100000)  #< 1970 - before Spotify existed
        self.assertEqual(meta["playedAt"], self.END_TS - self.MS_PLAYED // 1000)

    def test_offline_timestamp_after_sync_time_falls_back_to_ts(self):
        meta = self._run(offline=True, offline_timestamp=self.END_TS + 500)
        self.assertEqual(meta["playedAt"], self.END_TS - self.MS_PLAYED // 1000)

    def test_zero_offline_timestamp_falls_back_to_ts(self):
        meta = self._run(offline=True, offline_timestamp=0)
        self.assertEqual(meta["playedAt"], self.END_TS - self.MS_PLAYED // 1000)


class TestExtrasExtraction(unittest.TestCase):
    """The extended export's behavioral fields ride along as
    meta["importExtras"] (incognito_mode -> incognito, booleans as 0/1);
    entries and export types without them carry no extras at all."""

    def _importer(self):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.track.return_value = FAKE_TRACK
        importer.sp.search.return_value = {"tracks": {"items": [FAKE_TRACK]}}
        return importer

    def test_extended_entry_extras_are_extracted(self):
        entry = {
            "ts": "2023-01-01T12:00:00Z", "ms_played": 180000,
            "master_metadata_track_name": "Song One",
            "master_metadata_album_artist_name": "Artist One",
            "spotify_track_uri": "spotify:track:track123",
            "platform": "ios", "conn_country": "CH", "ip_addr": "1.2.3.4",
            "reason_start": "clickrow", "reason_end": "trackdone",
            "shuffle": True, "skipped": False, "offline": False, "incognito_mode": True,
        }
        meta = list(self._importer().importExtendedHistory([entry], known=[], progressCallback=None))[0]
        self.assertEqual(meta["importExtras"], {
            "platform": "ios", "conn_country": "CH",
            "reason_start": "clickrow", "reason_end": "trackdone",
            "shuffle": 1, "skipped": 0, "offline": 0, "incognito": 1,
        })
        self.assertNotIn("ip_addr", meta["importExtras"])  #< never stored

    def test_entry_without_behavioral_fields_has_no_extras(self):
        entry = {
            "ts": "2023-01-01T12:00:00Z", "ms_played": 180000,
            "master_metadata_track_name": "Song One",
            "master_metadata_album_artist_name": "Artist One",
            "spotify_track_uri": "spotify:track:track123",
        }
        meta = list(self._importer().importExtendedHistory([entry], known=[], progressCallback=None))[0]
        self.assertIsNone(meta.get("importExtras"))

    def test_account_export_has_no_extras(self):
        history = [{"endTime": "2023-01-01 00:05:00", "msPlayed": 5000,
                    "trackName": "Song One", "artistName": "Artist One"}]
        meta = list(self._importer().importAcountHistory(history, known=[], progressCallback=None))[0]
        self.assertIsNone(meta.get("importExtras"))


class TestCoverage(unittest.TestCase):
    """coverage() feeds the overwrite import: the batch span plus the set of
    calendar years the export actually has entries in - years derived from
    START timestamps, so a play straddling New Year doesn't spuriously cover
    the next year."""

    def _importer(self):
        importer = Importer()
        importer.sp = MagicMock()
        return importer

    def _extendedEntry(self, ts, msPlayed=180000, **overrides):
        return {
            "ts": ts, "ms_played": msPlayed,
            "master_metadata_track_name": "Song One",
            "master_metadata_album_artist_name": "Artist One",
            "spotify_track_uri": "spotify:track:track123",
            **overrides,
        }

    def test_span_and_years_across_gap(self):
        history = [
            self._extendedEntry("2018-06-01T12:00:00Z"),
            self._extendedEntry("2020-06-01T12:00:00Z"),
        ]
        result = self._importer().coverage(history, "spotifyExtendedExport")
        self.assertIsNotNone(result)
        minStart, maxEnd, years = result
        start2018 = int(datetime.datetime(2018, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()) - 180
        self.assertEqual(minStart, start2018)
        self.assertEqual(maxEnd, int(datetime.datetime(2020, 6, 1, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp()))
        expectedYears = {_appTzYear(minStart), _appTzYear(maxEnd - 180)}
        self.assertEqual(years, expectedYears)

    def test_sub_threshold_entries_count_toward_coverage(self):
        history = [self._extendedEntry("2019-06-01T12:00:00Z", msPlayed=300)]
        minStart, maxEnd, years = self._importer().coverage(history, "spotifyExtendedExport")
        self.assertEqual(years, {_appTzYear(minStart)})

    def test_offline_timestamp_widens_the_span(self):
        trueStart = int(datetime.datetime(2019, 6, 1, 1, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
        history = [
            self._extendedEntry("2019-06-03T12:00:00Z", offline=True, offline_timestamp=trueStart),
        ]
        minStart, maxEnd, years = self._importer().coverage(history, "spotifyExtendedExport")
        self.assertEqual(minStart, trueStart)

    def test_straddling_play_only_covers_its_start_year(self):
        history = [self._extendedEntry("2020-01-01T00:02:00Z", msPlayed=180000)]  #< started 2019-12-31 23:59 UTC
        minStart, maxEnd, years = self._importer().coverage(history, "spotifyExtendedExport")
        self.assertEqual(years, {_appTzYear(minStart)})

    def test_musicolet_years_sit_at_the_synthetic_anchor(self):
        rows = MUSICOLET_CSV.splitlines()[1:]
        minStart, maxEnd, years = self._importer().coverage(rows, "musicoletPremium")
        self.assertEqual(years, {2000})

    def test_unrecognized_or_empty_exports_have_no_coverage(self):
        importer = self._importer()
        self.assertIsNone(importer.coverage([], "emptyExport"))
        self.assertIsNone(importer.coverage([], "None"))
        self.assertIsNone(importer.coverage([], "spotifyExtendedExport"))


class TestVideoExportRows(unittest.TestCase):
    """Streaming_History_Video_*.json rows are music-video streams of real
    tracks - same shape as audio rows (episode/audiobook fields null) - and
    must import as normal plays."""

    def test_video_shaped_row_imports_as_play(self):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.track.return_value = FAKE_TRACK
        entry = {
            "ts": "2026-01-15T14:54:15Z", "platform": "not_applicable",
            "ms_played": 193320, "conn_country": "CH",
            "master_metadata_track_name": "Song One",
            "master_metadata_album_artist_name": "Artist One",
            "master_metadata_album_album_name": "Album One",
            "spotify_track_uri": "spotify:track:track123",
            "episode_name": None, "episode_show_name": None, "spotify_episode_uri": None,
            "audiobook_title": None, "audiobook_uri": None,
            "reason_start": "clickrow", "reason_end": "endplay",
            "shuffle": False, "skipped": False, "offline": False,
            "offline_timestamp": None, "incognito_mode": False,
        }
        metas = list(importer.importExtendedHistory([entry], known=[], progressCallback=None))
        self.assertEqual(len(metas), 1)
        self.assertFalse(metas[0]["isSkip"])
        self.assertEqual(metas[0]["name"], "Song One")

    def test_podcast_row_is_dropped_and_counted(self):
        """Episode rows carry null track names - they can't become plays, but
        the drop must be visible in the stats dict instead of silent."""
        importer = Importer()
        importer.sp = MagicMock()
        entry = {
            "ts": "2023-05-01T10:00:00Z", "ms_played": 1500000,
            "master_metadata_track_name": None,
            "master_metadata_album_artist_name": None,
            "spotify_track_uri": None,
            "episode_name": "Some Podcast Episode", "episode_show_name": "Some Show",
        }
        stats = {}
        metas = list(importer.importExtendedHistory([entry], known=[], progressCallback=None, stats=stats))
        self.assertEqual(metas, [])
        self.assertEqual(stats.get("droppedNoTrack"), 1)


class TestResolveKnownKey(unittest.TestCase):
    def _importer(self):
        importer = Importer()
        importer.sp = MagicMock()
        return importer

    def test_prefers_trackUri_when_both_keys_are_known(self):
        importer = self._importer()
        known = {"uri1": {"id": "uri1"}, "NameArtist": {"id": "other"}}
        self.assertEqual(importer._resolveKnownKey("uri1", "Name", "Artist", known), "uri1")

    def test_falls_back_to_name_artist_key_when_trackUri_not_known(self):
        """A trackUri absent from the cache must not stop matching by name+artist -
        e.g. a reissue/remaster URI for a song already cached under its name+artist."""
        importer = self._importer()
        known = {"NameArtist": {"id": "cached"}}
        self.assertEqual(importer._resolveKnownKey("uriNotCached", "Name", "Artist", known), "NameArtist")

    def test_returns_none_when_neither_key_is_known(self):
        importer = self._importer()
        self.assertIsNone(importer._resolveKnownKey("uriX", "Name", "Artist", {}))

    def test_returns_none_without_trackUri_or_name_and_artist(self):
        importer = self._importer()
        self.assertIsNone(importer._resolveKnownKey(None, None, None, {"anything": {}}))


class TestFetchTrackMeta(unittest.TestCase):
    def _importer(self):
        importer = Importer()
        importer.sp = MagicMock()
        return importer

    def test_uses_track_lookup_when_trackUri_given(self):
        importer = self._importer()
        importer.sp.track.return_value = {"id": "abc"}
        result = importer._fetchTrackMeta("Song", "Artist", "abc")
        importer.sp.track.assert_called_once_with("abc")
        self.assertEqual(result, {"id": "abc"})

    def test_falls_back_to_search_when_track_lookup_fails(self):
        importer = self._importer()
        importer.sp.track.side_effect = Exception("not found")
        importer.sp.search.return_value = {"tracks": {"items": [FAKE_TRACK]}}
        result = importer._fetchTrackMeta("Song One", "Artist One", "abc")
        self.assertEqual(result, FAKE_TRACK)

    def test_searches_directly_when_no_trackUri(self):
        importer = self._importer()
        importer.sp.search.return_value = {"tracks": {"items": [FAKE_TRACK]}}
        result = importer._fetchTrackMeta("Song One", "Artist One", None)
        importer.sp.track.assert_not_called()
        self.assertEqual(result, FAKE_TRACK)


class TestAccountExportUsesUtcTimestamps(unittest.TestCase):
    """Spotify's Account-export "endTime" field is documented as UTC but has no
    timezone marker on the wire - it must not be interpreted as local time."""

    def test_end_time_is_parsed_as_utc_regardless_of_local_tz(self):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.search.return_value = {"tracks": {"items": [FAKE_TRACK]}}

        history = [{"endTime": "2023-07-08 12:00:00", "msPlayed": 5000,
                    "trackName": "Song One", "artistName": "Artist One"}]

        # A local TZ far from UTC - if endTime were (incorrectly) localized to
        # this offset instead of treated as UTC, playedAt would be off by 8h.
        with patch.object(utilsModule, "tz", datetime.timezone(datetime.timedelta(hours=-8))):
            tracks = list(importer.importAcountHistory(history, known=[], progressCallback=None))

        expectedEndTimestamp = int(datetime.datetime(2023, 7, 8, 12, 0, 0, tzinfo=datetime.timezone.utc).timestamp())
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["playedAt"], expectedEndTimestamp - 5)  # msPlayed//1000 = 5s before endTime


class TestMusicoletSyntheticTimestampsAreDeterministic(unittest.TestCase):
    """Musicolet's CSV only carries an aggregate play count, not real play
    timestamps. The synthetic timestamps generated for it must be reproducible
    across independent import runs so that re-importing the same (or an
    updated) file is deduped by plays.UNIQUE(username, track_id, played_at)
    (see test_duplicates.py for proof that identical (track_id, played_at)
    pairs are in fact ignored on re-insert) instead of creating a fresh batch
    of fake plays every time."""

    def _run(self, csvBody, playCount):
        csvData = (
            "FILE_PATH,TITLE,ARTIST,ALBUM,ALBUM_ARTIST,COMPOSER,GENRE,YEAR,DURATION_MS,PLAY_COUNT\n"
            f"/music/song.mp3,Song One,Artist One,Album One,Artist One,,Pop,2020,200000,{playCount}\n"
        )
        rows = csvData.splitlines()[1:]
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.search.return_value = {"tracks": {"items": [FAKE_TRACK]}}
        tracks = list(importer.importMusicoletCSVExport(rows, known=[], progressCallback=None))
        return [t["playedAt"] for t in tracks]

    def test_two_independent_imports_of_the_same_file_produce_identical_timestamps(self):
        firstRun = self._run(None, playCount=3)
        secondRun = self._run(None, playCount=3)

        self.assertEqual(len(firstRun), 3)
        self.assertEqual(firstRun, secondRun)
        self.assertEqual(len(set(firstRun)), 3)  #< the 3 plays within one import are still distinct

    def test_an_updated_file_with_a_higher_play_count_only_adds_new_trailing_timestamps(self):
        """A later export of the same track with a higher cumulative PLAY_COUNT
        must reproduce the earlier run's timestamps exactly (so those already-
        imported plays get deduped) and only append new ones for the delta."""
        earlierRun = self._run(None, playCount=2)
        laterRun = self._run(None, playCount=5)

        self.assertEqual(laterRun[:2], earlierRun)
        self.assertEqual(len(laterRun), 5)
        self.assertEqual(len(set(laterRun)), 5)


def makeRestrictedTrack(artistName="Various Artists", artistId="0LyfQWJT6nXafLPZqxe9Of", albumName=""):
    """Raw meta shape Spotify returns for region-restricted tracks: real track and
    album ids, but blanked name/duration and the generic Various Artists profile."""
    return {
        "id": "track123",
        "name": "",
        "external_urls": {"spotify": "https://open.spotify.com/track/track123"},
        "duration_ms": 0,
        "explicit": False,
        "disc_number": 1,
        "track_number": 1,
        "external_ids": {"isrc": ""},
        "playability": {"playable": False, "reason": "COUNTRY_RESTRICTED"},
        "album": {
            "id": "album123",
            "name": albumName,
            "external_urls": {"spotify": "https://open.spotify.com/album/album123"},
            "images": [],
            "total_tracks": 1,
            "release_date": "0000-00-00",
            "artists": [{
                "name": artistName,
                "id": artistId,
                "external_urls": {"spotify": f"https://open.spotify.com/artist/{artistId}"},
            }],
        },
    }


class TestRestrictedTrackOverlay(unittest.TestCase):
    HISTORY = [("Real Song", "Real Artist", "2023-01-01 00:00:00", 10354, "track123", "Real Album")]

    @staticmethod
    def _importerReturning(meta):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.track.return_value = meta
        return importer

    def _runImport(self, importer, history=None):
        def dummyDataFunction(item):
            return item
        return list(importer._import(dummyDataFunction, history or self.HISTORY, known={}, progressCallback=None))

    def test_blank_lookup_gets_export_metadata_and_tag(self):
        importer = self._importerReturning(makeRestrictedTrack())

        tracks = self._runImport(importer)

        self.assertEqual(len(tracks), 1)
        track = tracks[0]
        # Export data fills the blanked fields
        self.assertEqual(track["name"], "Real Song")
        self.assertEqual(track["artists"][0]["name"], "Real Artist")
        self.assertEqual(track["album"]["name"], "Real Album")
        self.assertEqual(track["created_reason"], RESTRICTED_FALLBACK_REASON)
        self.assertEqual(track["availability_reason"], "COUNTRY_RESTRICTED")
        # The real Spotify ids/links are kept
        self.assertEqual(track["id"], "track123")
        self.assertEqual(track["url"], "https://open.spotify.com/track/track123")
        self.assertEqual(track["album"]["id"], "album123")
        self.assertEqual(track["album"]["url"], "https://open.spotify.com/album/album123")
        # Replacement artist is fabricated (keyed by name), not the Various Artists profile
        self.assertNotEqual(track["artists"][0]["id"], "0LyfQWJT6nXafLPZqxe9Of")
        self.assertTrue(track["artists"][0]["id"].startswith("artist_"))
        self.assertEqual(track["artists"][0]["url"], "")

    def test_matching_returned_artist_is_kept(self):
        """If Spotify returned the true artist despite the blanked name, keep its
        real id and link instead of fabricating one."""
        importer = self._importerReturning(makeRestrictedTrack(artistName="Real Artist", artistId="realArtist1"))

        track = self._runImport(importer)[0]

        self.assertEqual(track["artists"][0]["id"], "realArtist1")
        self.assertEqual(track["artists"][0]["url"], "https://open.spotify.com/artist/realArtist1")

    def test_album_name_falls_back_to_track_name_without_export_album(self):
        importer = self._importerReturning(makeRestrictedTrack())
        history = [("Real Song", "Real Artist", "2023-01-01 00:00:00", 10354, "track123")]  #< 5-tuple, no album

        track = self._runImport(importer, history)[0]

        self.assertEqual(track["album"]["name"], "Real Song")

    def test_returned_album_name_is_not_overwritten(self):
        importer = self._importerReturning(makeRestrictedTrack(albumName="Spotify Album"))

        track = self._runImport(importer)[0]

        self.assertEqual(track["album"]["name"], "Spotify Album")

    def test_available_track_is_untouched(self):
        importer = self._importerReturning(FAKE_TRACK)

        track = self._runImport(importer)[0]

        self.assertEqual(track["name"], "Song One")
        self.assertIsNone(track.get("created_reason"))
        self.assertEqual(track["artists"][0]["id"], "artist123")

    def test_catalog_artist_id_is_reused(self):
        """When the export artist already exists in the catalog (from other
        tracks), the fallback must reuse the real artist id/link instead of
        fabricating one - keeps Top Artists stats grouped."""
        importer = self._importerReturning(makeRestrictedTrack())
        catalog = [{
            "id": "otherTrack1", "name": "Other Song",
            "artists": [{"id": "realArtist99", "name": "Real Artist",
                         "url": "https://open.spotify.com/artist/realArtist99",
                         "imageUrl": "", "imageId": "realArtist99"}],
        }]

        def dummyDataFunction(item):
            return item
        track = list(importer._import(dummyDataFunction, self.HISTORY, known=catalog, progressCallback=None))[0]

        self.assertEqual(track["artists"][0]["id"], "realArtist99")
        self.assertEqual(track["artists"][0]["url"], "https://open.spotify.com/artist/realArtist99")

    def test_synthetic_track_reuses_catalog_artist(self):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.track.side_effect = Exception("Spotify 404 Track Not Found")
        importer.sp.search.side_effect = Exception("Spotify 404 Search Failed")
        catalog = [{
            "id": "otherTrack1", "name": "Other Song",
            "artists": [{"id": "realArtist99", "name": "Mark Watson",
                         "url": "https://open.spotify.com/artist/realArtist99",
                         "imageUrl": "", "imageId": "realArtist99"}],
        }]
        history = [("Arctic Future", "Mark Watson", "2023-01-01 00:00:00", 10354, "uriX", None)]

        def dummyDataFunction(item):
            return item
        track = list(importer._import(dummyDataFunction, history, known=catalog, progressCallback=None))[0]

        self.assertEqual(track["created_reason"], SYNTHETIC_FALLBACK_REASON)
        self.assertEqual(track["artists"][0]["id"], "realArtist99")

    def test_build_known_index_skips_blank_names(self):
        """Catalog rows stored with blank names (pre-overlay restricted lookups)
        must not seed the cache, so a re-import re-fetches and heals them."""
        importer = Importer()
        blank = {"id": "t1", "name": "", "artists": [{"name": "Various Artists"}]}
        named = {"id": "t2", "name": "Song", "artists": [{"name": "Artist"}]}

        index = importer.buildKnownIndex([blank, named])

        self.assertNotIn("t1", index)
        self.assertIn("t2", index)
        self.assertIn("SongArtist", index)


class TestImportFallbackToSyntheticTrack(unittest.TestCase):
    HISTORY = [("Arctic Future", "Mark Watson", "2023-01-01 00:00:00", 10354, "uri_2s9mjCqeU26eivqPXY04V8")]

    @staticmethod
    def _importerFailingWith(message):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.track.side_effect = Exception(message)
        importer.sp.search.side_effect = Exception(message)
        return importer

    def _runImport(self, importer):
        def dummyDataFunction(item):
            return item
        return list(importer._import(dummyDataFunction, self.HISTORY, known={}, progressCallback=None))

    def test_fallback_when_spotify_lookup_fails(self):
        # Simulate deleted/unavailable track (permanent failure)
        importer = self._importerFailingWith("Spotify 404 Track Not Found")

        tracks = self._runImport(importer)

        # Verify that we did NOT drop the play and successfully resolved it to a synthetic track/album
        self.assertEqual(len(tracks), 1)
        track = tracks[0]
        self.assertEqual(track["name"], "Arctic Future")
        self.assertEqual(track["id"], "uri_2s9mjCqeU26eivqPXY04V8")
        self.assertEqual(track["duration"], 10354)
        self.assertEqual(track["artists"][0]["name"], "Mark Watson")
        self.assertEqual(track["album"]["name"], "Arctic Future")
        self.assertEqual(track["album"]["totalTracks"], 1)
        self.assertEqual(track["created_reason"], SYNTHETIC_FALLBACK_REASON)

    def test_synthetic_track_with_uri_keeps_spotify_link(self):
        """A removed track's Spotify page still exists (just unplayable), so the
        link is kept when a real URI is known. The fabricated album_/artist_ ids
        never existed on Spotify - their urls stay empty (templates guard 'Open
        in Spotify' on a truthy url)."""
        importer = self._importerFailingWith("Spotify 404 Track Not Found")

        track = self._runImport(importer)[0]

        self.assertEqual(track["url"], "https://open.spotify.com/track/uri_2s9mjCqeU26eivqPXY04V8")
        self.assertEqual(track["album"]["url"], "")
        self.assertEqual(track["artists"][0]["url"], "")

    def test_synthetic_track_without_uri_has_no_link(self):
        """Without a URI the id is an md5 hash - a fabricated open.spotify.com
        link would point at nothing, so the url stays empty."""
        importer = self._importerFailingWith("Spotify 404 Track Not Found")
        history = [("Arctic Future", "Mark Watson", "2023-01-01 00:00:00", 10354, None)]

        def dummyDataFunction(item):
            return item
        tracks = list(importer._import(dummyDataFunction, history, known={}, progressCallback=None))

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["url"], "")

    def test_synthetic_album_uses_export_album_name(self):
        importer = self._importerFailingWith("Spotify 404 Track Not Found")
        history = [("Arctic Future", "Mark Watson", "2023-01-01 00:00:00", 10354,
                    "uri_2s9mjCqeU26eivqPXY04V8", "Polar Sounds")]

        def dummyDataFunction(item):
            return item
        track = list(importer._import(dummyDataFunction, history, known={}, progressCallback=None))[0]

        self.assertEqual(track["album"]["name"], "Polar Sounds")

    def test_transient_lookup_error_skips_play_instead_of_synthesizing(self):
        """Network/auth/rate-limit failures are temporary - the play must be dropped
        (recoverable via re-import) rather than frozen into a synthetic record."""
        for message in ("Connection reset by peer", "429 Too Many Requests",
                        "Could not get session", "Read timed out"):
            with self.subTest(error=message):
                importer = self._importerFailingWith(message)
                self.assertEqual(self._runImport(importer), [])

    def test_builtin_connection_and_timeout_errors_are_transient(self):
        importer = Importer()
        self.assertTrue(importer._isTransientLookupError(ConnectionError("boom")))
        self.assertTrue(importer._isTransientLookupError(TimeoutError("boom")))
        self.assertFalse(importer._isTransientLookupError(IndexError("list index out of range")))


class TestConvertToList(unittest.TestCase):
    """_convertToList must classify every input without raising: recognized
    exports get their type, a valid-but-empty JSON list is "emptyExport", and
    anything else (dict, scalar, corrupt JSON) is ([], "None") - which
    Database.importHistory treats as a failed import."""

    def _importer(self):
        importer = Importer()
        importer.sp = MagicMock()
        return importer

    def test_account_export_is_detected(self):
        data = '[{"endTime": "2023-01-01 00:00:00", "msPlayed": 5000, "trackName": "S", "artistName": "A"}]'
        parsed, exportType = self._importer()._convertToList(data)
        self.assertEqual(exportType, "spotifyAcountExport")
        self.assertEqual(len(parsed), 1)

    def test_extended_export_is_detected(self):
        data = '[{"ts": "2023-01-01T00:00:00Z", "ms_played": 5000}]'
        parsed, exportType = self._importer()._convertToList(data)
        self.assertEqual(exportType, "spotifyExtendedExport")
        self.assertEqual(len(parsed), 1)

    def test_empty_json_list_is_emptyExport(self):
        self.assertEqual(self._importer()._convertToList("[]"), ([], "emptyExport"))

    def test_json_dict_is_unrecognized(self):
        self.assertEqual(self._importer()._convertToList('{"msPlayed": 5000}'), ([], "None"))

    def test_json_scalar_is_unrecognized(self):
        self.assertEqual(self._importer()._convertToList("42"), ([], "None"))

    def test_list_of_non_dicts_is_unrecognized(self):
        self.assertEqual(self._importer()._convertToList("[1, 2, 3]"), ([], "None"))

    def test_corrupt_json_is_unrecognized(self):
        self.assertEqual(self._importer()._convertToList('[{"ts": "2023-'), ([], "None"))


class TestSearchForSongEmptyResults(unittest.TestCase):
    """An empty search result must raise a readable error whose text can never
    match a TRANSIENT_LOOKUP_ERROR_MARKERS entry - it signals "track is gone
    from Spotify" to _processPlay, which then synthesizes a fallback record.
    A marker match would instead silently drop the play."""

    def _importerWithEmptySearch(self):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.search.return_value = {"tracks": {"items": []}}
        return importer

    def test_empty_search_raises_value_error(self):
        importer = self._importerWithEmptySearch()
        with self.assertRaises(ValueError):
            importer._searchForSong("Unknown Song", "Unknown Artist")

    def test_error_is_never_transient_even_for_marker_named_tracks(self):
        """Track/artist names are user data - a track literally named
        "Connection Timeout" must not make the error look transient. The
        message therefore must not embed the name/artist."""
        importer = self._importerWithEmptySearch()
        try:
            importer._searchForSong("Connection Timeout", "504 Band")
        except ValueError as e:
            self.assertFalse(importer._isTransientLookupError(e))
        else:
            self.fail("expected ValueError")

    def test_empty_search_still_produces_synthetic_track(self):
        importer = self._importerWithEmptySearch()
        history = [("Gone Song", "Gone Artist", "2023-01-01 00:00:00", 10354, None)]

        def dummyDataFunction(item):
            return item
        tracks = list(importer._import(dummyDataFunction, history, known={}, progressCallback=None))

        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["name"], "Gone Song")
        self.assertEqual(tracks[0]["created_reason"], SYNTHETIC_FALLBACK_REASON)


if __name__ == "__main__":
    unittest.main()

