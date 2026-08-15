# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""The runtime image must not ship pip.

pip is a build tool: `pip install -r requirements.txt` runs once during the
build and nothing at runtime invokes it (CMD is `python wsgi.py`). Two reasons
it is removed in the same RUN that used it:

- It was the sole source of every "fixable" finding in the image scan. The two
  packages the scanner names, msgpack and setuptools, are not dependencies of
  this app at all - they come from pip/_vendor/vendor.txt, pip's manifest of
  what it vendors. setuptools is not even present as code (`import setuptools`
  raises), and msgpack only ever executes as pip._vendor.msgpack while pip
  itself is running. No requirements pin can reach either, because the
  vendored version is pip's to choose. Measured 2026-08-15: removing pip took
  the image from 2C/5H/5M to 2C/2H/4M, and its Scout health score from D to B.
- The container deliberately runs as root (see the Dockerfile's own note), so
  a fetch-and-install tool left behind in it is a gift to anything that gets a
  shell there.

This is a structural check on the Dockerfile, NOT the real matcher. The real
matcher is `docker run --entrypoint sh <img> -c 'python -c "import pip"'`, and
a build is far too slow for this suite. So rather than grep for a substring -
which can sit in a comment, or in a RUN that never touches the final image,
and still be "in the file" - it parses the RUN instructions and asserts the
removal happens *inside* the RUN that installed the requirements, *after* the
install.

The removal is recoverable rather than a one-way door: `python -m ensurepip`
reinstalls pip from the bundled wheel, verified the same day.
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"

#< the one build-time use, `pip install --no-cache-dir -r requirements.txt`
INSTALL_PATTERN = re.compile(r"pip\s+install\b[^&|]*-r\s+requirements\.txt")
#< the removal, `python -m pip uninstall -y pip`
REMOVAL_PATTERN = re.compile(r"pip\s+uninstall\b[^&|]*\bpip\b")
#< a version-pinned `rm -rf .../python3.13/site-packages/pip` instead of the above
VERSIONED_PATH_REMOVAL_PATTERN = re.compile(
    r"rm\s+-[rf]+[^&|]*python3\.\d+[^&|]*site-packages")


def _runInstructions(text: str) -> list[str]:
    """Every RUN instruction as one line, `\\` continuations joined.

    Comment lines are dropped first: this Dockerfile carries long explanatory
    comments *between* the continuation lines it documents, and a comment
    mentioning `pip uninstall` must not be able to satisfy these tests.
    """
    instructions: list[str] = []
    buffer = ""
    for line in text.splitlines():
        if line.lstrip().startswith("#"):
            continue
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            buffer += stripped[:-1] + " "
            continue
        buffer += stripped
        instructions.append(buffer)
        buffer = ""
    if buffer:                      #< a trailing continuation with no final line
        instructions.append(buffer)
    return [i for i in instructions if i.lstrip().upper().startswith("RUN ")]


class TestDockerfileExcludesPip(unittest.TestCase):
    def setUp(self):
        self.runs = _runInstructions(DOCKERFILE_PATH.read_text(encoding="utf-8"))

    def test_something_installs_the_requirements(self):
        """The positive control: if this stops matching, the two tests below
        pass vacuously and stop guarding anything."""
        self.assertTrue([r for r in self.runs if INSTALL_PATTERN.search(r)],
                        "no RUN installs requirements.txt - the patterns drifted "
                        "from the Dockerfile, so the pip checks below are dead")

    def test_pip_is_removed_in_the_same_run_that_used_it(self):
        """Same RUN, so pip's ~15 MB never lands in a layer at all. A later
        RUN would delete it from the final filesystem but keep it in the
        image's history."""
        installing = [r for r in self.runs if INSTALL_PATTERN.search(r)]

        for run in installing:
            self.assertRegex(
                run, REMOVAL_PATTERN,
                "the RUN that installs requirements.txt must also uninstall pip "
                "- without it the image scan regains 3 High + 1 Medium from "
                "pip/_vendor, none of them an actual dependency of this app")

    def test_the_removal_comes_after_the_install(self):
        """Ordering is not cosmetic - uninstalling first would take pip out
        before it has installed anything, and the build would fail."""
        for run in [r for r in self.runs if INSTALL_PATTERN.search(r)]:
            removal = REMOVAL_PATTERN.search(run)
            #< the test above owns "is it there at all"; without this guard its
            #  absence surfaces here as an AttributeError instead of a message
            if removal is None:
                continue
            self.assertLess(INSTALL_PATTERN.search(run).start(), removal.start(),
                            "pip is removed before it installs the requirements")

    def test_the_removal_does_not_hardcode_a_python_version_path(self):
        """`rm -rf /usr/local/lib/python3.13/site-packages/pip` looks like the
        same thing and is a trap: the day the base image moves to 3.14 it
        matches nothing, succeeds silently, and the findings come back with no
        failing build to say so. `python -m pip uninstall` has no such
        dependency on the interpreter version."""
        for run in self.runs:
            self.assertNotRegex(
                run, VERSIONED_PATH_REMOVAL_PATTERN,
                "removing pip by a versioned site-packages path no-ops silently "
                "on a base-image bump - use `python -m pip uninstall -y pip`")


if __name__ == "__main__":
    unittest.main()
