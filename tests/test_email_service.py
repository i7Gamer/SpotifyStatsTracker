# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
from unittest.mock import patch, MagicMock

from Database.repository import Repository
from services.email_service import (
    get_smtp_config,
    save_smtp_config,
    get_instance_public_url,
    save_instance_public_url,
    build_email_message,
    send_email_notification,
    send_test_email,
    _render_event_template,
)
from Database.queries.email_queries import (
    EVENT_INVALID_COOKIES,
    EVENT_API_KEY_FAILED,
    EVENT_SHARE_REQUEST,
)


def test_smtp_config_save_and_get():
    repo = Repository()

    # Initial state (defaults)
    config = get_smtp_config(repo)
    assert config["enabled"] is False
    assert config["host"] == ""
    assert config["port"] == 587
    assert config["encryption"] == "tls"

    # Save new settings
    save_smtp_config(
        repo=repo,
        enabled=True,
        host="smtp.example.com",
        port=465,
        encryption="ssl",
        user="testuser@example.com",
        password="secretpassword123",
        from_email="noreply@example.com",
        from_name="Spotify Stats Tracker",
    )

    config_after = get_smtp_config(repo)
    assert config_after["enabled"] is True
    assert config_after["host"] == "smtp.example.com"
    assert config_after["port"] == 465
    assert config_after["encryption"] == "ssl"
    assert config_after["user"] == "testuser@example.com"
    assert config_after["from_email"] == "noreply@example.com"
    assert config_after["from_name"] == "Spotify Stats Tracker"


def test_instance_public_url_save_and_get():
    repo = Repository()
    assert get_instance_public_url(repo) == ""

    save_instance_public_url(repo, "https://tracker.example.com")
    assert get_instance_public_url(repo) == "https://tracker.example.com"


def test_instance_public_url_strips_trailing_slash():
    repo = Repository()
    save_instance_public_url(repo, "https://tracker.example.com/ ")
    assert get_instance_public_url(repo) == "https://tracker.example.com"


def test_build_email_message():
    msg = build_email_message(
        to_email="user@example.com",
        subject="Test Subject",
        text_body="Hello Plain Text",
        html_body="<h1>Hello HTML</h1>",
        from_email="noreply@example.com",
        from_name="Spotify Tracker",
    )

    assert msg["To"] == "user@example.com"
    assert msg["Subject"] == "Test Subject"
    assert "Spotify Tracker <noreply@example.com>" in msg["From"]


@patch("smtplib.SMTP")
@patch("smtplib.SMTP_SSL")
def test_send_email_notification_disabled_globally(mock_ssl, mock_smtp):
    repo = Repository()
    username = "user_notif_disabled"
    repo.upsertUser(username, "user@example.com")

    # Global notifications disabled
    save_smtp_config(repo, enabled=False, host="smtp.example.com", port=587, encryption="tls", user="", password="", from_email="n@e.com", from_name="N")

    sent = send_email_notification(repo, username, EVENT_INVALID_COOKIES, context={})
    assert sent is False
    mock_smtp.assert_not_called()
    mock_ssl.assert_not_called()


@patch("smtplib.SMTP")
def test_send_email_notification_success(mock_smtp_class):
    mock_server = MagicMock()
    mock_smtp_class.return_value.__enter__.return_value = mock_server

    repo = Repository()
    username = "user_notif_success"
    repo.upsertUser(username, "user_success@example.com")

    save_smtp_config(
        repo=repo,
        enabled=True,
        host="smtp.example.com",
        port=587,
        encryption="tls",
        user="smtp_user",
        password="smtp_password",
        from_email="noreply@example.com",
        from_name="Spotify Tracker",
    )

    sent = send_email_notification(repo, username, EVENT_INVALID_COOKIES, context={})
    assert sent is True
    assert mock_server.send_message.called is True

    # Cooldown should prevent immediate second email
    sent_again = send_email_notification(repo, username, EVENT_INVALID_COOKIES, context={})
    assert sent_again is False


@patch("smtplib.SMTP")
def test_send_email_notification_includes_configured_instance_link(mock_smtp_class):
    mock_server = MagicMock()
    mock_smtp_class.return_value.__enter__.return_value = mock_server

    repo = Repository()
    username = "user_notif_link"
    repo.upsertUser(username, "user_link@example.com")

    save_smtp_config(
        repo=repo, enabled=True, host="smtp.example.com", port=587, encryption="tls",
        user="smtp_user", password="smtp_password", from_email="noreply@example.com", from_name="Spotify Tracker",
    )
    save_instance_public_url(repo, "https://tracker.example.com")

    sent = send_email_notification(repo, username, EVENT_INVALID_COOKIES, context={})
    assert sent is True

    msg = mock_server.send_message.call_args[0][0]
    htmlPart = next(part for part in msg.walk() if part.get_content_type() == "text/html")
    html = htmlPart.get_payload(decode=True).decode("utf-8")
    assert 'href="https://tracker.example.com/login"' in html


