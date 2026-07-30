"""Nothing private may reach the image, whoever builds it.

The published images are built by .github/workflows/, whose context is a fresh
actions/checkout - so it holds only TRACKED files, and autoImport/, TestData/ and
.claude/ are all gitignored. Nothing private has ever shipped, and this file is
not guarding an incident.

It guards the OTHER build path: uploadDocker.ps1 is committed, runs
`docker build .` against the developer's real working tree, and pushes to the
same public i7gamer/spotify-tracker:latest tag. There, `COPY . .` takes whatever
is on disk - and docker-compose's bind mounts hide the result, since the running
container reads the host's folder either way, so the copy baked into the layer
would never be noticed locally.

The trap it pins is that .dockerignore patterns are ROOT-ANCHORED and `*` does
not cross `/`. `Streaming*.json` therefore never matched
`autoImport/<user>/Streaming_History_Audio_2023.json` - the export files the
folder exists to hold - even though it reads exactly like it would.

So this asserts on PATHS, through a matcher, rather than on the presence of
lines: "the pattern is in the file" was true for Streaming*.json the whole time.
"""
import fnmatch
import os
import sys
import unittest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

_REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCKERIGNORE = os.path.join(_REPO_DIR, ".dockerignore")

# Paths that must never enter the image. Spelled as they appear relative to the
# build context, including the nested ones that defeated the anchored patterns.
MUST_BE_EXCLUDED = (
    #< the drop folder for Spotify export uploads: complete listening histories
    "autoImport/timorzipa/Streaming_History_Audio_2023.json",
    "autoImport/7kevinegger/StreamingHistory0.json",
    "autoImport/testuser/endsong_0.json",
    #< a real export of the maintainer's own top songs, present in the tree
    "TestData/Most played Songs - Week 24 - 2026.csv",
    #< local agent state, including a full second copy of the source tree
    ".claude/settings.local.json",
    ".claude/worktrees/some-worktree/app.py",
    #< the databases hold Spotify session cookies; one inside a worktree is the
    #  case the root-anchored *.db rule could not reach
    "Database/Data/spotify_stats.db",
    ".claude/worktrees/some-worktree/Database/Data/spotify_stats.db",
    "secrets/secret.key",
    ".claude/worktrees/some-worktree/secrets/secret.key",
    "Database/Data/app.log",
    ".claude/worktrees/some-worktree/Database/Data/app.log",
    #< the smoketest's session file holds live sp_dc/sp_key cookies, and its
    #  own docs say to keep it "beside this module" - i.e. inside the tree
    "Database/Spotify/session.json",
    "session.json",
    "sessions.json",
)

# The image genuinely needs these - a rule broad enough to catch the above must
# not swallow them (negative control for the matcher AND for the patterns).
MUST_BE_INCLUDED = (
    "app.py",
    "wsgi.py",
    "config.py",
    "requirements.txt",
    "Database/database.py",
    #< Database/Data is deliberately NOT here: it is excluded wholesale, and
    #  ConnectionManager mkdirs it on first connect (Database/db.py:441)
    "routes/charts.py",
    "services/export.py",
    "static/js/dashboard-page.js",
    "templates/tracks.html",
    #< the license must travel with the work (AGPL-3.0 / GPL-3.0 section 4)
    "COPYING",
    "NOTICE",
    #< placeholders only, and the reference the smoketest's usage text points at -
    #  the negation that keeps it is also this suite's proof negation works at all
    "Database/Spotify/session.example.json",
)


def _patterns():
    """The .dockerignore's effective patterns, comments and blanks dropped."""
    with open(DOCKERIGNORE, encoding="utf-8") as handle:
        for rawLine in handle:
            line = rawLine.strip()
            if line and not line.startswith("#"):
                yield line.rstrip("/")   #< docker cleans the path, so a trailing / is not part of it


def _matches(pattern: str, path: str) -> bool:
    """Docker's .dockerignore semantics, for the pattern shapes this file uses.

    A pattern is matched against the whole context-relative path (it is NOT
    applied per directory the way .gitignore would), and it also excludes
    everything beneath a directory it names. `**` is the only way to cross a
    separator - fnmatch's `*` would happily do so, which is exactly the illusion
    that hid the bug, so single-star patterns are matched segment by segment.
    """
    if "**" in pattern:
        return fnmatch.fnmatch(path, pattern.replace("**", "*"))

    patternParts = pattern.split("/")
    pathParts = path.split("/")
    if len(pathParts) < len(patternParts):
        return False
    #< a directory pattern also covers its subtree, so compare only the prefix
    return all(fnmatch.fnmatch(pathPart, patternPart)
               for pathPart, patternPart in zip(pathParts, patternParts))


def _isExcluded(path: str) -> bool:
    """Docker applies the rules in order and the LAST match wins, which is what
    lets a `!` exception re-include one file from under a broad exclusion."""
    excluded = False
    for pattern in _patterns():
        if pattern.startswith("!"):
            if _matches(pattern[1:], path):
                excluded = False
        elif _matches(pattern, path):
            excluded = True
    return excluded


class DockerignoreTestCase(unittest.TestCase):
    def test_private_paths_are_excluded_from_the_image(self):
        for path in MUST_BE_EXCLUDED:
            with self.subTest(path=path):
                self.assertTrue(_isExcluded(path),
                                f"{path} would be copied into the published public image")

    def test_the_application_itself_is_still_copied(self):
        for path in MUST_BE_INCLUDED:
            with self.subTest(path=path):
                self.assertFalse(_isExcluded(path),
                                 f"{path} is needed at runtime but would be excluded")

    def test_a_single_star_does_not_cross_a_directory_separator(self):
        """The matcher's own negative control: if `*` crossed `/`, every
        assertion above would pass for the wrong reason - which is precisely how
        `Streaming*.json` looked correct while matching nothing."""
        self.assertFalse(_matches("Streaming*.json", "autoImport/alice/StreamingHistory0.json"))
        self.assertTrue(_matches("**/Streaming*.json", "autoImport/alice/StreamingHistory0.json"))
        self.assertTrue(_matches("Streaming*.json", "StreamingHistory0.json"))

    def test_a_directory_pattern_covers_its_subtree(self):
        self.assertTrue(_matches("secrets", "secrets/secret.key"))
        self.assertFalse(_matches("secrets", "secretsomething.py"))

    def test_a_negation_only_reincludes_its_own_path(self):
        """The matcher's negation control: last match wins, and an exception
        re-includes exactly its named file - not its siblings."""
        #< bare + **/ pair, same as the real file: `**` does not cover the
        #  zero-directory case in this matcher (nor reliably in docker's own)
        patterns = ["session*.json", "**/session*.json",
                    "!Database/Spotify/session.example.json"]

        def isExcluded(path):
            excluded = False
            for pattern in patterns:
                if pattern.startswith("!"):
                    if _matches(pattern[1:], path):
                        excluded = False
                elif _matches(pattern, path):
                    excluded = True
            return excluded

        self.assertFalse(isExcluded("Database/Spotify/session.example.json"))
        self.assertTrue(isExcluded("Database/Spotify/session.json"))
        self.assertTrue(isExcluded("session.json"))


if __name__ == "__main__":
    unittest.main()
