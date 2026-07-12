import logging
import os
import json
import secrets
import tempfile
import threading
import requests
from pathlib import Path
import time
from datetime import timedelta

from flask import Flask, render_template, redirect, request, url_for, jsonify, send_from_directory, session, g
from werkzeug.security import generate_password_hash, check_password_hash

from Database.database import Database
from Database.repository import Repository
from Database.Migrators.migrate import migrateIfNeeded
from Database.Listeners.spotifyListener import _suppress_signal_in_thread
from Database.logging_config import configureLogging
from Database.utils import msToString, convertToDatetime, formatDuration, dateToString, versionTuple, now, startOfDay, parseDateString
import SpotipyFree
from SpotipyFree import saveSession, parseCookieString

logger = logging.getLogger(__name__)

PAGE_SIZE = 50                  #< list items shown per page
LOGIN_CACHE_TTL_SECONDS = 180  #< seconds to cache isListenerLoggedIn result per user
CHART_ARTIST_TREND_TOP_N = 5   #< how many top artists are plotted on the trend line chart
WRAPPED_LIST_SIZE = 10          #< default/fallback for ?limit= - how many items per category the Wrapped page shows
WRAPPED_LIMIT_OPTIONS = (10, 25, 50, 100)   #< selectable values for Wrapped's items-per-category dropdown
MAX_UPLOAD_MB = 500              #< cap on a single import-history request's total upload size
DEFAULT_SORT_BY = "totalTimeListened"
# The only sortBy values Repository.SONG_SORT_COLUMNS/ALBUM_SORT_COLUMNS/
# ARTIST_SORT_COLUMNS know how to handle - an unrecognized ?sortBy= would
# otherwise reach a ValueError deep in the DB layer and 500 instead of just
# falling back to the default.
VALID_SORT_BY = {"totalTimeListened", "plays", "name"}
TRUTHY_ENV_VALUES = {"1", "true", "yes", "on"}
PASSWORD_MIN_LENGTH = 8   #< also enforced client-side via the minlength attribute


def _passwordPolicyError(password: str) -> str | None:
    """None if `password` satisfies the account password policy, otherwise a
    user-facing message naming the first unmet rule."""
    if len(password) < PASSWORD_MIN_LENGTH:
        return f"Password must be at least {PASSWORD_MIN_LENGTH} characters long."
    if not any(c.isupper() for c in password):
        return "Password must contain at least one uppercase letter."
    if not any(c.islower() for c in password):
        return "Password must contain at least one lowercase letter."
    if not any(c.isdigit() or not c.isalnum() for c in password):
        return "Password must contain at least one number or special character."
    return None


