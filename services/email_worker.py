# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging
import queue
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from Database.repository import Repository

from services.email_service import get_smtp_config, send_email_notification

logger = logging.getLogger(__name__)

EMAIL_WORKER_POLL_INTERVAL_SECONDS = 2.0
# How long stop() waits for the worker thread, which can be inside an SMTP
# send. Part of the shutdown budget the compose file's stop_grace_period has to
# cover (tests/test_compose_shutdown_budget.py).
EMAIL_WORKER_STOP_JOIN_TIMEOUT_SECONDS = 3.0


class EmailWorker:
    """Background worker thread that processes queued email notification dispatches."""

    def __init__(self, repo: Repository | None = None):
        self._repo = repo
        self._queue: queue.Queue[tuple[str, str, dict[str, Any]]] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def bind_repo(self, repo: Repository) -> None:
        """Attach the shared Repository this worker's jobs should use, instead
        of each job opening its own throwaway connection (see process_one)."""
        self._repo = repo

    def get_summary(self, repo: Repository | None = None) -> dict[str, Any]:
        """Return status summary of the email worker for admin status views."""
        target_repo = repo if repo is not None else self._repo
        if target_repo is None:
            from Database.repository import Repository
            target_repo = Repository()

        try:
            config = get_smtp_config(target_repo)
            enabled = config.get("enabled", False)
            smtp_configured = bool(str(config.get("host") or "").strip())
        except Exception as e:
            logger.warning("Failed to retrieve SMTP config in EmailWorker.get_summary: %s", e)
            enabled = False
            smtp_configured = False

        t = self._thread
        is_running = t is not None and t.is_alive()
        if not enabled:
            status = "DISABLED"
        elif is_running:
            status = "RUNNING"
        else:
            status = "INACTIVE"


        return {
            "status": status,
            "queue_size": self._queue.qsize(),
            "enabled": enabled,
            "smtp_configured": smtp_configured,
        }

    def enqueue(self, username: str, event_type: str, context: dict[str, Any] | None = None) -> None:
        """Enqueue an email notification job."""
        self._queue.put((username, event_type, context or {}))

    def process_one(self) -> bool:
        """Process a single job from the queue if present. Returns True if a job was processed."""
        try:
            username, event_type, context = self._queue.get_nowait()
        except queue.Empty:
            return False

        try:
            from Database.repository import Repository
            repo = self._repo if self._repo is not None else Repository()
            send_email_notification(repo, username, event_type, context)
        except Exception as e:
            logger.error("Error processing email notification for %s (%s): %s", username, event_type, e)
        finally:
            self._queue.task_done()
        return True

    def start(self) -> None:
        """Start the background worker thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        # A fresh event per run, passed INTO the loop rather than read back off
        # self - the invariant PeriodicWorkerMixin documents. stop() joins with
        # a timeout, so a worker inside a slow SMTP send can outlive it;
        # reusing and clearing one event would hand that thread a cleared flag
        # and revive it. Today start()'s is_alive check happens to prevent
        # that, which is a subtle thing to depend on.
        stopEvent = threading.Event()
        self._stop_event = stopEvent
        self._thread = threading.Thread(target=self._run, args=(stopEvent,),
                                        daemon=True, name="EmailWorker")
        self._thread.start()

    def _run(self, stopEvent: threading.Event) -> None:
        while not stopEvent.is_set():
            processed = self.process_one()
            if not processed:
                # wait(), not sleep(): an idle worker has to notice the stop
                # flag inside the join window, or stop() returns while the
                # thread is still parked. Shutdown used to work only because
                # the interval was shorter than the join timeout.
                stopEvent.wait(EMAIL_WORKER_POLL_INTERVAL_SECONDS)

    def _pendingByEvent(self) -> dict[str, int]:
        """What is still queued, counted by event type - read WITHOUT removing
        it. Draining here would make a later start() silently lose jobs it
        would otherwise still send, and stop() has no business deciding that.

        Taken under the queue's own mutex because that is the only way to read
        the backlog rather than consume it; qsize() answers "how many", and
        "how many" is not the question the caller has (see stop())."""
        with self._queue.mutex:
            pending = list(self._queue.queue)
        counts: dict[str, int] = {}
        for _username, eventType, _context in pending:
            counts[eventType] = counts.get(eventType, 0) + 1
        return counts

    def stop(self) -> None:
        """Stop the background worker thread.

        Deliberately does NOT drain what is left: draining would put an
        unbounded number of SMTP sends inside the join budget the compose
        file's stop_grace_period has to cover. The cost of dropping one is not
        the same for every event, which is why the line below names them:

        - invalid_cookies re-fires on its own, constantly - the listener that
          raises it is rebuilt on every stale-feed reconnect.
        - api_key_failed re-fires too, on the next detection pass, for as long
          as the key is still failing.
        - share_request does NOT. createShareRequest answers
          "already_requested" the second time and enqueues nothing, so that
          mail is gone for good and only the in-app request survives.

        (The cooldown never stands in the way of a retry either: it is stamped
        on a SUCCESSFUL send only, see email_service.)"""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=EMAIL_WORKER_STOP_JOIN_TIMEOUT_SECONDS)
        counts = self._pendingByEvent()
        if counts:
            breakdown = ", ".join(f"{eventType}={count}" for eventType, count in sorted(counts.items()))
            logger.warning("EmailWorker stopped with %d undelivered notification(s): %s",
                           sum(counts.values()), breakdown)


# Module-level singleton worker instance
EMAIL_WORKER = EmailWorker()


def queue_email_notification(username: str, event_type: str, context: dict[str, Any] | None = None) -> None:
    """Helper function to enqueue an email notification into the background worker."""
    EMAIL_WORKER.enqueue(username, event_type, context)
