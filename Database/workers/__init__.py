"""Per-user background worker + listener lifecycle for Database.

Split into cohesive sub-mixins (listener, wrapped worker, metadata backfiller,
Last.fm backfillers) recomposed here so `from Database.workers import
WorkerLifecycleMixin` (used by Database/database.py) is unchanged.
"""
from Database.workers.listener import ListenerMixin
from Database.workers.wrapped_worker import WrappedWorkerMixin
from Database.workers.metadata_backfiller import MetadataBackfillMixin
from Database.workers.lastfm_backfillers import LastfmBackfillMixin
from Database.workers.telemetry import WorkerTelemetryMixin


class WorkerLifecycleMixin(ListenerMixin, WrappedWorkerMixin,
                           MetadataBackfillMixin, LastfmBackfillMixin,
                           WorkerTelemetryMixin):
    """Composition of the background-worker sub-mixins, mixed into Database."""
