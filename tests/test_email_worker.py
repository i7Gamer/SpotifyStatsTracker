# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import threading

import pytest
from unittest.mock import patch

from Database.repository import Repository
from services.email_worker import EmailWorker, queue_email_notification
from Database.queries.email_queries import EVENT_INVALID_COOKIES

# A failure deadline for the cross-thread waits below, not a pace: each wait
# returns as soon as the worker thread gets there.
_DEADLINE_SECONDS = 5
# Longer than any join in these tests, so a worker that sleeps through its stop
# flag instead of waiting on it is still parked when the assertion runs.
_LONG_IDLE_INTERVAL_SECONDS = 60


def test_the_idle_loop_notices_the_stop_flag_instead_of_sleeping_through_it():
    """An idle worker waits on its stop event, so stop() is immediate no
    matter how long the poll interval is. With a plain sleep, shutdown worked
    only because the interval happened to be shorter than the join timeout -
    raise the interval past it and stop() would return with the thread still
    running, which is the shape ceb4209 fixed in the listener's poll loop."""
    worker = EmailWorker()
    idling = threading.Event()
    worker.process_one = lambda: (idling.set(), False)[1]   #< empty queue: straight to the idle wait

    with patch("services.email_worker.EMAIL_WORKER_POLL_INTERVAL_SECONDS",
               _LONG_IDLE_INTERVAL_SECONDS):
        worker.start()
        assert idling.wait(_DEADLINE_SECONDS), "the worker thread never polled"
        worker.stop()

    assert not worker._thread.is_alive()


def test_each_run_gets_its_own_stop_event():
    """The invariant PeriodicWorkerMixin documents: a thread that outlived
    stop()'s join must keep watching a SET event, so it exits on its own
    rather than being handed a cleared one by the next start()."""
    worker = EmailWorker()
    worker.start()
    firstEvent = worker._stop_event
    worker.stop()

    worker.start()

    assert worker._stop_event is not firstEvent
    assert firstEvent.is_set(), "the previous run's event was reused, so its thread could be revived"
    worker.stop()


def test_stopping_with_work_still_queued_says_so(caplog):
    """Undelivered notifications are dropped at shutdown, and that is the right
    trade - draining would put an unbounded number of SMTP sends inside the
    join budget the compose stop_grace_period has to cover. What is not right
    is doing it silently: a share-request mail that never went out leaves no
    trace at all otherwise."""
    worker = EmailWorker()
    worker.enqueue("alice", EVENT_INVALID_COOKIES)
    worker.enqueue("bob", EVENT_INVALID_COOKIES)

    with caplog.at_level("WARNING", logger="services.email_worker"):
        worker.stop()   #< never started: nothing has drained the queue

    assert "2" in caplog.text
    assert "undelivered" in caplog.text.lower()


def test_stopping_with_an_empty_queue_is_quiet(caplog):
    worker = EmailWorker()

    with caplog.at_level("WARNING", logger="services.email_worker"):
        worker.stop()

    assert caplog.text == ""


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


