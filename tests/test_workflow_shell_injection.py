"""No workflow may interpolate untrusted input into a shell.

`${{ ... }}` is substituted TEXTUALLY before bash parses the line, so a value
containing `;` or a backtick executes. dockerReleaseTag.yml did this with its
`workflow_dispatch` input, in a job whose first step logs in to Docker Hub - so
`~/.docker/config.json` holds a live push token by the time any injected command
runs, and `permissions: contents: read` does nothing to stop it leaving.

Only someone with repo write access can dispatch that workflow, so this is
hardening rather than an open hole. It is still the pattern actionlint and CodeQL
flag, and the fix (pass through `env:`, quote in the script) costs nothing.

`github.*` fields are checked too even though nothing uses them today: a branch
or PR title is attacker-controllable on a public repository, which is the version
of this that IS a hole.
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

WORKFLOWS_DIR = Path(__file__).resolve().parent.parent / ".github" / "workflows"

# Expression contexts an attacker (or a careless operator) can put arbitrary text
# into. `secrets.*` is deliberately absent: those are trusted values, and passing
# them through env is a separate concern (masking) from injection.
UNTRUSTED_CONTEXTS = ("inputs.", "github.event.", "github.head_ref", "github.ref_name")

# `run:` can be a list item (`- run: ...`), so the key is not at the start of the
# indent. Scanned line by line rather than with one big regex: a block scalar's
# extent is defined by indentation, which a single pattern gets wrong in exactly
# the way that made the first version of this file match nothing at all - its own
# negative controls are what caught that.
RUN_KEY = re.compile(r"^(\s*)(?:-\s+)?run:\s*(.*)$")
EXPRESSION = re.compile(r"\$\{\{([^}]*)\}\}")


def _runScripts(text):
    """Every `run:` script in a workflow, block and inline form alike."""
    scripts = []
    lines = text.splitlines()
    for index, line in enumerate(lines):
        match = RUN_KEY.match(line)
        if not match:
            continue
        indent, remainder = match.group(1), match.group(2).strip()
        if remainder not in ("|", ">", "|-", ">-", "|+", ">+", ""):
            scripts.append(remainder)          #< inline form
            continue
        body = []
        for following in lines[index + 1:]:
            if not following.strip():
                body.append(following)         #< a blank line does not end a block scalar
                continue
            followingIndent = len(following) - len(following.lstrip())
            if followingIndent <= len(indent):
                break
            body.append(following)
        scripts.append("\n".join(body))
    return scripts


def _offendingExpressions(text):
    offenders = []
    for script in _runScripts(text):
        for expression in EXPRESSION.findall(script):
            if any(context in expression for context in UNTRUSTED_CONTEXTS):
                offenders.append(expression.strip())
    return offenders


class WorkflowShellInjectionTestCase(unittest.TestCase):
    def test_no_run_block_interpolates_untrusted_input(self):
        for path in sorted(WORKFLOWS_DIR.glob("*.yml")):
            with self.subTest(workflow=path.name):
                offenders = _offendingExpressions(path.read_text(encoding="utf-8"))

                self.assertEqual(offenders, [],
                                 f"{path.name} substitutes {offenders} into a shell script. Pass it "
                                 "through `env:` and reference it as a quoted \"$VAR\" instead - "
                                 "expression syntax is expanded before bash parses the line.")

    def test_the_detector_finds_the_pattern_it_is_looking_for(self):
        """Negative control: without this the assertion above would pass for any
        possible set of workflows."""
        vulnerable = 'jobs:\n  x:\n    steps:\n    - run: |\n        docker build -t app:${{ inputs.target_tag }} .\n'
        safe = 'jobs:\n  x:\n    steps:\n    - run: |\n        docker build -t "app:$TARGET_TAG" .\n'

        self.assertEqual(_offendingExpressions(vulnerable), ["inputs.target_tag"])
        self.assertEqual(_offendingExpressions(safe), [])

    def test_an_inline_run_is_scanned_too(self):
        inline = 'jobs:\n  x:\n    steps:\n    - run: echo ${{ github.event.issue.title }}\n'

        self.assertEqual(_offendingExpressions(inline), ["github.event.issue.title"])

    def test_using_an_expression_outside_a_shell_is_still_allowed(self):
        """`with: ref:` and `env:` are data, not code - the action receives the
        value as an argument and never evaluates it."""
        fine = ('jobs:\n  x:\n    env:\n      TARGET_TAG: ${{ inputs.target_tag }}\n'
                '    steps:\n    - uses: actions/checkout@v7\n      with:\n'
                '        ref: ${{ inputs.target_tag }}\n')

        self.assertEqual(_offendingExpressions(fine), [])


if __name__ == "__main__":
    unittest.main()
