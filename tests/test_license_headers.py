"""Every file the built artifact conveys names its copyright and its license.

The project-level `COPYING`/`NOTICE`/`LICENSE.MIT` cover the work as a whole;
these two SPDX lines are what travels with a single file copied out of the tree.
The AGPL's own "How to Apply These Terms" asks for exactly this much - "at least
the 'copyright' line and a pointer to where the full notice is found" - so the
short form is the requirement, not an abbreviation of one.

This test exists because a sweep is a one-time event and the next new file would
silently ship unmarked. Scope deliberately matches what the Docker image
conveys (see .dockerignore): tests/ and dev.py are excluded there, so they are
excluded here.

The mixed-authorship check is computed from `git blame` at test time rather than
hardcoded. A file that gains or loses upstream lines then fails loudly instead
of quietly carrying a wrong attribution - which is the failure mode that
actually matters, since MIT's notice-retention condition is what those extra
lines exist to satisfy.
"""
import subprocess
import sys
import os
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO_ROOT = Path(__file__).resolve().parent.parent

#< the license text every in-scope file must point at
LICENSE_TAG = "SPDX-License-Identifier: AGPL-3.0-or-later"
COPYRIGHT_TAG = "SPDX-FileCopyrightText:"
#< the upstream author whose MIT lines survive in some files (see NOTICE)
UPSTREAM_AUTHOR = "Tzur Soffer"

SOURCE_GLOBS = ("*.py", "*.js", "*.html", "*.css")
# Not conveyed by the image - .dockerignore excludes both. Kept as the single
# definition of "in scope" so the sweep and this test can't drift apart.
EXCLUDED_PREFIXES = ("tests/",)
EXCLUDED_FILES = ("dev.py",)

#< a header buried under a hundred lines is not a notice anyone will see
MAX_HEADER_LINES = 12


def _git(*args):
    return subprocess.run(("git", *args), cwd=REPO_ROOT, capture_output=True,
                          text=True, errors="replace").stdout


def inScopeFiles():
    """Tracked source files the built artifact conveys."""
    tracked = _git("ls-files", *SOURCE_GLOBS).split()
    return sorted(
        f for f in tracked
        if not f.startswith(EXCLUDED_PREFIXES) and f not in EXCLUDED_FILES
    )


def _upstreamLineCount(path):
    """How many of this file's current lines git blames on the upstream author."""
    blame = _git("blame", "--line-porcelain", "--", path)
    return sum(1 for line in blame.splitlines()
               if line.startswith("author ") and UPSTREAM_AUTHOR in line)


def _head(path, lines=MAX_HEADER_LINES):
    text = (REPO_ROOT / path).read_text(encoding="utf-8", errors="replace")
    return "\n".join(text.splitlines()[:lines])


class TestScopeItself(unittest.TestCase):
    """If this file set ever comes back empty the assertions below all pass
    vacuously, which would be the quietest possible way to lose the guard."""

    def test_the_file_set_is_not_empty(self):
        self.assertGreater(len(inScopeFiles()), 100)

    def test_tests_and_dev_are_out_of_scope(self):
        files = inScopeFiles()

        self.assertNotIn("dev.py", files)
        self.assertFalse([f for f in files if f.startswith("tests/")])

    def test_every_source_kind_is_represented(self):
        """Guards the globs: dropping '*.html' would silently stop checking 53
        files rather than fail."""
        suffixes = {Path(f).suffix for f in inScopeFiles()}

        self.assertEqual(suffixes, {".py", ".js", ".html", ".css"})


class TestLicenseHeaders(unittest.TestCase):
    def test_every_file_declares_the_license(self):
        missing = [f for f in inScopeFiles() if LICENSE_TAG not in _head(f)]

        self.assertEqual(missing, [], f"{len(missing)} file(s) missing {LICENSE_TAG!r}")

    def test_every_file_declares_a_copyright_holder(self):
        missing = [f for f in inScopeFiles() if COPYRIGHT_TAG not in _head(f)]

        self.assertEqual(missing, [], f"{len(missing)} file(s) missing {COPYRIGHT_TAG!r}")

    def test_the_notice_sits_at_the_top_of_the_file(self):
        """Within MAX_HEADER_LINES, not merely present somewhere in the file -
        a match inside a string literal or a mid-file comment isn't a notice."""
        buried = []
        for f in inScopeFiles():
            text = (REPO_ROOT / f).read_text(encoding="utf-8", errors="replace")
            if LICENSE_TAG in text and LICENSE_TAG not in _head(f):
                buried.append(f)

        self.assertEqual(buried, [])


class TestMixedAuthorshipAttribution(unittest.TestCase):
    """Files still carrying upstream MIT lines must name their author too.

    A bare "Copyright i7Gamer" on those would both misstate authorship and drop
    the notice MIT requires be retained.
    """

    @classmethod
    def setUpClass(cls):
        #< one blame pass for the whole suite; it is ~200 subprocess calls
        cls.upstreamCounts = {f: _upstreamLineCount(f) for f in inScopeFiles()}

    def test_some_files_still_carry_upstream_lines(self):
        """If this hits zero the fork has fully diverged and the dual-attribution
        rule below is dead code - a real event worth noticing deliberately."""
        withUpstream = [f for f, n in self.upstreamCounts.items() if n]

        self.assertTrue(withUpstream, "no upstream lines survive - revisit NOTICE and this test")

    def test_files_with_upstream_lines_credit_the_upstream_author(self):
        uncredited = [f for f, n in self.upstreamCounts.items()
                      if n and UPSTREAM_AUTHOR not in _head(f)]

        self.assertEqual(uncredited, [],
                         f"{len(uncredited)} file(s) contain upstream lines but don't credit "
                         f"{UPSTREAM_AUTHOR}")

    def test_files_without_upstream_lines_do_not_claim_upstream_authorship(self):
        """The other direction: a copy-pasted header would credit an author who
        has no lines in that file, which is its own misstatement."""
        overCredited = [f for f, n in self.upstreamCounts.items()
                        if not n and UPSTREAM_AUTHOR in _head(f)]

        self.assertEqual(overCredited, [])

    def test_the_license_expression_stays_agpl_only(self):
        """Deliberately NOT `AGPL-3.0-or-later AND MIT`. AND would assert the
        file as a whole requires compliance with both licenses; it doesn't - AGPL
        is the effective license of the combination, and the MIT portions are
        additionally available under MIT. See NOTICE."""
        wrong = [f for f in inScopeFiles() if "AND MIT" in _head(f)]

        self.assertEqual(wrong, [])


if __name__ == "__main__":
    unittest.main()
