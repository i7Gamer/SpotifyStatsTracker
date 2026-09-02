"""dependabot.yml must name real ecosystems, for the manifests this repo has,
and must group the ones that would otherwise arrive one PR per dependency.

GitHub's starter file ships `package-ecosystem: ""` with a "See documentation
for possible values" comment, and that is what got committed here (5e149e4).
An empty value is not a valid ecosystem, and Dependabot rejects the WHOLE file
when any single entry is invalid - so the config that looks configured updates
nothing at all, and says so only on the repository's Dependabot page, which
nobody opens until they wonder why no PRs ever arrived.

The other two checks are the forward-looking ones, and both derive their
expectation from what is ON DISK rather than restating the config back at
itself:

  - every manifest present (requirements*.txt/pyproject.toml, package.json,
    Dockerfile, .github/workflows) has an `updates:` entry, so a fifth kind
    arriving later cannot silently go untracked;

  - every ecosystem holding more than ONE dependency has a group, so a bump
    wave arrives as a PR rather than as five. Counting is why docker needs no
    group - the Dockerfile has a single image - and that exemption expires by
    itself if a multi-stage build ever adds a second.

Parsed by hand because PyYAML is not a dependency of this project (see
requirements-dev.txt) and the workflow tests already scan YAML line by line.
The parser and the counters get their own negative controls at the bottom - a
scanner that silently matched nothing would make every assertion above it pass.
"""
import json
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
VALID_UPDATE_TYPES = frozenset(("major", "minor", "patch"))
VALID_APPLIES_TO = frozenset(("version-updates", "security-updates"))

# Ecosystems whose majors are deliberately left OUT of the grouped PR. Every
# pin in requirements.txt carries a paragraph on what was checked before it
# moved (websockets 16.1 -> 17.0.1 is the worked example), and that review does
# not survive being bundled with four unrelated bumps. github-actions is absent
# on purpose: an action's version IS its major tag, so excluding majors there
# would group nothing.
MAJORS_REVIEWED_ALONE = ("pip", "npm")

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
VERSION_KEY = re.compile(r"^version:\s*(.*)$")
#< `name==1.2`, `name>=1.2`, `name @ git+...`, `name[extra]` - all end the name
PIP_NAME_END = re.compile(r"[=<>!~;@\[\s]")
FROM_LINE = re.compile(r"^FROM\s+(\S+)(?:\s+[Aa][Ss]\s+(\S+))?", re.IGNORECASE)
USES_LINE = re.compile(r"uses:\s*(\S+)")


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
    """Every `updates:` entry as {ecosystem, directory, groups}.

    Indentation-driven, because `groups:` is the one nested block here whose
    contents have to be read rather than skipped. `- package-ecosystem:` opens
    an entry and any key back at entry level closes whatever block was open, so
    the `- dependency-name:` items under `ignore:` cannot leak into a group.
    """
    entries = []
    keyIndent = groupsIndent = None
    currentGroup = currentKey = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())

        opening = ENTRY_KEY.match(line)
        if opening:
            entries.append({"ecosystem": _scalar(opening.group(1)),
                            "directory": None, "groups": {}})
            keyIndent = line.index("-") + 2
            groupsIndent = currentGroup = currentKey = None
            continue
        if not entries:
            continue
        entry = entries[-1]

        if indent <= keyIndent:
            groupsIndent = currentGroup = currentKey = None
            if stripped.startswith("directory:"):
                entry["directory"] = _scalar(stripped.split(":", 1)[1])
            elif stripped == "groups:":
                groupsIndent = keyIndent + 2
            continue
        if groupsIndent is None:
            continue                                   #< inside schedule:, ignore:, ...

        if indent == groupsIndent and stripped.endswith(":"):
            currentGroup = stripped[:-1].strip()
            entry["groups"][currentGroup] = {}
            currentKey = None
            continue
        if currentGroup is None:
            continue
        if indent == groupsIndent + 2 and ":" in stripped:
            name, _, inline = stripped.partition(":")
            currentKey = name.strip()
            value = _scalar(inline)
            entry["groups"][currentGroup][currentKey] = value if value else []
            continue
        if indent > groupsIndent + 2 and stripped.startswith("- ") and currentKey:
            bucket = entry["groups"][currentGroup][currentKey]
            if isinstance(bucket, list):
                bucket.append(_scalar(stripped[2:]))
    return entries


