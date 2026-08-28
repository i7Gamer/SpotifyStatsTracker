"""CI must run the tests on the Python the image actually ships.

tests.yml says `python-version: "3.13"` under the comment "Matches the
Dockerfile's python:3.13-slim base image" - and that comment was the ONLY thing
holding the two together. Nothing enforced it, which matters because of how the
two halves get updated: Dependabot proposes base-image bumps (it opened
python 3.13-slim -> 3.14-slim), and none of the four docker* workflows run on a
pull request, so the image is never built there either. Merging such a bump on
its own would leave the suite green on 3.13 while the container shipped 3.14,
with nothing anywhere having executed this code on the version users run.

So the pin is asserted rather than described. The Dockerfile is the source of
truth - it is what ships - and every `python-version:` in .github/workflows must
equal it.

A deliberate change still passes; it just has to be deliberate. Moving the base
image means moving the pins in the same commit, and introducing a version MATRIX
(3.13 and 3.14 together, which would be a fine thing to want) means editing this
test to say so. That edit is the point: it is the conversation that merging a
one-line base-image bump silently skips.
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCKERFILE = REPO_ROOT / "Dockerfile"
WORKFLOWS_DIR = REPO_ROOT / ".github" / "workflows"

#< `FROM python:3.14-slim`, and the registry-qualified/suffixed spellings of it
FROM_PYTHON = re.compile(r"^FROM\s+(?:[\w.\-]+/)*python:(\d+\.\d+)", re.IGNORECASE)
#< `python-version: "3.14"` as actions/setup-python takes it
PYTHON_VERSION_KEY = re.compile(r"^\s*python-version:\s*(.*)$")


def _unquoted(raw):
    text = raw.strip().split("#", 1)[0].strip()
    return text[1:-1] if len(text) > 1 and text[0] == text[-1] and text[0] in "'\"" else text


def _baseImageVersion(text):
    """The `X.Y` of the Dockerfile's python base image, or None.

    Only the first two components: the image is tagged `3.14-slim`, and a pin
    to a patch release would still be the same interpreter series for
    setup-python's purposes.
    """
    for line in text.splitlines():
        match = FROM_PYTHON.match(line.strip())
        if match:
            return match.group(1)
    return None


def _workflowPythonPins(directory):
    """Every `python-version:` pin as (file name, line number, version)."""
    pins = []
    for path in sorted(directory.glob("*.yml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            match = PYTHON_VERSION_KEY.match(line)
            if match:
                pins.append((path.name, number, _unquoted(match.group(1))))
    return pins


class PythonVersionAlignmentTest(unittest.TestCase):
    def setUp(self):
        self.baseVersion = _baseImageVersion(DOCKERFILE.read_text(encoding="utf-8"))
        self.pins = _workflowPythonPins(WORKFLOWS_DIR)

    def test_the_dockerfile_pins_a_python_base_image(self):
        self.assertIsNotNone(self.baseVersion,
                             f"no `FROM python:X.Y` found in {DOCKERFILE} - this test "
                             "cannot tell CI what to match.")

    def test_the_workflows_pin_a_python_version_at_all(self):
        """Negative control: with no pins found, the alignment check below would
        pass by comparing an empty list."""
        self.assertTrue(self.pins, f"no `python-version:` pins found in {WORKFLOWS_DIR}")

    def test_every_ci_python_pin_matches_the_shipped_base_image(self):
        mismatched = [f"{name}:{number} pins {version}"
                      for name, number, version in self.pins
                      if version != self.baseVersion]

        self.assertEqual(mismatched, [],
                         f"the image ships Python {self.baseVersion} but CI runs "
                         f"{mismatched}. The suite would be green on a version nobody "
                         "deploys - move the pins in the same commit as the base image.")


class ParserTest(unittest.TestCase):
    """Negative controls - a scanner that found nothing would make the
    assertions above pass on any repository."""

    def test_the_base_version_is_read_from_the_from_line(self):
        self.assertEqual(_baseImageVersion("FROM python:3.14-slim\nWORKDIR /app\n"), "3.14")
        self.assertEqual(_baseImageVersion("FROM python:3.13-slim-bookworm\n"), "3.13")
        self.assertEqual(_baseImageVersion("FROM docker.io/library/python:3.12\n"), "3.12")

    def test_a_dockerfile_on_another_base_reports_no_python_version(self):
        self.assertIsNone(_baseImageVersion("FROM debian:13-slim\nRUN true\n"))

    def test_pins_are_read_with_the_file_and_line_that_hold_them(self):
        with tempfile.TemporaryDirectory() as directory:
            workflows = Path(directory)
            (workflows / "tests.yml").write_text(
                'jobs:\n'
                '  a:\n'
                '    steps:\n'
                '      - uses: actions/setup-python@v7\n'
                '        with:\n'
                '          python-version: "3.14"\n',
                encoding="utf-8")
            (workflows / "lint.yml").write_text(
                'jobs:\n'
                '  b:\n'
                '    steps:\n'
                '      - uses: actions/setup-python@v7\n'
                '        with:\n'
                '          python-version: 3.13\n',
                encoding="utf-8")

            self.assertEqual(_workflowPythonPins(workflows),
                             [("lint.yml", 6, "3.13"), ("tests.yml", 6, "3.14")])

    def test_a_quoted_pin_loses_its_quotes_and_its_comment(self):
        self.assertEqual(_unquoted('"3.14"'), "3.14")
        self.assertEqual(_unquoted("3.14 # matches the Dockerfile"), "3.14")
        self.assertEqual(_unquoted("'3.14'"), "3.14")

    def test_the_pin_scanner_finds_this_repos_own_workflows(self):
        """The on-disk half: three pins across tests.yml and lint.yml today, and
        the count matters more than the number - zero would be a silent pass."""
        pins = _workflowPythonPins(WORKFLOWS_DIR)

        self.assertGreaterEqual(len(pins), 2)
        self.assertEqual({name for name, _, _ in pins}, {"tests.yml", "lint.yml"})


if __name__ == "__main__":
    unittest.main()
