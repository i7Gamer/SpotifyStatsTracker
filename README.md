<img width="1435" height="1545" alt="SpotifyTrackerOverview" src="https://github.com/user-attachments/assets/4ec167b5-b8a8-4247-b1dd-801e2a8523dc" />
<img width="1437" height="931" alt="SpotifyTrackerTopSongs" src="https://github.com/user-attachments/assets/b11e504d-399f-46ff-9097-239ef404c7da" />
<img width="1438" height="2169" alt="SpotifyTrackerChartsV3" src="https://github.com/user-attachments/assets/2dbc0b6a-0d30-4527-a895-582ab30db639" />
<img width="1437" height="1468" alt="SpotifyTrackerWrappedV3" src="https://github.com/user-attachments/assets/da01ff13-f9a3-4eb0-b2c4-bc72f16f992b" />

## Spotify Stats Tracker - [![Tests](https://github.com/i7Gamer/SpotifyStatsTracker/actions/workflows/tests.yml/badge.svg)](https://github.com/i7Gamer/SpotifyStatsTracker/actions/workflows/tests.yml)

### If you found [this repository](https://github.com/i7Gamer/SpotifyStatsTracker) useful, please give it a ⭐!
A web application that allows users to track and analyze their Spotify listening habits and statistics **without Spotify Premium**.

## Features

- **Top Lists**: View your top songs, artists, and albums with detailed statistics
- **Listening History**: See your listening history and track Spotify activity in real time
- **Charts & Analytics**: Visualize your listening patterns and statistics with interactive charts
- **Yearly Wrapped**: Get a personalized recap of your yearly listening with category filters (Top Songs, Artists, Albums, Discovered Songs, Artists, Albums)
- **Detail Pages**: Drill down into individual songs, artists, and albums to see play history and detailed stats
- **Multi-File Import**: Import multiple Spotify data export files at once with progress tracking
- **Overview Page**: See total data saved in the database and check list of users, their current sync state and their api backfill configuration.
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
      - ./Database/Users:/app/Database/Users  #< pre-1.7.0 data dir; only needed for the one restart that<img width="1435" height="1545" alt="SpotifyTrackerOverview" src="https://github.com/user-attachments/assets/a9a80574-4a1d-4f9e-81b1-499b04086175" />
 migrates it into Data/ above, safe to remove after that
      - ./autoImport:/app/autoImport  #< files put in this folder will be imported automatically
    environment:
      - FLASK_APP=wsgi.py
      - PYTHONUNBUFFERED=1
      - TZ=America/Los_Angeles        #< don't forget to change this or you will get the wrong times for songs
      - FLASK_SECRET_KEY=changeme-generate-your-own-random-value  #< fixed value = sessions survive a restart; unset = a new one is generated each restart, logging everyone out
      # - SPOTIFY_CALLBACK_URL=http://localhost:5000/spotify-callback  #< Uncomment and set to your public callback URL to enable Spotify Web API backfilling
      # - IMPORT_KEYWORD=Weekly       #< Uncomment to apply a filter to what files get auto-imported (only files containing this will be imported)
      # - FLASK_DEBUG=1               #< To get more detailed logs from Flask (provide this when opening an issue)
      # - SKIP_EMAIL_VERIFICATION=1   #< Uncomment to disable the "do these cookies belong to this email" check at login (only do this if you trust everyone who can reach this instance - it's what stops one user from claiming another's account)
    restart: always
```

### Then you can run `docker compose up -d` and the app should start on `http://127.0.0.1:5000` or `http://yourIp:5000`

### To update the container if an update is available, run `docker compose pull`

### Upgrading from an older version

Listening history, tracks, images, and login sessions now live in a single SQLite
database under `Database/Data/` instead of the old per-user JSON files under
`Database/Users/` and the `secrets/` folder - the app migrates existing data
automatically on first startup after the update, no action needed beyond keeping
both volumes mounted (as shown above) for that first restart. Once you've confirmed
the migration succeeded, the `./Database/Users:/app/Database/Users` line can be
removed. If you were relying on `secrets/` being mounted (e.g. so
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
http://127.0.0.1:5444
```
or whatever your IP is

3. Open the app in your browser:
```text
http://127.0.0.1:5444
```

**Note:** The Docker container persists data in the `Database/Data/` directory on your host machine.

### Backups

Listening history, tracks, images, and login sessions all live in one SQLite file at `Database/Data/spotify_stats.db`. The app runs it in [WAL mode](https://www.sqlite.org/wal.html), so **don't just copy the `.db` file** while the container is running - recent writes can still be sitting in a separate `-wal` file that a raw copy would miss, producing a backup that's silently missing data or corrupt. Use SQLite's own online backup API instead, which is safe to run against a live, in-use database:

```bash
docker compose exec spotify-tracker python -c "import sqlite3; sqlite3.connect('/app/Database/Data/spotify_stats.db').backup(sqlite3.connect('/app/Database/Data/spotify_stats_backup.db'))"
```

This writes `spotify_stats_backup.db` into the same `Database/Data/` folder on your host machine (via the volume mount). Copy that file somewhere else - a different disk, cloud storage, etc. - for it to actually protect you against data loss, and rename or timestamp it before backing up again if you want to keep more than one snapshot.

### Spotify Web API Backfilling (Optional)

To enable automatic backfilling of missed plays via the Spotify Developer API, you must configure the `SPOTIFY_CALLBACK_URL` environment variable:

1. Register an application in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard).
2. Set the **Redirect URI** in your Spotify app configuration to match your public callback URL (e.g. `http://localhost:5000/spotify-callback`).
3. Set the `SPOTIFY_CALLBACK_URL` environment variable in your `docker-compose.yml` to this exact callback URL.
4. Once set, the Spotify Developer settings section will become visible on your User Profile page, allowing you to link your account.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

For support, please open an issue on the GitHub repository or contact me.

Additional Screenshots:
<img width="659" height="1726" alt="SpotifyTrackerProfile" src="https://github.com/user-attachments/assets/5bfd55d5-1af6-4251-9ecd-6c13b3b3c92e" />
<img width="1437" height="1175" alt="SpotifyTrackerArtistSubpage" src="https://github.com/user-attachments/assets/bdfc8a58-2064-4a19-a1d1-68433a1edb07" />
<img width="1438" height="1166" alt="SpotifyTrackerSongSubpage" src="https://github.com/user-attachments/assets/82ae38ae-27c8-4311-9e7e-e1317433222d" />
<img width="1429" height="1190" alt="SpotifyTrackerAlbumSubpage" src="https://github.com/user-attachments/assets/52ae6921-0a03-4a31-9464-dca94ef97d64" />
