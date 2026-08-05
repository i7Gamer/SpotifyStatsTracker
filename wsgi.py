# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import os
import signal
from app import SpotifyDashboardApp

# Initialize the application instance. Construction only builds the WSGI app;
# the background workers (backup, email, version-check, per-user listeners) are
# a separate, explicit step - see SpotifyDashboardApp.startWorkers.
dashboardApp = SpotifyDashboardApp()
dashboardApp.startWorkers()
app = dashboardApp.app

def _sigtermHandler(signum, frame):
    """Route SIGTERM into the KeyboardInterrupt path Ctrl+C already takes.

    Under Docker this process is PID 1 (exec-form CMD), and the kernel
    discards default-action signals for PID 1 - Python leaves SIGTERM at
    SIG_DFL and waitress installs no handler either, so without this
    `docker stop` did nothing: the container sat out the whole
    stop_grace_period and was SIGKILLed with main()'s finally never run."""
    raise KeyboardInterrupt


def main():
    from waitress import serve
    # Before serve() blocks: a stop arriving mid-serve is the normal case.
    # ValueError = not the main thread, which only a test harness driving
    # main() can be; the real process installs from the main thread.
    try:
        signal.signal(signal.SIGTERM, _sigtermHandler)
    except ValueError:
        pass
    threads = int(os.environ.get("WAITRESS_THREADS", 16))
    try:
        serve(app, host="0.0.0.0", port=5000, threads=threads)
    finally:
        # Stop every user's listener/auto-importer threads before the process
        # exits, so a SIGINT/SIGTERM to waitress doesn't leave them to be force-
        # killed mid-request during interpreter shutdown.
        dashboardApp.shutdown()


if __name__ == "__main__":
    main()
