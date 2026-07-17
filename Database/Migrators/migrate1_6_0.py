import json
import shutil
from pathlib import Path

try:
    from Database.Migrators.base import BaseMigrator, resolveRuntimeDir
    from Database.repository import Repository, IMAGE_KIND_TRACK, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK
except ModuleNotFoundError:
    from Migrators.base import BaseMigrator, resolveRuntimeDir
    from repository import Repository, IMAGE_KIND_TRACK, IMAGE_KIND_ARTIST, IMAGE_STATUS_OK


class Migrator(BaseMigrator):
    """Migrates every user's JSON files (entries.json/tracks.json/playlists.json)
    and per-user image folders into the shared SQLite database, and folds
    secrets/cookies.json + secrets/users_map.json into the users table. Catalog
    data (tracks/artists/albums/images) that used to be duplicated once per user
    collapses into shared rows/files as a side effect - upsertTrack()/
    tryClaimImageDownload() are idempotent, so the same track or image
    encountered under multiple users' folders just gets written once.

    As its last step, renames the runtime-data directory from Users/ to Data/ -
    "Users" stopped being an accurate name once its main contents became a
    shared database and shared media, rather than per-user files. The rename
    happens last (after this migrator has already read everything it needs from
    Users/) so migrate1_0_0 through migrate1_5_0, which all hardcode "Users"
    internally, run completely unaffected - they always run earlier in the
    chain, before this directory ever gets renamed.

    Safe to re-run: every write is an upsert/INSERT-OR-IGNORE, and the app
    version marker is only advanced (and the rename only performed) after every
    user migrates successfully - a failure partway through leaves both
    unchanged, so the next startup retries the whole migration rather than
    silently leaving that user's history behind. A retry after a successful
    migration whose version bump was somehow lost operates directly against the
    already-renamed Data/ directory (see resolveRuntimeDir()).
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.usersDir = resolveRuntimeDir(self.baseDir)
        self.secretsDir = self.baseDir / ".." / ".." / "secrets"
        self.mediaDir = self.usersDir / "Media"

    @staticmethod
    def _loadJson(path: Path, default):
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            print(f"Corrupted JSON in {path}, skipping")
            return default

    def _loadEmailByUsername(self) -> dict:
        usersMap = self._loadJson(self.secretsDir / "users_map.json", {})
        return {username: email for email, username in usersMap.items()}

    def _loadCookiesByEmail(self) -> dict:
        cookiesData = self._loadJson(self.secretsDir / "cookies.json", [])
        return {entry["identifier"]: entry.get("cookies", {}) for entry in cookiesData if entry.get("identifier")}

    def _migrateUserImages(self, userDir: Path, repo: Repository) -> None:
        for kind, subdir in ((IMAGE_KIND_TRACK, "tracks"), (IMAGE_KIND_ARTIST, "artists")):
            srcDir = userDir / "img" / subdir
            if not srcDir.exists():
                continue
            destDir = self.mediaDir / subdir
            destDir.mkdir(parents=True, exist_ok=True)
            for imgFile in srcDir.glob("*.jpeg"):
                destFile = destDir / imgFile.name
                if not destFile.exists():
                    shutil.move(str(imgFile), str(destFile))
                else:
                    imgFile.unlink()   #< already have this shared image from another user - drop the duplicate
                repo.markImageStatus(imgFile.stem, kind, IMAGE_STATUS_OK)

    def _migrateUser(self, username: str, email: str | None, repo: Repository) -> None:
        userDir = self.usersDir / username
        print(f"Migrating user '{username}'...")

        repo.upsertUser(username, email)

        tracks = self._loadJson(userDir / "tracks.json", {})
        for track in tracks.values():
            repo.upsertTrack(track)
        repo.commit()

        playlists = self._loadJson(userDir / "playlists.json", {"album": {}, "playlist": {}})
        for playlistType in ("album", "playlist"):
            for playlistId, name in playlists.get(playlistType, {}).items():
                repo.upsertPlaylistName(playlistId, playlistType, name)

        entries = self._loadJson(userDir / "entries.json", [])
        insertedCount = 0
        for entry in entries:
            if repo.insertPlay(username, entry["id"], entry["playedAt"], entry["timePlayed"], entry.get("playedFrom")):
                insertedCount += 1
        repo.commit()

        self._migrateUserImages(userDir, repo)

        print(f"  -> {len(tracks)} tracks, {insertedCount}/{len(entries)} plays migrated")

    def migrate(self):
        self.checkPreconditions()

        # migrateIfNeeded() (Migrators/migrate.py) already guarantees a runtime
        # data dir with a VERSION file exists before any numbered Migrator is
        # constructed (BaseMigrator.__init__ itself reads it) - a fresh install
        # with no user subdirectories yet just means the loop below runs zero
        # times, not that self.usersDir is missing.

        # Explicit path (not the bare Repository() default) so this always targets
        # the same directory this migrator just resolved baseDir against,
        # regardless of what Database.db.DEFAULT_DB_PATH happens to be.
        repo = Repository(self.usersDir / "spotify_stats.db")
        try:
            emailByUsername = self._loadEmailByUsername()
            cookiesByEmail = self._loadCookiesByEmail()

            usernames = sorted(p.name for p in self.usersDir.iterdir() if p.is_dir() and p.name != "Media")
            failedUsers = []
            for username in usernames:
                try:
                    email = emailByUsername.get(username)
                    self._migrateUser(username, email, repo)
                    if email and email in cookiesByEmail:
                        repo.setUserCookies(username, cookiesByEmail[email])
                except Exception as e:
                    print(f"Failed to migrate user '{username}': {e}")
                    failedUsers.append(username)
        finally:
            # Must close before the Users/ -> Data/ rename below - Windows can't
            # rename a directory containing a file with an open handle.
            repo.connectionManager.close()

        if failedUsers:
            raise RuntimeError(
                f"Migration failed for {len(failedUsers)} user(s): {', '.join(failedUsers)}. "
                "The version marker was not advanced - fix the underlying issue and restart "
                "to retry (already-migrated users/tracks/plays are safely skipped on retry)."
            )

        dataDir = self.baseDir / ".." / "Data"
        if self.usersDir.resolve() != dataDir.resolve():
            print(f"Renaming {self.usersDir} -> {dataDir}")
            # shutil.move (not Path.rename/os.rename): Users/ and Data/ can be
            # separate Docker bind mounts, and a plain rename() fails with EXDEV
            # across a mount boundary - shutil.move falls back to copy+delete
            # when that happens, same end result either way.
            shutil.move(str(self.usersDir), str(dataDir))
        # updateAppVersion() re-resolves the runtime dir itself (see base.py),
        # so it picks up the Data/ rename above without needing this reassigned
        # here first.
        self.updateAppVersion("1.7.0")
        print(f"Migration complete: {len(usernames)} user(s) migrated to SQLite.")


if __name__ == "__main__":
    Migrator("1.6.0", "1.7.0").migrate()
