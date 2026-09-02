"""Control labels are Title Case (2026-09-02 review, UI-09).

Buttons and button-styled links mixed "Save Email Settings" with "Create
backup now" - both on /admin, sixty lines apart - and the profile's
"Log out of Tracker" sat beside the topbar's "Log out" for the same POST.
One convention now: Title Case for control labels, with the small joining
words ("to", "with", "of", "and", ...) left lowercase, and the two logout
buttons reading identically. Headings keep whatever case they have; this
looks only at controls.
"""
import glob
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_TEMPLATES_GLOB = os.path.join(_ROOT, "templates", "*.html")

#< the joining words that stay lowercase inside a Title Case label
_MINOR_WORDS = {"a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "of", "on", "or", "the", "to", "with"}
#< labels that are not a Title Case candidate: the artist-list expander is a
#  count ("+2 more"), not a command
_EXEMPT_LABELS = {"+ more"}

_CONTROL = re.compile(r"<(button|a)\b([^>]*)>(.*?)</\1>", re.S)
_TAG = re.compile(r"<[^>]+>")
_JINJA = re.compile(r"\{[{%#].*?[}%#]\}", re.S)
_ENTITY = re.compile(r"&[a-z]+;")

#< the one action with two buttons: the topbar's and the profile's
_LOGOUT_SITES = ("layout.html", "_profile_base.html")
_LOGOUT_LABEL = "Log Out"


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _labelText(inner):
    text = _JINJA.sub("", _TAG.sub("", inner))
    text = _ENTITY.sub(" ", text)
    return " ".join(text.split())


def controlLabels():
    """(template, label) for every <button> and every <a> styled as one."""
    found = []
    for path in sorted(glob.glob(_TEMPLATES_GLOB)):
        for tag, attrs, inner in _CONTROL.findall(_readFile(path)):
            if tag == "a" and "button" not in attrs:
                continue
            label = _labelText(inner)
            if label:
                found.append((os.path.basename(path), label))
    return found


def isTitleCase(label):
    """Every word that starts with a letter is capitalised, except a minor
    word that is not the first."""
    words = label.split()
    for index, word in enumerate(words):
        if not word[0].isalpha():
            continue
        if index and word.lower() in _MINOR_WORDS:
            continue
        if not word[0].isupper():
            return False
    return True


class TestControlLabelsAreTitleCase(unittest.TestCase):
    def test_every_button_and_button_link_label_is_title_case(self):
        offending = [(template, label) for template, label in controlLabels()
                     if label not in _EXEMPT_LABELS and not isTitleCase(label)]

        self.assertEqual(offending, [])

    def test_the_scan_sees_the_controls(self):
        """The ratchet above passes on an empty scan; this pins that it reads
        the buttons it is meant to."""
        self.assertIn(("admin.html", "Create Backup Now"), controlLabels())

    def test_the_two_logout_buttons_read_the_same(self):
        for template in _LOGOUT_SITES:
            with self.subTest(template=template):
                logout = [label for name, label in controlLabels()
                          if name == template and label.startswith("Log")]

                self.assertEqual(logout, [_LOGOUT_LABEL])


class TestTitleCaseRule(unittest.TestCase):
    def test_minor_words_stay_lowercase_inside_but_not_first(self):
        self.assertTrue(isTitleCase("Restart App to Apply"))
        self.assertTrue(isTitleCase("Log In with Cookies"))
        self.assertFalse(isTitleCase("Restart app to apply"))
        self.assertFalse(isTitleCase("to Apply"))

    def test_symbols_and_numbers_are_not_words(self):
        self.assertTrue(isTitleCase("Same Recording Merge"))
        self.assertTrue(isTitleCase("+ 2 More"))


if __name__ == "__main__":
    unittest.main()
