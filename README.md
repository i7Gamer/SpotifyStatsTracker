<img width="1437" height="1946" alt="SpotifyTrackerOverviewV2" src="https://github.com/user-attachments/assets/7ca9d5f5-3e97-4cd3-b0f8-f57376733c6f" />
<img width="1438" height="2350" alt="SpotifyTrackerCompare" src="https://github.com/user-attachments/assets/d1637725-5bb6-4755-af5d-88d8c501d45a" />
<img width="1438" height="2169" alt="SpotifyTrackerChartsV3" src="https://github.com/user-attachments/assets/2dbc0b6a-0d30-4527-a895-582ab30db639" />
<img width="1437" height="1468" alt="SpotifyTrackerWrappedV3" src="https://github.com/user-attachments/assets/da01ff13-f9a3-4eb0-b2c4-bc72f16f992b" />

## Spotify Stats Tracker - [![Tests](https://github.com/i7Gamer/SpotifyStatsTracker/actions/workflows/tests.yml/badge.svg)](https://github.com/i7Gamer/SpotifyStatsTracker/actions/workflows/tests.yml)

### If you found [this repository](https://github.com/i7Gamer/SpotifyStatsTracker) useful, please give it a ⭐!
A web application that allows users to track and analyze their Spotify listening habits and statistics **without Spotify Premium**.

## Features

