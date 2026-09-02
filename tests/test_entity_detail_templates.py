"""The artist and album detail pages are one template with parameters
(2026-09-02 review, UI-11).

_artist_detail_body.html and _album_detail_body.html were 88 lines each and
differed in twelve, every one an `artist.get(` / `album.get(` rename or one
of three strings; the two shells differed in eight of 81. Every fix to one
had to be re-applied to the other, and the album copy's own comment ("Mirrors
_artist_detail_body.html section for section") admitted the drift without
preventing it.

The shared markup now lives in _entity_detail_body.html and
_entity_detail_shell.html; the four original files stay as thin wrappers
that set the parameters and include them, so routes/charts.py's template
names and every test that requests these pages are untouched. These guards
keep the wrappers thin - the moment markup grows back into one of them the
two pages can diverge again.
"""
import os
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_TEMPLATES = os.path.join(_ROOT, "templates")

#< a wrapper is licence header, a comment, the {% set %}s and one include;
#  the shells add the extends/title/block lines. Real markup does not fit.
WRAPPER_LINE_CEILING = 24
_WRAPPERS = {
    "_artist_detail_body.html": "_entity_detail_body.html",
    "_album_detail_body.html": "_entity_detail_body.html",
    "artist_detail.html": "_entity_detail_shell.html",
    "album_detail.html": "_entity_detail_shell.html",
}


def _readFile(name):
    with open(os.path.join(_TEMPLATES, name), encoding="utf-8") as fh:
        return fh.read()


class TestDetailWrappersStayThin(unittest.TestCase):
    def test_each_wrapper_includes_the_shared_template(self):
        for wrapper, shared in _WRAPPERS.items():
            with self.subTest(wrapper=wrapper):
                #< the tag, whatever whitespace control it carries
                self.assertIn(f"include '{shared}'", _readFile(wrapper))

    def test_each_wrapper_is_short_enough_to_hold_no_markup(self):
        for wrapper in _WRAPPERS:
            with self.subTest(wrapper=wrapper):
                self.assertLessEqual(len(_readFile(wrapper).splitlines()), WRAPPER_LINE_CEILING)

    def test_the_shared_templates_name_neither_entity_directly(self):
        """The shared markup reads `entity`; an `artist.get(` or `album.get(`
        inside it is the drift creeping back under a new roof."""
        for shared in set(_WRAPPERS.values()):
            with self.subTest(shared=shared):
                body = _readFile(shared)
                self.assertNotIn("artist.get(", body)
                self.assertNotIn("album.get(", body)


if __name__ == "__main__":
    unittest.main()
