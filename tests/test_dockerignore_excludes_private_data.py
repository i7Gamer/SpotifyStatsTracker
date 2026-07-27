"""Nothing private may reach the published image.

`Dockerfile` ends in `COPY . .`, and `uploadDocker.ps1` pushes the result to a
PUBLIC Docker Hub repository. So every .dockerignore gap is a disclosure, and
`docker-compose`'s bind mounts hide it locally: the running container reads the
host's real folder either way, so a maintainer would never notice the copy
baked into the layer.

The trap this pins is that .dockerignore patterns are ROOT-ANCHORED and `*` does
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
DOCKERFILE = os.path.join(_REPO_DIR, "Dockerfile")

# Paths that must never enter the image. Spelled as they appear relative to the
# build context, including the nested ones that defeated the anchored patterns.
MUST_BE_EXCLUDED = (
    #< the drop folder for Spotify export uploads: complete listening histories
    "autoImport/timorzipa/Streaming_History_Audio_2023.json",
    "autoImport/7kevinegger/StreamingHistory0.json",
    "autoImport/testuser/endsong_0.json",
    #< a real export of the maintainer's own top songs, present in the tree today
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
    #< the licenses must travel with the work (AGPL-3.0 / GPL-3.0 section 4)
    "COPYING",
    "LICENSE.MIT",
    "NOTICE",
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
    return any(_matches(pattern, path) for pattern in _patterns())


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


class DockerfileTestCase(unittest.TestCase):
    """The image must not run as root: docker-compose bind-mounts the host's own
    Database/Data and autoImport, so a root process writes root-owned files into
    the user's working tree, and any RCE inherits write access to them."""

    def setUp(self):
        with open(DOCKERFILE, encoding="utf-8") as handle:
            self.lines = [line.strip() for line in handle]
        self.text = "\n".join(self.lines)

    def _indexOf(self, prefix):
        for index, line in enumerate(self.lines):
            if line.startswith(prefix):
                return index
        return -1

    def test_the_container_drops_root(self):
        self.assertIn("USER app", self.lines)

    def test_root_is_dropped_after_the_code_is_copied(self):
        """USER before COPY would make the copied tree app-owned and, worse,
        break the pip install that has to run as root."""
        userIndex = self._indexOf("USER app")
        copyIndex = self._indexOf("COPY . .")

        self.assertGreater(userIndex, copyIndex)
        self.assertGreater(userIndex, self._indexOf("RUN apt-get"))

    def test_the_writable_volumes_are_owned_by_that_user(self):
        """Ownership has to be handed over inside the image, or the first write
        to a bind mount fails instead of the app starting."""
        self.assertIn("chown -R app:app", self.text)
        for volume in ("/app/Database/Data", "/app/autoImport", "/app/secrets"):
            with self.subTest(volume=volume):
                self.assertIn(volume, self.text)


if __name__ == "__main__":
    unittest.main()
