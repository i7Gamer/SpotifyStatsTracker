from __future__ import annotations

"""Shared module-level imports and constants for the Repository query mixins.

Split out of Database/repository.py so every Database/queries/*.py mixin and
the composed Repository can pull the same catalog/plays/settings constants and
db-layer helpers from one place (`from Database.queries._base import *`).
"""
import datetime
import json
import secrets
import threading
import time
from pathlib import Path

try:
    import Database.db as db
    from Database.db import ConnectionManager, SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON, BEHAVIORAL_COLUMNS
    from Database.secret_store import encryptSecret, decryptSecret, isEncrypted
except ModuleNotFoundError:
    import db
    from db import ConnectionManager, SYNTHETIC_FALLBACK_REASON, RESTRICTED_FALLBACK_REASON, BEHAVIORAL_COLUMNS
    from secret_store import encryptSecret, decryptSecret, isEncrypted

IMAGE_KIND_TRACK = "track"
IMAGE_KIND_ARTIST = "artist"
IMAGE_STATUS_PENDING = "pending"
IMAGE_STATUS_OK = "ok"
IMAGE_STATUS_FAILED = "failed"

# How long the metadata backfiller waits before re-attempting an album it already
# processed - covers restricted/blanked albums whose metadata Spotify may fill in
# (or unblock) later, without hammering the API for permanently dateless albums.
ALBUM_BACKFILL_RETRY_SECONDS = 7 * 24 * 3600

# How long the Last.fm genre backfiller waits before re-attempting an entity
# whose lookup came back empty/not-found. Entities that got real (non-inherited)
# genres never re-enter the queue - community tags are stable enough that a
# one-time fetch is the whole point of marking them attempted.
GENRE_BACKFILL_RETRY_SECONDS = 30 * 24 * 3600

# How many leading track_artists.position slots the genre backfiller queues
# for an artist lookup - 0 is the primary credit; widened past 0 so
# feature/collab-only artists (never anyone's primary) aren't permanently
# excluded from the backfill. Bounded rather than unlimited: position <= 4
# covers 99%+ of all real credit rows in practice, so it captures nearly
# every feature-only artist without unboundedly widening the queue.
GENRE_BACKFILL_MAX_ARTIST_POSITION = 4

# How long the background biography backfiller waits before re-attempting an
# artist whose fetch came back with no usable bio. An artist with real bio
# text never re-enters the queue (see getArtistsMissingBiographies) - only a
# definitive-empty result is retried, in case Last.fm gains a bio later.
BIOGRAPHY_BACKFILL_RETRY_SECONDS = 30 * 24 * 3600

# app_settings key for the admin's instance-wide toggle: do inherited (artist-
# derived) genre rows count in genre stats and coverage? Absent row = enabled.
INHERITED_GENRES_SETTING_KEY = "genres_include_inherited"
APP_SETTING_TRUE = "1"
APP_SETTING_FALSE = "0"

# app_settings keys for the admin's instance-wide feature kill switches (see
# the overview settings panel) - each defaults to enabled (absent row), same
# contract as INHERITED_GENRES_SETTING_KEY above.
SPOTIFY_BACKFILL_SETTING_KEY = "spotify_api_backfill_enabled"
LASTFM_BACKFILL_SETTING_KEY = "lastfm_genre_backfill_enabled"
DATA_SHARING_SETTING_KEY = "data_sharing_enabled"
REGISTRATION_SETTING_KEY = "registration_enabled"
SHARE_LINKS_SETTING_KEY = "share_links_enabled"
ARTIST_BIO_SETTING_KEY = "artist_bio_enabled"
ALBUM_BIO_SETTING_KEY = "album_bio_enabled"

# Instance-wide skip threshold (app_settings). This is the single, admin-tunable
# boundary between a "skip" and a real listen - it replaced both the old fixed
# play_skips split and getCompletionStats' 30s line. Two modes:
#   "seconds": a play is a skip when time_played < value * 1000 (value in 5..60s)
#   "percent": a skip when it played less than value% of the track's duration
#              (value in 5..25%); tracks with unknown duration (<=0) fall back to
#              the fixed db.SKIP_THRESHOLD_MS floor.
# Materialized per row into plays.is_skip at write time and by recomputeSkipFlags()
# whenever the threshold changes. Default seconds/5 matches the historical
# SKIP_THRESHOLD_MS the merge migration seeds.
SKIP_THRESHOLD_MODE_KEY = "skip_threshold_mode"
SKIP_THRESHOLD_VALUE_KEY = "skip_threshold_value"
SKIP_MODE_SECONDS = "seconds"
SKIP_MODE_PERCENT = "percent"
SKIP_SECONDS_MIN = 5
SKIP_SECONDS_MAX = 60
SKIP_PERCENT_MIN = 5
SKIP_PERCENT_MAX = 25
SKIP_THRESHOLD_DEFAULT_MODE = SKIP_MODE_SECONDS
SKIP_THRESHOLD_DEFAULT_VALUE = 5

# app_settings keys for numeric tunables migrated out of code constants. Each
# falls back to its code default (config.py / Database.database) when the row is
# absent, so behavior is unchanged until an admin sets one. DISCOVER_ARTIST_LIMIT
# is read per request (live). The *_WORKERS values size ThreadPoolExecutors built
# once at process start, so a change only applies after a restart.
DISCOVER_ARTIST_LIMIT_KEY = "discover_artist_limit"
DISCOVER_ARTIST_LIMIT_MIN = 1
DISCOVER_ARTIST_LIMIT_MAX = 25
IMAGE_DOWNLOAD_WORKERS_KEY = "image_download_workers"
ARTIST_BIO_FETCH_WORKERS_KEY = "artist_bio_fetch_workers"
ALBUM_BIO_FETCH_WORKERS_KEY = "album_bio_fetch_workers"
WORKER_COUNT_MIN = 1
WORKER_COUNT_MAX = 32

# getBucketedPlayTotals' fixed UTC bucket width. 15 minutes is the smallest
# granularity any real-world UTC offset uses (e.g. Asia/Kathmandu +5:45), so
# every play in one bucket maps to the same local day/hour/weekday no matter
# which IANA timezone Python later applies - which is what lets the heavy
# per-play aggregation move into SQL without losing timezone correctness.
PLAY_BUCKET_SECONDS = 15 * 60

# Whitelist mapping the public sortBy values to the SQL output-column aliases
# they're allowed to sort by. sortBy is interpolated directly into ORDER BY
# (column names can't be bound as query parameters), and it's user-controlled
# (app.py's sortBy query param) - this whitelist is what makes that safe.
# "name" sorts COLLATE NOCASE so e.g. "abba" and "ABBA" interleave by letter
# instead of every uppercase name sorting before every lowercase one (SQLite's
# default BINARY collation).
SONG_SORT_COLUMNS = {
    "plays": "plays",
    "totalTimeListened": "total_time_listened",
    "name": "name COLLATE NOCASE",
}

ALBUM_SORT_COLUMNS = {
    "plays": "plays",
    "totalTimeListened": "total_time_listened",
    "name": "name COLLATE NOCASE",
}

ARTIST_SORT_COLUMNS = {
    "plays": "plays",
    "totalTimeListened": "total_time_listened",
    "name": "name COLLATE NOCASE",
}
