import unittest
from unittest.mock import MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# See tests/test_database_images.py for why this guard is needed: other test
# modules replace Database modules with MagicMocks at import time, and unittest
# discover imports every test file before running any of them. formatTrack needs
# the real utils (timeToInt/convertToDatetime), so force fresh, real imports.
for m in ("Database.Formatters.spotifyClient", "Database.utils"):
    if isinstance(sys.modules.get(m), MagicMock):
        del sys.modules[m]

from Database.Formatters.spotifyClient import Client


def _rawTrack():
    return {
        "id": "t1",
        "name": "Song",
        "external_urls": {"spotify": "https://open.spotify.com/track/t1"},
        "duration_ms": 200000,
        "album": {
            "name": "Album",
            "id": "alb1",
            "external_urls": {"spotify": "https://open.spotify.com/album/alb1"},
            "images": [{"url": "https://img.example/alb1"}],
            "total_tracks": 10,
            "release_date": "2020-01-01",
            "artists": [
                {"name": "Artist", "id": "a1", "external_urls": {"spotify": "https://open.spotify.com/artist/a1"}}
            ],
        },
    }


class TestFormatTrackContext(unittest.TestCase):
    """A play reported with a context that has no usable uri must still be recorded
    as a track (with playedFrom=None). Returning None here used to crash the
    listener callback and stall all future listens until the item aged out of
    the recently-played feed."""

    def test_context_without_uri_still_returns_track(self):
        result = Client.formatTrack(_rawTrack(), 1000, 5000, context={})
        self.assertIsInstance(result, dict)
        self.assertEqual(result["id"], "t1")
        self.assertIsNone(result["playedFrom"])

    def test_context_with_empty_uri_still_returns_track(self):
        result = Client.formatTrack(_rawTrack(), 1000, 5000, context={"uri": ""})
        self.assertIsInstance(result, dict)
        self.assertIsNone(result["playedFrom"])

    def test_context_with_unrelated_uri_gives_no_playedFrom(self):
        result = Client.formatTrack(_rawTrack(), 1000, 5000, context={"uri": "spotify:artist:a1"})
        self.assertIsInstance(result, dict)
        self.assertIsNone(result["playedFrom"])

    def test_playlist_context_sets_playedFrom(self):
        result = Client.formatTrack(_rawTrack(), 1000, 5000, context={"uri": "spotify:playlist:pl1"})
        self.assertEqual(result["playedFrom"], "playlist:pl1")

    def test_album_context_sets_playedFrom(self):
        result = Client.formatTrack(_rawTrack(), 1000, 5000, context={"uri": "spotify:album:alb1"})
        self.assertEqual(result["playedFrom"], "album:alb1")

    def test_no_context_gives_no_playedFrom(self):
        result = Client.formatTrack(_rawTrack(), 1000, 5000)
        self.assertIsNone(result["playedFrom"])


class TestEmbedPlayInfoDurationCap(unittest.TestCase):
    """The duration cap guards against corrupt play times from the live
    listener (SpotipyFree sometimes reports absurd values). Sources with
    authoritative play times (history imports) opt out via capAtDuration=False."""

    TRACK_DURATION_MS = 200000
    LONGER_THAN_DURATION_MS = 250000
    PLAYED_AT = 1000

    def _track(self):
        return {"id": "t1", "duration": self.TRACK_DURATION_MS}

    def test_caps_at_duration_by_default(self):
        result = Client.embedPlayInfo(self._track(), self.PLAYED_AT, self.LONGER_THAN_DURATION_MS)
        self.assertEqual(result["timePlayed"], self.TRACK_DURATION_MS)

    def test_capping_can_be_disabled(self):
        result = Client.embedPlayInfo(self._track(), self.PLAYED_AT, self.LONGER_THAN_DURATION_MS,
                                      capAtDuration=False)
        self.assertEqual(result["timePlayed"], self.LONGER_THAN_DURATION_MS)

    def test_no_cap_when_duration_unknown(self):
        track = {"id": "t1", "duration": 0}
        result = Client.embedPlayInfo(track, self.PLAYED_AT, self.LONGER_THAN_DURATION_MS)
        self.assertEqual(result["timePlayed"], self.LONGER_THAN_DURATION_MS)

    def test_listener_format_track_still_caps(self):
        result = Client.formatTrack(_rawTrack(), self.PLAYED_AT, self.LONGER_THAN_DURATION_MS)
        self.assertEqual(result["timePlayed"], self.TRACK_DURATION_MS)


if __name__ == "__main__":
    unittest.main()
