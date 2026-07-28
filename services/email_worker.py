# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from Database.repository import Repository

from services.email_service import send_email_notification

logger = logging.getLogger(__name__)

EMAIL_WORKER_POLL_INTERVAL_SECONDS = 2.0


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
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="EmailWorker")
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            processed = self.process_one()
            if not processed:
                time.sleep(EMAIL_WORKER_POLL_INTERVAL_SECONDS)

    def stop(self) -> None:
        """Stop the background worker thread."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=3.0)


# Module-level singleton worker instance
EMAIL_WORKER = EmailWorker()


def queue_email_notification(username: str, event_type: str, context: dict[str, Any] | None = None) -> None:
    """Helper function to enqueue an email notification into the background worker."""
    EMAIL_WORKER.enqueue(username, event_type, context)
