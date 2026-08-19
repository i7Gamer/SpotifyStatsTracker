import unittest
from xml.etree import ElementTree

from services.export import (
    generatePlaylistCsv, generatePlaylistM3u, generatePlaylistXspf, PLAYLIST_CSV_COLUMNS,
    _csvSafeCell,
)

#< every element generatePlaylistXspf emits carries the XSPF namespace, so
#  findtext() has to name it - the alternative is stripping it from every tag
XSPF_NS = "{http://xspf.org/ns/0/}"


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


class TestXspfIsParseableXml(unittest.TestCase):
    """XML 1.0 forbids most C0 control characters outright - there is no escape
    for them, and one makes the WHOLE document unparseable rather than just the
    element holding it. They reach here from two directions: track/artist/album
    names come out of uploaded export files, and the playlist title is built
    straight out of ?tags= (routes/tags.py::playlistExport). Every assertion
    below PARSES the result instead of matching strings, because "a player can
    open this file" is the actual claim being made."""

    # The three C0 characters XML 1.0 does allow. They must survive: dropping
    # them would rewrite names that are legal in the format today.
    TAB, LINE_FEED, CARRIAGE_RETURN = chr(0x09), chr(0x0A), chr(0x0D)
    FORBIDDEN = "".join(chr(code) for code in
                        list(range(0x00, 0x09)) + [0x0B, 0x0C] + list(range(0x0E, 0x20)))

    def _xspf(self, name="Song", artist="Artist", album="Album", title="Playlist"):
        return "".join(generatePlaylistXspf([{
            "id": "t1", "name": name, "artists": [{"name": artist}],
            "album": {"name": album}, "isrc": "", "url": "",
        }], title=title))

    def _trackField(self, content, field):
        root = ElementTree.fromstring(content)
        return root.findtext(f"{XSPF_NS}trackList/{XSPF_NS}track/{XSPF_NS}{field}")

    def test_a_control_character_in_a_track_name_still_parses(self):
        content = self._xspf(name="Song" + chr(0x0B) + "Two")

        self.assertEqual(self._trackField(content, "title"), "SongTwo")

    def test_a_control_character_in_an_artist_name_still_parses(self):
        content = self._xspf(artist="Artist" + chr(0x00) + "Zero")

        self.assertEqual(self._trackField(content, "creator"), "ArtistZero")

    def test_a_control_character_in_an_album_name_still_parses(self):
        content = self._xspf(album="Album" + chr(0x1F) + "One")

        self.assertEqual(self._trackField(content, "album"), "AlbumOne")

    def test_a_control_character_in_the_playlist_title_still_parses(self):
        # The one an ordinary user reaches without importing anything:
        # /playlist/export?fmt=xspf&tags=<control character>.
        root = ElementTree.fromstring(self._xspf(title="Playlist (" + chr(0x08) + ")"))

        self.assertEqual(root.findtext(f"{XSPF_NS}title"), "Playlist ()")

    def test_every_character_xml_forbids_is_removed(self):
        content = self._xspf(name="a" + self.FORBIDDEN + "b")

        self.assertEqual(self._trackField(content, "title"), "ab")

    def test_the_control_characters_xml_allows_are_kept(self):
        content = self._xspf(name="a" + self.TAB + self.LINE_FEED + self.CARRIAGE_RETURN + "b")

        # The parser normalizes CR to LF on the way back out (XML's own
        # line-end handling), so this asserts nothing was DROPPED rather than
        # byte equality.
        self.assertEqual(self._trackField(content, "title"),
                         "a" + self.TAB + self.LINE_FEED + self.LINE_FEED + "b")

    def test_the_metacharacters_it_already_escaped_still_round_trip(self):
        content = self._xspf(name="Song 1 <Rock>", artist="Artist & Co")

        self.assertEqual(self._trackField(content, "title"), "Song 1 <Rock>")
        self.assertEqual(self._trackField(content, "creator"), "Artist & Co")


if __name__ == "__main__":
    unittest.main()
