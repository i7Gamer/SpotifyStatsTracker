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
