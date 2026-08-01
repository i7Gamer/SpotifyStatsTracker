# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Per-user background worker + listener lifecycle for Database.

Split into cohesive sub-mixins (listener, wrapped worker, metadata backfiller,
Last.fm backfillers) recomposed here so `from Database.workers import
WorkerLifecycleMixin` (used by Database/database.py) is unchanged.
"""
from Database.workers.listener import ListenerMixin
from Database.workers.periodic import PeriodicWorkerMixin
from Database.workers.wrapped_worker import WrappedWorkerMixin
from Database.workers.metadata_backfiller import MetadataBackfillMixin
from Database.workers.lastfm_backfillers import LastfmBackfillMixin
from Database.telemetry import WorkerTelemetryMixin   #< a leaf module, see its docstring


class WorkerLifecycleMixin(ListenerMixin, WrappedWorkerMixin,
                           MetadataBackfillMixin, LastfmBackfillMixin,
                           PeriodicWorkerMixin, WorkerTelemetryMixin):
    """Composition of the background-worker sub-mixins, mixed into Database.

    PeriodicWorkerMixin sits behind the workers that use it: they define the
    named start/stop pairs, it defines the one lifecycle underneath."""
