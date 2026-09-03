"""Ratchet for UT-1 (2026-09-02 review): an htmx filter form with a search or
text input must list `submit` in hx-trigger, or Enter in that field falls
through to the browser's native form submission - a full navigation that
PUSHES a history entry and carries an empty query string (the vendored
htmx's shouldCancel only suppresses the native submit for a trigger it is
actually listening for).

/history's #historyFilters and the Top pages' shared #topListFilters
(templates/_page_card.html) both learned this the hard way; this scans every
`<form ...hx-get=|hx-post=...>` in templates/ so a new or edited one with a
text field cannot silently reintroduce the bug.
"""
import glob
import os
import re
import unittest

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_TEMPLATES_GLOB = os.path.join(_ROOT, "templates", "*.html")

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.S)
#< attrs is everything up to the form tag's own closing '>' - safe as long as
#  no attribute value itself contains a literal '>', true of every form here
_FORM = re.compile(r"<form\b([^>]*)>(.*?)</form>", re.S)
_HX_TRIGGER = re.compile(r'hx-trigger="([^"]*)"')
_TEXT_INPUT = re.compile(r'<input\b[^>]*\btype="(search|text)"')


def _readFile(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def htmxFormsWithATextField():
    """(template, attrs, body) for every hx-get/hx-post <form> in templates/
    that also holds an enabled search/text <input>."""
    found = []
    for path in sorted(glob.glob(_TEMPLATES_GLOB)):
        content = _JINJA_COMMENT.sub("", _readFile(path))
        for attrs, body in _FORM.findall(content):
            if "hx-get=" not in attrs and "hx-post=" not in attrs:
                continue
            if not _TEXT_INPUT.search(body):
                continue
            found.append((os.path.basename(path), attrs, body))
    return found


class TestHtmxFormsWithATextFieldListSubmit(unittest.TestCase):
    def test_every_such_form_lists_submit_in_hx_trigger(self):
        offending = []
        for template, attrs, _body in htmxFormsWithATextField():
            match = _HX_TRIGGER.search(attrs)
            triggers = [part.strip() for part in match.group(1).split(",")] if match else []
            if "submit" not in triggers:
                offending.append(template)

        self.assertEqual(offending, [])

    def test_the_scan_sees_the_known_forms(self):
        """The ratchet above passes on an empty scan; this pins that it reads
        the two forms it exists to guard."""
        templates = {template for template, _attrs, _body in htmxFormsWithATextField()}

        self.assertEqual(templates, {"history.html", "_page_card.html"})


if __name__ == "__main__":
    unittest.main()
