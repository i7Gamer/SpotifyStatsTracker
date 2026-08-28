"""dependabot.yml must name real ecosystems, for the manifests this repo has.

GitHub's starter file ships `package-ecosystem: ""` with a "See documentation
for possible values" comment, and that is what got committed here (5e149e4).
An empty value is not a valid ecosystem, and Dependabot rejects the WHOLE file
when any single entry is invalid - so the config that looks configured updates
nothing at all, and says so only on the repository's Dependabot page, which
nobody opens until they wonder why no PRs ever arrived.

The second check is the forward-looking one. This repo has four kinds of
manifest - requirements*.txt/pyproject.toml (pip), package.json (npm),
Dockerfile (docker) and .github/workflows (github-actions) - and a fifth
arriving later would not announce that Dependabot cannot see it. So the
expectation is derived from what is ON DISK rather than written out as a fixed
list of entries: add a manifest without adding an updates entry and this goes
red.

Parsed by hand because PyYAML is not a dependency of this project (see
requirements-dev.txt) and the workflow tests already scan YAML line by line.
The parser gets its own negative controls at the bottom - a scanner that
silently matched nothing would make every assertion above it pass.
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

REPO_ROOT = Path(__file__).resolve().parent.parent
DEPENDABOT_YML = REPO_ROOT / ".github" / "dependabot.yml"

# https://docs.github.com/code-security/dependabot/dependabot-version-updates/configuration-options-for-the-dependabot.yml-file
# Spelled out rather than "not empty" so a plausible typo - "pip3", "python",
# "actions" - fails here instead of on GitHub's side after a push.
VALID_ECOSYSTEMS = frozenset((
    "bun", "bundler", "cargo", "composer", "devcontainers", "docker",
    "docker-compose", "dotnet-sdk", "elm", "gitsubmodule", "github-actions",
    "gomod", "gradle", "helm", "maven", "mix", "npm", "nuget", "pip", "pub",
    "swift", "terraform", "uv", "vcpkg",
))

# (path relative to the repo root, the ecosystem that reads it). Checked for
# existence first, so removing a manifest relaxes the requirement instead of
# leaving a test that demands an entry for a file that is gone.
MANIFESTS = (
    ("requirements.txt", "pip"),
    ("requirements-dev.txt", "pip"),
    ("pyproject.toml", "pip"),
    ("package.json", "npm"),
    ("Dockerfile", "docker"),
    (".github/workflows", "github-actions"),
)

ENTRY_KEY = re.compile(r"^\s*-\s*package-ecosystem:\s*(.*)$")
DIRECTORY_KEY = re.compile(r"^\s*directory:\s*(.*)$")
VERSION_KEY = re.compile(r"^version:\s*(.*)$")


def _scalar(raw):
    """A YAML scalar with its quotes and any trailing `# comment` removed.

    The quoted branch matters: the starter file's value is `"" # See ...`, and
    splitting that on `#` first would leave `""` and read as two characters
    rather than as the empty ecosystem it is.
    """
    text = raw.strip()
    if text[:1] in ("'", '"'):
        quote = text[0]
        closing = text.find(quote, 1)
        return text[1:closing] if closing != -1 else text[1:]
    return text.split("#", 1)[0].strip()


def _updateEntries(text):
    """Every `updates:` entry as (ecosystem, directory).

    `- package-ecosystem:` opens an entry, so nested list items under `ignore:`
    or `groups:` - which use other keys entirely - cannot be mistaken for one.
    """
    entries = []
    for line in text.splitlines():
        opening = ENTRY_KEY.match(line)
        if opening:
            entries.append([_scalar(opening.group(1)), None])
            continue
        directory = DIRECTORY_KEY.match(line)
        if directory and entries:
            entries[-1][1] = _scalar(directory.group(1))
    return [tuple(entry) for entry in entries]


class DependabotConfigTest(unittest.TestCase):
    def setUp(self):
        self.text = DEPENDABOT_YML.read_text(encoding="utf-8")
        self.entries = _updateEntries(self.text)

    def test_the_config_declares_schema_version_two(self):
        versions = [_scalar(match.group(1))
                    for match in map(VERSION_KEY.match, self.text.splitlines()) if match]

        self.assertEqual(versions, ["2"],
                         "dependabot.yml needs exactly one top-level `version: 2`.")

    def test_at_least_one_update_entry_exists(self):
        """Guards the parser as much as the file: zero entries would satisfy
        every per-entry assertion below it."""
        self.assertTrue(self.entries, f"no `updates:` entries found in {DEPENDABOT_YML}")

    def test_every_entry_names_a_real_ecosystem(self):
        unknown = [ecosystem for ecosystem, _ in self.entries
                   if ecosystem not in VALID_ECOSYSTEMS]

        self.assertEqual(unknown, [],
                         f"{unknown} is not a Dependabot ecosystem. One bad entry makes "
                         "Dependabot reject the entire file, so the other entries stop "
                         "working too.")

    def test_every_entry_names_a_directory(self):
        missing = [ecosystem for ecosystem, directory in self.entries if not directory]

        self.assertEqual(missing, [],
                         f"{missing} has no `directory:` - Dependabot needs one to know "
                         "where the manifest lives.")

    def test_every_manifest_in_the_repo_has_an_updates_entry(self):
        covered = {ecosystem for ecosystem, _ in self.entries}
        uncovered = sorted({ecosystem for path, ecosystem in MANIFESTS
                            if (REPO_ROOT / path).exists() and ecosystem not in covered})

        self.assertEqual(uncovered, [],
                         f"{uncovered} manifests exist in this repo but nothing in "
                         "dependabot.yml updates them.")

    def test_no_ecosystem_and_directory_pair_is_declared_twice(self):
        """Dependabot rejects a duplicate pair the same way it rejects a bad
        ecosystem - by refusing the whole file."""
        duplicates = sorted({entry for entry in self.entries
                             if self.entries.count(entry) > 1})

        self.assertEqual(duplicates, [], f"{duplicates} appears more than once.")


class ParserTest(unittest.TestCase):
    """Negative controls. Without these, a parser that returned [] would make
    every assertion in DependabotConfigTest pass except the count check."""

    def test_the_starter_files_empty_ecosystem_is_read_as_empty(self):
        starter = ('version: 2\n'
                   'updates:\n'
                   '  - package-ecosystem: "" # See documentation for possible values\n'
                   '    directory: "/" # Location of package manifests\n')

        self.assertEqual(_updateEntries(starter), [("", "/")])

    def test_a_populated_config_parses_into_its_entries(self):
        good = ('version: 2\n'
                'updates:\n'
                '  - package-ecosystem: "pip"\n'
                '    directory: "/"\n'
                '  - package-ecosystem: "github-actions"\n'
                '    directory: "/"\n')

        self.assertEqual(_updateEntries(good), [("pip", "/"), ("github-actions", "/")])

    def test_a_nested_list_item_is_not_mistaken_for_an_entry(self):
        nested = ('updates:\n'
                  '  - package-ecosystem: "pip"\n'
                  '    directory: "/"\n'
                  '    ignore:\n'
                  '      - dependency-name: "spotapi"\n')

        self.assertEqual(_updateEntries(nested), [("pip", "/")])

    def test_an_unquoted_value_keeps_its_comment_out(self):
        self.assertEqual(_scalar("/ # Location of package manifests"), "/")
        self.assertEqual(_scalar('"pip"'), "pip")


if __name__ == "__main__":
    unittest.main()
