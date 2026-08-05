"""A `data-` attribute a script branches on must be one something actually sets.

Browser scripts read per-element configuration off `dataset`: milestone-more.js
reads `data-chunk-size`, play-embed.js reads `data-spotify-url`, tags.js reads
`data-entity-id`. Each of those is a contract with the markup, and the markup
half is invisible to every other check - ESLint reads only .js and never sees an
attribute, the Python tests assert on rendered HTML and cannot know which
attributes a script cares about.

So a knob can be added, documented, unit-tested against a synthetic object, and
never wired to anything. That is not a crash; it is worse. It reads as a
supported mechanism, so the next person configures it and nothing happens - and
the branch it guards silently never runs, which means the behaviour everyone
believes is configurable has exactly one setting.

That is what `data-htmx-failure` was: htmx-filters.js's failureUi branched on it
to choose the page-level banner over the inline error, the comment said the
choice "stays declarative, in the markup", tests/test_htmx_filters.js asserted
both branches - and no template ever emitted it. The two pages that genuinely
wanted a banner (/genres, /compare) hand-rolled their own handlers instead,
because their retry shape differs, so the knob had no possible user.

A knob counts as wired if a template emits it or another script sets it
(copy-link.js stashes `data-restore-text` on itself). Comments are stripped from
both sides first: the sibling gate in test_ajax_loader_error_handling.py spent
its life counting prose, and the comment naming a dead attribute is exactly the
thing that made it look alive.
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = REPO_ROOT / "templates"
STATIC_JS_DIR = REPO_ROOT / "static" / "js"
VENDOR_DIR = "vendor"

_LINE_COMMENT = re.compile(r"//[^\n]*")
_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)

#< `elt.dataset.someName` - the read. Bare `elt.dataset` (a presence check, as in
#  "is this an element at all") has no attribute behind it and is not a knob.
DATASET_READ = re.compile(r"dataset\s*\.\s*([A-Za-z][A-Za-z0-9]*)")


def _stripComments(src: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", src))


def _kebab(datasetName: str) -> str:
    """`htmxFailure` -> `data-htmx-failure`, the DOM's own mapping."""
    return "data-" + re.sub(r"([A-Z])", lambda m: "-" + m.group(1).lower(), datasetName)


def _browserScripts():
    return sorted(p for p in STATIC_JS_DIR.rglob("*.js") if VENDOR_DIR not in p.parts)


def _allTemplateText() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in TEMPLATES_DIR.rglob("*.html"))


def _allScriptCode() -> str:
    return "\n".join(_stripComments(p.read_text(encoding="utf-8")) for p in _browserScripts())


def _isWired(datasetName: str, attr: str, templates: str, scripts: str) -> bool:
    """Something puts this attribute on an element: a template renders it, or a
    script assigns it (`dataset.x =`, but not the `dataset.x ===` that is the
    READ we started from) or sets it by name."""
    if attr in templates:
        return True
    if re.search(r"dataset\s*\.\s*" + datasetName + r"\s*=(?!=)", scripts):
        return True
    return bool(re.search(r"setAttribute\(\s*['\"]" + attr, scripts))


class TestDataAttributeKnobsAreWired(unittest.TestCase):
    def test_every_dataset_read_has_something_that_sets_it(self):
        templates, scripts = _allTemplateText(), _allScriptCode()
        dead = []
        for path in _browserScripts():
            code = _stripComments(path.read_text(encoding="utf-8"))
            for datasetName in sorted(set(DATASET_READ.findall(code))):
                attr = _kebab(datasetName)
                if not _isWired(datasetName, attr, templates, scripts):
                    dead.append(f"{path.name} branches on {attr}, which nothing emits")
        self.assertEqual(
            dead, [],
            "Dead configuration: the branch behind each of these can never be taken, "
            "so the behaviour reads as configurable but has one setting. Either emit "
            "the attribute from the markup that wants it, or delete the branch.\n  "
            + "\n  ".join(dead))

    def test_the_scan_finds_the_knobs_that_are_wired(self):
        """Guards the gate: if the read pattern stopped matching, the test above
        would pass by finding nothing to check."""
        scripts = _allScriptCode()
        found = set(DATASET_READ.findall(scripts))
        for known in ("chunkSize", "spotifyUrl", "entityId"):
            with self.subTest(knob=known):
                self.assertIn(known, found)

    def test_a_knob_nothing_emits_is_reported(self):
        self.assertFalse(_isWired("madeUpKnob", "data-made-up-knob",
                                  _allTemplateText(), _allScriptCode()))

    def test_a_read_is_not_mistaken_for_the_thing_that_sets_it(self):
        """`dataset.x === 'v'` is the read. An `=` regex that also matched `===`
        would call every knob wired by its own reader - which is exactly how
        data-htmx-failure looked alive."""
        self.assertFalse(_isWired("knob", "data-knob", "", "if (t.dataset.knob === 'v') {}"))
        self.assertTrue(_isWired("knob", "data-knob", "", "t.dataset.knob = 'v';"))


if __name__ == "__main__":
    unittest.main()
