<img width="1438" height="985" alt="SpotifyTrackerDashboardV2" src="https://github.com/user-attachments/assets/378a7f64-0bde-40eb-94e5-a1922984a942" />
<img width="1438" height="1497" alt="SpotifyTrackerWrappedV2" src="https://github.com/user-attachments/assets/807226de-8ac9-44c5-a3ca-371777976c74" />
<img width="1438" height="1507" alt="SpotifyTrackerChartsV2" src="https://github.com/user-attachments/assets/584e24b9-7c62-48f3-8bfb-caeab07007e9" />

## Spotify Stats Tracker

### If you found [this repository](https://github.com/i7Gamer/SpotifyStatsTracker) useful, please give it a ⭐!
A web application that allows users to track and analyze their Spotify listening habits and statistics **without Spotify Premium**.

## Features

- **Top Lists**: View your top songs, artists, and albums with detailed statistics
- **Listening History**: See your listening history and track Spotify activity in real time
- **Charts & Analytics**: Visualize your listening patterns and statistics with interactive charts
- **Yearly Wrapped**: Get a personalized recap of your yearly listening with category filters (Top Songs, Artists, Albums, Discoveries)
- **Detail Pages**: Drill down into individual songs, artists, and albums to see play history and detailed stats
- **Multi-File Import**: Import multiple Spotify data export files at once with progress tracking
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
      - ./Database/Users:/app/Database/Users  #< pre-1.7.0 data dir; only needed for the one restart that migrates it into Data/ above, safe to remove after that
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