@patch("smtplib.SMTP")
def test_send_test_email(mock_smtp_class):
    mock_server = MagicMock()
    mock_smtp_class.return_value.__enter__.return_value = mock_server

    repo = Repository()
    save_smtp_config(
        repo=repo,
        enabled=True,
        host="smtp.example.com",
        port=587,
        encryption="tls",
        user="smtp_user",
        password="smtp_password",
        from_email="noreply@example.com",
        from_name="Spotify Tracker",
    )

    result, err = send_test_email(repo, "admin@example.com")
    assert result is True
    assert err is None
    assert mock_server.send_message.called is True


class TestRenderEventTemplate:
    """_render_event_template's html_body is shown as-is in an email client -
    every link it offers must actually go somewhere, since there is no way
    for a recipient to retry a dead button."""

    def test_invalid_cookies_has_no_dead_link(self):
        subject, text_body, html_body = _render_event_template(EVENT_INVALID_COOKIES, "alice", {})
        assert "alice" in text_body
        assert "alice" in html_body
        assert 'href="#"' not in html_body
        assert "<a " not in html_body   #< no link at all beats a dead one

    def test_api_key_failed_mentions_username(self):
        subject, text_body, html_body = _render_event_template(EVENT_API_KEY_FAILED, "alice", {})
        assert "alice" in text_body
        assert "alice" in html_body
        assert 'href="#"' not in html_body

    def test_share_request_mentions_requester(self):
        subject, text_body, html_body = _render_event_template(
            EVENT_SHARE_REQUEST, "alice", {"requester_username": "bob"})
        assert "bob" in subject
        assert "bob" in text_body
        assert "bob" in html_body
        assert 'href="#"' not in html_body

    def test_share_request_defaults_requester_when_missing_from_context(self):
        subject, text_body, html_body = _render_event_template(EVENT_SHARE_REQUEST, "alice", {})
        assert "A user" in subject

    def test_unknown_event_type_falls_back_gracefully(self):
        subject, text_body, html_body = _render_event_template("some_future_event", "alice", {})
        assert "alice" in text_body
        assert "alice" in html_body
        assert "some_future_event" in subject

    def test_names_are_escaped_in_every_html_body(self):
        """Usernames are sanitized to [A-Za-z0-9_-] at creation today, so no
        metacharacter reaches these f-strings - but the escaping decision was
        made nowhere (while _ctaButton right beside them escapes its link),
        and a future switch to display names (which allow spaces and more)
        would have turned the interpolation live. The text bodies are
        text/plain and stay raw."""
        hostile = "<img src=x onerror=alert(1)>"
        for event, context in ((EVENT_INVALID_COOKIES, {}),
                               (EVENT_API_KEY_FAILED, {}),
                               (EVENT_SHARE_REQUEST, {}),
                               ("some_future_event", {})):
            _subject, _text, html_body = _render_event_template(event, hostile, context)
            assert "<img" not in html_body, f"{event}: username reached the HTML unescaped"
            assert "&lt;img" in html_body

        _subject, _text, html_body = _render_event_template(
            EVENT_SHARE_REQUEST, "alice", {"requester_username": hostile})
        assert "<img" not in html_body, "requester reached the HTML unescaped"
        assert "&lt;img" in html_body

    def test_invalid_cookies_links_to_login_when_base_url_configured(self):
        _subject, text_body, html_body = _render_event_template(
            EVENT_INVALID_COOKIES, "alice", {}, base_url="https://tracker.example.com")
        assert 'href="https://tracker.example.com/login"' in html_body
        assert "https://tracker.example.com/login" in text_body

    def test_api_key_failed_links_to_connections_when_base_url_configured(self):
        _subject, text_body, html_body = _render_event_template(
            EVENT_API_KEY_FAILED, "alice", {}, base_url="https://tracker.example.com")
        assert 'href="https://tracker.example.com/profile/connections"' in html_body
        assert "https://tracker.example.com/profile/connections" in text_body

    def test_share_request_links_to_sharing_when_base_url_configured(self):
        _subject, text_body, html_body = _render_event_template(
            EVENT_SHARE_REQUEST, "alice", {"requester_username": "bob"}, base_url="https://tracker.example.com")
        assert 'href="https://tracker.example.com/profile/sharing"' in html_body
        assert "https://tracker.example.com/profile/sharing" in text_body

    def test_base_url_is_html_escaped_in_href(self):
        """The base URL is admin-supplied and stored, not hardcoded - it must
        not be able to break out of the href attribute it's interpolated into."""
        _subject, _text_body, html_body = _render_event_template(
            EVENT_INVALID_COOKIES, "alice", {}, base_url='https://evil.example.com"onmouseover="alert(1)')
        assert 'onmouseover="alert(1)"' not in html_body
        assert "&quot;" in html_body

    def test_trailing_slash_in_base_url_does_not_double_up(self):
        _subject, _text_body, html_body = _render_event_template(
            EVENT_INVALID_COOKIES, "alice", {}, base_url="https://tracker.example.com/")
        assert "//login" not in html_body
