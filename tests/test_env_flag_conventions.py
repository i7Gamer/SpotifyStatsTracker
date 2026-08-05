"""How this project reads on/off environment flags, pinned in one place.

Two conventions live here, and both were broken by the same refactor.

The first is that "which strings mean on" has ONE answer. It briefly had two:
`config.TRUTHY_ENV_VALUES` accepted {1, true, yes, on} while
`Database.utils.TRUTHY_ENV_VALUES` accepted only {1, true}. Same name, different
members, both live and both imported by name - so ENABLE_HSTS=yes was on while
TOTP_AUTO_RECOVER=yes was off, and nothing in the code said so.

The second is that FLASK_DEBUG is read through `flaskDebugEnabled()` and nowhere
else. The commit that introduced that helper converted six of the seven sites;
the one it missed spelled the exact bug the helper exists to remove - a bare
truthiness test on the raw string, which reads FLASK_DEBUG=0 as ON.

Both gates are structural, because both failures are invisible to a behavioural
test: a second copy of the set behaves identically until someone edits one of
them, and a missed call site only misbehaves under an env var no test sets.
"""
import os
import re
import sys
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import config
from Database import utils as databaseUtils

REPO_ROOT = Path(__file__).resolve().parent.parent

# Where the app's own Python lives. tests/ is excluded deliberately - a test may
# legitimately spell an env var it is exercising.
SOURCE_DIRS = ("Database", "routes", "dashboard", "services")
SOURCE_FILES = ("app.py", "config.py", "wsgi.py")

#< the only module allowed to read FLASK_DEBUG out of the environment
FLASK_DEBUG_OWNER = Path("Database") / "utils.py"

_LINE_COMMENT = re.compile(r"#[^\n]*")
_DOCSTRING = re.compile(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'')

#< any read of the variable, however it is spelled to get at the environment
_FLASK_DEBUG_READ = re.compile(r"""["']FLASK_DEBUG["']""")


def _productionSources():
    """Every .py file the running app is built from, as (relativePath, text)."""
    paths = []
    for directory in SOURCE_DIRS:
        paths.extend(sorted((REPO_ROOT / directory).rglob("*.py")))
    for name in SOURCE_FILES:
        paths.append(REPO_ROOT / name)
    for path in paths:
        if "__pycache__" in path.parts:
            continue
        yield path.relative_to(REPO_ROOT), path.read_text(encoding="utf-8")


def _stripProse(text):
    """Comments and docstrings removed. Both gates below are about what the code
    DOES, and every one of these constants is discussed in prose somewhere."""
    return _LINE_COMMENT.sub("", _DOCSTRING.sub("", text))


class TestOneTruthyEnvValueSet(unittest.TestCase):
    def test_the_two_modules_share_one_object(self):
        """Not merely equal - the same object, so they cannot drift apart."""
        self.assertIs(databaseUtils.TRUTHY_ENV_VALUES, config.TRUTHY_ENV_VALUES)

    def test_yes_and_on_are_accepted(self):
        """The members the narrower copy was missing. A flag set to `yes` that
        silently means off is worse than one that rejects the spelling."""
        for value in ("1", "true", "yes", "on"):
            with self.subTest(value=value):
                self.assertIn(value, config.TRUTHY_ENV_VALUES)

    def test_off_spellings_stay_out(self):
        """The whole reason this is a set rather than a truthiness test."""
        for value in ("0", "false", "no", "off", ""):
            with self.subTest(value=value):
                self.assertNotIn(value, config.TRUTHY_ENV_VALUES)

    def test_only_one_module_defines_it(self):
        definers = [
            str(path) for path, text in _productionSources()
            if re.search(r"^TRUTHY_ENV_VALUES\s*=", _stripProse(text), re.MULTILINE)
        ]

        self.assertEqual(
            definers, ["config.py"],
            "TRUTHY_ENV_VALUES must be defined once, in config.py (the module that "
            f"imports nothing); found {len(definers)} definitions: {definers}")


class TestFlaskDebugHasOneReader(unittest.TestCase):
    """FLASK_DEBUG is asked about through flaskDebugEnabled(), nowhere else.

    The helper was introduced to collapse six spellings into one, and the site
    it missed - Database/workers/listener.py's `if os.environ.get("FLASK_DEBUG")`
    - was the one that mattered most: a bare truthiness test on the raw string,
    on the hottest path in the app. "0" is a non-empty string, so an operator
    setting FLASK_DEBUG=0 to silence diagnostics turned that one ON, logging per
    ingest batch, per user, per cycle.

    Structural because the bug only shows under an env var no test sets, and
    because the next site to be written is the one this catches.
    """

    def test_only_the_owner_reads_it_from_the_environment(self):
        readers = sorted(
            str(path) for path, text in _productionSources()
            if _FLASK_DEBUG_READ.search(_stripProse(text))
        )

        self.assertEqual(
            readers, [str(FLASK_DEBUG_OWNER)],
            "FLASK_DEBUG must be read only by Database/utils.py's flaskDebugEnabled(); "
            f"these modules spell it themselves: {readers}")

    def test_the_helper_is_what_the_owner_exposes(self):
        """Guards the gate above: if the helper were renamed away, the scan
        would pass by finding nothing anywhere, which reads like success."""
        self.assertTrue(callable(databaseUtils.flaskDebugEnabled))

    def test_zero_is_off(self):
        """The bug the missed call site had, stated as behaviour."""
        with unittest.mock.patch.dict(os.environ, {"FLASK_DEBUG": "0"}):
            self.assertFalse(databaseUtils.flaskDebugEnabled())