def _pipDependencyNames(text):
    names = set()
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        names.add(PIP_NAME_END.split(stripped, maxsplit=1)[0].strip().lower())
    return names - {""}


def _dockerImages(text):
    """Distinct images a Dockerfile pulls, ignoring references to its own
    stages - a two-stage build off one base is still one thing to update."""
    images, aliases = set(), set()
    for line in text.splitlines():
        match = FROM_LINE.match(line.strip())
        if not match:
            continue
        image, alias = match.group(1), match.group(2)
        if image not in aliases:
            images.add(image)
        if alias:
            aliases.add(alias)
    return images


def _actionRefs(text):
    """Actions a workflow calls, minus the local `./.github/workflows/...`
    reusable-workflow calls, which are this repo's own files."""
    return {match.group(1).split("@")[0]
            for match in map(USES_LINE.search, text.splitlines())
            if match and not match.group(1).startswith("./")}


def _readIfPresent(*relativeParts):
    path = REPO_ROOT.joinpath(*relativeParts)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _pipCount():
    return len(_pipDependencyNames(_readIfPresent("requirements.txt")
                                   + "\n" + _readIfPresent("requirements-dev.txt")))


def _npmCount():
    text = _readIfPresent("package.json")
    if not text:
        return 0
    manifest = json.loads(text)
    return len(set(manifest.get("dependencies", {})) | set(manifest.get("devDependencies", {})))


def _dockerCount():
    return len(_dockerImages(_readIfPresent("Dockerfile")))


def _actionsCount():
    workflows = REPO_ROOT / ".github" / "workflows"
    if not workflows.exists():
        return 0
    refs = set()
    for path in sorted(workflows.glob("*.yml")):
        refs |= _actionRefs(path.read_text(encoding="utf-8"))
    return len(refs)


