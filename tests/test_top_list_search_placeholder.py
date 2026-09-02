"""The Top pages' search box promises only what its query matches
(2026-09-02 review, UI-08).

_page_card.html is shared by /top-songs, /top-artists and /top-albums and
read "Search songs, artists, playlists" on all three - but the artists
query matches artist names only, the albums query album names or their
artists, and playlists (played_from) are matched by /history's search
alone. A reader typing a song title into Top Artists got an empty list
with no hint that the field could never find it.
"""
import unittest

import bs4

from test_top_list_default_window import TopListWindowTestCase

#< what each page's list query actually matches - see the searchQuery notes
#  on getSongsStats / getArtistsStats / getAlbumsStats in Database/queries
_PLACEHOLDERS = {
    "/top-songs": "Search songs, artists, albums",
    "/top-artists": "Search artists",
    "/top-albums": "Search albums or their artists",
}
#< only /history's search matches where a play came from
_HISTORY_ONLY_TERM = "playlists"


class TestTopPageSearchPlaceholders(TopListWindowTestCase):
    def _placeholder(self, path):
        shell = bs4.BeautifulSoup(self._shell(path), "html.parser")
        return shell.select_one("#searchQuery")["placeholder"]

    def test_each_top_page_says_what_its_search_matches(self):
        for path, expected in _PLACEHOLDERS.items():
            with self.subTest(path=path):
                self.assertEqual(self._placeholder(path), expected)

    def test_no_top_page_promises_playlists(self):
        for path in _PLACEHOLDERS:
            with self.subTest(path=path):
                self.assertNotIn(_HISTORY_ONLY_TERM, self._placeholder(path))


if __name__ == "__main__":
    unittest.main()
