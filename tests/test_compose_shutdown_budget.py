"""The compose file must give shutdown longer than Docker's default.

Every join in the shutdown path is deliberately bounded - they exist because
unbounded ones once left the app hanging for ~2.5 minutes on Ctrl+C - but the
bounds add up, and they are spent SEQUENTIALLY: the process-wide workers first,
then each user's listener, watchdog and five periodic workers in turn
(app.shutdown -> Database.stop).

Docker's own default is 10 seconds between SIGTERM and SIGKILL, and that
covers the WHOLE shutdown. Without a declared stop_grace_period, one wedged
Spotify call is enough to have the container killed partway through the user
loop. Nothing corrupts when that happens (SQLite is crash-safe, and a play is
one transaction), but the remaining users' threads never get their clean stop.

Costs nothing when shutdown is quick, which is the normal case: the grace
period is a ceiling, not a wait - Docker proceeds the moment the process exits.

The README carries its own copy of the compose file, so it is checked too: a
setting only the repo's copy has is a setting nobody deploying from the README
gets.
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

import Database.database  # noqa: F401 - must come first: Database.workers'
# __init__ imports the listener, which imports Database.database, so importing
# a workers submodule before it leaves that package half-initialized.
from Database.backup import BACKUP_STOP_JOIN_TIMEOUT_SECONDS
from Database.Importers.AutoImporter import WATCHDOG_STOP_JOIN_TIMEOUT_SECONDS
from Database.Listeners.spotifyListener import LISTENER_STOP_JOIN_TIMEOUT_SECONDS
from Database.workers.periodic import WORKER_STOP_JOIN_TIMEOUT_SECONDS
from services.email_worker import EMAIL_WORKER_STOP_JOIN_TIMEOUT_SECONDS

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_PATH = REPO_ROOT / "docker-compose.yml"
README_PATH = REPO_ROOT / "README.md"

#< Listener.stop() joins twice: spotapi's LastPlayed thread, then the poll thread
LISTENER_JOINS_PER_USER = 2
#< Database.stop() joins the five periodic workers (see WORKER_STOP_EVENT_NAMES)
PERIODIC_WORKERS_PER_USER = 5

PROCESS_WIDE_JOIN_BUDGET_SECONDS = (
    BACKUP_STOP_JOIN_TIMEOUT_SECONDS + EMAIL_WORKER_STOP_JOIN_TIMEOUT_SECONDS)
PER_USER_JOIN_BUDGET_SECONDS = (
    LISTENER_JOINS_PER_USER * LISTENER_STOP_JOIN_TIMEOUT_SECONDS
    + WATCHDOG_STOP_JOIN_TIMEOUT_SECONDS
    + PERIODIC_WORKERS_PER_USER * WORKER_STOP_JOIN_TIMEOUT_SECONDS)


def _graceSeconds(text: str):
    """The declared stop_grace_period in seconds, or None if there is none.
    A trailing `#<` comment is this repo's house style, so allow one."""
    match = re.search(r"^\s*stop_grace_period:\s*(\d+)s\s*(#.*)?$", text, re.MULTILINE)
    return int(match.group(1)) if match else None


class TestComposeShutdownBudget(unittest.TestCase):
    def test_the_compose_file_declares_one(self):
        self.assertIsNotNone(_graceSeconds(COMPOSE_PATH.read_text(encoding="utf-8")),
                             "without it Docker allows 10s for the whole shutdown")

    def test_it_covers_the_process_wide_workers_and_one_full_user(self):
        """Not N users - that grows without bound and no fixed value can cover
        it. One user plus the process-wide workers is the line worth holding:
        below it, a single wedged listener guarantees the force-kill."""
        needed = PROCESS_WIDE_JOIN_BUDGET_SECONDS + PER_USER_JOIN_BUDGET_SECONDS

        self.assertGreaterEqual(
            _graceSeconds(COMPOSE_PATH.read_text(encoding="utf-8")), needed,
            f"the shutdown path can spend {needed}s on joins for a single user; "
            "raise stop_grace_period, or lower whichever join bound grew")

    def test_the_readme_copy_declares_the_same_value(self):
        composeValue = _graceSeconds(COMPOSE_PATH.read_text(encoding="utf-8"))
        readmeValue = _graceSeconds(README_PATH.read_text(encoding="utf-8"))

        self.assertEqual(readmeValue, composeValue,
                         "the README's compose snippet drifted from the real file")


if __name__ == "__main__":
    unittest.main()
