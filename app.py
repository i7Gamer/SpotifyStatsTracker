import os
import json
import threading
from pathlib import Path
import time

from flask import Flask, render_template, redirect, request, url_for, jsonify, send_from_directory

from Database.database import Database
from Database.Migrators.migrate import migrateIfNeeded
from Database.utils import msToString, convertToDatetime, formatDuration, dateToString
from SpotipyFree import saveSession, parseCookieString

class SpotifyDashboardApp:
    def __init__(self):
        migrateIfNeeded()
        self.app = Flask(__name__)
        self.baseDir = Path(__file__).resolve().parent
        self.username = "Tzur"
        self.cookiesFile = self.baseDir / "secrets" / "cookies.json"
        self.database = Database(user=self.username)
        self.database.resetProgress()

        self.registerRoutes()

        # Initialize background listener if cookies exist
        if self.cookiesFile.exists():
            self.startListenerIfNeeded()

    def startListenerIfNeeded(self):
        if self.database.listener is None:
            self.database.startListener(str(self.cookiesFile))
            print("Started listener thread.")
            time.sleep(2)  # Give listener time to initialize

    def _embedSongTextElements(self, song) -> dict:
        if "playedAt" in song:   #< some tracks just dont have it (top tracks)
            playedAt = convertToDatetime(song["playedAt"])
            song["playedAtText"] = playedAt.strftime("%Y-%m-%d %H:%M")
            song["timePlayedText"] = msToString(song["timePlayed"])

        artistsText = ", ".join(a.get("name", "") for a in song["artists"])
        releaseDateText = dateToString(song["album"]["releaseDate"])
        song["releaseDateText"] = releaseDateText
        song["artistsText"] = artistsText
        song["durationText"] = formatDuration(song["duration"])
        song["album"]["releaseDateText"] = releaseDateText
        return song

    def _embedTopSongTextElements(self, song) -> dict:
        song["totalTimeListenedText"] = msToString(song.get("totalTimeListened", 0))
        return song

    def _embedArtistTextElements(self, artist) -> dict:
        artist["totalTimeListenedText"] = msToString(artist.get("totalTimeListened", 0))
        return artist

    def _embedSongsTextElements(self, songs) -> list[dict]:
        return [self._embedSongTextElements(song) for song in songs]

    def _embedTopSongsTextElements(self, songs) -> list[dict]:
        return [self._embedTopSongTextElements(song) for song in songs]

    def _embedArtistsTextElements(self, songs) -> list[dict]:
        return [self._embedArtistTextElements(song) for song in songs]

    def _getNeighboringUrls(self, name, page, totalPages):
        prevUrl = url_for(name, page=page - 1) if page > 1 else None
        nextUrl = url_for(name, page=page + 1) if page < totalPages else None
        return prevUrl, nextUrl
    
    def _getTotal(self, arr, key):
        return sum(i.get(key, 0) for i in arr)

    def getLatestHistory(self, limit=None):
        return self.database.getEntriesFromNew(limit)

    def getPage(self, items, page, pageSize=50):
        """ Gets items in page as well as other data including total pages and start index """
        page = max(1, page)
        total = len(items)
        totalPages = max(1, (total + pageSize - 1) // pageSize)
        start = (page - 1) * pageSize
        end = start + pageSize
        return (items[start:end], totalPages, start)

    def runImportBackground(self, historyData):
        try:
            self.database.importSpotifyHistory(historyData)
        except Exception:
            pass

    def ensureLoggedIn(self):
        if self.cookiesFile.exists():
            try:
                json.loads(self.cookiesFile.read_text(encoding="utf-8"))
                self.startListenerIfNeeded()
                if self.database.isListenerLoggedIn():
                    return True
            except Exception as e:
                print(e)
        return False

    def registerRoutes(self):
        @self.app.route('/img/<username>/tracks/<filename>')
        def serveTrackImage(username, filename):
            imageDir = os.path.join(self.baseDir, "Database", "Users", username, "img", "tracks")
            return send_from_directory(imageDir, filename)

        @self.app.route('/img/<username>/artists/<filename>')
        def serveArtistImage(username, filename):
            imageDir = os.path.join(self.baseDir, "Database", "Users", username, "img", "artists")
            return send_from_directory(imageDir, filename)

        @self.app.route("/import-history", methods=["POST"])
        def importHistory():
            if self.database.readProgress().get("status") == "running":
                return redirect(url_for("importPage"))

            upload = request.files.get("history_file")
            if upload is None or upload.filename == "":
                return redirect(url_for("importPage"))

            try:
                historyData = json.load(upload)
            except json.JSONDecodeError:
                return redirect(url_for("importPage"))

            thread = threading.Thread(target=self.runImportBackground, args=(historyData,), daemon=True)
            thread.start()
            time.sleep(1)  # Give thread time to start and update progress
            return redirect(url_for("importPage"))

        @self.app.route("/import", methods=["GET"])
        def importPage():
            return render_template("import.html", importProgress=self.database.readProgress())

        @self.app.route("/login", methods=["GET", "POST"])
        def login():
            step = request.form.get("step", "1")

            if step == "1":
                if request.method == "GET":
                    return render_template("login.html", step=1)

                email = request.form.get("email", "").strip()
                if not email:
                    return render_template("login.html", step=1, error="Email required.")

                return render_template("login.html", step=2, email=email)

            if step == "2":
                email = request.form.get("email", "")
                cookies = request.form.get("cookies", "")

                if not cookies:
                    return render_template("login.html", step=2, email=email, error="Cookies required.")

                # with open(self.cookiesFile, "w", encoding="utf-8") as f:
                #     json.dump({"email": email, "cookies": cookies}, f, indent=2)
                saveSession(parseCookieString(cookies), email, self.cookiesFile)

                # FIX: updated 'dashboard' endpoint target
                return redirect(url_for("dashboard"))

        @self.app.route("/import-progress", methods=["GET"])
        def importProgress():
            return jsonify(self.database.readProgress())

        @self.app.route("/", methods=["GET"])
        def dashboard():
            if not self.ensureLoggedIn():
                return redirect(url_for("login", next=request.path))
            page = int(request.args.get("page", 1) or 1)
            pageSize = 50
            total = self.database.getEntriesCount()
            startIndex = (page - 1) * pageSize
            tracks = self.database.getEntriesFromNew(count=pageSize, startIndex=startIndex)
            tracks = self._embedSongsTextElements(tracks)

            totalPages = max(1, (total + pageSize - 1) // pageSize)

            totalDurationMs = sum(track.get("timePlayed", 0) for track in self.getLatestHistory(None))
            totalDurationText = msToString(totalDurationMs)

            uniqueArtists = len({track.get("artist") for track in self.getLatestHistory(None) if track.get("artist")})
            prevUrl, nextUrl = self._getNeighboringUrls("dashboard", page, totalPages)

            return render_template(
                "tracks.html",
                tracks=tracks,
                total=total,
                uniqueArtists=uniqueArtists,
                totalDuration=totalDurationText,
                username=self.username,
                page=page,
                totalPages=totalPages,
                prevUrl=prevUrl,
                nextUrl=nextUrl,
                startIndex=startIndex,
                section="dashboard",
            )

        @self.app.route("/top-songs", methods=["GET"])
        def topSongsPage():
            if not self.ensureLoggedIn():
                return redirect(url_for("login", next=request.path))

            page = int(request.args.get("page", 1) or 1)
            rawTopSongs = self.database.getTopSongs() or []
            tracks, totalPages, startIndex = self.getPage(rawTopSongs, page)
            tracks = self._embedSongsTextElements(tracks)
            tracks = self._embedTopSongsTextElements(tracks)

            totalPlays = self._getTotal(rawTopSongs, "plays")
            totalMs = self._getTotal(rawTopSongs, "totalTimeListened")
            prevUrl, nextUrl = self._getNeighboringUrls("topSongsPage", page, totalPages)

            return render_template(
                "top_songs.html",
                tracks=tracks,
                username=self.username,
                totalPlays=totalPlays,
                totalTime=msToString(totalMs),
                page=page,
                totalPages=totalPages,
                prevUrl=prevUrl,
                nextUrl=nextUrl,
                startIndex=startIndex,
                section="top_songs",
            )

        @self.app.route("/top-artists", methods=["GET"])
        def topArtistsPage():
            if not self.ensureLoggedIn():
                return redirect(url_for("login", next=request.path))

            page = int(request.args.get("page", 1) or 1)
            rawTopArtists = self.database.getTopArtists() or []
            artists, totalPages, startIndex = self.getPage(rawTopArtists, page)
            artists = self._embedArtistsTextElements(artists)

            totalPlays = self._getTotal(rawTopArtists, "plays")
            totalUnique = self._getTotal(rawTopArtists, "uniqueSongCount")
            totalMs = self._getTotal(rawTopArtists, "totalTimeListened")
            prevUrl, nextUrl = self._getNeighboringUrls("topArtistsPage", page, totalPages)

            return render_template(
                "top_artists.html",
                tracks=artists,
                username=self.username,
                totalPlays=totalPlays,
                totalUnique=totalUnique,
                totalTime=msToString(totalMs),
                page=page,
                totalPages=totalPages,
                prevUrl=prevUrl,
                nextUrl=nextUrl,
                startIndex=startIndex,
                section="top_artists",
            )

    def run(self):
        self.app.run(host="0.0.0.0", debug=True, port=5000, threaded=False, use_reloader=False)

if __name__ == "__main__":
    dashboardApp = SpotifyDashboardApp()
    dashboardApp.run()