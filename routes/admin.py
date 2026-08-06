# SPDX-FileCopyrightText: 2026 i7Gamer
# SPDX-License-Identifier: AGPL-3.0-or-later

"""Admin-only routes: the /admin console and its settings/action endpoints.

Extracted verbatim from app.py. Every handler is fully gated on
Repository.isAdmin. register(app, dashboard) wires them via app.add_url_rule
under their original endpoint names.
"""
import logging
import os
import threading

from flask import render_template, redirect, request, url_for, abort, jsonify

from routes._auth import makeRequiresUser

from config import (
    RECOMMENDATION_ARTIST_LIMIT, TRUTHY_ENV_VALUES,
    ALLOW_INSTANCE_RESTART_ENV_VAR, INSTANCE_RESTART_DELAY_SECONDS,
)
from Database.database import (
    Database, IMAGE_DOWNLOAD_WORKERS, ARTIST_BIO_FETCH_WORKERS, ALBUM_BIO_FETCH_WORKERS,
)
from Database.repository import (
    SKIP_MODE_SECONDS, SKIP_MODE_PERCENT,
    SKIP_SECONDS_MIN, SKIP_SECONDS_MAX, SKIP_PERCENT_MIN, SKIP_PERCENT_MAX,
    DISCOVER_ARTIST_LIMIT_KEY, DISCOVER_ARTIST_LIMIT_MIN, DISCOVER_ARTIST_LIMIT_MAX,
    IMAGE_DOWNLOAD_WORKERS_KEY, ARTIST_BIO_FETCH_WORKERS_KEY, ALBUM_BIO_FETCH_WORKERS_KEY,
    WORKER_COUNT_MIN, WORKER_COUNT_MAX,
    COMPLETION_COMPLETE_PERCENT_KEY, COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX,
    BACKUP_INTERVAL_HOURS_KEY, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX,
    BACKUP_RETENTION_COUNT_KEY, BACKUP_RETENTION_COUNT_MIN, BACKUP_RETENTION_COUNT_MAX,
    GENRE_BACKFILL_RETRY_DAYS_KEY, BIO_BACKFILL_RETRY_DAYS_KEY,
    BACKFILL_RETRY_DAYS_MIN, BACKFILL_RETRY_DAYS_MAX,
)
from Database.backup import DEFAULT_BACKUP_INTERVAL_HOURS, DEFAULT_BACKUP_RETENTION_COUNT
from Database.rate_limit import SPOTIFY_LIMITER
from Database.patches import totpAuthSnapshot
from Database.utils import convertToDatetime
from services.email_service import (
    get_smtp_config, save_smtp_config, send_test_email,
    get_instance_public_url, save_instance_public_url,
    DEFAULT_SMTP_PORT, DEFAULT_SMTP_ENCRYPTION, DEFAULT_SMTP_FROM_NAME,
)
from services.email_worker import EMAIL_WORKER

logger = logging.getLogger(__name__)

# How long a manual "Create backup now" request waits for the snapshot before
# returning and letting it finish in the background. The common case (a small
# database) completes well within this and reports the filename synchronously;
# only a large database exceeds it, and there we return rather than hold the
# request thread open (and risk an HTTP timeout).
MANUAL_BACKUP_SYNC_WAIT_SECONDS = 20


def _listenerSessionLedger(health: dict, tz) -> dict | None:
    """The Worker Health card's listener-session entry for one user: sessions
    built since process start, plus a "when - why" line for the last rebuild
    (shown as the badge's tooltip). None when the snapshot carries no ledger
    (a Database predating it, or a test mock), so the template skips the badge
    instead of rendering zeros."""
    builds = health.get("session_builds")
    if not builds:
        return None
    parts = []
    rebuiltAt = health.get("last_rebuild_time")
    if rebuiltAt:
        try:
            parts.append(convertToDatetime(rebuiltAt, tz=tz).strftime("%Y-%m-%d %H:%M:%S"))
        except Exception:  # noqa: S110 - an unformattable timestamp costs the tooltip its
            pass           #  date, not the page
    reason = health.get("last_rebuild_reason")
    if reason:
        parts.append(reason)
    return {"builds": builds, "last_rebuild": " - ".join(parts) or None}


