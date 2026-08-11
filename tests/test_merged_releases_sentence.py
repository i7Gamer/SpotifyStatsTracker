# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The song hero's "Also released on ..." line is one readable sentence.

Every other whitespace join in that block is trimmed by hand ({%- ... -#}),
because the sentence is assembled from a loop and inline elements. The one
between the admin Split <button> and its </form> was not, and an inline form
renders that newline as a space - so the closing period detached:

    Also released on Psycho Killer (Single) split . Plays across all ...

Found in the browser, which is the only place it shows: the markup looks
correct in the file, and every route test asserting on this block passes
either way. Rendered through a bare Jinja environment rather than a route so
the assertion pins the template itself.
"""
import os
import re
import sys
import unittest

import jinja2

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TEMPLATES = os.path.join(_ROOT, "templates")

_RELEASES = [
    {"trackId": "A" * 22, "album": {"id": "albSingle", "name": "Psycho Killer (Single)"}},
    {"trackId": "C" * 22, "album": {"id": "albBest", "name": "The Best Of"}},
]


def _heroSentence(isAdmin, releases=_RELEASES):
    """The rendered text of the merged-releases block, whitespace-collapsed the
    way a browser collapses it."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES), autoescape=True)
    env.globals["url_for"] = lambda *args, **kwargs: "#"
    env.globals["csrf_token"] = lambda: "token"
    source = env.loader.get_source(env, "song_detail.html")[0]
    #< the block alone: the full page extends layout.html and pulls in the
    #  whole chrome, none of which this is about
    start = source.index('<div class="hero-merged-releases">')
    end = source.index("</div>", start) + len("</div>")
    rendered = env.from_string(source[start:end]).render(
        mergedReleases=releases, isAdmin=isAdmin)
    #< tags drop to NOTHING, not to a space: a browser collapses whitespace in
    #  text nodes, and markup between two words contributes none of its own.
    #  Substituting a space here would fabricate the very gap being asserted
    #  against, and pass whatever the template did.
    return " ".join(re.sub(r"<[^>]+>", "", rendered).split())


def _heroMarkup(isAdmin=True, releases=_RELEASES):
    """The block's raw HTML, for assertions about the control rather than the
    prose."""
    env = jinja2.Environment(loader=jinja2.FileSystemLoader(_TEMPLATES), autoescape=True)
    env.globals["url_for"] = lambda *args, **kwargs: "#"
    env.globals["csrf_token"] = lambda: "token"
    source = env.loader.get_source(env, "song_detail.html")[0]
    start = source.index('<div class="hero-merged-releases">')
    end = source.index("</div>", start) + len("</div>")
    return env.from_string(source[start:end]).render(mergedReleases=releases, isAdmin=isAdmin)


class MergedReleasesSentenceTestCase(unittest.TestCase):
    def test_the_period_is_not_orphaned_from_the_last_release(self):
        """The original defect: a newline before </form> renders as a space in
        an inline form, so the period drifted off the sentence.

        Asserted on the MARKUP now that the control is an icon. Stripping tags
        to nothing stopped modelling what a reader sees the moment the control
        carried no text of its own: the deliberate space between the album
        link and the icon survives that stripping and looks like the very
        defect this guards, while on screen it is just the gap the icon sits
        in. What must still hold is that nothing separates the control from
        the period."""
        markup = _heroMarkup(isAdmin=True)

        self.assertIn("</button></form>.", markup)
        self.assertNotIn(" .", _heroSentence(isAdmin=False))

    def test_the_control_contributes_no_words_to_the_sentence(self):
        """It used to spell itself "split", lowercase, immediately after the
        album link - so the sentence read "Also released on Mono Masters split
        and The Best Of split." and the control looked like part of the title.
        An icon says the same thing without joining the prose."""
        admin = _heroSentence(isAdmin=True)

        self.assertNotIn("split", admin.lower(), admin)
        #< the ONLY difference from the plain sentence is the space the icon
        #  occupies; no words are added or removed
        self.assertEqual(admin.replace(" .", "."), _heroSentence(isAdmin=False))


class SplitControlTestCase(unittest.TestCase):
    """An icon-only button has no text to name it, so the accessible name has
    to come from somewhere - and there are TWO of them on a two-release song,
    both firing a destructive instance-wide action. "split, button" twice over
    tells a screen-reader user nothing about which release each one detaches.

    The sibling control on /admin/merge-review already settled this: its radio
    carries an aria-label naming the release, with a comment saying why the
    neighbouring text is not enough."""

    def test_each_control_names_the_release_it_would_split(self):
        markup = _heroMarkup()

        self.assertIn("Split Psycho Killer (Single) out of this song", markup)
        self.assertIn("Split The Best Of out of this song", markup)

    def test_the_glyph_is_hidden_from_the_accessible_name(self):
        """Otherwise the SVG's own content competes with the aria-label."""
        markup = _heroMarkup()

        self.assertEqual(markup.count('aria-hidden="true"'), len(_RELEASES))

    def test_a_reader_who_cannot_split_gets_no_control_at_all(self):
        markup = _heroMarkup(isAdmin=False)

        self.assertNotIn("<button", markup)
        self.assertNotIn("aria-label", markup)

    def test_the_same_holds_for_a_reader_who_sees_no_split_control(self):
        sentence = _heroSentence(isAdmin=False)

        self.assertNotIn(" .", sentence, sentence)
        self.assertIn("The Best Of.", sentence)

    def test_the_releases_still_read_as_a_list(self):
        sentence = _heroSentence(isAdmin=False)

        self.assertIn("Also released on Psycho Killer (Single) and The Best Of.", sentence)

    def test_a_single_release_needs_no_conjunction(self):
        sentence = _heroSentence(isAdmin=False, releases=_RELEASES[:1])

        self.assertIn("Also released on Psycho Killer (Single).", sentence)
        self.assertNotIn(" and ", sentence)


if __name__ == "__main__":
    unittest.main()
