# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

import threading

import pytest
from unittest.mock import MagicMock, patch

from Database.repository import Repository
from services.email_worker import (
    EmailWorker, queue_email_notification, EMAIL_WORKER_STOP_JOIN_TIMEOUT_SECONDS,
)
from Database.queries.email_queries import EVENT_INVALID_COOKIES, EVENT_SHARE_REQUEST

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
        #< captured BEFORE stop(), which releases the worker's own reference
        #  (see test_a_thread_that_outlived_the_join_does_not_block_a_restart)
        thread = worker._thread
        worker.stop()

    assert not thread.is_alive()


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


def test_the_warning_names_what_was_dropped(caplog):
    """A count alone does not answer the question the line exists for. Two of
    the three events that fill this queue re-fire on their own after a restart
    (a listener with bad cookies rebuilds constantly; a failing API key is
    re-detected), so losing those costs nothing. A share request is created
    ONCE - createShareRequest answers "already_requested" the next time - so
    its mail is gone for good, and the operator needs to be able to tell which
    kind sat in the queue."""
    worker = EmailWorker()
    worker.enqueue("alice", EVENT_INVALID_COOKIES)
    worker.enqueue("bob", EVENT_INVALID_COOKIES)
    worker.enqueue("carol", EVENT_SHARE_REQUEST)

    with caplog.at_level("WARNING", logger="services.email_worker"):
        worker.stop()

    assert EVENT_SHARE_REQUEST in caplog.text
    assert f"{EVENT_INVALID_COOKIES}=2" in caplog.text
    assert f"{EVENT_SHARE_REQUEST}=1" in caplog.text


def test_the_warning_does_not_consume_the_queue(caplog):
    """Reported, not drained: stop() is only reached at shutdown today, but a
    worker that threw its queue away on the way out would silently lose the
    jobs a later start() would otherwise still send."""
    worker = EmailWorker()
    worker.enqueue("alice", EVENT_INVALID_COOKIES)

    with caplog.at_level("WARNING", logger="services.email_worker"):
        worker.stop()

    assert worker._queue.qsize() == 1


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
def test_a_failing_send_is_logged_and_the_job_still_completes(mock_send, caplog):
    """The except/finally pair around one job, previously uncovered: a send
    that raises must be LOGGED rather than silently dropped, must still
    task_done() its queue item, and must report the job as processed so the
    loop keeps draining instead of treating the queue as idle."""
    mock_send.side_effect = RuntimeError("SMTP said no")

    worker = EmailWorker()
    worker.enqueue("test_user_w9", EVENT_INVALID_COOKIES, {})

    with caplog.at_level("ERROR", logger="services.email_worker"):
        processed = worker.process_one()

    assert processed is True
    assert "Error processing email notification" in caplog.text
    assert worker._queue.unfinished_tasks == 0, "a failed send must still task_done() its item"


def test_start_while_running_keeps_the_existing_thread():
    """stop()'s release-the-reference dance leans on this guard by name -
    "start() declines while a thread is alive" - and it was uncovered: a
    second start() against a live worker must be a no-op, not a replacement
    that orphans the first thread on an event nothing holds a reference to."""
    worker = EmailWorker()
    worker.start()
    try:
        first = worker._thread
        assert first.is_alive()

        worker.start()

        assert worker._thread is first, "a live worker was replaced, orphaning its thread"
    finally:
        worker.stop()


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




def _stillRunningThread():
    """A stand-in for the case stop() cannot rule out: its join is BOUNDED
    (EMAIL_WORKER_STOP_JOIN_TIMEOUT_SECONDS), so a worker part-way through an
    SMTP send is still alive when stop() returns.

    Stood in for rather than built out of a real slow send, so these tests have
    no clock in them - the race is the point, not the timing."""
    thread = MagicMock()
    thread.is_alive.return_value = True
    return thread


def test_a_thread_that_outlived_the_join_does_not_block_a_restart():
    """start() declines when a thread is already alive - and that thread was
    already on its way out, watching a stop event that is SET. So stop() then
    start() could leave the worker with nothing running while start() believed
    otherwise, and every later enqueue() would queue behind no consumer.

    stop() releasing its reference is what closes that: a thread it no longer
    owns cannot answer for whether a new one is needed."""
    worker = EmailWorker()
    worker._thread = _stillRunningThread()
    idling = threading.Event()
    worker.process_one = lambda: (idling.set(), False)[1]

    worker.stop()
    worker.start()

    assert idling.wait(_DEADLINE_SECONDS), "start() declined to replace a thread that was exiting"
    thread = worker._thread
    worker.stop()
    assert not thread.is_alive()


def test_stop_still_joins_the_thread_it_releases():
    """The trap in the fix above: clearing the reference before joining, with
    no local kept, would make stop() return without waiting at all. Shutdown
    would stop covering the in-flight SMTP send that
    tests/test_compose_shutdown_budget.py sizes stop_grace_period against."""
    worker = EmailWorker()
    thread = _stillRunningThread()
    worker._thread = thread

    worker.stop()

    thread.join.assert_called_once_with(timeout=EMAIL_WORKER_STOP_JOIN_TIMEOUT_SECONDS)
    assert worker._thread is None


def test_stop_sets_the_flag_before_joining():
    """Ordering, not just presence: joining a thread that has not been told to
    stop waits out the whole timeout for nothing."""
    worker = EmailWorker()
    thread = _stillRunningThread()
    flagAtJoin = []
    thread.join.side_effect = lambda timeout=None: flagAtJoin.append(worker._stop_event.is_set())
    worker._thread = thread

    worker.stop()

    assert flagAtJoin == [True]


def test_a_clean_stop_and_restart_gives_a_running_worker():
    """The ordinary path, end to end with real threads - no stand-in."""
    worker = EmailWorker()
    firstIdle = threading.Event()
    worker.process_one = lambda: (firstIdle.set(), False)[1]
    worker.start()
    assert firstIdle.wait(_DEADLINE_SECONDS)
    first = worker._thread
    worker.stop()

    secondIdle = threading.Event()
    worker.process_one = lambda: (secondIdle.set(), False)[1]
    worker.start()

    assert worker._thread is not first
    assert secondIdle.wait(_DEADLINE_SECONDS), "the restarted worker never polled"
    second = worker._thread
    worker.stop()
    assert not second.is_alive()


def test_a_stopped_worker_does_not_report_itself_as_running():
    """The deliberate consequence of releasing the reference: a thread still
    draining its last send is no longer what /admin reports on. It has been
    told to stop, so "RUNNING" would be the wrong answer - and the queue_size
    beside it is what shows anything left behind."""
    worker = EmailWorker()
    worker._thread = _stillRunningThread()

    worker.stop()

    with patch("services.email_worker.get_smtp_config",
               return_value={"enabled": True, "host": "smtp.example.com"}):
        assert worker.get_summary()["status"] == "INACTIVE"


def test_a_double_stop_is_harmless():
    """stop() reaches this from two places that do not coordinate - wsgi.py's
    finally and the admin restart's threading.Timer - so it has to tolerate
    arriving twice."""
    worker = EmailWorker()
    worker._thread = _stillRunningThread()

    worker.stop()
    worker.stop()

    assert worker._thread is None
