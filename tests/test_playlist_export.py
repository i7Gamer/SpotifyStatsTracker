import unittest
from services.export import (
    generatePlaylistCsv, generatePlaylistM3u, generatePlaylistXspf, PLAYLIST_CSV_COLUMNS,
    _csvSafeCell,
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

    def test_csv_safe_cell_prefixes_formula_leaders(self):
        # A cell whose text starts with a spreadsheet formula character is
        # prefixed with an apostrophe; everything else is passed through.
        self.assertEqual(_csvSafeCell("=1+1"), "'=1+1")
        self.assertEqual(_csvSafeCell("+ok"), "'+ok")
        self.assertEqual(_csvSafeCell("-ok"), "'-ok")
        self.assertEqual(_csvSafeCell("@ok"), "'@ok")
        self.assertEqual(_csvSafeCell("\tok"), "'\tok")
        self.assertEqual(_csvSafeCell("normal"), "normal")
        self.assertEqual(_csvSafeCell("Artist & Co"), "Artist & Co")
        self.assertEqual(_csvSafeCell(""), "")
        self.assertEqual(_csvSafeCell(None), "")

    def test_generate_playlist_csv_neutralizes_formula_injection(self):
        tracks = [{
            "id": "t9",
            "name": '=HYPERLINK("http://evil","x")',
            "artists": [{"name": "-2+3"}],
            "album": {"name": "@SUM(A1:A9)"},
            "isrc": "",
            "url": "",
        }]
        content = "".join(generatePlaylistCsv(tracks))
        # Each formula-leading text cell is defused with a leading apostrophe.
        self.assertIn("'=HYPERLINK", content)
        self.assertIn("'-2+3", content)
        self.assertIn("'@SUM", content)
        # The machine-readable URI column is never rewritten.
        self.assertIn("spotify:track:t9", content)

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


class TestM3uIsLineSafe(unittest.TestCase):
    """M3U is line-oriented, so a CR/LF in a track or artist name would end the
    #EXTINF line early and let the rest be read as further playlist entries -
    an arbitrary extra URI in the file the user opens. The CSV and XSPF
    generators escape their own metacharacters; this is the M3U equivalent."""

    def _m3u(self, name, artist="Artist"):
        return "".join(generatePlaylistM3u([{
            "id": "t1", "name": name, "artists": [{"name": artist}],
            "album": {"name": "Album"}, "isrc": "", "url": "",
        }]))

    def test_a_newline_in_a_title_cannot_inject_a_line(self):
        content = self._m3u("Song\n#EXTINF:-1,Injected\nspotify:track:evil")

        lines = [line for line in content.splitlines() if line]
        # The injected text survives as inert characters inside the #EXTINF
        # line; what matters is that it never becomes a line of its own, which
        # is the only way a player would read it as another entry.
        self.assertEqual(lines, ["#EXTM3U",
                                 "#EXTINF:-1,Artist - Song #EXTINF:-1,Injected spotify:track:evil",
                                 "spotify:track:t1"])

    def test_a_carriage_return_in_an_artist_name_is_neutralized(self):
        content = self._m3u("Song", artist="Artist\rInjected")

        self.assertEqual(len([line for line in content.splitlines() if line]), 3)

    def test_ordinary_names_are_untouched(self):
        content = self._m3u("Song 1 <Rock>", artist="Artist & Co")

        self.assertIn("#EXTINF:-1,Artist & Co - Song 1 <Rock>", content)


if __name__ == "__main__":
    unittest.main()
