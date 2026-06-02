# Spotify Stats Tracker

A web application that allows users to track and analyze their Spotify listening habits and statistics.

## Features

- View your top songs.
- View your top artists.
- See your listening history.

## Installation

1. Clone the repository:
```bash
git clone https://github.com/TzurSoffer/SpotifyStatsTracker
cd SpotifyStatsTracker
```

2. Install dependencies:
```bash
pip install -r requirements.txt
```

## Run the Application

### Local Development

1. Start the app:
```bash
python app.py
```

2. Open the app in your browser:
```text
http://127.0.0.1:5000
```
or whatever your IP is

### Using Docker

1. Build and run with Docker Compose (recommended):
```bash
docker-compose up --build
```

2. Or build manually and run:
```bash
docker build -t spotify-tracker .
docker run -p 5000:5000 -v $(pwd)/Database:/app/Database spotify-tracker
```

3. Open the app in your browser:
```text
http://127.0.0.1:5000
```

**Note:** The Docker container persists data in the `Database/` directory on your host machine.

## License

This project is licensed under the MIT License - see the LICENSE file for details.

## Support

For support, please open an issue on the GitHub repository or contact me.