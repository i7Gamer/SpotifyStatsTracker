# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging
import time

from Database.queries._base import *  # noqa: F401,F403 - shared constants/db helpers

logger = logging.getLogger(__name__)

EVENT_INVALID_COOKIES = "invalid_cookies"
EVENT_API_KEY_FAILED = "api_key_failed"
EVENT_SHARE_REQUEST = "share_request"

VALID_NOTIFICATION_EVENTS = (
    EVENT_INVALID_COOKIES,
    EVENT_API_KEY_FAILED,
    EVENT_SHARE_REQUEST,
)

DEFAULT_NOTIFICATION_COOLDOWN_SECONDS = 86400  # 24 hours


class EmailQueries:
    """EmailQueries: data-access methods for email settings, preferences and cooldowns."""

    def getUserNotificationPreference(self, username: str, event_type: str) -> bool:
        """Read a user's notification preference for event_type.
        Defaults to True if unconfigured."""
        if event_type not in VALID_NOTIFICATION_EVENTS:
            logger.warning("Unknown notification event type: %s", event_type)
            return True
        conn = self._conn()
        row = conn.execute(
            "SELECT enabled FROM user_notification_preferences WHERE username=? AND event_type=?",
            (username, event_type),
        ).fetchone()
        return bool(row["enabled"]) if row is not None else True

    def setUserNotificationPreference(self, username: str, event_type: str, enabled: bool) -> None:
        """Set a user's notification preference for event_type."""
        if event_type not in VALID_NOTIFICATION_EVENTS:
            raise ValueError(f"Invalid event type: {event_type}")
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO user_notification_preferences (username, event_type, enabled, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(username, event_type) DO UPDATE SET
                    enabled=excluded.enabled,
                    updated_at=excluded.updated_at
                """,
                (username, event_type, 1 if enabled else 0, time.time()),
            )

    def getAllUserNotificationPreferences(self, username: str) -> dict[str, bool]:
        """Return a mapping of all event_types to boolean preferences for username."""
        conn = self._conn()
        rows = conn.execute(
            "SELECT event_type, enabled FROM user_notification_preferences WHERE username=?",
            (username,),
        ).fetchall()
        result = {event: True for event in VALID_NOTIFICATION_EVENTS}
        for r in rows:
            if r["event_type"] in result:
                result[r["event_type"]] = bool(r["enabled"])
        return result

    def isNotificationCooldownActive(
        self, username: str, event_type: str, cooldown_seconds: float = DEFAULT_NOTIFICATION_COOLDOWN_SECONDS
    ) -> bool:
        """Check if an email notification for event_type was sent to username within cooldown_seconds."""
        conn = self._conn()
        row = conn.execute(
            "SELECT last_sent_at FROM user_notification_cooldowns WHERE username=? AND event_type=?",
            (username, event_type),
        ).fetchone()
        if row is None or row["last_sent_at"] is None:
            return False
        return (time.time() - float(row["last_sent_at"])) < cooldown_seconds

    def recordNotificationSent(self, username: str, event_type: str, sent_at: float | None = None) -> None:
        """Record the timestamp when a notification email was sent to username."""
        now = time.time() if sent_at is None else sent_at
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT INTO user_notification_cooldowns (username, event_type, last_sent_at)
                VALUES (?, ?, ?)
                ON CONFLICT(username, event_type) DO UPDATE SET
                    last_sent_at=excluded.last_sent_at
                """,
                (username, event_type, now),
            )
