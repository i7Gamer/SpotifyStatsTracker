import datetime
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from Database.Importers.StreamingHistoryImporter import Importer
from Database.db import SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON
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


class TestZeroDurationFiltering(unittest.TestCase):
    """Plays under MIN_TIME_PLAYED_MS (skips/errors) must never reach the
    database - across every export format."""

    def _importer(self):
        importer = Importer()
        importer.sp = MagicMock()
        importer.sp.search.return_value = {"tracks": {"items": [FAKE_TRACK]}}
        importer.sp.track.return_value = FAKE_TRACK
        return importer

    def test_parse_history_skips_zero_played_items(self):
        importer = self._importer()
        history = [
            ("Song A", "Artist A", 100, 0, None),
            ("Song B", "Artist B", 200, 5000, None),
        ]
        parsed = importer._parseHistory(lambda item: item, history)
        self.assertEqual([name for name, *_ in parsed], ["Song B"])

    def test_parse_history_skips_negative_played_items(self):
        importer = self._importer()
        history = [("Song A", "Artist A", 100, -5, None)]
        parsed = importer._parseHistory(lambda item: item, history)
        self.assertEqual(parsed, [])

    def test_parse_history_skips_items_below_minimum_threshold(self):
        importer = self._importer()
        history = [
            ("Song A", "Artist A", 100, Importer.MIN_TIME_PLAYED_MS - 1, None),
            ("Song B", "Artist B", 200, Importer.MIN_TIME_PLAYED_MS, None),
        ]
        parsed = importer._parseHistory(lambda item: item, history)
        self.assertEqual([name for name, *_ in parsed], ["Song B"])

    def test_import_extended_history_skips_zero_ms_played(self):
        importer = self._importer()
        history = [
            {
                "ts": "2023-01-01T00:00:00Z", "ms_played": 0,
                "master_metadata_track_name": "Song One",
                "master_metadata_album_artist_name": "Artist One",
                "spotify_track_uri": "spotify:track:track123",
            },
            {
                "ts": "2023-01-01T00:05:00Z", "ms_played": 5000,
                "master_metadata_track_name": "Song One",
                "master_metadata_album_artist_name": "Artist One",
                "spotify_track_uri": "spotify:track:track123",
            },
        ]
        tracks = list(importer.importExtendedHistory(history, known=[], progressCallback=None))
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["timePlayed"], 5000)

    def test_import_account_history_skips_zero_ms_played(self):
        importer = self._importer()
        history = [
            {"endTime": "2023-01-01 00:00:00", "msPlayed": 0, "trackName": "Song One", "artistName": "Artist One"},
            {"endTime": "2023-01-01 00:05:00", "msPlayed": 5000, "trackName": "Song One", "artistName": "Artist One"},
        ]
        tracks = list(importer.importAcountHistory(history, known=[], progressCallback=None))
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["timePlayed"], 5000)

    def test_import_musicolet_csv_skips_zero_duration_rows(self):
        csvData = (
            "FILE_PATH,TITLE,ARTIST,ALBUM,ALBUM_ARTIST,COMPOSER,GENRE,YEAR,DURATION_MS,PLAY_COUNT\n"
            "/music/zero.mp3,Zero Song,Artist One,Album One,Artist One,,Pop,2020,0,1\n"
            "/music/song.mp3,Song One,Artist One,Album One,Artist One,,Pop,2020,200000,1\n"
        )
        importer = self._importer()
        rows = csvData.splitlines()[1:]
        tracks = list(importer.importMusicoletCSVExport(rows, known=[], progressCallback=None))
        self.assertEqual(len(tracks), 1)
        self.assertEqual(tracks[0]["timePlayed"], 200000)

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


if __name__ == "__main__":
    unittest.main()