DEPENDENCY_COUNTS = {
    "pip": _pipCount,
    "npm": _npmCount,
    "docker": _dockerCount,
    "github-actions": _actionsCount,
}


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
        unknown = [entry["ecosystem"] for entry in self.entries
                   if entry["ecosystem"] not in VALID_ECOSYSTEMS]

        self.assertEqual(unknown, [],
                         f"{unknown} is not a Dependabot ecosystem. One bad entry makes "
                         "Dependabot reject the entire file, so the other entries stop "
                         "working too.")

    def test_every_entry_names_a_directory(self):
        missing = [entry["ecosystem"] for entry in self.entries if not entry["directory"]]

        self.assertEqual(missing, [],
                         f"{missing} has no `directory:` - Dependabot needs one to know "
                         "where the manifest lives.")

    def test_every_manifest_in_the_repo_has_an_updates_entry(self):
        covered = {entry["ecosystem"] for entry in self.entries}
        uncovered = sorted({ecosystem for path, ecosystem in MANIFESTS
                            if (REPO_ROOT / path).exists() and ecosystem not in covered})

        self.assertEqual(uncovered, [],
                         f"{uncovered} manifests exist in this repo but nothing in "
                         "dependabot.yml updates them.")

    def test_no_ecosystem_and_directory_pair_is_declared_twice(self):
        """Dependabot rejects a duplicate pair the same way it rejects a bad
        ecosystem - by refusing the whole file."""
        pairs = [(entry["ecosystem"], entry["directory"]) for entry in self.entries]
        duplicates = sorted({pair for pair in pairs if pairs.count(pair) > 1})

        self.assertEqual(duplicates, [], f"{duplicates} appears more than once.")

    def test_an_ecosystem_with_more_than_one_dependency_is_grouped(self):
        """Ungrouped, a weekly bump wave opens one PR per dependency - and with
        four ecosystems at a limit of 5 each, that is up to 20 open at once."""
        ungrouped = sorted(entry["ecosystem"] for entry in self.entries
                           if not entry["groups"]
                           and DEPENDENCY_COUNTS.get(entry["ecosystem"], lambda: 0)() > 1)

        self.assertEqual(ungrouped, [],
                         f"{ungrouped} tracks more than one dependency and has no "
                         "`groups:`, so every bump arrives as its own pull request.")

    def test_every_group_filters_on_real_update_types(self):
        wrong = sorted({value for entry in self.entries
                        for group in entry["groups"].values()
                        for value in group.get("update-types", [])
                        if value not in VALID_UPDATE_TYPES})

        self.assertEqual(wrong, [],
                         f"{wrong} is not a Dependabot update-type. Like a bad ecosystem, "
                         "it invalidates the whole file.")

    def test_every_group_applies_to_a_real_update_kind(self):
        wrong = sorted({group["applies-to"] for entry in self.entries
                        for group in entry["groups"].values()
                        if "applies-to" in group
                        and group["applies-to"] not in VALID_APPLIES_TO})

        self.assertEqual(wrong, [], f"{wrong} is not a valid `applies-to`.")

    def test_every_group_declares_applies_to_version_updates(self):
        missing = [f"{entry['ecosystem']}/{name}"
                   for entry in self.entries
                   for name, group in entry["groups"].items()
                   if group.get("applies-to") != "version-updates"]

        self.assertEqual(missing, [],
                         f"{missing} does not explicitly declare `applies-to: version-updates`.")

    def test_majors_are_not_swept_into_the_grouped_pull_request(self):
        """See MAJORS_REVIEWED_ALONE. A group that omits `update-types`
        entirely takes ALL of them, so silence fails here too."""
        swept = sorted(f"{entry['ecosystem']}/{name}"
                       for entry in self.entries
                       if entry["ecosystem"] in MAJORS_REVIEWED_ALONE
                       for name, group in entry["groups"].items()
                       if "major" in group.get("update-types", ["major"]))

        self.assertEqual(swept, [],
                         f"{swept} bundles major version bumps into a shared PR, where the "
                         "per-pin reasoning in requirements.txt cannot be reviewed.")

    def test_the_dependency_counters_see_this_repos_manifests(self):
        """The on-disk half of the grouping check. Without this, counters that
        all returned 0 would make it pass by finding nothing to group - and the
        docker exemption below is an assertion, not a hardcoded skip: a second
        image lands here as a red test asking for a group."""
        self.assertGreater(_pipCount(), 1)
        self.assertGreater(_npmCount(), 1)
        self.assertGreater(_actionsCount(), 1)
        self.assertEqual(_dockerCount(), 1)