class SpotifyDashboardApp:
    def __init__(self):
        configureLogging()
        migrateIfNeeded()
        self.app = Flask(__name__)
        self.baseDir = Path(__file__).resolve().parent
        self.app.secret_key = self._get_or_create_secret_key()
        self.app.permanent_session_lifetime = timedelta(days=30)
        # Caps a single import-history request's total upload size (summed across
        # every file in a multi-file upload) - without this, an oversized/
        # accidental upload is read fully into memory before anything can reject
        # it.
        self.app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
        # Users, emails and Spotify session cookies live in the shared database
        # (see Database/repository.py) instead of secrets/users_map.json and
        # secrets/cookies.json.
        self.repo = Repository()

        self.user_databases = {}
        self._db_lock = threading.RLock()
        self._session_lock = threading.RLock()
        self._login_cache: dict = {}  #< {email: (result: bool, expires_at: float)}
        # Lets a self-hoster turn off the "do these cookies actually belong to
        # this email" check at login, e.g. if Spotify starts blocking the
        # verification request for their account. Off by default since it's
        # what stops one user from claiming another's account/database.
        self.skipEmailVerification = os.environ.get("SKIP_EMAIL_VERIFICATION", "").strip().lower() in TRUTHY_ENV_VALUES
        
        try:
            self.currentVersion = (self.baseDir / "Database" / "VERSION").read_text(encoding="utf-8").strip()  #< only needs to be checked once because app cant update without restart
        except Exception:
            self.currentVersion = "0.0.0"
        self.latestVersion = None
        self._version_lock = threading.Lock()
        self.startVersionCheck_thread()
        self.checkLogin_thread()

        self.registerRoutes()

    def _get_or_create_secret_key(self):
        """Resolve the Flask session-signing key. Prefers FLASK_SECRET_KEY, otherwise
        persists a random key under secrets/ so sessions can't be forged using the
        publicly-known default that used to ship in this repo."""
        envKey = os.environ.get("FLASK_SECRET_KEY")
        if envKey:
            return envKey

        keyFile = self.baseDir / "secrets" / "flask_secret_key.txt"
        if keyFile.exists():
            existingKey = keyFile.read_text(encoding="utf-8").strip()
            if existingKey:
                return existingKey

        newKey = secrets.token_hex(32)
        keyFile.parent.mkdir(parents=True, exist_ok=True)
        keyFile.write_text(newKey, encoding="utf-8")
        return newKey

    def get_username_for_email(self, email):
        return self.repo.getUsernameForEmail(email)

    def get_or_create_user(self, email):
        # The whole check-then-create sequence needs the lock, not just the final
        # write: two concurrent first-time logins for different emails that
        # happen to sanitize to the same username prefix (e.g. "alice@a.com" and
        # "alice@b.com") could otherwise both pass the uniqueness check before
        # either has actually created their row.
        with self._session_lock:
            username = self.repo.getUsernameForEmail(email)
            if not username:
                # Create a new username from email prefix
                prefix = email.split("@")[0]
                sanitized = "".join(c for c in prefix if c.isalnum() or c in ("-", "_")).strip()
                if not sanitized:
                    sanitized = f"user_{int(time.time())}"

                username = sanitized
                counter = 1
                while True:
                    if username in self.user_databases:
                        pass  # a live Database already exists under this name - can't be the same account, needs a new suffix
                    elif not self.repo.usernameExists(username):
                        self.repo.upsertUser(username, email)
                        break
                    elif self.repo.getEmailForUsername(username) is None:
                        # Orphaned account with no email on record - e.g. a
                        # migration whose users_map.json didn't have this user's
                        # email. Claim it instead of creating a sibling account
                        # that strands its existing history (the caller already
                        # verified these cookies belong to `email`).
                        self.repo.setUserEmail(username, email)
                        break

                    username = f"{sanitized}_{counter}"
                    counter += 1

        return username

    def _verifyCookiesMatchEmail(self, cookies: dict, email: str) -> bool:
        """Check that the submitted Spotify cookies actually belong to `email` by
        fetching the account profile with them. The cookies are written to a
        throwaway session file so an unverified login attempt can never overwrite
        another user's stored cookies. Without this check, anyone could claim any
        email at login and be handed that user's database."""
        if not cookies or not email:
            return False

        tmpFd, tmpPath = tempfile.mkstemp(prefix="verify_cookies_", suffix=".json")
        os.close(tmpFd)
        try:
            saveSession(cookies, email, tmpPath)
            with _suppress_signal_in_thread():
                sp = SpotipyFree.Spotify(cookiesFile=tmpPath, email=email)
            if not sp.isLoggedIn():
                return False
            profile = sp.current_user() or {}
            profileEmail = (profile.get("email") or "").strip().lower()
            return profileEmail == email.strip().lower()
        except Exception as e:
            logger.warning("Cookie verification failed for %s: %s", email, e)
            return False
        finally:
            try:
                os.unlink(tmpPath)
            except OSError:
                pass

    def get_user_db(self, username, email):
        with self._db_lock:
            if username not in self.user_databases:
                db = Database(user=username, email=email)
                db.startAutoImporter()
                db.resetProgress()
                db.startListener(email=email)
                try:
                    db.deduplicate()
                except Exception as e:
                    logger.error("Failed to deduplicate database for %s: %s", username, e)
                self.user_databases[username] = db
            return self.user_databases[username]

    def _refresh_user_session(self, username, email):
        """Restart this user's listener against the cookies just saved to the
        database, and drop any cached login-status result. Without this, a
        re-login after expired/invalid cookies (get_user_db is a no-op for a
        username that already has a live Database) would leave the old, dead
        listener running and the stale cached is_user_logged_in() result in
        place until the process restarts."""
        with self._db_lock:
            db = self.user_databases.get(username)
            if db is not None:
                if db.listener is not None:
                    db.listener.stop()
                db.startListener(email=email)
        self._login_cache.pop(email, None)

    def is_user_logged_in(self, email):
        if not email:
            return False

        username = self.repo.getUsernameForEmail(email)
        if not username or self.repo.getUserCookies(username) is None:
            return False

        # isListenerLoggedIn() can make a live network call to Spotify - the result
        # is cached per user for LOGIN_CACHE_TTL_SECONDS to avoid a round-trip on
        # every request (the main cause of Waitress queue saturation).
        if username in self.user_databases:
            now_ts = time.monotonic()
            cached = self._login_cache.get(email)
            if cached is not None and cached[1] > now_ts:
                return cached[0]
            result = self.user_databases[username].isListenerLoggedIn()
            self._login_cache[email] = (result, now_ts + LOGIN_CACHE_TTL_SECONDS)
            return result
        return True

    def checkLogin_thread(self):
        self._ensureAllUsersLogin()
        thread = threading.Thread(target=self._checkLoginLoop, daemon=True)
        thread.start()

    def _ensureAllUsersLogin(self):
        try:
            usersWithCookies = self.repo.getAllUsersWithCookies()
        except Exception as e:
            logger.error("Error initializing users: %s", e)
            return

        for username, email in usersWithCookies:
            try:
                self.get_user_db(username, email)
            except Exception as e:
                logger.error("Error initializing user %s: %s", username, e)
    
    def _checkLoginLoop(self):
        while True:
            self._ensureAllUsersLogin()
            time.sleep(60 * 5)  # Check every 5 minutes

    def startVersionCheck_thread(self):
        thread = threading.Thread(target=self._versionCheckLoop, daemon=True)
        thread.start()

    def _versionCheckLoop(self):
        # Check version from GitHub at startup and then every hour.
        url = "https://raw.githubusercontent.com/i7Gamer/SpotifyStatsTracker/main/Database/VERSION"
        while True:
            try:
                resp = requests.get(url, timeout=6)
                if resp.status_code == 200:
                    remoteVersion = resp.text.strip()
                    # store remoteVersion if it's newer than current
                    try:
                        with self._version_lock:
                            if versionTuple(remoteVersion) > versionTuple(self.currentVersion):
                                self.latestVersion = remoteVersion
                            else:
                                self.latestVersion = None
                    except:
                        pass
            except Exception:
                pass

            time.sleep(60 * 60)

    def _getPercentPlayedText(self, item, sortBy, totalPlays, totalMs):
        if sortBy == "plays":
            percent = round((item.get("plays", 0) / totalPlays * 100), 1) if totalPlays else 0
            return f"{percent}% of all plays"
        elif sortBy == "totalTimeListened":
            percent =  round((item.get("totalTimeListened", 0) / totalMs * 100), 1) if totalMs else 0
            return f"{percent}% of all time played"
        else:
            return ""

    def _embedSongTextElements(self, song) -> dict:
        if "playedAt" in song:   #< some tracks just dont have it (top tracks)
            playedAt = convertToDatetime(song["playedAt"])
            song["playedAtText"] = playedAt.strftime("%d %b %Y, %H:%M")
            song["timePlayedText"] = msToString(song["timePlayed"])

        song["contextName"] = None
        if "playedFrom" in song:
            db = g.get("db", None)
            if db:
                song["contextName"] = db.playlistName(song["playedFrom"])

        artistsText = ", ".join(a.get("name", "") for a in song["artists"])
        album = song.get("album")   #< can be None - see Repository._songRowToDict()'s LEFT JOIN fallback
        releaseDateText = dateToString(album["releaseDate"]) if album else ""
        song["releaseDateText"] = releaseDateText
        song["artistsText"] = artistsText
        song["durationText"] = formatDuration(song["duration"])
        if album:
            album["releaseDateText"] = releaseDateText
        return song

    def _embedTopSongTextElements(self, song, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        song["totalTimeListenedText"] = msToString(song.get("totalTimeListened", 0))
        song["firstListenedText"] = convertToDatetime(song.get("firstListenedAt", 0)).strftime("%b %d, %Y")
        song["sortPercentText"] = self._getPercentPlayedText(song, sortBy, totalPlays, totalMs)
        return song

    def _embedAlbumTextElements(self, album, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        album["totalTimeListenedText"] = msToString(album.get("totalTimeListened", 0))
        album["firstListenedText"] = convertToDatetime(album.get("firstListenedAt", 0)).strftime("%b %d, %Y")
        album["sortPercentText"] = self._getPercentPlayedText(album, sortBy, totalPlays, totalMs)
        album["releaseDateText"] = dateToString(album.get("releaseDate", 0))
        album["artistsText"] = ", ".join(a.get("name", "") for a in album.get("artists", []))
        return album

    def _embedAlbumsTextElements(self, albums, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedAlbumTextElements(album, sortBy, totalPlays, totalMs) for album in albums]

    def _embedArtistTextElement(self, artist, sortBy=None, totalPlays=0, totalMs=0) -> dict:
        artist["totalTimeListenedText"] = msToString(artist.get("totalTimeListened", 0))
        artist["firstListenedText"] = convertToDatetime(artist.get("firstListenedAt", 0)).strftime("%b %d, %Y")
        artist["sortPercentText"] = self._getPercentPlayedText(artist, sortBy, totalPlays, totalMs)
        return artist

    def _embedSongsTextElements(self, songs) -> list[dict]:
        return [self._embedSongTextElements(song) for song in songs]

    def _embedTopSongsTextElements(self, songs, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedTopSongTextElements(song, sortBy, totalPlays, totalMs) for song in songs]

    def _embedArtistsTextElements(self, songs, sortBy=None, totalPlays=0, totalMs=0) -> list[dict]:
        return [self._embedArtistTextElement(song, sortBy, totalPlays, totalMs) for song in songs]

    def _buildPageUrl(self, endpoint, page, **queryArgs):
        cleanArgs = {key: value for key, value in queryArgs.items() if value not in (None, "")}
        cleanArgs["page"] = page
        return url_for(endpoint, **cleanArgs)

    def _getNeighboringUrls(self, name, page, totalPages, **queryArgs):
        prevUrl = self._buildPageUrl(name, page - 1, **queryArgs) if page > 1 else None
        nextUrl = self._buildPageUrl(name, page + 1, **queryArgs) if page < totalPages else None
        return prevUrl, nextUrl

    def _buildPageNumberLinks(self, endpoint, page, totalPages, window=2, **queryArgs):
        """Page-number links for a pagination strip: always page 1 and the last
        page, plus a `window`-page radius around the current page, with an
        {"ellipsis": True} marker filling any gap between shown pages."""
        if totalPages <= 1:
            return []

        pagesToShow = {1, totalPages}
        for p in range(page - window, page + window + 1):
            if 1 <= p <= totalPages:
                pagesToShow.add(p)

        links = []
        previousPage = None
        for p in sorted(pagesToShow):
            if previousPage is not None and p - previousPage > 1:
                links.append({"ellipsis": True})
            links.append({"num": p, "url": self._buildPageUrl(endpoint, p, **queryArgs), "current": p == page})
            previousPage = p
        return links

    def _buildPaginationContext(self, endpoint, page, totalPages, totalCount, pageSize=PAGE_SIZE, **queryArgs):
        """Everything a list page's pagination strip needs: prev/next links,
        windowed page-number links, and the 'Showing X-Y of Z' counts."""
        prevUrl, nextUrl = self._getNeighboringUrls(endpoint, page, totalPages, **queryArgs)
        pageLinks = self._buildPageNumberLinks(endpoint, page, totalPages, **queryArgs)
        showingStart = (page - 1) * pageSize + 1 if totalCount else 0
        showingEnd = min(page * pageSize, totalCount)
        return {
            "page": page,
            "totalPages": totalPages,
            "prevUrl": prevUrl,
            "nextUrl": nextUrl,
            "pageLinks": pageLinks,
            "showingStart": showingStart,
            "showingEnd": showingEnd,
            "totalCount": totalCount,
        }

    def _getChangeText(self, currentValue, previousValue):
        if previousValue is None or previousValue == 0:
            if currentValue == 0:
                return None, ""
            return f"New this period", "change-positive"

        change = ((currentValue - previousValue) / previousValue) * 100
        if round(change, 1) == 0:
            return "No change from the previous period", ""

        formatted = f"{abs(round(change, 1))}% {'more' if change > 0 else 'less'} than the previous period"
        cssClass = "change-positive" if change > 0 else "change-negative"
        return formatted, cssClass

    def _getPageParam(self):
        """The current request's ?page=... as an int >= 1, tolerating junk input."""
        try:
            return max(1, int(request.args.get("page", 1) or 1))
        except (TypeError, ValueError):
            return 1

    def _getSortByParam(self, default=DEFAULT_SORT_BY):
        """The current request's ?sortBy=..., falling back to `default` for any
        value the DB layer doesn't know how to sort by (see VALID_SORT_BY) -
        without this, an unrecognized value reaches a ValueError/KeyError deep
        in Repository/Database and 500s instead of just using the default."""
        sortBy = request.args.get("sortBy", default)
        return sortBy if sortBy in VALID_SORT_BY else default

    def _getDateRange(self, interval: str = None, customStart: str = None, customEnd: str = None, default="day"):
            """Get start and end dates based on interval or custom dates.

            Returns a half-open local interval [startDate, endDate).
            """
            nowLocal = now()
            startDate = None

            futureBuffer = timedelta(days=1) 

            endDate = nowLocal + futureBuffer   #< bypass any timezone issues

            if customStart and customEnd:
                try:
                    startLocal = parseDateString(customStart)
                    endLocal = parseDateString(customEnd)
                    if startLocal is None or endLocal is None:
                        raise ValueError("Invalid custom date")

                    startDate = startLocal
                    endDate = endLocal + timedelta(days=1)
                except ValueError:
                    pass
            if interval == "":
                interval = default
            if not startDate:
                if interval == "today":
                    startDate = convertToDatetime(startOfDay(nowLocal))
                    endDate = convertToDatetime(startOfDay(nowLocal + timedelta(days=1)))

                elif interval == "day":
                    startDate = convertToDatetime(startOfDay(nowLocal - timedelta(days=1)))
                    endDate = convertToDatetime(startOfDay(nowLocal))

                elif interval == "week":
                    startDate = nowLocal - timedelta(weeks=1)

                elif interval == "month":
                    startDate = nowLocal - timedelta(days=30)

                elif interval == "year":
                    startDate = nowLocal - timedelta(days=365)

                elif interval == "5years":
                    startDate = nowLocal - timedelta(days=365*5)
                else:
                    startDate = None
                    endDate = None

            return startDate, endDate

    def _getIntervalLabel(self, interval: str = None, customStart: str = None, customEnd: str = None):
        labels = {
            "all time": "All Time",
            "today": "Today",
            "day": "Yesterday",
            "week": "Last Week",
            "month": "Last Month",
            "year": "Last Year",
            "5years": "Last 5 Years",
        }

        if interval == "custom" and customStart and customEnd:
            return f"Custom range: {customStart} to {customEnd}"

        return labels.get(interval or "day", "Yesterday")

    def _embedTimeSeriesTextElements(self, timeSeries: list) -> list:
        for bucket in timeSeries:
            bucket["totalTimeListenedText"] = msToString(bucket["totalTimeListened"])
        return timeSeries

    def _embedHeatmapTextElements(self, heatmap: list) -> list:
        for row in heatmap:
            for cell in row:
                cell["totalTimeListenedText"] = msToString(cell["totalTimeListened"])
        return heatmap

    def _getWrappedYearParam(self, availableYears: list, defaultYear: int) -> int:
        """The current request's ?year=... if it's one of the years the user
        actually has data for, else `defaultYear` - mirrors _getPageParam()'s
        tolerate-junk-input, silently-clamp behavior for ?page=."""
        try:
            year = int(request.args.get("year", defaultYear))
        except (TypeError, ValueError):
            return defaultYear
        return year if year in availableYears else defaultYear

    def _discoveriesInYear(self, items: list, yearStart, yearEnd, limit: int) -> list:
        """Items (songs or artists) whose true, all-time first listen falls
        within [yearStart, yearEnd) - not just their earliest play *within* that
        range, which a date-scoped query would report instead. `items` must
        therefore come from an unbounded (no date range) stats call. Sorted by
        play count, most-played discovery first."""
        yearStartTs, yearEndTs = yearStart.timestamp(), yearEnd.timestamp()
        discovered = [
            item for item in items
            if item.get("firstListenedAt") is not None and yearStartTs <= item["firstListenedAt"] < yearEndTs
        ]
        discovered.sort(key=lambda item: item.get("plays", 0), reverse=True)
        return discovered[:limit]

    def registerRoutes(self):
        @self.app.context_processor
        def _injectPasswordPolicy():
            # Lets register.html/reset_password.html show the actual configured
            # minimum instead of a hardcoded number that could drift from
            # PASSWORD_MIN_LENGTH.
            return {"minPasswordLength": PASSWORD_MIN_LENGTH}

        def _is_version_newer(remote: str, local: str) -> bool:
            try:
                return versionTuple(remote) > versionTuple(local)
            except Exception:
                return False

        def get_current_user_or_redirect():
            email = session.get("email")
            if not email or not self.is_user_logged_in(email):
                return None, None, None
            
            # Ensure the username matches the correct email mapping to prevent session pollution from legacy user "Tzur"
            correct_username = self.get_username_for_email(email)
            if not correct_username:
                correct_username = self.get_or_create_user(email)
            
            if session.get("username") != correct_username:
                session["username"] = correct_username

            username = correct_username
            db = self.get_user_db(username, email)
            g.db = db
            return email, username, db

        def _authorized_image_username():
            """Returns the username the current session is allowed to view images for, or None."""
            email = session.get("email")
            if not email or not self.is_user_logged_in(email):
                return None
            return self.get_username_for_email(email)

        # Images are shared across every user (Database.imgDir_tracks/imgDir_artists
        # are class-level, not per user) - the <username> segment in these routes is
        # kept only as the authorization check ("is this session allowed to ask at
        # all"), not to select which directory to read from.
        @self.app.route('/img/<username>/tracks/<filename>')
        def serveTrackImage(username, filename):
            if username != _authorized_image_username() or filename != os.path.basename(filename):
                return "", 404
            return send_from_directory(Database.imgDir_tracks, filename)

        @self.app.route('/img/<username>/artists/<filename>')
        def serveArtistImage(username, filename):
            if username != _authorized_image_username() or filename != os.path.basename(filename):
                return "", 404
            imageDir = Database.imgDir_artists
            imagePath = os.path.join(imageDir, filename)

            if not os.path.exists(imagePath):
                artistId = filename.split('.')[0]
                db = self.user_databases.get(username)
                if db:
                    db.lazyFetchArtistImage(artistId, Path(imagePath))

            return send_from_directory(imageDir, filename)

        @self.app.route("/import-history", methods=["POST"])
        def importHistory():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login"))

            if db.readProgress().get("status") == "running":
                return redirect(url_for("importPage"))

            uploads = [f for f in request.files.getlist("history_file") if f and f.filename]
            if not uploads:
                return redirect(url_for("importPage"))

            contents = [upload.read().decode("utf-8") for upload in uploads]
            thread = threading.Thread(target=db.importHistoryBatch, args=(contents,), daemon=True)
            thread.start()
            time.sleep(1)  # Give thread time to start and update progress
            return redirect(url_for("importPage"))

        @self.app.route("/import", methods=["GET"])
        def importPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login"))
            return render_template(
                "import.html",
                importProgress=db.readProgress(),
                maxUploadMb=MAX_UPLOAD_MB,
                uploadTooLarge=request.args.get("error") == "upload_too_large",
                section="import",
            )

        @self.app.errorhandler(413)
        def _uploadTooLarge(error):
            return redirect(url_for("importPage", error="upload_too_large"))

        def _safeNextUrl(nextUrl):
            """Only allow same-origin relative redirects after login - a `next`
            value like `//evil.com` or `https://evil.com` would otherwise send a
            freshly authenticated session to an attacker-controlled site."""
            if not nextUrl or not nextUrl.startswith("/") or nextUrl.startswith("//"):
                return None
            return nextUrl

        @self.app.route("/login", methods=["GET", "POST"])
        def login():
            if request.method == "GET":
                return render_template("login.html", next=_safeNextUrl(request.args.get("next")))

            email = request.form.get("email", "").strip()
            nextUrl = _safeNextUrl(request.form.get("next"))

            # The password form and the (collapsed, fallback) cookies form are
            # separate <form>s on login.html - only the submitted one's fields
            # are present, so their presence tells the two branches apart.
            if "password" in request.form:
                password = request.form.get("password", "")
                if not email or not password:
                    return render_template(
                        "login.html", email=email, next=nextUrl,
                        error="Email and password are both required.")

                username = self.repo.getUsernameForEmail(email)
                passwordHash = self.repo.getUserPasswordHash(username) if username else None
                if username and not passwordHash:
                    return render_template(
                        "login.html", email=email, next=nextUrl,
                        error="This account doesn't have a password yet - register to add one, "
                              "or log in with cookies instead.")
                if not passwordHash or not check_password_hash(passwordHash, password):
                    return render_template(
                        "login.html", email=email, next=nextUrl,
                        error="Invalid email or password.")

                # Password auth only ever unlocks the session while the cookies
                # stored from the last cookie-based (re-)login/register/reset
                # are still live - a lapsed Spotify session must be refreshed
                # via /reset-password or the cookies form, not just a password.
                if not self.is_user_logged_in(email):
                    return render_template(
                        "login.html", email=email, next=nextUrl,
                        error="Your saved Spotify session has expired. Reset your password with "
                              "fresh cookies, or log in with cookies instead.")

                session.permanent = True
                self.get_user_db(username, email)
                session["email"] = email
                session["username"] = username
                return redirect(nextUrl or url_for("dashboard"))

            cookies = request.form.get("cookies", "")

            if not email or not cookies:
                return render_template(
                    "login.html", email=email, next=nextUrl,
                    error="Email and cookies are both required.")

            # Verification happens against a throwaway session file, so nothing
            # is persisted for this email unless the cookies really are theirs.
            parsedCookies = parseCookieString(cookies)
            if not self.skipEmailVerification and not self._verifyCookiesMatchEmail(parsedCookies, email):
                return render_template(
                    "login.html", email=email, next=nextUrl,
                    error=f"Couldn't verify that these cookies belong to {email}. "
                          "Make sure you are logged into open.spotify.com with that account and copied all cookies.")

            session.permanent = True
            # get_or_create_user/get_user_db manage their own locking and can
            # start a listener (a live network call) - kept outside the session
            # lock so logging one user in doesn't block everyone else.
            username = self.get_or_create_user(email)
            self.repo.setUserCookies(username, parsedCookies)
            if username in self.user_databases:
                # Returning user re-logging in (e.g. after their session
                # expired) - restart their listener against the fresh cookies
                # instead of leaving the old, dead one running.
                self._refresh_user_session(username, email)
            else:
                self.get_user_db(username, email)
            session["email"] = email
            session["username"] = username

            return redirect(nextUrl or url_for("dashboard"))

        @self.app.route("/register", methods=["GET", "POST"])
        def register():
            if request.method == "GET":
                return render_template("register.html")

            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            confirmPassword = request.form.get("confirm_password", "")
            cookies = request.form.get("cookies", "")

            if not email or not password or not confirmPassword or not cookies:
                return render_template(
                    "register.html", email=email,
                    error="Email, password, confirmed password and cookies are all required.")

            if password != confirmPassword:
                return render_template(
                    "register.html", email=email, error="Passwords do not match.")

            policyError = _passwordPolicyError(password)
            if policyError:
                return render_template("register.html", email=email, error=policyError)

            # Verification happens against a throwaway session file, so nothing
            # is persisted for this email unless the cookies really are theirs.
            parsedCookies = parseCookieString(cookies)
            if not self.skipEmailVerification and not self._verifyCookiesMatchEmail(parsedCookies, email):
                return render_template(
                    "register.html", email=email,
                    error=f"Couldn't verify that these cookies belong to {email}. "
                          "Make sure you are logged into open.spotify.com with that account and copied all cookies.")

            existingUsername = self.repo.getUsernameForEmail(email)
            if existingUsername and self.repo.getUserPasswordHash(existingUsername):
                return render_template(
                    "register.html", email=email,
                    error="An account with this email already exists. Log in, or reset your "
                          "password if you forgot it.")

            # get_or_create_user returns the existing username for a legacy
            # (pre-password) account instead of erroring, so registering with
            # an email that's already logged in via cookies just adds a
            # password to that account rather than being rejected as a dupe.
            session.permanent = True
            username = self.get_or_create_user(email)
            self.repo.setUserCookies(username, parsedCookies)
            self.repo.setUserPassword(username, generate_password_hash(password))
            if username in self.user_databases:
                self._refresh_user_session(username, email)
            else:
                self.get_user_db(username, email)
            session["email"] = email
            session["username"] = username

            return redirect(url_for("dashboard"))

        @self.app.route("/reset-password", methods=["GET", "POST"])
        def resetPassword():
            if request.method == "GET":
                return render_template("reset_password.html")

            email = request.form.get("email", "").strip()
            password = request.form.get("password", "")
            confirmPassword = request.form.get("confirm_password", "")
            cookies = request.form.get("cookies", "")

            if not email or not password or not confirmPassword or not cookies:
                return render_template(
                    "reset_password.html", email=email,
                    error="Email, new password, confirmed password and cookies are all required.")

            if password != confirmPassword:
                return render_template(
                    "reset_password.html", email=email, error="Passwords do not match.")

            policyError = _passwordPolicyError(password)
            if policyError:
                return render_template("reset_password.html", email=email, error=policyError)

            username = self.repo.getUsernameForEmail(email)
            if not username:
                return render_template(
                    "reset_password.html", email=email, error="No account found for this email.")

            # There's no old password to check against a forgotten one - proof
            # of identity is the same as everywhere else in this app: valid,
            # matching Spotify cookies for the account's email.
            parsedCookies = parseCookieString(cookies)
            if not self.skipEmailVerification and not self._verifyCookiesMatchEmail(parsedCookies, email):
                return render_template(
                    "reset_password.html", email=email,
                    error=f"Couldn't verify that these cookies belong to {email}. "
                          "Make sure you are logged into open.spotify.com with that account and copied all cookies.")

            self.repo.setUserCookies(username, parsedCookies)
            self.repo.setUserPassword(username, generate_password_hash(password))
            if username in self.user_databases:
                self._refresh_user_session(username, email)
            else:
                self.get_user_db(username, email)

            session.permanent = True
            session["email"] = email
            session["username"] = username

            return redirect(url_for("dashboard"))

        @self.app.route("/logout", methods=["GET"])
        def logout():
            session.clear()
            return redirect(url_for("login"))

        @self.app.route("/import-progress", methods=["GET"])
        def importProgress():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return jsonify({"error": "unauthorized"}), 401
            return jsonify(db.readProgress())

        @self.app.route("/version_status", methods=["GET"])
        def version_status():
            # Return the current and latest versions (latest is null if not newer)
            with self._version_lock:
                latest = self.latestVersion
            if latest and _is_version_newer(latest, self.currentVersion):
                return jsonify({"current": self.currentVersion, "latest": latest})
            else:
                return jsonify({"current": self.currentVersion, "latest": None})

        @self.app.route("/", methods=["GET"])
        def dashboard():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            page = self._getPageParam()
            searchQuery = request.args.get("q", "")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")
            interval = request.args.get("interval", "day")
            if interval == "custom" and not (customStart and customEnd):
                interval = "all time"

            if searchQuery:
                # Matching and pagination both happen in SQL (Repository.searchPlays)
                # instead of fetching every play ever recorded and filtering in Python.
                totalCount = db.searchEntriesCount(searchQuery)
                totalPages = max(1, (totalCount + PAGE_SIZE - 1) // PAGE_SIZE)
                page = max(1, min(page, totalPages))
                startIndex = (page - 1) * PAGE_SIZE
                tracks = db.searchEntries(searchQuery, count=PAGE_SIZE, startIndex=startIndex)
            else:
                # Only materialize the page being shown - joining full track
                # metadata onto every entry ever recorded on every request gets
                # slow once the history grows large.
                totalCount = db.getEntriesCount()
                totalPages = max(1, (totalCount + PAGE_SIZE - 1) // PAGE_SIZE)
                page = max(1, min(page, totalPages))
                startIndex = (page - 1) * PAGE_SIZE
                tracks = db.getEntriesFromNew(count=PAGE_SIZE, startIndex=startIndex)
            tracks = self._embedSongsTextElements(tracks)

            intervalLabel = self._getIntervalLabel(interval, customStart, customEnd)
            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="day")
            stats = db.getOverallStats(startDate, endDate) 

            totalDurationText = msToString(stats["totalDurationMs"])

            currentTopSong = self._embedTopSongTextElements(stats["currentTopSongs"][0], sortBy="plays", totalPlays=stats["totalSongsPlayed"], totalMs=stats["totalDurationMs"]) if stats["currentTopSongs"] else None
            currentTopArtist = self._embedArtistTextElement(stats["currentTopArtists"][0], sortBy="totalTimeListened", totalPlays=stats["totalSongsPlayed"], totalMs=stats["totalDurationMs"]) if stats["currentTopArtists"] else None

            totalSongsChangeText, totalSongsChangeClass = self._getChangeText(stats["totalSongsPlayed"], stats["previousSongsPlayed"])
            totalListenChangeText, totalListenChangeClass = self._getChangeText(stats["totalDurationMs"], stats["previousDurationMs"])

            pagination = self._buildPaginationContext(
                "dashboard",
                page,
                totalPages,
                totalCount,
                q=searchQuery,
                interval=interval,
                startDate=customStart,
                endDate=customEnd,
            )

            return render_template(
                "tracks.html",
                tracks=tracks,
                totalSongsPlayed=stats["totalSongsPlayed"],
                totalListenTime=totalDurationText,
                totalSongsChangeText=totalSongsChangeText,
                totalSongsChangeClass=totalSongsChangeClass,
                totalListenChangeText=totalListenChangeText,
                totalListenChangeClass=totalListenChangeClass,
                currentTopSong=currentTopSong,
                currentTopArtist=currentTopArtist,
                intervalLabel=intervalLabel,
                username=username,
                startIndex=startIndex,
                section="dashboard",
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                **pagination,
            )

        @self.app.route("/top-songs", methods=["GET"])
        def topSongsPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            page = self._getPageParam()
            searchQuery = request.args.get("q", "")
            sortBy = self._getSortByParam()
            interval = request.args.get("interval", "")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")

            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="all time")
            # totalPlays/totalMs are a whole-range aggregate regardless of search -
            # a cheap dedicated query instead of summing every song's metadata.
            totalPlays, totalMs = db.getPlayTotals(startDate, endDate)

            # Only materialize the page being shown - SQL-level LIMIT/OFFSET and
            # WHERE-clause matching (see Repository.getSongsPage) instead of
            # sorting+hydrating+filtering every song ever played in Python.
            totalCount = db.getSongsCount(startDate, endDate, searchQuery=searchQuery)
            totalPages = max(1, (totalCount + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(1, min(page, totalPages))
            startIndex = (page - 1) * PAGE_SIZE
            tracks = db.getTopSongs(startDate=startDate, endDate=endDate, by=sortBy,
                                     limit=PAGE_SIZE, offset=startIndex, searchQuery=searchQuery)

            pagination = self._buildPaginationContext(
                "topSongsPage",
                page,
                totalPages,
                totalCount,
                q=searchQuery,
                sortBy=sortBy,
                interval=interval,
                startDate=customStart,
                endDate=customEnd,
            )

            tracks = self._embedSongsTextElements(tracks)
            tracks = self._embedTopSongsTextElements(tracks, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)

            return render_template(
                "top_songs.html",
                tracks=tracks,
                username=username,
                totalPlays=totalPlays,
                totalTime=msToString(totalMs),
                startIndex=startIndex,
                section="top_songs",
                sortBy=sortBy,
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                **pagination,
            )

        @self.app.route("/top-albums", methods=["GET"])
        def topAlbumsPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            page = self._getPageParam()
            searchQuery = request.args.get("q", "")
            sortBy = self._getSortByParam()
            interval = request.args.get("interval", "")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")

            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="all time")
            totalPlays, totalMs = db.getPlayTotals(startDate, endDate)

            # Only materialize the page being shown - SQL-level LIMIT/OFFSET and
            # WHERE-clause matching (see Repository.getAlbumsPage) instead of
            # sorting+hydrating+filtering every album ever played in Python.
            totalCount = db.getAlbumsCount(startDate, endDate, searchQuery=searchQuery)
            totalPages = max(1, (totalCount + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(1, min(page, totalPages))
            startIndex = (page - 1) * PAGE_SIZE
            albums = db.getTopAlbums(startDate=startDate, endDate=endDate, by=sortBy,
                                      limit=PAGE_SIZE, offset=startIndex, searchQuery=searchQuery)

            pagination = self._buildPaginationContext(
                "topAlbumsPage",
                page,
                totalPages,
                totalCount,
                q=searchQuery,
                sortBy=sortBy,
                interval=interval,
                startDate=customStart,
                endDate=customEnd,
            )

            albums = self._embedAlbumsTextElements(albums, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)

            return render_template(
                "top_albums.html",
                tracks=albums,
                username=username,
                totalPlays=totalPlays,
                totalTime=msToString(totalMs),
                startIndex=startIndex,
                section="top_albums",
                sortBy=sortBy,
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                **pagination,
            )

        @self.app.route("/top-artists", methods=["GET"])
        def topArtistsPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            page = self._getPageParam()
            searchQuery = request.args.get("q", "")
            sortBy = self._getSortByParam()
            interval = request.args.get("interval", "")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")

            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="all time")
            # totalPlays/totalUnique/totalMs are the whole (date-range-scoped) top
            # list's totals regardless of search - mirrors getPlayTotals()'s role
            # for the songs/albums pages, computed via a dedicated SQL aggregate
            # instead of fetching every artist and summing in Python.
            totalPlays, totalUnique, totalMs = db.getArtistTotals(startDate, endDate)

            # Only materialize the page being shown - SQL-level LIMIT/OFFSET
            # instead of sorting+hydrating every artist ever played.
            totalCount = db.getArtistsCount(startDate, endDate, searchQuery=searchQuery)
            totalPages = max(1, (totalCount + PAGE_SIZE - 1) // PAGE_SIZE)
            page = max(1, min(page, totalPages))
            startIndex = (page - 1) * PAGE_SIZE
            artists = db.getTopArtists(startDate=startDate, endDate=endDate, by=sortBy,
                                        limit=PAGE_SIZE, offset=startIndex, searchQuery=searchQuery)

            artists = self._embedArtistsTextElements(artists, sortBy=sortBy, totalPlays=totalPlays, totalMs=totalMs)
            pagination = self._buildPaginationContext(
                "topArtistsPage",
                page,
                totalPages,
                totalCount,
                q=searchQuery,
                sortBy=sortBy,
                interval=interval,
                startDate=customStart,
                endDate=customEnd,
            )

            return render_template(
                "top_artists.html",
                tracks=artists,
                username=username,
                totalPlays=totalPlays,
                totalUnique=totalUnique,
                totalTime=msToString(totalMs),
                startIndex=startIndex,
                section="top_artists",
                sortBy=sortBy,
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                **pagination,
            )

        @self.app.route("/charts", methods=["GET"])
        def chartsPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            interval = request.args.get("interval", "month")
            customStart = request.args.get("startDate", "")
            customEnd = request.args.get("endDate", "")
            if interval == "custom" and not (customStart and customEnd):
                interval = "month"
            groupBy = request.args.get("groupBy", "day")
            if groupBy not in ("day", "week", "month"):
                groupBy = "day"

            startDate, endDate = self._getDateRange(interval, customStart, customEnd, default="month")
            intervalLabel = self._getIntervalLabel(interval, customStart, customEnd)

            isSingleDayView = interval in ("day", "today")
            lastDayDate = startDate.strftime("%Y-%m-%d") if isSingleDayView and startDate else None

            timeSeriesGroupBy = "hour" if isSingleDayView else groupBy

            timeSeries = self._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(startDate=startDate, endDate=endDate, groupBy=timeSeriesGroupBy)
            )
            heatmap = self._embedHeatmapTextElements(db.getHourOfDayHeatmap(startDate=startDate, endDate=endDate))
            artistTrend = None if isSingleDayView else db.getArtistTrend(startDate=startDate, endDate=endDate, topN=CHART_ARTIST_TREND_TOP_N, groupBy=groupBy)

            return render_template(
                "charts.html",
                username=username,
                section="charts",
                interval=interval,
                customStart=customStart,
                customEnd=customEnd,
                groupBy=groupBy,
                intervalLabel=intervalLabel,
                lastDayDate=lastDayDate,
                timeSeries=timeSeries,
                heatmap=heatmap,
                artistTrend=artistTrend,
            )

        @self.app.route("/wrapped", methods=["GET"])
        def wrappedPage():
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            nowLocal = now()
            currentYear = nowLocal.year

            oldestEntries = db.getEntriesFromOld(count=1, fullPagination=False)
            earliestYear = convertToDatetime(oldestEntries[0]["playedAt"]).year if oldestEntries else currentYear
            availableYears = list(range(currentYear, earliestYear - 1, -1))   #< most recent first, for the year badges

            year = self._getWrappedYearParam(availableYears, currentYear)
            yearStart = nowLocal.replace(year=year, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
            yearEnd = nowLocal.replace(year=year + 1, month=1, day=1, hour=0, minute=0, second=0, microsecond=0)

            groupBy = request.args.get("groupBy", "week")
            if groupBy not in ("day", "week", "month"):
                groupBy = "week"

            limit = request.args.get("limit", type=int)
            if limit not in WRAPPED_LIMIT_OPTIONS:
                limit = WRAPPED_LIST_SIZE

            topSongs = db.getTopSongs(startDate=yearStart, endDate=yearEnd, by="plays", limit=limit)
            topArtists = db.getTopArtists(startDate=yearStart, endDate=yearEnd, by="plays", limit=limit)
            topAlbums = db.getTopAlbums(startDate=yearStart, endDate=yearEnd, by="plays", limit=limit)
            totalPlays, totalMs = db.getPlayTotals(yearStart, yearEnd)

            # Discoveries need each item's true, all-time first listen, so these
            # three calls are deliberately unbounded (no date range) rather than
            # scoped to the year - see _discoveriesInYear()'s docstring.
            discoveredSongs = self._discoveriesInYear(
                db.getSongsStats(sortBy="plays"), yearStart, yearEnd, limit
            )
            discoveredArtists = self._discoveriesInYear(
                db.getArtistsStats(), yearStart, yearEnd, limit
            )
            discoveredAlbums = self._discoveriesInYear(
                db.getAlbumsStats(sortBy="plays"), yearStart, yearEnd, limit
            )

            timeSeries = self._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(startDate=yearStart, endDate=yearEnd, groupBy=groupBy)
            )

            topSongs = self._embedSongsTextElements(topSongs)
            topSongs = self._embedTopSongsTextElements(topSongs, sortBy="plays", totalPlays=totalPlays, totalMs=totalMs)
            topArtists = self._embedArtistsTextElements(topArtists, sortBy="plays", totalPlays=totalPlays, totalMs=totalMs)
            topAlbums = self._embedAlbumsTextElements(topAlbums, sortBy="plays", totalPlays=totalPlays, totalMs=totalMs)
            discoveredSongs = self._embedTopSongsTextElements(self._embedSongsTextElements(discoveredSongs))
            discoveredArtists = self._embedArtistsTextElements(discoveredArtists)
            discoveredAlbums = self._embedAlbumsTextElements(discoveredAlbums)

            return render_template(
                "wrapped.html",
                username=username,
                section="wrapped",
                year=year,
                availableYears=availableYears,
                groupBy=groupBy,
                limit=limit,
                limitOptions=WRAPPED_LIMIT_OPTIONS,
                totalPlays=totalPlays,
                totalTime=msToString(totalMs),
                topSongs=topSongs,
                topArtists=topArtists,
                topAlbums=topAlbums,
                discoveredSongs=discoveredSongs,
                discoveredArtists=discoveredArtists,
                discoveredAlbums=discoveredAlbums,
                timeSeries=timeSeries,
            )

        @self.app.route("/song/<track_id>", methods=["GET"])
        def songDetailPage(track_id):
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            song = db.getSong(track_id)
            if song is None:
                return redirect(url_for("topSongsPage"))

            groupBy = request.args.get("groupBy", "week")
            if groupBy not in ("day", "week", "month"):
                groupBy = "week"

            song = self._embedSongTextElements(song)
            song = self._embedTopSongTextElements(song)

            timeSeries = self._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(trackId=track_id, groupBy=groupBy)
            )
            heatmap = self._embedHeatmapTextElements(db.getHourOfDayHeatmap(trackId=track_id))

            return render_template(
                "song_detail.html",
                song=song,
                username=username,
                groupBy=groupBy,
                timeSeries=timeSeries,
                heatmap=heatmap,
                section="top_songs",
            )

        @self.app.route("/artist/<artist_id>", methods=["GET"])
        def artistDetailPage(artist_id):
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            artist = db.getArtist(artist_id)
            if artist is None:
                return redirect(url_for("topArtistsPage"))

            groupBy = request.args.get("groupBy", "week")
            if groupBy not in ("day", "week", "month"):
                groupBy = "week"

            songs = db.getSongsStats(sortBy="plays", artistId=artist_id)
            firstSong = min(songs, key=lambda s: s.get("firstListenedAt") or float("inf")) if songs else None
            firstSongName = firstSong.get("name") if firstSong else None

            songs = self._embedSongsTextElements(songs)
            songs = self._embedTopSongsTextElements(
                songs, sortBy="plays", totalPlays=artist.get("plays", 0), totalMs=artist.get("totalTimeListened", 0)
            )
            artist = self._embedArtistTextElement(artist)

            timeSeries = self._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(artistId=artist_id, groupBy=groupBy)
            )

            return render_template(
                "artist_detail.html",
                artist=artist,
                songs=songs,
                firstSongName=firstSongName,
                username=username,
                groupBy=groupBy,
                timeSeries=timeSeries,
                section="top_artists",
            )

        @self.app.route("/album/<album_id>", methods=["GET"])
        def albumDetailPage(album_id):
            email, username, db = get_current_user_or_redirect()
            if not email:
                return redirect(url_for("login", next=request.path))

            album = db.getAlbum(album_id)
            if album is None:
                return redirect(url_for("topAlbumsPage"))

            groupBy = request.args.get("groupBy", "week")
            if groupBy not in ("day", "week", "month"):
                groupBy = "week"

            songs = db.getSongsStats(sortBy="plays", albumId=album_id)
            firstSong = min(songs, key=lambda s: s.get("firstListenedAt") or float("inf")) if songs else None
            firstSongName = firstSong.get("name") if firstSong else None

            songs = self._embedSongsTextElements(songs)
            songs = self._embedTopSongsTextElements(
                songs, sortBy="plays", totalPlays=album.get("plays", 0), totalMs=album.get("totalTimeListened", 0)
            )
            album = self._embedAlbumTextElements(album)

            timeSeries = self._embedTimeSeriesTextElements(
                db.getListeningTimeSeries(albumId=album_id, groupBy=groupBy)
            )

            return render_template(
                "album_detail.html",
                album=album,
                songs=songs,
                firstSongName=firstSongName,
                groupBy=groupBy,
                username=username,
                timeSeries=timeSeries,
                section="top_albums",
            )

    def shutdown(self):
        with self._db_lock:
            for db in self.user_databases.values():
                try:
                    db.stop()
                except Exception as e:
                    logger.error("Error stopping database for %s: %s", db.user, e)

    def run(self):
        try:
            self.app.run(host="0.0.0.0", debug=True, port=5444, use_reloader=False)#, threaded=False)
        finally:
            self.shutdown()

if __name__ == "__main__":
    ## $env:IMPORT_KEYWORD="Weekly"
    ## $env:TZ="America/Los_Angeles"

    dashboardApp = SpotifyDashboardApp()
    dashboardApp.run()