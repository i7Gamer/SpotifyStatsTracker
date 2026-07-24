import unittest
from services.export import (
    generatePlaylistCsv, generatePlaylistM3u, generatePlaylistXspf, PLAYLIST_CSV_COLUMNS,
)


class TestPlaylistExport(unittest.TestCase):
    def setUp(self):
        self.sample_tracks = [
            {
                "id": "t1",
                "name": "Song 1 <Rock>",
                "artists": [{"name": "Artist & Co"}],
                "album": {"name": "Album 1"},
                "isrc": "US1234567890",
                "url": "https://open.spotify.com/track/t1",
            },
            {
                "id": "t2",
                "name": "Song 2",
                "artists": [{"name": "Artist 2"}],
                "album": {"name": "Album 2"},
                "isrc": "",
                "url": "",
            },
        ]

    def test_generate_playlist_csv(self):
        content = "".join(generatePlaylistCsv(self.sample_tracks))
        lines = content.strip().split("\n")
        self.assertEqual(lines[0], ",".join(PLAYLIST_CSV_COLUMNS))
        self.assertIn("spotify:track:t1", lines[1])
        self.assertIn("US1234567890", lines[1])
        self.assertIn("Artist & Co", lines[1])

    def test_generate_playlist_m3u(self):
        content = "".join(generatePlaylistM3u(self.sample_tracks))
        lines = content.strip().split("\n")
        self.assertEqual(lines[0], "#EXTM3U")
        self.assertIn("#EXTINF:-1,Artist & Co - Song 1 <Rock>", content)
        self.assertIn("spotify:track:t1", content)
        self.assertIn("spotify:track:t2", content)

    def test_generate_playlist_xspf(self):
        content = "".join(generatePlaylistXspf(self.sample_tracks, title="Test Workout"))
        self.assertIn('<?xml version="1.0" encoding="UTF-8"?>', content)
        self.assertIn('<title>Test Workout</title>', content)
        self.assertIn('<location>spotify:track:t1</location>', content)
        # Check XML escaping of special characters like '&' and '<'
        self.assertIn('Artist &amp; Co', content)
        self.assertIn('Song 1 &lt;Rock&gt;', content)


if __name__ == "__main__":
    unittest.main()
