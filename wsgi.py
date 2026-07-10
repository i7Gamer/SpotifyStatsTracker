import os
from app import SpotifyDashboardApp

# Initialize the application instance
dashboardApp = SpotifyDashboardApp()
app = dashboardApp.app

def main():
    from waitress import serve
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
