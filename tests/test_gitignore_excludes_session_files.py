# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""A real Spotify session file must never be committable.

Database/Spotify/smoketest.py tells the operator to create a spotapi sessions
file "beside this module" (session.example.json documents the shape), and
templates/admin.html points at that smoketest during a TOTP-rotation incident -
so the file WILL exist inside working trees, holding live sp_dc/sp_key cookies
that grant full account access. cookies*.json has been gitignored for exactly
this reason since the beginning; session*.json arrived with the owned client
and needs the same treatment.

Asserted through `git check-ignore` - the real matcher - rather than by parsing
.gitignore, for the same reason the .dockerignore suite matches paths instead
of grepping for lines: "the pattern is in the file" can be true while matching
nothing (root-anchoring, `*` vs `/`). The tracked example file doubles as the
negative control for the `!` exception.
"""
import os
import subprocess
import unittest

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Everywhere the docs could plausibly lead someone to put the real file.
MUST_BE_IGNORED = (
    "Database/Spotify/session.json",   #< "beside this module", per the smoketest usage text
    "session.json",                    #< repo root, where the smoketest is actually run from
    "sessions.json",                   #< smoketest.py calls it "a spotapi sessions file"
    "cookies.json",                    #< positive control: ignored since before this suite
)

MUST_NOT_BE_IGNORED = (
    "Database/Spotify/session.example.json",   #< placeholders only; the shape documentation
)


def _checkIgnore(path: str) -> bool:
    """True when git would ignore `path`. check-ignore exits 0 for ignored,
    1 for not ignored, 128 for a real error (which should fail loudly, not
    read as either answer)."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", path],
        cwd=_REPO_DIR, capture_output=True, text=True,
    )
    if result.returncode not in (0, 1):
        raise RuntimeError(f"git check-ignore failed for {path}: {result.stderr.strip()}")
    return result.returncode == 0


class GitignoreSessionFilesTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            subprocess.run(["git", "--version"], capture_output=True, check=True)
        except (OSError, subprocess.CalledProcessError):   # pragma: no cover - CI always has git
            raise unittest.SkipTest("git is not available")

    def test_real_session_files_are_ignored(self):
        for path in MUST_BE_IGNORED:
            with self.subTest(path=path):
                self.assertTrue(_checkIgnore(path),
                                f"{path} would be committable - it holds live Spotify cookies")

    def test_the_example_file_stays_tracked(self):
        for path in MUST_NOT_BE_IGNORED:
            with self.subTest(path=path):
                self.assertFalse(_checkIgnore(path),
                                 f"{path} is documentation and must stay in the repo")


if __name__ == "__main__":
    unittest.main()
