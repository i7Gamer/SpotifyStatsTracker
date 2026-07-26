from __future__ import annotations

import logging

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers

logger = logging.getLogger(__name__)


class UserQueries:
    """UserQueries: users data-access methods, mixed into Repository."""

    # ---- Per-user: users / cookies ----------------------------------------------

    def upsertUser(self, username: str, email: str, createdAt: float | None = None) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "INSERT INTO users (username, email, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(username) DO NOTHING",
                (username, email, createdAt if createdAt is not None else time.time()),
            )

    def getUsernameForEmail(self, email: str) -> str | None:
        conn = self._conn()
        row = conn.execute("SELECT username FROM users WHERE email=?", (email,)).fetchone()
        return row["username"] if row else None

    def usernameExists(self, username: str) -> bool:
        conn = self._conn()
        row = conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone()
        return row is not None

    def getEmailForUsername(self, username: str) -> str | None:
        """The stored email for an existing username, or None - either because
        the username doesn't exist, or it exists but has no email on record yet
        (e.g. a migrated account whose users_map.json didn't know it). Callers
        that need to tell those two cases apart should check usernameExists()
        first."""
        conn = self._conn()
        row = conn.execute("SELECT email FROM users WHERE username=?", (username,)).fetchone()
        return row["email"] if row else None

    def getUsernameForEmailCaseInsensitive(self, email: str) -> str | None:
        """getUsernameForEmail with case-insensitive matching - emails are
        stored as typed at login, so an ADMIN_EMAIL differing only in case
        must still resolve. ASCII-only folding (SQLite NOCASE), which is all
        email addresses need."""
        conn = self._conn()
        row = conn.execute(
            "SELECT username FROM users WHERE email=? COLLATE NOCASE", (email,)
        ).fetchone()
        return row["username"] if row else None

    def setUserEmail(self, username: str, email: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute("UPDATE users SET email=? WHERE username=?", (email, username))

    def setUserCookies(self, username: str, cookies: dict) -> None:
        # Encrypted at rest: these are a live Spotify session - see
        # Database/secret_store.py.
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET cookies_json=? WHERE username=?",
                (encryptSecret(json.dumps(cookies)), username),
            )

    def getUserCookies(self, username: str) -> dict | None:
        conn = self._conn()
        row = conn.execute("SELECT cookies_json FROM users WHERE username=?", (username,)).fetchone()
        if row is None or row["cookies_json"] is None:
            return None
        # Legacy plaintext rows pass through decryptSecret unchanged; an
        # undecryptable row (rotated/lost key) reads as "no cookies stored",
        # which routes the user through re-login instead of crashing.
        decrypted = decryptSecret(row["cookies_json"])
        if decrypted is None:
            return None
        return json.loads(decrypted)

    def setUserPassword(self, username: str, passwordHash: str) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET password_hash=? WHERE username=?",
                (passwordHash, username),
            )

    def getUserPasswordHash(self, username: str) -> str | None:
        conn = self._conn()
        row = conn.execute("SELECT password_hash FROM users WHERE username=?", (username,)).fetchone()
        return row["password_hash"] if row else None

    def getUserSpotifyCredentials(self, username: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT spotify_client_id, spotify_client_secret, spotify_refresh_token, "
            "spotify_needs_reauth FROM users WHERE username=?",
            (username,)
        ).fetchone()
        if not row:
            return None
        return {
            "client_id": row["spotify_client_id"],
            "client_secret": decryptSecret(row["spotify_client_secret"]),
            "refresh_token": decryptSecret(row["spotify_refresh_token"]),
            "needs_reauth": bool(row["spotify_needs_reauth"]),
        }

    def getSpotifyNeedsReauth(self, username: str) -> bool:
        """Cheap standalone read of the reauth flag (no secret decryption) -
        for the topbar badge context processor, which runs on every page
        render and has no other reason to touch the encrypted credential
        columns."""
        conn = self._conn()
        row = conn.execute(
            "SELECT spotify_needs_reauth FROM users WHERE username=?", (username,)
        ).fetchone()
        return bool(row["spotify_needs_reauth"]) if row else False

    def setSpotifyNeedsReauth(self, username: str, needsReauth: bool) -> None:
        """Flips the "this account's Spotify authorization is missing a
        required scope" flag - set when the Web API backfill gets a 403
        Insufficient client scope response, cleared the next time it gets a
        definitive success. Guarded on the current value so a routine poll
        that already matches doesn't write every time (see
        Listener.on_scope_status_change, called after every poll)."""
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET spotify_needs_reauth = ? WHERE username = ? AND spotify_needs_reauth != ?",
                (int(needsReauth), username, int(needsReauth)),
            )

    def updateUserSpotifyCredentials(self, username: str, clientId: str | None,
                                     clientSecret: str | None, refreshToken: str | None) -> None:
        # The client id is public (it appears in the OAuth authorize URL);
        # the secret and refresh token are encrypted at rest - see
        # Database/secret_store.py.
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET spotify_client_id = ?, spotify_client_secret = ?, spotify_refresh_token = ? WHERE username = ?",
                (clientId,
                 encryptSecret(clientSecret) if clientSecret else clientSecret,
                 encryptSecret(refreshToken) if refreshToken else refreshToken,
                 username)
            )

    def encryptStoredSecretsIfPlaintext(self) -> int:
        """Encrypt any users-table secret still stored as plaintext (rows
        written before encryption existed) - the 1.16.0 -> 1.17.0 migration.
        Already-encrypted and empty values are left untouched, so re-running
        is safe. Returns the number of users updated. Does NOT commit - the
        caller (migrator) owns the transaction."""
        conn = self._conn()
        secretColumns = ("cookies_json", "spotify_client_secret", "spotify_refresh_token")
        updated = 0
        for row in conn.execute(f"SELECT username, {', '.join(secretColumns)} FROM users").fetchall():
            changes = {column: encryptSecret(row[column])
                       for column in secretColumns
                       if row[column] and not isEncrypted(row[column])}
            if changes:
                setClause = ", ".join(f"{column}=?" for column in changes)
                conn.execute(f"UPDATE users SET {setClause} WHERE username=?",
                             (*changes.values(), row["username"]))
                updated += 1
        return updated

    def getUserLastfmApiKey(self, username: str) -> str | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT lastfm_api_key FROM users WHERE username=?", (username,)
        ).fetchone()
        if not row:
            return None
        # Encrypted at rest like the Spotify client secret - see
        # Database/secret_store.py. Legacy plaintext passes through.
        return decryptSecret(row["lastfm_api_key"])

    def updateUserLastfmApiKey(self, username: str, apiKey: str | None) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                "UPDATE users SET lastfm_api_key = ? WHERE username = ?",
                (encryptSecret(apiKey) if apiKey else None, username),
            )

    # ---- Per-user: admin role -------------------------------------------------
    # Single-admin model (see docs/proposal-admin-and-share-links.md): the
    # earliest-created user is promoted when no admin exists, and app.py's
    # ADMIN_EMAIL bootstrap is the explicit/recovery path. There's
    # deliberately no in-app grant/revoke UI.

    def isAdmin(self, username: str | None) -> bool:
        if not username:
            return False
        conn = self._conn()
        row = conn.execute("SELECT is_admin FROM users WHERE username=?", (username,)).fetchone()
        return bool(row["is_admin"]) if row else False

    def setUserAdmin(self, username: str, isAdmin: bool) -> None:
        conn = self._conn()
        with conn:
            conn.execute("UPDATE users SET is_admin=? WHERE username=?", (1 if isAdmin else 0, username))

    def demoteAdmin(self, username: str) -> bool:
        """Atomically clear a user's admin flag unless they are the only
        remaining admin. The count guard lives inside the UPDATE (evaluated
        under SQLite's write lock), so two admins demoting each other at the
        same instant can't both pass a stale count and strand the instance with
        zero admins. Returns True iff a row was actually demoted (False = the
        target wasn't an admin, or was the last one). Unlike setUserAdmin, this
        never promotes and never leaves zero admins, so it - not setUserAdmin -
        is what the admin console's demote path calls."""
        conn = self._conn()
        with conn:
            cur = conn.execute(
                """
                UPDATE users SET is_admin = 0
                WHERE username = ? AND is_admin = 1
                  AND (SELECT COUNT(*) FROM users WHERE is_admin = 1) > 1
                """,
                (username,),
            )
        return cur.rowcount > 0

    def getAdminUsernames(self) -> list[str]:
        conn = self._conn()
        rows = conn.execute("SELECT username FROM users WHERE is_admin=1 ORDER BY username").fetchall()
        return [r["username"] for r in rows]

    def promoteEarliestUserToAdminIfNoneExists(self) -> str | None:
        """Promote the earliest-created user (whoever set the instance up) to
        admin - only when no admin exists at all, so re-running (every app
        startup, plus migration 1.17.0) never creates a second admin or
        overrides a deliberate reassignment. Returns the promoted username,
        or None if nothing changed."""
        conn = self._conn()
        with conn:
            if conn.execute("SELECT 1 FROM users WHERE is_admin=1 LIMIT 1").fetchone():
                return None
            row = conn.execute(
                "SELECT username FROM users ORDER BY created_at ASC, username ASC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE users SET is_admin=1 WHERE username=?", (row["username"],))
            return row["username"]

    def getUserSettings(self, username: str) -> dict:
        conn = self._conn()
        row = conn.execute(
            "SELECT default_dashboard_window, timezone, hide_tags_panel, hide_now_playing FROM users WHERE username=?",
            (username,),
        ).fetchone()
        if row:
            return {
                "default_dashboard_window": row["default_dashboard_window"] or "day",
                "timezone": row["timezone"],
                "hide_tags_panel": bool(row["hide_tags_panel"]),
                "hide_now_playing": bool(row["hide_now_playing"]),
            }
        return {"default_dashboard_window": "day", "timezone": None,
                "hide_tags_panel": False, "hide_now_playing": False}

    def updateUserSettings(self, username: str, default_dashboard_window: str, timezone: str | None,
                           hide_tags_panel: bool = False, hide_now_playing: bool = False) -> None:
        conn = self._conn()
        previousTimezone = self.getUserSettings(username)["timezone"]
        with conn:
            conn.execute(
                "UPDATE users SET default_dashboard_window=?, timezone=?, hide_tags_panel=?, "
                "hide_now_playing=? WHERE username=?",
                (default_dashboard_window, timezone, int(hide_tags_panel), int(hide_now_playing), username),
            )
        # A timezone change moves every bucket boundary, weekday name and streak
        # day - so everything timezone-derived in a cached Wrapped year (the
        # day/week/month series, peak_day, longest_streak, and the year's own
        # start/end) is wrong, while the two signals staleness is judged on
        # (max_played_at and the play count) are untouched. The page recalculates
        # only on a cache MISS, so without this the old zone's figures were
        # served indefinitely, share links included.
        #
        # Here rather than in the profile route because this is the single
        # writer of users.timezone: any future caller inherits it.
        if timezone != previousTimezone:
            dropped = self.deleteAllUserWrapped(username)
            if dropped:
                logger.info("Timezone changed for %s - dropped %d cached Wrapped year(s) to rebuild",
                            username, dropped)

    def getHideTagsPanel(self, username: str) -> bool:
        """Cheap standalone read of the per-user "hide the tag panel"
        preference (set on /profile) - for the context processor that gates
        _tag_widget.html on song/artist/album detail pages, which runs on
        every render and has no other reason to touch the rest of
        getUserSettings. Mirrors getSpotifyNeedsReauth's rationale."""
        conn = self._conn()
        row = conn.execute(
            "SELECT hide_tags_panel FROM users WHERE username=?", (username,)
        ).fetchone()
        return bool(row["hide_tags_panel"]) if row else False

    def getAllUsernamesExcept(self, username: str) -> list[str]:
        """Plain username list for a "who can I request a share with" picker -
        deliberately narrower than getAllUsersDetails(), which also selects
        cookies_json/spotify_refresh_token that this list has no reason to
        touch."""
        conn = self._conn()
        rows = conn.execute("SELECT username FROM users WHERE username != ? ORDER BY username", (username,)).fetchall()
        return [r["username"] for r in rows]

    def getAllUsersWithCookies(self) -> list[tuple[str, str]]:
        """(username, email) for every user who has logged in at least once -
        used at startup to make sure each of them has a running listener."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT username, email FROM users WHERE cookies_json IS NOT NULL"
        ).fetchall()
        return [(r["username"], r["email"]) for r in rows]

    # ---- Per-user: import progress ------------------------------------------------

    def writeProgress(self, username: str, status: str, current: int, total: int,
                       message: str, error: bool) -> None:
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO import_progress (username, status, current, total, message, error)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(username) DO UPDATE SET
                    status=excluded.status, current=excluded.current, total=excluded.total,
                    message=excluded.message, error=excluded.error
                """,
                (username, status, current, total, message, int(error)),
            )

    def tryClaimImportRunning(self, username: str) -> bool:
        """Atomically mark this user's import 'running' only if it isn't already,
        so a double-submitted /import-history can't launch two concurrent import
        threads. Returns True iff this call is the one that claimed the slot.

        The conditional ON CONFLICT ... WHERE runs as one statement under
        SQLite's write lock, so the "is it running?" check and the "mark it
        running" claim are indivisible - unlike a readProgress() then
        writeProgress(), whose gap two rapid submissions could both slip
        through. total_changes is used rather than cursor.rowcount because the
        latter is unreliable for INSERT ... ON CONFLICT no-ops."""
        conn = self._conn()
        before = conn.total_changes
        with conn:
            conn.execute(
                """
                INSERT INTO import_progress (username, status, current, total, message, error)
                VALUES (?, 'running', 0, 0, 'Starting import', 0)
                ON CONFLICT(username) DO UPDATE SET
                    status='running', current=0, total=0, message='Starting import', error=0
                    WHERE import_progress.status <> 'running'
                """,
                (username,),
            )
        return conn.total_changes > before

    def readProgress(self, username: str) -> dict | None:
        conn = self._conn()
        row = conn.execute(
            "SELECT status, current, total, message, error FROM import_progress WHERE username=?",
            (username,),
        ).fetchone()
        if row is None:
            return None
        return {
            "status": row["status"],
            "current": row["current"],
            "total": row["total"],
            "percentage": round((row["current"] / row["total"] * 100) if row["total"] else 0),
            "message": row["message"],
            "error": bool(row["error"]),
        }

    def isFileImported(self, username: str, file_hash: str) -> bool:
        conn = self._conn()
        row = conn.execute(
            "SELECT 1 FROM imported_files WHERE username = ? AND file_hash = ?",
            (username, file_hash)
        ).fetchone()
        return row is not None

    def markFileImported(self, username: str, file_hash: str) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT OR IGNORE INTO imported_files (username, file_hash) VALUES (?, ?)",
            (username, file_hash)
        )

    def getAllUsersDetails(self, username: str | None = None) -> list[dict]:
        """The overview page's per-user rows. `username` narrows to a single
        user's own row - what a non-admin viewer is allowed to see (the full
        listing is admin-only, see app.py's overviewPage)."""
        conn = self._conn()
        query = ("SELECT username, email, cookies_json, spotify_client_id, spotify_refresh_token, "
                  "spotify_needs_reauth, lastfm_api_key, created_at, is_admin FROM users")
        params: tuple = ()
        if username is not None:
            query += " WHERE username=?"
            params = (username,)
        rows = conn.execute(query, params).fetchall()
        return [{**dict(r), "is_admin": bool(r["is_admin"])} for r in rows]
