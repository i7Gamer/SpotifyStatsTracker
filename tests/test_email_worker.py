# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import pytest
from unittest.mock import patch

from Database.repository import Repository
from services.email_worker import EmailWorker, queue_email_notification
from Database.queries.email_queries import EVENT_INVALID_COOKIES


@patch("services.email_worker.send_email_notification")
def test_email_worker_processes_queue(mock_send):
    mock_send.return_value = True

    worker = EmailWorker()
    worker.enqueue("test_user_w1", EVENT_INVALID_COOKIES, {"key": "val"})

    # Run single loop iteration
    processed = worker.process_one()
    assert processed is True
    assert mock_send.called is True
    assert mock_send.call_args[0][1] == "test_user_w1"
    assert mock_send.call_args[0][2] == EVENT_INVALID_COOKIES


@patch("services.email_worker.send_email_notification")
def test_global_queue_email_notification(mock_send):
    mock_send.return_value = True

    # Helper function enqueues into global worker singleton
    queue_email_notification("test_user_w2", EVENT_INVALID_COOKIES)

    from services.email_worker import EMAIL_WORKER
    processed = EMAIL_WORKER.process_one()
    assert processed is True


@patch("services.email_worker.get_smtp_config")
def test_email_worker_get_summary_disabled(mock_get_config):
    mock_get_config.return_value = {"enabled": False, "host": "smtp.example.com"}
    worker = EmailWorker()
    summary = worker.get_summary()
    assert summary["status"] == "DISABLED"
    assert summary["enabled"] is False
    assert summary["smtp_configured"] is True
    assert summary["queue_size"] == 0


@patch("services.email_worker.get_smtp_config")
def test_email_worker_get_summary_running_and_inactive(mock_get_config):
    mock_get_config.return_value = {"enabled": True, "host": "smtp.example.com"}
    worker = EmailWorker()

    # When thread is not started
    summary = worker.get_summary()
    assert summary["status"] == "INACTIVE"
    assert summary["enabled"] is True
    assert summary["smtp_configured"] is True

    # When thread is mocked as alive
    mock_thread = patch.object(worker, "_thread").start()
    mock_thread.is_alive.return_value = True
    try:
        summary_running = worker.get_summary()
        assert summary_running["status"] == "RUNNING"
    finally:
        patch.stopall()


@patch("services.email_worker.get_smtp_config")
def test_email_worker_get_summary_unconfigured_smtp(mock_get_config):
    mock_get_config.return_value = {"enabled": True, "host": ""}
    worker = EmailWorker()
    worker.enqueue("user1", EVENT_INVALID_COOKIES)

    summary = worker.get_summary()
    assert summary["status"] == "INACTIVE"
    assert summary["smtp_configured"] is False
    assert summary["queue_size"] == 1


@patch("services.email_worker.get_smtp_config")
def test_email_worker_get_summary_non_string_host_repro(mock_get_config):
    # Host configured as an integer (e.g. 587 instead of "smtp.example.com")
    mock_get_config.return_value = {"enabled": True, "host": 587}
    worker = EmailWorker()

    summary = worker.get_summary()
    # Expect enabled to remain True and host to be treated as configured
    assert summary["enabled"] is True
    assert summary["smtp_configured"] is True