class ParserTest(unittest.TestCase):
    """Negative controls. Without these, a parser that returned [] would make
    every assertion in DependabotConfigTest pass except the count check."""

    def test_the_starter_files_empty_ecosystem_is_read_as_empty(self):
        starter = ('version: 2\n'
                   'updates:\n'
                   '  - package-ecosystem: "" # See documentation for possible values\n'
                   '    directory: "/" # Location of package manifests\n')
        entries = _updateEntries(starter)

        self.assertEqual([(e["ecosystem"], e["directory"]) for e in entries], [("", "/")])

    def test_a_populated_config_parses_into_its_entries(self):
        good = ('version: 2\n'
                'updates:\n'
                '  - package-ecosystem: "pip"\n'
                '    directory: "/"\n'
                '  - package-ecosystem: "github-actions"\n'
                '    directory: "/"\n')
        entries = _updateEntries(good)

        self.assertEqual([(e["ecosystem"], e["directory"]) for e in entries],
                         [("pip", "/"), ("github-actions", "/")])

    def test_a_group_block_parses_into_its_keys_and_lists(self):
        grouped = ('updates:\n'
                   '  - package-ecosystem: "pip"\n'
                   '    directory: "/"\n'
                   '    groups:\n'
                   '      python-minor-and-patch:\n'
                   '        applies-to: version-updates\n'
                   '        patterns:\n'
                   '          - "*"\n'
                   '        update-types:\n'
                   '          - "minor"\n'
                   '          - "patch"\n')

        self.assertEqual(_updateEntries(grouped)[0]["groups"],
                         {"python-minor-and-patch": {"applies-to": "version-updates",
                                                     "patterns": ["*"],
                                                     "update-types": ["minor", "patch"]}})

    def test_two_groups_under_one_entry_stay_separate(self):
        two = ('updates:\n'
               '  - package-ecosystem: "npm"\n'
               '    directory: "/"\n'
               '    groups:\n'
               '      first:\n'
               '        update-types:\n'
               '          - "patch"\n'
               '      second:\n'
               '        update-types:\n'
               '          - "minor"\n')

        self.assertEqual(_updateEntries(two)[0]["groups"],
                         {"first": {"update-types": ["patch"]},
                          "second": {"update-types": ["minor"]}})

    def test_an_ignore_block_after_a_group_does_not_leak_into_it(self):
        """The ordering that would break an indentation-blind parser: `ignore:`
        sits at entry level with `- dependency-name:` items nested deeper than
        the group keys just read."""
        mixed = ('updates:\n'
                 '  - package-ecosystem: "pip"\n'
                 '    directory: "/"\n'
                 '    groups:\n'
                 '      python:\n'
                 '        update-types:\n'
                 '          - "minor"\n'
                 '    ignore:\n'
                 '      - dependency-name: "spotapi"\n')
        entry = _updateEntries(mixed)[0]

        self.assertEqual(entry["groups"], {"python": {"update-types": ["minor"]}})
        self.assertEqual(entry["ecosystem"], "pip")

    def test_a_group_without_update_types_is_read_as_having_none(self):
        """github-actions is configured this way, and the majors check reads
        the absence as "all update types" rather than as "none"."""
        allTypes = ('updates:\n'
                    '  - package-ecosystem: "github-actions"\n'
                    '    directory: "/"\n'
                    '    groups:\n'
                    '      github-actions:\n'
                    '        patterns:\n'
                    '          - "*"\n')
        group = _updateEntries(allTypes)[0]["groups"]["github-actions"]

        self.assertNotIn("update-types", group)
        self.assertIn("major", group.get("update-types", ["major"]))

    def test_an_unquoted_value_keeps_its_comment_out(self):
        self.assertEqual(_scalar("/ # Location of package manifests"), "/")
        self.assertEqual(_scalar('"pip"'), "pip")


class DependencyCounterTest(unittest.TestCase):
    """Negative controls for the counters that decide who needs a group."""

    def test_pip_names_are_read_without_their_version_specifiers(self):
        requirements = ('# a comment\n'
                        'Flask==3.1.3\n'
                        '\n'
                        'pillow>=12\n'
                        'spotapi @ git+https://example.invalid/SpotAPI@abc123\n'
                        'uvicorn[standard]==0.1\n')

        self.assertEqual(_pipDependencyNames(requirements),
                         {"flask", "pillow", "spotapi", "uvicorn"})

    def test_a_multi_stage_build_off_one_base_counts_as_one_image(self):
        oneBase = 'FROM python:3.13-slim AS builder\nRUN true\nFROM builder\n'
        twoBases = 'FROM python:3.13-slim AS builder\nFROM debian:13-slim\n'

        self.assertEqual(_dockerImages(oneBase), {"python:3.13-slim"})
        self.assertEqual(_dockerImages(twoBases), {"python:3.13-slim", "debian:13-slim"})

    def test_action_refs_drop_the_version_and_the_local_workflow_calls(self):
        workflow = ('jobs:\n'
                    '  a:\n'
                    '    uses: ./.github/workflows/dockerPublish.yml\n'
                    '  b:\n'
                    '    steps:\n'
                    '      - uses: actions/checkout@v7\n'
                    '      - uses: docker/login-action@v4\n')

        self.assertEqual(_actionRefs(workflow), {"actions/checkout", "docker/login-action"})


if __name__ == "__main__":
    unittest.main()
