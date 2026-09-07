# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
import time
from Database.repository import Repository

EVENT_INVALID_COOKIES = "invalid_cookies"
EVENT_API_KEY_FAILED = "api_key_failed"
EVENT_SHARE_REQUEST = "share_request"
EVENT_MILESTONE_REACHED = "milestone_reached"
DEFAULT_COOLDOWN_SECONDS = 86400  # 24 hours


def test_email_notification_preferences_defaults():
    repo = Repository()
    username = "test_user_notif_1"
    email = "test1@example.com"
    repo.upsertUser(username, email)

    # Defaults should be True for all valid events if unconfigured
    assert repo.getUserNotificationPreference(username, EVENT_INVALID_COOKIES) is True
    assert repo.getUserNotificationPreference(username, EVENT_API_KEY_FAILED) is True
    assert repo.getUserNotificationPreference(username, EVENT_SHARE_REQUEST) is True


def test_milestone_reached_defaults_to_off():
    """Opt-IN, unlike every other event: shipping the feature must not start
    mailing existing users who never touched the preference."""
    repo = Repository()
    username = "test_user_notif_milestone_default"
    repo.upsertUser(username, "milestone_default@example.com")

    assert repo.getUserNotificationPreference(username, EVENT_MILESTONE_REACHED) is False


def test_milestone_reached_set_get_round_trip():
    repo = Repository()
    username = "test_user_notif_milestone_roundtrip"
    repo.upsertUser(username, "milestone_roundtrip@example.com")

    repo.setUserNotificationPreference(username, EVENT_MILESTONE_REACHED, True)
    assert repo.getUserNotificationPreference(username, EVENT_MILESTONE_REACHED) is True

    repo.setUserNotificationPreference(username, EVENT_MILESTONE_REACHED, False)
    assert repo.getUserNotificationPreference(username, EVENT_MILESTONE_REACHED) is False


def test_milestone_reached_default_carried_by_all_preferences():
    repo = Repository()
    username = "test_user_notif_milestone_all"
    repo.upsertUser(username, "milestone_all@example.com")

    prefs = repo.getAllUserNotificationPreferences(username)
    assert prefs.get(EVENT_MILESTONE_REACHED) is False
    # The other three events keep their opt-out default alongside it.
    assert prefs.get(EVENT_INVALID_COOKIES) is True
    assert prefs.get(EVENT_API_KEY_FAILED) is True
    assert prefs.get(EVENT_SHARE_REQUEST) is True


def test_email_notification_preferences_set_get():
    repo = Repository()
    username = "test_user_notif_2"
    email = "test2@example.com"
    repo.upsertUser(username, email)

    # Disable invalid_cookies
    repo.setUserNotificationPreference(username, EVENT_INVALID_COOKIES, False)
    assert repo.getUserNotificationPreference(username, EVENT_INVALID_COOKIES) is False
    assert repo.getUserNotificationPreference(username, EVENT_API_KEY_FAILED) is True

    # Re-enable invalid_cookies
    repo.setUserNotificationPreference(username, EVENT_INVALID_COOKIES, True)
    assert repo.getUserNotificationPreference(username, EVENT_INVALID_COOKIES) is True


def test_email_notification_all_preferences():
    repo = Repository()
    username = "test_user_notif_3"
    email = "test3@example.com"
    repo.upsertUser(username, email)

    repo.setUserNotificationPreference(username, EVENT_INVALID_COOKIES, False)

    prefs = repo.getAllUserNotificationPreferences(username)
    assert prefs.get(EVENT_INVALID_COOKIES) is False
    assert prefs.get(EVENT_API_KEY_FAILED) is True
    assert prefs.get(EVENT_SHARE_REQUEST) is True


def test_email_notification_cooldown():
    repo = Repository()
    username = "test_user_notif_4"
    email = "test4@example.com"
    repo.upsertUser(username, email)

    # Initially allowed (no record)
    assert repo.isNotificationCooldownActive(username, EVENT_INVALID_COOKIES, cooldown_seconds=DEFAULT_COOLDOWN_SECONDS) is False

    # Record notification sent
    now = time.time()
    repo.recordNotificationSent(username, EVENT_INVALID_COOKIES, sent_at=now)

    # Cooldown should now be active
    assert repo.isNotificationCooldownActive(username, EVENT_INVALID_COOKIES, cooldown_seconds=DEFAULT_COOLDOWN_SECONDS) is True

    # After cooldown expires (simulate past timestamp)
    past_time = now - (DEFAULT_COOLDOWN_SECONDS + 10)
    repo.recordNotificationSent(username, EVENT_INVALID_COOKIES, sent_at=past_time)

    assert repo.isNotificationCooldownActive(username, EVENT_INVALID_COOKIES, cooldown_seconds=DEFAULT_COOLDOWN_SECONDS) is False
