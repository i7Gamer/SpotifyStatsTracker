# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

from __future__ import annotations

import threading


class WorkerTelemetryMixin:
    """Per-worker cycle success/failure counters shared by every periodic
    backfill loop in Database/workers/ (metadata backfiller, wrapped
    calculator, Last.fm genre/artist-bio/album-bio backfillers) and by the
    process-wide BackupWorker. Feeds the /admin Worker Health card's FAILING
    badges - see Database.WORKER_HEALTH_FAILING_THRESHOLD.

    Deliberately a leaf module at Database/ root rather than inside
    Database/workers/, where the rest of the worker code lives: importing
    anything from that package runs its __init__, which pulls in the listener
    and through it Database.database. Database/backup.py is imported on its own
    (the migrators import it standalone, before any of that exists), and doing
    so through the workers package raised a circular ImportError."""

    def _initWorkerTelemetry(self) -> None:
        self._worker_telemetry_lock = threading.Lock()
        self._worker_telemetry: dict[str, dict] = {}

    def _recordWorkerCycle(self, name: str, success: bool, error: str | None = None) -> None:
        """Record the outcome of one completed loop cycle for worker `name`.
        Call once per cycle that actually ran to completion (not for cycles
        that idled out early with nothing to do) - see the try/except/else
        shape in each loop for how that distinction is made."""
        with self._worker_telemetry_lock:
            telemetry = self._worker_telemetry.setdefault(name, {
                "total_cycles": 0, "total_failures": 0,
                "consecutive_failures": 0, "last_error": None,
            })
            telemetry["total_cycles"] += 1
            if success:
                telemetry["consecutive_failures"] = 0
            else:
                telemetry["total_failures"] += 1
                telemetry["consecutive_failures"] += 1
                telemetry["last_error"] = error

    def _getWorkerTelemetry(self, name: str) -> dict:
        """Snapshot for worker `name` - zero-defaults if it hasn't recorded a
        cycle yet (never started, or still in its startup delay)."""
        with self._worker_telemetry_lock:
            telemetry = self._worker_telemetry.get(name, {
                "total_cycles": 0, "total_failures": 0,
                "consecutive_failures": 0, "last_error": None,
            })
            totalCycles = telemetry["total_cycles"]
            failureRate = (telemetry["total_failures"] / totalCycles) if totalCycles else 0.0
            return {
                "consecutive_failures": telemetry["consecutive_failures"],
                "failure_rate": failureRate,
                "last_error": telemetry["last_error"],
            }