- **Top Lists**: View your top songs, artists, and albums with detailed statistics
- **Listening History**: Browse your play history on a dedicated `/history` page with instant AJAX filtering, and track daily listening activity with a contribution-style streak calendar on the Dashboard
- **Achievement Milestones**: Automatically celebrate lifetime play-count and listening-time thresholds, listening streaks, and each new all-time #1 artist - surfaced as a topbar badge, Next Milestones dashboard panel, and a Milestones section on your Profile
- **Charts & Analytics**: Visualize your listening patterns and statistics with interactive charts, customizable trend bucket granularity (hour, day, week, month, year), and a Top Genres breakdown once enough genre data has been backfilled (see Genre Insights below)
- **Yearly Wrapped & Share Links**: Get a personalized recap of your yearly listening with category filters (Top Songs, Artists, Albums, Discovered Songs, Artists, Albums) plus top genres, and generate shareable links with custom expiration
- **Data Sharing & Comparison**: Request to share your listening stats with another user - once they accept, compare top songs/artists/albums, a taste-match score, and shared genres side by side on the Compare page
- **Genre Insights & Biographies**: Add a free Last.fm API key on your Profile page to backfill genre tags and rich artist/album biographies in the background (see [Genre Data](#genre-data-optional) below)
- **Detail Pages & Interactive Timeline**: Drill down into individual songs, artists, and albums with an interactive play history timeline (with date headers, time gaps, and skip filters), embedded Spotify player, detailed stats, and biographies, plus a "Refresh Last.fm Data" button
- **Admin Console**: Instance admins can monitor real-time worker health (auto-importer, Last.fm backfiller, backup worker, metadata backfiller), manage user sync states, inspect catalog backfill coverage, and configure instance-wide settings at `/admin`
- **Multi-File Import**: Import multiple Spotify data export files at once with progress tracking
- **Overview Page**: See total database statistics, your listening breakdown, API backfill configuration, and genre-backfill progress
- **Auto-Import**: Automatically import files from the 'auto-import' folder with optional keyword filtering
- **Cross-Linking**: Click on artist names to explore artist pages from any song, and album links to see album details

## Installation

1. Clone the repository:
```bash
git clone https://github.com/i7Gamer/SpotifyStatsTracker
cd SpotifyStatsTracker
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Run the Application

### Using Docker

Use this docker-compose file:
```docker
version: '3.8'

services:
  spotify-tracker:
    image: i7gamer/spotify-tracker
    ports:
      - "5000:5000"
    volumes:
      - ./Database/Data:/app/Database/Data
      - ./autoImport:/app/autoImport  #< files put in this folder will be imported automatically
    environment:
      - FLASK_APP=wsgi.py
      - PYTHONUNBUFFERED=1
      - TZ=America/Los_Angeles        #< don't forget to change this or you will get the wrong times for songs
      - FLASK_SECRET_KEY=changeme-generate-your-own-random-value  #< YOU MUST CHANGE THIS - the app refuses to start on this exact placeholder (it's public, so it makes sessions forgeable). Generate one with `python -c "import secrets; print(secrets.token_hex(32))"`. A fixed value = sessions survive a restart; unset = a new one is generated each restart, logging everyone out. Also used to encrypt stored Spotify sessions unless DATA_ENCRYPTION_KEY is set - changing it means everyone must log in again.
      # - DATA_ENCRYPTION_KEY=changeme-another-random-value  #< Optional dedicated key for encrypting stored Spotify sessions/API secrets at rest (falls back to FLASK_SECRET_KEY). Keep it safe alongside your backups: without the key that encrypted them, stored sessions can't be read and every user must re-login with fresh cookies.
      # - TRUST_PROXY_HEADERS=1       #< Set when running behind a reverse proxy (nginx/traefik/caddy) so rate limiting sees real client IPs instead of the proxy's; use the number of proxy hops (usually 1). Only set this if a proxy is actually in front - otherwise clients could forge their IP.
      # - ENABLE_HSTS=1               #< Send a Strict-Transport-Security header so browsers pin this origin to HTTPS. Only enable behind a TLS-terminating reverse proxy - on plain-HTTP access it will lock browsers out of the site.
      # - ADMIN_EMAIL=you@example.com #< Makes this account the instance's only admin (grants access to the Admin Console at /admin to view all user sync states, worker health, and system settings). Without it, the earliest-registered user is promoted automatically.
      # - SPOTIFY_CALLBACK_URL=http://localhost:5000/spotify-callback  #< Uncomment and set to your public callback URL to enable Spotify Web API backfilling
      # - IMPORT_KEYWORD=Weekly       #< Uncomment to apply a filter to what files get auto-imported (only files containing this will be imported)
      # - FLASK_DEBUG=1               #< To get more detailed logs from Flask (provide this when opening an issue)
      # - SKIP_EMAIL_VERIFICATION=1   #< Uncomment to disable the "do these cookies belong to this email" check at login (only do this if you trust everyone who can reach this instance - it's what stops one user from claiming another's account, AND what stops the /reset-password flow from letting anyone set a new password on any account)
    restart: always
```

Then you can run `docker compose up -d` and the app should start on `http://127.0.0.1:5000` or `http://yourIp:5000`

To update the container if an update is available, run `docker compose pull`

> **Note on scaling:** the app runs as a single process (Waitress/Flask). In-memory state - the per-IP auth rate limiter, the login-status cache, and the background worker pools - lives in that one process and is not shared, so run one instance rather than scaling it horizontally behind a load balancer.

### Upgrading from an older version

Listening history, tracks, images, and login sessions live in a single SQLite
database under `Database/Data/`. If you were relying on `secrets/` being mounted (e.g. so
`secrets/flask_secret_key.txt` persisted across restarts), set `FLASK_SECRET_KEY`
as shown above instead; otherwise everyone's login session resets on each container
restart.

### Local Development

1. Start the app:
```bash
python app.py
```

2. Open the app in your browser:
```text
http://localhost:5444
```
or whatever your IP is
```text
http://127.0.0.1:5444
```

**Note:** The Docker container persists data in the `Database/Data/` directory on your host machine.

### Restarting the app (admin restart button)

The admin console has an optional **"Restart app to apply"** button — used after changing worker-pool sizes on the Advanced Tuning panel, since those only take effect on restart. It works by gracefully stopping background workers and exiting, so **something must relaunch the process**. It stays hidden unless you set `ALLOW_INSTANCE_RESTART=1`.

- **Docker** already relaunches on exit (`restart: always` in `docker-compose.yml`), so it is safe to enable there.
- **Running `python wsgi.py` directly**, wrap it in a supervisor that restarts on exit (a Windows service via NSSM, a Task Scheduler task, or a simple loop script such as `while ($true) { python wsgi.py }`) **before** enabling this — otherwise the button just stops the app with nothing to bring it back.

### Backups

Listening history, tracks, images, and login sessions all live in one SQLite file at `Database/Data/spotify_stats.db`.

**Automatic backups are on by default**: the app snapshots the database every 24 hours into `Database/Data/Backups/` (covered by the standard volume mount) and keeps the newest 7 snapshots. Tune or disable via environment variables:

```yaml
      # - BACKUP_INTERVAL_HOURS=24   #< how often to snapshot; 0 disables automatic backups
      # - BACKUP_RETENTION_COUNT=7   #< how many snapshots to keep; 0 disables automatic backups
```

These snapshots live on the same disk as the database, so they protect against corruption and accidental deletion - copy them somewhere else (a different disk, cloud storage) for real disaster protection.

You can also export your own play history from the Profile page (JSON in Spotify's extended-export format - re-importable via the Import page - or CSV).

To take a manual snapshot: the app runs the database in [WAL mode](https://www.sqlite.org/wal.html), so **don't just copy the `.db` file** while the container is running - recent writes can still be sitting in a separate `-wal` file that a raw copy would miss, producing a backup that's silently missing data or corrupt. Use SQLite's own online backup API instead, which is safe to run against a live, in-use database:

```bash
docker compose exec spotify-tracker python -c "import sqlite3; sqlite3.connect('/app/Database/Data/spotify_stats.db').backup(sqlite3.connect('/app/Database/Data/spotify_stats_backup.db'))"
```

This writes `spotify_stats_backup.db` into the same `Database/Data/` folder on your host machine (via the volume mount). Copy that file somewhere else - a different disk, cloud storage, etc. - for it to actually protect you against data loss, and rename or timestamp it before backing up again if you want to keep more than one snapshot.

Stored Spotify sessions and API secrets inside the database are encrypted with the key from `DATA_ENCRYPTION_KEY` (or `FLASK_SECRET_KEY` if that's not set - see the compose example above). Two practical consequences: keep that key somewhere safe alongside your backups, since a restored backup is unreadable without the key that encrypted it (listening history stays intact; everyone just has to log in with fresh cookies again) - and don't treat a backup as fully safe to hand around either, because anyone holding both the backup **and** the key can read every user's live Spotify session.

### Spotify Web API Backfilling (Optional)

To enable automatic backfilling of missed plays via the Spotify Developer API, you must configure the `SPOTIFY_CALLBACK_URL` environment variable:

1. Register an application in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Set the **Redirect URI** in your Spotify app configuration to match your public callback URL (e.g. `http://localhost:5000/spotify-callback`).
3. Set the `SPOTIFY_CALLBACK_URL` environment variable in your `docker-compose.yml` to this exact callback URL.
4. Once set, the Spotify Developer settings section will become visible on your User Profile page, allowing you to link your account.

### Genre Data (Optional)

Each user can add their own [Last.fm](https://www.last.fm) API key to have a background worker fetch genre tags for the artists, albums, and songs in their listening history:

1. Create a free key on the [Last.fm API account page](https://www.last.fm/api/account/create) (no Last.fm scrobbling account required).
2. Paste it into the Last.fm API Settings section on your Profile page.
3. A background worker starts fetching genre tags for your most-played artists, albums, and songs first, respecting Last.fm's request-rate limits. Once your own library is covered, it keeps helping backfill genres for everyone else's, since the artist/album/song catalog is shared across all users.
4. Track progress on the Overview page. Once enough of your history has genre data, genre breakdowns unlock on the Charts, Wrapped, and Compare pages.

Songs or albums Last.fm has no tags for inherit their artist's genres; the instance admin can toggle whether those inherited genres count towards backfill progress and genre stats from the Admin Console (`/admin`).

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

For support, please open an issue on the GitHub repository or contact me.

Additional Screenshots:
<img width="1437" height="931" alt="SpotifyTrackerTopSongs" src="https://github.com/user-attachments/assets/b11e504d-399f-46ff-9097-239ef404c7da" />
<img width="659" height="1726" alt="SpotifyTrackerProfile" src="https://github.com/user-attachments/assets/5bfd55d5-1af6-4251-9ecd-6c13b3b3c92e" />
<img width="1437" height="1175" alt="SpotifyTrackerArtistSubpage" src="https://github.com/user-attachments/assets/bdfc8a58-2064-4a19-a1d1-68433a1edb07" />
<img width="1438" height="1166" alt="SpotifyTrackerSongSubpage" src="https://github.com/user-attachments/assets/82ae38ae-27c8-4311-9e7e-e1317433222d" />
<img width="1429" height="1190" alt="SpotifyTrackerAlbumSubpage" src="https://github.com/user-attachments/assets/52ae6921-0a03-4a31-9464-dca94ef97d64" />
