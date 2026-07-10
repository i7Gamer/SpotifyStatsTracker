import os
from app import SpotifyDashboardApp

# Initialize the application instance
dashboardApp = SpotifyDashboardApp()
app = dashboardApp.app

if __name__ == "__main__":
    from waitress import serve
    threads = int(os.environ.get("WAITRESS_THREADS", 16))
    serve(app, host="0.0.0.0", port=5000, threads=threads)
