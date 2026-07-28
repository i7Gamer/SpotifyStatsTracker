# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
from unittest.mock import patch, MagicMock

from _app_factory import AppTestCase
from services.email_service import get_smtp_config
from Database.queries.email_queries import EVENT_INVALID_COOKIES, EVENT_SHARE_REQUEST


class EmailRoutesTestCase(AppTestCase):

    def setUp(self):
        super().setUp()
        self.app_instance = self._makeApp()
        self.repo = self.app_instance.repo

    def test_admin_email_settings_route(self):
        # Create admin user
        self.repo.upsertUser("admin_user", "admin@example.com")
        self.repo.setUserAdmin("admin_user", True)

        with patch.object(self.app_instance, "is_user_logged_in", return_value=True):
            client = self.app_instance.app.test_client()

            with client.session_transaction() as sess:
                sess["username"] = "admin_user"
                sess["email"] = "admin@example.com"

            # POST to update SMTP settings
            res = client.post(
                "/admin/email_settings",
                data={
                    "email_notifications_enabled": "1",
                    "smtp_host": "smtp.test.com",
                    "smtp_port": "465",
                    "smtp_encryption": "ssl",
                    "smtp_user": "admin_smtp",
                    "smtp_password": "new_password",
                    "smtp_from_email": "admin_from@test.com",
                    "smtp_from_name": "Test Admin",
                },
                follow_redirects=True,
            )
            assert res.status_code == 200

        config = get_smtp_config(self.repo)
        assert config["enabled"] is True
        assert config["host"] == "smtp.test.com"
        assert config["port"] == 465
        assert config["encryption"] == "ssl"
        assert config["user"] == "admin_smtp"
        assert config["from_email"] == "admin_from@test.com"
        assert config["from_name"] == "Test Admin"

    def test_profile_notifications_route(self):
        self.repo.upsertUser("normal_user", "user@example.com")

        # Enable notifications globally first
        self.repo.setAppSetting("email_notifications_enabled", "1")

        with patch.object(self.app_instance, "is_user_logged_in", return_value=True):
            client = self.app_instance.app.test_client()

            with client.session_transaction() as sess:
                sess["username"] = "normal_user"
                sess["email"] = "user@example.com"

            # GET profile notifications page
            res_get = client.get("/profile/notifications")
            assert res_get.status_code == 200
            assert b"Notification Preferences" in res_get.data

            # POST to toggle off invalid_cookies
            res_post = client.post(
                "/profile/notifications",
                data={
                    "notif_invalid_cookies": "0",
                    "notif_api_key_failed": "1",
                    "notif_share_request": "1",
                },
                follow_redirects=True,
            )
            assert res_post.status_code == 200
            assert self.repo.getUserNotificationPreference("normal_user", EVENT_INVALID_COOKIES) is False
            assert self.repo.getUserNotificationPreference("normal_user", EVENT_SHARE_REQUEST) is True

    def test_profile_notifications_disabled_globally_returns_404(self):
        self.repo.upsertUser("normal_user", "user@example.com")

        # Disable notifications globally
        self.repo.setAppSetting("email_notifications_enabled", "0")

        with patch.object(self.app_instance, "is_user_logged_in", return_value=True):
            client = self.app_instance.app.test_client()

            with client.session_transaction() as sess:
                sess["username"] = "normal_user"
                sess["email"] = "user@example.com"

            res_get = client.get("/profile/notifications")
            assert res_get.status_code == 404
