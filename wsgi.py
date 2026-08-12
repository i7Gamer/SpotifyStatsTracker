# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import logging
import os
import signal
from app import SpotifyDashboardApp

logger = logging.getLogger(__name__)

# waitress's own default is 4; 16 matches the per-user AJAX fan-out the
# dashboard pages issue. The floor is 1 because waitress with zero threads
# accepts connections and serves nothing - unlike Database.backup._envInt,
# whose max(0, ...) is right for a retention count and wrong for this.
WAITRESS_THREADS_DEFAULT = 16
WAITRESS_THREADS_MINIMUM = 1


def _waitressThreads() -> int:
    """WAITRESS_THREADS, or the default for anything unusable.

    os.environ.get(name, default) yields the default only when the variable is
    UNSET - a variable present but EMPTY (`- WAITRESS_THREADS=` in a compose
    file) yields "", and int("") raises. This is resolved after module scope has
    already opened every user's database and started their listeners, so raising
    here is a crash loop whose traceback never names the variable."""
    raw = os.environ.get("WAITRESS_THREADS", "").strip()
    if not raw:
        return WAITRESS_THREADS_DEFAULT
    try:
        return max(WAITRESS_THREADS_MINIMUM, int(raw))
    except ValueError:
        logger.warning("Ignoring non-numeric WAITRESS_THREADS=%r, using default %d",
                       raw, WAITRESS_THREADS_DEFAULT)
        return WAITRESS_THREADS_DEFAULT

def _sigtermHandler(signum, frame):
    """Route SIGTERM into the KeyboardInterrupt path Ctrl+C already takes.

    Under Docker this process is PID 1 (exec-form CMD), and the kernel
    discards default-action signals for PID 1 - Python leaves SIGTERM at
    SIG_DFL and waitress installs no handler either, so without this
    `docker stop` did nothing: the container sat out the whole
    stop_grace_period and was SIGKILLed with main()'s finally never run.

    It disarms itself first. A second SIGTERM - a repeated `docker stop`, or a
    supervisor that re-signals - would otherwise raise again INSIDE the
    shutdown the first one started, landing in _stopDatabasesConcurrently's
    thread.join() so the remaining users are never joined and the traceback
    escapes main(). Ignored rather than restored to SIG_DFL, because for PID 1
    the default action is discarded anyway; the real escape hatch is Docker's
    own SIGKILL at the end of stop_grace_period."""
    _armSigterm(signal.SIG_IGN)
    raise KeyboardInterrupt


def _armSigterm(handler):
    """Point SIGTERM at `handler`, tolerating the not-the-main-thread case.

    ValueError = not the main thread, which only a test harness driving this
    can be; the real process installs from the main thread."""
    try:
        signal.signal(signal.SIGTERM, handler)
    except ValueError:
        pass


# Armed BEFORE startWorkers, not in main(). startWorkers runs at module scope
# and checkLogin_thread's first pass is synchronous - it opens every user's
# database and performs a network-bound Spotify login per user, seconds to
# minutes - so a handler installed in main() left that whole window covered by
# nothing, and a stop during boot was discarded exactly as it was before this
# handler existed.
_armSigterm(_sigtermHandler)

# Initialize the application instance. Construction only builds the WSGI app;
# the background workers (backup, email, version-check, per-user listeners) are
# a separate, explicit step - see SpotifyDashboardApp.startWorkers.
dashboardApp = SpotifyDashboardApp()
try:
    dashboardApp.startWorkers()
except BaseException:
    # Module scope has to own what main()'s finally owns for the serving phase:
    # arming the handler this early means the raise arrives out here, and
    # whatever startWorkers got as far as starting still has to be stopped.
    dashboardApp.shutdown()
    raise
app = dashboardApp.app


def main():
    # Everything between here and serve() is inside the try: module scope has
    # already started every user's listeners and watchdogs, so anything that
    # raises on the way to serving still owes them the shutdown below.
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=5000, threads=_waitressThreads())
    finally:
        # Stop every user's listener/auto-importer threads before the process
        # exits, so a SIGINT/SIGTERM to waitress doesn't leave them to be force-
        # killed mid-request during interpreter shutdown.
        dashboardApp.shutdown()


if __name__ == "__main__":
    main()