def register(app, dashboard):
    # The pre-bound admin flavour (see routes/_auth.py): logged-in + isAdmin,
    # or 403; anonymous redirects to login with next=/admin. This replaced 13
    # hand-rolled copies of the same four-line preamble - the exact
    # forgettable-guard situation makeRequiresUser was written to close.
    # adminRefreshLastfmEntity keeps its own guard: its login redirect targets
    # the detail page the button lives on, not /admin.
    requiresAdmin = makeRequiresUser(dashboard)(admin=True)

    def _saveClampedIntSetting(field, key, lo, hi):
        """The admin forms' shared numeric-field rule - previously four
        hand-rolled copies (Last.fm retry days, completion percent, backup
        interval/retention, tuning): read the form field, store it clamped
        into [lo, hi] via setIntSetting, and leave the stored value alone on
        a blank or unparseable input. Note form values are STRINGS, so a
        literal "0" is truthy and passes the `not raw` guard - the backup
        form's 0-means-disable fields save fine (one copy spelled the guard
        `raw is None or raw == ""` out of caution; same behavior)."""
        raw = request.form.get(field)
        if not raw:
            return
        try:
            dashboard.repo.setIntSetting(key, int(raw), lo, hi)
        except (TypeError, ValueError):
            pass

    @requiresAdmin
    def adminPage(username, db):
        """Every admin-only setting/view for the instance: the full
        users table (with per-account admin promote/demote), the 8
        feature/backfill toggles regrouped into 3 logical categories, and
        read-only instance-wide insights. Fully gated (unlike
        overviewPage, which stays visible to everyone) since there's
        nothing here for a non-admin to see."""

        users_list = []
        # One grouped scan for every user's play/skip counts instead of a
        # getPlaysCount()+getSkipCount() pair per user (2*N queries).
        countsByUser = dashboard.repo.getPlayAndSkipCountsByUser()
        for u in dashboard.repo.getAllUsersDetails():
            u_username = u["username"]
            u_email = u["email"]
            has_lastfm_key = bool(u.get("lastfm_api_key"))

            # dashboard.user_databases only holds a Database for a user with
            # an already-active session (started by their own login/usage) -
            # deliberately NOT dashboard.get_user_db(), which would construct
            # one on demand and start its listener/auto-importer/worker
            # threads (a live Spotify poll included) just to report status.
            # A user who isn't currently active is reported as "Inactive"
            # rather than paying that cost to find out.
            u_db = dashboard.user_databases.get(u_username)

            listener_sessions = None
            if u["cookies_json"]:
                if u_db is not None:
                    health = u_db.getListenerHealth()
                    sync_status = health.get("status", "UNKNOWN")
                    listener_sessions = _listenerSessionLedger(health, db.tz)
                else:
                    sync_status = "Inactive"
            else:
                sync_status = "Not Configured"

            has_api = bool(u["spotify_client_id"] and u["spotify_refresh_token"])
            needs_reauth = bool(u.get("spotify_needs_reauth"))

            # Per-user background worker statuses for the Worker Health panel.
            # consecutive_failures/failure_rate/last_error are only populated
            # for the 5 periodic workers with cycle telemetry (see
            # Database/workers/telemetry.py) - auto_importer's watchdog loop
            # lives outside Database/workers/ and has no equivalent counters.
            _telemetryDefaults = {"consecutive_failures": 0, "failure_rate": 0.0, "last_error": None}
            spotify_api_worker = {"configured": has_api, "running": False, **_telemetryDefaults}
            genre_worker = {"configured": has_lastfm_key, "running": False, **_telemetryDefaults}
            album_bio_worker = {"configured": has_lastfm_key, "running": False, **_telemetryDefaults}
            artist_bio_worker = {"configured": has_lastfm_key, "running": False, **_telemetryDefaults}
            auto_importer_worker = {"configured": True, "running": False}
            wrapped_worker = {"configured": True, "running": False, **_telemetryDefaults}

            if u_db is not None:
                try:
                    if hasattr(u_db, "getSpotifyApiWorkerStatus"):
                        st = u_db.getSpotifyApiWorkerStatus()
                        if isinstance(st, dict):
                            spotify_api_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running")),
                                                   "consecutive_failures": st.get("consecutive_failures", 0),
                                                   "failure_rate": st.get("failure_rate", 0.0),
                                                   "last_error": st.get("last_error")}
                except Exception as e:
                    logger.warning("Spotify API worker status lookup failed for %s: %s", u_username, e)

                if has_lastfm_key:
                    try:
                        workerStatus = u_db.getLastfmWorkerStatus()
                        if isinstance(workerStatus, dict):
                            genre_worker = {"configured": bool(workerStatus.get("configured")), "running": bool(workerStatus.get("running")),
                                             "consecutive_failures": workerStatus.get("consecutive_failures", 0),
                                             "failure_rate": workerStatus.get("failure_rate", 0.0),
                                             "last_error": workerStatus.get("last_error")}
                    except Exception as e:
                        logger.warning("Last.fm worker status lookup failed for %s: %s", u_username, e)

                    try:
                        if hasattr(u_db, "getLastfmAlbumBiographyWorkerStatus"):
                            st = u_db.getLastfmAlbumBiographyWorkerStatus()
                            if isinstance(st, dict):
                                album_bio_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running")),
                                                     "consecutive_failures": st.get("consecutive_failures", 0),
                                                     "failure_rate": st.get("failure_rate", 0.0),
                                                     "last_error": st.get("last_error")}
                    except Exception as e:
                        logger.warning("Last.fm album bio worker status lookup failed for %s: %s", u_username, e)

                    try:
                        if hasattr(u_db, "getLastfmBiographyWorkerStatus"):
                            st = u_db.getLastfmBiographyWorkerStatus()
                            if isinstance(st, dict):
                                artist_bio_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running")),
                                                      "consecutive_failures": st.get("consecutive_failures", 0),
                                                      "failure_rate": st.get("failure_rate", 0.0),
                                                      "last_error": st.get("last_error")}
                    except Exception as e:
                        logger.warning("Last.fm artist bio worker status lookup failed for %s: %s", u_username, e)

                try:
                    if hasattr(u_db, "getAutoImporterWorkerStatus"):
                        st = u_db.getAutoImporterWorkerStatus()
                        if isinstance(st, dict):
                            auto_importer_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running"))}
                except Exception as e:
                    logger.warning("AutoImporter worker status lookup failed for %s: %s", u_username, e)

                try:
                    if hasattr(u_db, "getWrappedWorkerStatus"):
                        st = u_db.getWrappedWorkerStatus()
                        if isinstance(st, dict):
                            wrapped_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running")),
                                               "consecutive_failures": st.get("consecutive_failures", 0),
                                               "failure_rate": st.get("failure_rate", 0.0),
                                               "last_error": st.get("last_error")}
                except Exception as e:
                    logger.warning("Wrapped worker status lookup failed for %s: %s", u_username, e)

            created_at_val = u.get("created_at")
            created_date_str = ""
            if created_at_val:
                try:
                    created_date_str = convertToDatetime(created_at_val, tz=db.tz).strftime("%Y-%m-%d %H:%M:%S")
                except Exception:  # noqa: S110 - an unformattable created_at renders as a blank
                    pass           #  cell rather than 500-ing the whole admin table

            users_list.append({
                "username": u_username,
                "email": u_email,
                "is_admin": u["is_admin"],
                "sync_status": sync_status,
                "listener_sessions": listener_sessions,
                "spotify_api_status": "Needs Re-Auth" if (has_api and needs_reauth) else ("Configured" if has_api else "Not Configured"),
                #< .get(): raw row presence check only - the stored key
                #  is encrypted and never needs decrypting here
                "lastfm_api_status": "Configured" if u.get("lastfm_api_key") else "Not Configured",
                "genre_worker": genre_worker,
                "spotify_api_worker": spotify_api_worker,
                "album_bio_worker": album_bio_worker,
                "artist_bio_worker": artist_bio_worker,
                "auto_importer_worker": auto_importer_worker,
                "wrapped_worker": wrapped_worker,
                "plays_count": countsByUser.get(u_username, {}).get("plays", 0),
                "skips_count": countsByUser.get(u_username, {}).get("skips", 0),
                "created_at": created_date_str,
            })

        listener_summary: dict[str, int] = {}
        for u in users_list:
            listener_summary[u["sync_status"]] = listener_summary.get(u["sync_status"], 0) + 1

        spotify_api_worker_summary = {"running": 0, "idle": 0, "no_key": 0, "failing": 0}
        lastfm_worker_summary = {"running": 0, "idle": 0, "no_key": 0, "failing": 0}
        lastfm_album_bio_worker_summary = {"running": 0, "idle": 0, "no_key": 0, "failing": 0}
        lastfm_artist_bio_worker_summary = {"running": 0, "idle": 0, "no_key": 0, "failing": 0}
        auto_importer_worker_summary = {"running": 0, "idle": 0}
        wrapped_worker_summary = {"running": 0, "idle": 0, "failing": 0}

        def _isFailing(w: dict) -> bool:
            return w["configured"] and w["consecutive_failures"] >= Database.WORKER_HEALTH_FAILING_THRESHOLD

        for u in users_list:
            # Spotify API Backfill
            w = u["spotify_api_worker"]
            if not w["configured"]:
                spotify_api_worker_summary["no_key"] += 1
            elif w["running"]:
                spotify_api_worker_summary["running"] += 1
            else:
                spotify_api_worker_summary["idle"] += 1
            if _isFailing(w):
                spotify_api_worker_summary["failing"] += 1

            # Last.fm Genre
            w = u["genre_worker"]
            if not w["configured"]:
                lastfm_worker_summary["no_key"] += 1
            elif w["running"]:
                lastfm_worker_summary["running"] += 1
            else:
                lastfm_worker_summary["idle"] += 1
            if _isFailing(w):
                lastfm_worker_summary["failing"] += 1

            # Last.fm Album Bio
            w = u["album_bio_worker"]
            if not w["configured"]:
                lastfm_album_bio_worker_summary["no_key"] += 1
            elif w["running"]:
                lastfm_album_bio_worker_summary["running"] += 1
            else:
                lastfm_album_bio_worker_summary["idle"] += 1
            if _isFailing(w):
                lastfm_album_bio_worker_summary["failing"] += 1

            # Last.fm Artist Bio
            w = u["artist_bio_worker"]
            if not w["configured"]:
                lastfm_artist_bio_worker_summary["no_key"] += 1
            elif w["running"]:
                lastfm_artist_bio_worker_summary["running"] += 1
            else:
                lastfm_artist_bio_worker_summary["idle"] += 1
            if _isFailing(w):
                lastfm_artist_bio_worker_summary["failing"] += 1

            # AutoImporter
            w = u["auto_importer_worker"]
            if w["running"]:
                auto_importer_worker_summary["running"] += 1
            else:
                auto_importer_worker_summary["idle"] += 1

            # Wrapped Worker
            w = u["wrapped_worker"]
            if w["running"]:
                wrapped_worker_summary["running"] += 1
            else:
                wrapped_worker_summary["idle"] += 1
            if _isFailing(w):
                wrapped_worker_summary["failing"] += 1

        # Thread liveness alone said nothing about whether backups were being
        # TAKEN: a service failing every 15-minute cycle read RUNNING like any
        # other. The worker now reports the same cycle telemetry as the
        # per-user backfillers, judged here against the same threshold.
        backupWorker = getattr(dashboard, "backupWorker", None)
        backup_worker_summary = {"status": "INACTIVE", "consecutive_failures": 0,
                                 "failure_rate": 0.0, "last_error": None}
        if backupWorker is not None:
            backup_worker_summary = backupWorker.getSummary()
        backup_worker_summary["failing"] = (
            backup_worker_summary["consecutive_failures"] >= Database.WORKER_HEALTH_FAILING_THRESHOLD)

        # Milestone detection has no thread of its own - it rides the periodic
        # login-check loop (see _detectMilestonesSafely), so its health IS that
        # thread's liveness. DISABLED reflects the admin kill switch (the pass
        # no-ops then regardless of the thread); recalc_enabled surfaces the
        # import-hygiene toggle as a warning badge, since with it off imports
        # silently stop recalculating dates / suppressing badge floods.
        loginCheckThread = getattr(dashboard, "_checkLoginThread", None)
        if not dashboard.repo.isMilestonesEnabled():
            milestone_status = "DISABLED"
        elif loginCheckThread is not None and loginCheckThread.is_alive():
            milestone_status = "RUNNING"
        else:
            milestone_status = "INACTIVE"
        milestone_worker_summary = {
            "status": milestone_status,
            "recalc_enabled": dashboard.repo.isMilestoneRecalcEnabled(),
        }

        email_worker_summary = EMAIL_WORKER.get_summary(dashboard.repo)

        # Instance-wide, not per-user: every listener and worker shares one
        # Spotify request budget because Spotify enforces its limits per IP
        # (see Database/rate_limit.py). Before this, a rate-limit event was a
        # log line nobody counted - the only way to answer "is this getting
        # worse?" was to grep app.log.
        spotify_rate_limit = SPOTIFY_LIMITER.snapshot()

        # Spotify rotates the TOTP secret the web player authenticates with, and
        # this build pins it (see Database/patches.py). A rotation takes every
        # user's session down at once and its only other trace is a log line, so
        # it belongs on the panel someone actually opens when "nothing works".
        spotify_totp = totpAuthSnapshot()

        skip_mode, skip_value = dashboard.repo.getSkipThreshold()
        restart_enabled = os.environ.get(ALLOW_INSTANCE_RESTART_ENV_VAR, "").lower() in TRUTHY_ENV_VALUES

        # Run live rather than reusing the startup probe's result: the number's
        # value is in noticing when it CHANGES, and a boot-time figure would
        # still show the old count after a repair migration had cleared it -
        # which is exactly when someone is looking. ~240 ms on a 105 MB
        # database (quick_check, not integrity_check - see checkIntegrity), on
        # an admin-only page that already does heavier work per render.
        database_integrity = dashboard.repo.checkIntegrity()
        dangling_row_total = sum(database_integrity["foreignKeyViolations"].values())

        current_tab = request.args.get("tab", "overview").lower()
        if current_tab not in ("overview", "workers", "settings"):
            current_tab = "overview"

        return render_template(
            "admin.html",
            #< a deploy that copied files over a running process, which nothing
            #  else in the app can see (see services/deploy_state.py)
            deploy_mismatch=dashboard.getDeployMismatch(),
            restart_enabled=restart_enabled,
            users_list=users_list,
            admin_count=len(dashboard.repo.getAdminUsernames()),
            spotify_backfill_enabled=dashboard.repo.isSpotifyApiBackfillEnabled(),
            push_listener_enabled=dashboard.repo.isPushListenerEnabled(),
            lastfm_backfill_enabled=dashboard.repo.isLastfmGenreBackfillEnabled(),
            sharing_enabled=dashboard.repo.isDataSharingEnabled(),
            inherited_genres_enabled=dashboard.repo.isInheritedGenresEnabled(),
            skip_mode=skip_mode,
            skip_value=skip_value,
            skip_mode_seconds=SKIP_MODE_SECONDS,
            skip_mode_percent=SKIP_MODE_PERCENT,
            skip_seconds_min=SKIP_SECONDS_MIN, skip_seconds_max=SKIP_SECONDS_MAX,
            skip_percent_min=SKIP_PERCENT_MIN, skip_percent_max=SKIP_PERCENT_MAX,
            discover_artist_limit=dashboard.repo.getDiscoverArtistLimit(RECOMMENDATION_ARTIST_LIMIT),
            image_download_workers=dashboard.repo.getImageDownloadWorkers(IMAGE_DOWNLOAD_WORKERS),
            artist_bio_workers=dashboard.repo.getArtistBioFetchWorkers(ARTIST_BIO_FETCH_WORKERS),
            album_bio_workers=dashboard.repo.getAlbumBioFetchWorkers(ALBUM_BIO_FETCH_WORKERS),
            discover_min=DISCOVER_ARTIST_LIMIT_MIN, discover_max=DISCOVER_ARTIST_LIMIT_MAX,
            worker_min=WORKER_COUNT_MIN, worker_max=WORKER_COUNT_MAX,
            completion_complete_percent=dashboard.repo.getCompletionCompletePercent(),
            completion_min=COMPLETION_COMPLETE_PERCENT_MIN, completion_max=COMPLETION_COMPLETE_PERCENT_MAX,
            email_verification_enabled=dashboard.repo.isEmailVerificationEnabled(),
            milestone_recalc_enabled=dashboard.repo.isMilestoneRecalcEnabled(),
            friends_now_playing_enabled=dashboard.repo.isFriendsNowPlayingEnabled(),
            genre_backfill_retry_days=dashboard.repo.getGenreBackfillRetryDays(),
            bio_backfill_retry_days=dashboard.repo.getBioBackfillRetryDays(),
            backfill_retry_min=BACKFILL_RETRY_DAYS_MIN, backfill_retry_max=BACKFILL_RETRY_DAYS_MAX,
            backup_interval_hours=dashboard.repo.getBackupIntervalHours(DEFAULT_BACKUP_INTERVAL_HOURS),
            backup_retention_count=dashboard.repo.getBackupRetentionCount(DEFAULT_BACKUP_RETENTION_COUNT),
            backup_interval_min=BACKUP_INTERVAL_HOURS_MIN, backup_interval_max=BACKUP_INTERVAL_HOURS_MAX,
            backup_retention_min=BACKUP_RETENTION_COUNT_MIN, backup_retention_max=BACKUP_RETENTION_COUNT_MAX,
            smtp_config=get_smtp_config(dashboard.repo),
            instance_public_url=get_instance_public_url(dashboard.repo),
            database_integrity=database_integrity,
            dangling_row_total=dangling_row_total,
            listener_summary=listener_summary,
            spotify_rate_limit=spotify_rate_limit,
            spotify_totp=spotify_totp,
            spotify_api_worker_summary=spotify_api_worker_summary,
            lastfm_worker_summary=lastfm_worker_summary,
            lastfm_album_bio_worker_summary=lastfm_album_bio_worker_summary,
            lastfm_artist_bio_worker_summary=lastfm_artist_bio_worker_summary,
            auto_importer_worker_summary=auto_importer_worker_summary,
            wrapped_worker_summary=wrapped_worker_summary,
            backup_worker_summary=backup_worker_summary,
            milestone_worker_summary=milestone_worker_summary,
            email_worker_summary=email_worker_summary,
            catalog_genre_coverage=dashboard.repo.getCatalogGenreCoverage(),
            catalog_biography_coverage=dashboard.repo.getCatalogBiographyCoverage(),
            registration_counts=dashboard.repo.getRecentRegistrationCounts(),
            instance_share_counts=dashboard.repo.getInstanceShareCounts(),
            active_share_links_count=dashboard.repo.getActiveShareLinksCount(),
            current_tab=current_tab,
            error=request.args.get("error"),
            message=request.args.get("message"),
            section="admin",
        )
    app.add_url_rule("/admin", "adminPage", adminPage, methods=["GET"])

    @requiresAdmin
    def adminEmailSettings(username, db):
        """Admin-only: update instance-wide SMTP configuration, the global
        email notifications toggle, and the public URL notification emails
        link back to."""
        enabled = request.form.get("email_notifications_enabled") == "1"
        host = request.form.get("smtp_host", "")
        try:
            port = int(request.form.get("smtp_port", str(DEFAULT_SMTP_PORT)))
        except (ValueError, TypeError):
            port = DEFAULT_SMTP_PORT
        encryption = request.form.get("smtp_encryption", DEFAULT_SMTP_ENCRYPTION)
        user = request.form.get("smtp_user", "")
        raw_password = request.form.get("smtp_password", "")
        clear_password = request.form.get("clear_password") == "1"
        # None = keep existing encrypted value; "" = intentional clear; non-empty = update
        if clear_password:
            password = ""  # triggers explicit clear in save_smtp_config
        elif raw_password:
            password = raw_password
        else:
            password = None  # blank field submitted without clear → keep existing
        from_email = request.form.get("smtp_from_email", "")
        from_name = request.form.get("smtp_from_name", DEFAULT_SMTP_FROM_NAME)
        public_url = request.form.get("instance_public_url", "")

        save_smtp_config(
            repo=dashboard.repo,
            enabled=enabled,
            host=host,
            port=port,
            encryption=encryption,
            user=user,
            password=password,
            from_email=from_email,
            from_name=from_name,
        )
        save_instance_public_url(dashboard.repo, public_url)
        return redirect(url_for("adminPage", tab="settings", message="Email notification settings saved."))
    app.add_url_rule("/admin/email_settings", "adminEmailSettings", adminEmailSettings, methods=["POST"])

    @requiresAdmin
    def adminTestEmail(username, db):
        """Admin-only: send a test email to the current admin account."""
        #< requiresAdmin deliberately doesn't pass the email through (it's
        #  guard-only everywhere else); this one view actually sends to it
        email = dashboard.repo.getEmailForUsername(username)
        success, err = send_test_email(dashboard.repo, email)
        if success:
            return redirect(url_for("adminPage", tab="settings", message=f"Test email successfully sent to {email}."))
        else:
            return redirect(url_for("adminPage", tab="settings", error=f"Test email failed: {err}"))
    app.add_url_rule("/admin/test_email", "adminTestEmail", adminTestEmail, methods=["POST"])

    @requiresAdmin
    def adminUserSettings(username, db):
        """Admin-only: instance-wide toggles for data sharing (Compare +
        share requests), new user registration, public Wrapped share links,
        achievement milestones, automatic milestone-date recalculation, and
        the personal tagging system (tag panel, tag filters, Playlists page) -
        see Database/repository.py's app_settings."""
        # Unchecked checkboxes aren't submitted: absence means disable.
        dashboard.repo.setDataSharingEnabled(request.form.get("data_sharing") == "1")
        dashboard.repo.setRegistrationEnabled(request.form.get("registration") == "1")
        dashboard.repo.setShareLinksEnabled(request.form.get("share_links") == "1")
        dashboard.repo.setEmailVerificationEnabled(request.form.get("email_verification") == "1")
        dashboard.repo.setMilestonesEnabled(request.form.get("milestones") == "1")
        dashboard.repo.setMilestoneRecalcEnabled(request.form.get("milestone_recalc") == "1")
        dashboard.repo.setTagsEnabled(request.form.get("tags") == "1")
        dashboard.repo.setFriendsNowPlayingEnabled(request.form.get("friends_now_playing") == "1")
        return redirect(url_for("adminPage", tab="settings", message="User settings saved."))
    app.add_url_rule("/admin/user_settings", "adminUserSettings", adminUserSettings, methods=["POST"])

    @requiresAdmin
    def adminLastfmSettings(username, db):
        """Admin-only: Last.fm genre backfill, artist/album biography
        backfill, and whether inherited (artist-derived) genre rows count
        in genre stats and coverage - see Database/repository.py's
        app_settings."""
        dashboard.repo.setLastfmGenreBackfillEnabled(request.form.get("lastfm_backfill") == "1")
        dashboard.repo.setArtistBioEnabled(request.form.get("artist_bio") == "1")
        dashboard.repo.setAlbumBioEnabled(request.form.get("album_bio") == "1")
        dashboard.repo.setInheritedGenresEnabled(request.form.get("include_inherited") == "1")
        # Backfill retry intervals (days) for the empty-result re-attempt gate.
        for field, key in (("genre_backfill_retry_days", GENRE_BACKFILL_RETRY_DAYS_KEY),
                           ("bio_backfill_retry_days", BIO_BACKFILL_RETRY_DAYS_KEY)):
            _saveClampedIntSetting(field, key, BACKFILL_RETRY_DAYS_MIN, BACKFILL_RETRY_DAYS_MAX)
        return redirect(url_for("adminPage", tab="workers", message="Last.fm settings saved."))
    app.add_url_rule("/admin/lastfm_settings", "adminLastfmSettings", adminLastfmSettings, methods=["POST"])

    def adminRefreshLastfmEntity(kind, entity_id):
        """Admin-only: force a fresh Last.fm lookup for one artist/album/
        track (the detail pages' "Refresh Last.fm Data" button) - see
        Database.refreshLastfmEntity for what "fresh" bypasses."""
        routeByKind = {"artist": "artistDetailPage", "album": "albumDetailPage",
                      "track": "songDetailPage"}
        idKwargByKind = {"artist": "artist_id", "album": "album_id", "track": "track_id"}
        if kind not in routeByKind:
            abort(404)
        detailRoute = routeByKind[kind]
        idKwarg = idKwargByKind[kind]

        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for(detailRoute, **{idKwarg: entity_id})))
        if not dashboard.repo.isAdmin(username):
            abort(403)

        result = db.refreshLastfmEntity(kind, entity_id)
        STATUS_MESSAGES = {
            "no_api_key": ("error", "Add a Last.fm API key on your profile to refresh Last.fm data."),
            "invalid_key": ("error", "Your stored Last.fm API key was rejected by Last.fm."),
            "not_found": ("error", "Couldn't find this item to refresh."),
            "no_artist": ("error", "Couldn't determine this album's artist."),
            "transient": ("error", "Last.fm didn't respond - try again in a moment."),
            "ok": ("success", f"Refreshed Last.fm data for “{result.get('name', '')}”."),
        }
        messageKind, message = STATUS_MESSAGES[result["status"]]

        # The detail pages submit this form via fetch (static/js/admin-refresh.js)
        # so a refresh doesn't navigate away and reset tab/sort/page state; the
        # redirect below stays as the no-JS fallback.
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify(kind=messageKind, message=message)

        redirectArgs = {idKwarg: entity_id, messageKind: message}
        groupBy = request.form.get("groupBy")
        if groupBy:
            redirectArgs["groupBy"] = groupBy
        return redirect(url_for(detailRoute, **redirectArgs))
    app.add_url_rule("/admin/lastfm/refresh/<kind>/<entity_id>", "adminRefreshLastfmEntity", adminRefreshLastfmEntity, methods=["POST"])

    @requiresAdmin
    def adminSpotifySettings(username, db):
        """Admin-only: the Spotify Developer API backfill kill switch
        (missed-plays recovery and album/track metadata fetching)."""
        dashboard.repo.setSpotifyApiBackfillEnabled(request.form.get("spotify_backfill") == "1")
        # Read once per listener build, so this takes effect on the next
        # rebuild rather than mid-stream - say so instead of implying it is live.
        dashboard.repo.setPushListenerEnabled(request.form.get("push_listener") == "1")
        return redirect(url_for("adminPage", tab="workers",
                                message="Spotify settings saved. Push mode applies when each listener next restarts."))
    app.add_url_rule("/admin/spotify_settings", "adminSpotifySettings", adminSpotifySettings, methods=["POST"])

    @requiresAdmin
    def adminSkipSettings(username, db):
        """Admin-only: the instance-wide skip threshold (a plain seconds value
        or a percent of each track's duration). Saving recomputes plays.is_skip
        across every user's history via recomputeSkipFlags(), so all skip vs
        real-play stats reflect the new boundary immediately."""
        mode = request.form.get("skip_mode", SKIP_MODE_SECONDS)
        if mode not in (SKIP_MODE_SECONDS, SKIP_MODE_PERCENT):
            mode = SKIP_MODE_SECONDS
        try:
            value = int(request.form.get("skip_value", ""))
        except (TypeError, ValueError):
            return redirect(url_for("adminPage", tab="settings", error="Skip threshold must be a whole number."))
        # Both settings are stored BEFORE the recompute, because both are inputs
        # to it: computeIsSkip caps its threshold at the completion boundary
        # (73e1a2c), so recomputing first would classify every row under the old
        # completion percent and leave the flag on disk disagreeing with the
        # classifier - the same "complete and abandoned at once" contradiction
        # that cap was added to remove - until someone saved this form twice.
        # Lenient on a blank/bad completion value, as before.
        dashboard.repo.setSkipThreshold(mode, value)   #< clamps to the mode's bounds
        _saveClampedIntSetting("completion_complete_percent", COMPLETION_COMPLETE_PERCENT_KEY,
                               COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX)
        dashboard.repo.recomputeSkipFlags()             #< self-commits; reclassifies every play
        return redirect(url_for("adminPage", tab="settings", message="Playback classification settings saved."))
    app.add_url_rule("/admin/skip_settings", "adminSkipSettings", adminSkipSettings, methods=["POST"])

    @requiresAdmin
    def adminBackupSettings(username, db):
        """Admin-only: automatic-backup interval (hours) and retention (count),
        0 to disable either. Read when the BackupWorker is constructed, so a
        change applies after the app restarts."""
        for field, key, lo, hi in (
            ("backup_interval_hours", BACKUP_INTERVAL_HOURS_KEY, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX),
            ("backup_retention_count", BACKUP_RETENTION_COUNT_KEY, BACKUP_RETENTION_COUNT_MIN, BACKUP_RETENTION_COUNT_MAX),
        ):
            #< "0" (disable) is a truthy string, so it saves - see the helper
            _saveClampedIntSetting(field, key, lo, hi)
        return redirect(url_for("adminPage", tab="settings", message="Backup settings saved."))
    app.add_url_rule("/admin/backup_settings", "adminBackupSettings", adminBackupSettings, methods=["POST"])

    @requiresAdmin
    def adminCreateBackup(username, db):
        """Admin-only: trigger an immediate on-demand database backup. Runs
        unconditionally even if scheduled automatic backups are disabled.

        The snapshot runs on a background thread so a large database can't tie
        up the request thread (and risk an HTTP timeout): the request waits up
        to MANUAL_BACKUP_SYNC_WAIT_SECONDS for the fast common case - reporting
        the snapshot filename (or the failure) synchronously - and otherwise
        returns immediately, leaving the backup to finish in the background.
        Returns JSON when requested via AJAX, or redirects to /admin."""

        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        def respond(kind, message):
            if is_ajax:
                return jsonify(kind=kind, message=message)
            key = "message" if kind == "success" else "error"
            return redirect(url_for("adminPage", tab="settings", **{key: message}))

        backup_worker = getattr(dashboard, "backupWorker", None)
        if backup_worker is None:
            return respond("error", "Backup worker not available.")

        # Fast, friendly rejection for a second click. The real mutual
        # exclusion lives in runBackup itself, which also covers the scheduled
        # worker - this check alone couldn't, since the scheduler can start
        # between the check and the thread below.
        if backup_worker.isBackupRunning():
            return respond("error", "A backup is already in progress.")

        result = {}

        def _run():
            try:
                result["path"] = backup_worker.runBackup()
            except Exception as e:
                # Logged here so a failure that outlives the synchronous wait
                # (a slow backup that then errors) still reaches the log.
                result["error"] = e
                logger.error("Manual database backup failed: %s", e)

        thread = threading.Thread(target=_run, name="manual-backup", daemon=True)
        thread.start()
        thread.join(MANUAL_BACKUP_SYNC_WAIT_SECONDS)

        if thread.is_alive():
            # Still running - return rather than hold the request open; the
            # worker's lock stays held until the snapshot finishes, so a
            # follow-up click gets "already in progress" until then.
            return respond("success", "Backup started - a large database can take a while, "
                                      "so the snapshot will appear in the backups folder shortly.")

        if "error" in result:
            return respond("error", f"Backup failed: {result['error']}")

        backup_path = result.get("path")
        filename = getattr(backup_path, "name", str(backup_path))
        return respond("success", f"Database snapshot created: {filename}")
    app.add_url_rule("/admin/create_backup", "adminCreateBackup", adminCreateBackup, methods=["POST"])

    @requiresAdmin
    def adminTuningSettings(username, db):
        """Admin-only: numeric tunables migrated out of code constants. The
        Discover artist count is read live per request; the worker pool sizes
        apply only after a restart (see Database.configureWorkerPools). Each
        value is clamped to its bounds; a blank/unparseable field is left as-is."""

        _saveClampedIntSetting("discover_artist_limit", DISCOVER_ARTIST_LIMIT_KEY, DISCOVER_ARTIST_LIMIT_MIN, DISCOVER_ARTIST_LIMIT_MAX)
        _saveClampedIntSetting("image_download_workers", IMAGE_DOWNLOAD_WORKERS_KEY, WORKER_COUNT_MIN, WORKER_COUNT_MAX)
        _saveClampedIntSetting("artist_bio_workers", ARTIST_BIO_FETCH_WORKERS_KEY, WORKER_COUNT_MIN, WORKER_COUNT_MAX)
        _saveClampedIntSetting("album_bio_workers", ALBUM_BIO_FETCH_WORKERS_KEY, WORKER_COUNT_MIN, WORKER_COUNT_MAX)
        return redirect(url_for("adminPage", tab="workers", message="Advanced tuning settings saved."))
    app.add_url_rule("/admin/tuning_settings", "adminTuningSettings", adminTuningSettings, methods=["POST"])

    @requiresAdmin
    def adminRestart(username, db):
        """Admin-only: gracefully stop every worker and exit so a SUPERVISING
        launch script relaunches the process - the only way restart-only
        settings (worker pool sizes) take effect. Gated behind
        ALLOW_INSTANCE_RESTART so it can't be triggered on a bare, unsupervised
        process, which would just stop the app. The exit is deferred by
        INSTANCE_RESTART_DELAY_SECONDS so this response reaches the browser
        first; threading.Timer is the testable seam (no real os._exit under
        test, which patches it)."""
        if os.environ.get(ALLOW_INSTANCE_RESTART_ENV_VAR, "").lower() not in TRUTHY_ENV_VALUES:
            return redirect(url_for("adminPage", tab="settings",
                error="Instance restart is disabled. Set ALLOW_INSTANCE_RESTART=1 in a supervised launch to enable it."))

        def _gracefulExit():
            try:
                dashboard.shutdown()
            finally:
                os._exit(0)
        threading.Timer(INSTANCE_RESTART_DELAY_SECONDS, _gracefulExit).start()
        # `message`, not `error`: the restart was accepted, so /admin should
        # show it in the informational banner rather than the red error one.
        return redirect(url_for("adminPage", tab="settings",
            message="Restarting now - the app will be back in a few seconds if the process is supervised."))
    app.add_url_rule("/admin/restart", "adminRestart", adminRestart, methods=["POST"])

    @requiresAdmin
    def adminSetUserAdmin(actingUsername, db, username):
        """Admin-only: promote/demote a user's admin status. Demotion goes
        through Repository.demoteAdmin, which atomically refuses to remove the
        last remaining admin (setUserAdmin otherwise happily allows zero admins,
        stranding the instance with nobody able to reach any admin-gated
        surface). The block is raised only when the target actually IS that last
        admin - demoting a non-admin is a harmless no-op, not an error."""
        tab = request.args.get("tab") or request.form.get("tab") or "overview"
        makeAdmin = request.form.get("make_admin") == "1"
        if makeAdmin:
            dashboard.repo.setUserAdmin(username, True)
            return redirect(url_for("adminPage", tab=tab))
        # Demotion: demoteAdmin returns False both when the target was never an
        # admin (nothing to do) and when it's the last admin (must be blocked) -
        # only the latter is an error, so re-check admin status to tell them
        # apart.
        if not dashboard.repo.demoteAdmin(username) and dashboard.repo.isAdmin(username):
            return redirect(url_for("adminPage", tab=tab, error="Cannot remove the instance's last admin."))
        return redirect(url_for("adminPage", tab=tab))
    app.add_url_rule("/admin/users/<username>/admin", "adminSetUserAdmin", adminSetUserAdmin, methods=["POST"])

