"""Admin-only routes: the /admin console and its settings/action endpoints.

Extracted verbatim from app.py. Every handler is fully gated on
Repository.isAdmin. register(app, dashboard) wires them via app.add_url_rule
under their original endpoint names.
"""
import logging
import os
import threading

from flask import render_template, redirect, request, url_for, abort, jsonify

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
from Database.utils import convertToDatetime

logger = logging.getLogger(__name__)


def register(app, dashboard):
    def adminPage():
        """Every admin-only setting/view for the instance: the full
        users table (with per-account admin promote/demote), the 8
        feature/backfill toggles regrouped into 3 logical categories, and
        read-only instance-wide insights. Fully gated (unlike
        overviewPage, which stays visible to everyone) since there's
        nothing here for a non-admin to see."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("adminPage")))
        if not dashboard.repo.isAdmin(username):
            abort(403)

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

            if u["cookies_json"]:
                if u_db is not None:
                    health = u_db.getListenerHealth()
                    sync_status = health.get("status", "UNKNOWN")
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
                except Exception:
                    pass

            users_list.append({
                "username": u_username,
                "email": u_email,
                "is_admin": u["is_admin"],
                "sync_status": sync_status,
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

        backup_worker_running = False
        if hasattr(dashboard, "backupWorker") and dashboard.backupWorker is not None:
            th = getattr(dashboard.backupWorker, "thread", None)
            if th is not None:
                backup_worker_running = th.is_alive()
            elif hasattr(dashboard.backupWorker, "is_alive"):
                backup_worker_running = dashboard.backupWorker.is_alive()

        backup_worker_summary = {"status": "RUNNING" if backup_worker_running else "INACTIVE"}

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

        skip_mode, skip_value = dashboard.repo.getSkipThreshold()
        restart_enabled = os.environ.get(ALLOW_INSTANCE_RESTART_ENV_VAR, "").lower() in TRUTHY_ENV_VALUES

        return render_template(
            "admin.html",
            restart_enabled=restart_enabled,
            users_list=users_list,
            admin_count=len(dashboard.repo.getAdminUsernames()),
            spotify_backfill_enabled=dashboard.repo.isSpotifyApiBackfillEnabled(),
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
            genre_backfill_retry_days=dashboard.repo.getGenreBackfillRetryDays(),
            bio_backfill_retry_days=dashboard.repo.getBioBackfillRetryDays(),
            backfill_retry_min=BACKFILL_RETRY_DAYS_MIN, backfill_retry_max=BACKFILL_RETRY_DAYS_MAX,
            backup_interval_hours=dashboard.repo.getBackupIntervalHours(DEFAULT_BACKUP_INTERVAL_HOURS),
            backup_retention_count=dashboard.repo.getBackupRetentionCount(DEFAULT_BACKUP_RETENTION_COUNT),
            backup_interval_min=BACKUP_INTERVAL_HOURS_MIN, backup_interval_max=BACKUP_INTERVAL_HOURS_MAX,
            backup_retention_min=BACKUP_RETENTION_COUNT_MIN, backup_retention_max=BACKUP_RETENTION_COUNT_MAX,
            listener_summary=listener_summary,
            spotify_api_worker_summary=spotify_api_worker_summary,
            lastfm_worker_summary=lastfm_worker_summary,
            lastfm_album_bio_worker_summary=lastfm_album_bio_worker_summary,
            lastfm_artist_bio_worker_summary=lastfm_artist_bio_worker_summary,
            auto_importer_worker_summary=auto_importer_worker_summary,
            wrapped_worker_summary=wrapped_worker_summary,
            backup_worker_summary=backup_worker_summary,
            milestone_worker_summary=milestone_worker_summary,
            catalog_genre_coverage=dashboard.repo.getCatalogGenreCoverage(),
            catalog_biography_coverage=dashboard.repo.getCatalogBiographyCoverage(),
            registration_counts=dashboard.repo.getRecentRegistrationCounts(),
            instance_share_counts=dashboard.repo.getInstanceShareCounts(),
            active_share_links_count=dashboard.repo.getActiveShareLinksCount(),
            error=request.args.get("error"),
            message=request.args.get("message"),
            section="admin",
        )
    app.add_url_rule("/admin", "adminPage", adminPage, methods=["GET"])

    def adminUserSettings():
        """Admin-only: instance-wide toggles for data sharing (Compare +
        share requests), new user registration, public Wrapped share links,
        achievement milestones, and automatic milestone-date recalculation -
        see Database/repository.py's app_settings."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("adminPage")))
        if not dashboard.repo.isAdmin(username):
            abort(403)
        # Unchecked checkboxes aren't submitted: absence means disable.
        dashboard.repo.setDataSharingEnabled(request.form.get("data_sharing") == "1")
        dashboard.repo.setRegistrationEnabled(request.form.get("registration") == "1")
        dashboard.repo.setShareLinksEnabled(request.form.get("share_links") == "1")
        dashboard.repo.setEmailVerificationEnabled(request.form.get("email_verification") == "1")
        dashboard.repo.setMilestonesEnabled(request.form.get("milestones") == "1")
        dashboard.repo.setMilestoneRecalcEnabled(request.form.get("milestone_recalc") == "1")
        return redirect(url_for("adminPage"))
    app.add_url_rule("/admin/user_settings", "adminUserSettings", adminUserSettings, methods=["POST"])

    def adminLastfmSettings():
        """Admin-only: Last.fm genre backfill, artist/album biography
        backfill, and whether inherited (artist-derived) genre rows count
        in genre stats and coverage - see Database/repository.py's
        app_settings."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("adminPage")))
        if not dashboard.repo.isAdmin(username):
            abort(403)
        dashboard.repo.setLastfmGenreBackfillEnabled(request.form.get("lastfm_backfill") == "1")
        dashboard.repo.setArtistBioEnabled(request.form.get("artist_bio") == "1")
        dashboard.repo.setAlbumBioEnabled(request.form.get("album_bio") == "1")
        dashboard.repo.setInheritedGenresEnabled(request.form.get("include_inherited") == "1")
        # Backfill retry intervals (days) for the empty-result re-attempt gate.
        for field, key in (("genre_backfill_retry_days", GENRE_BACKFILL_RETRY_DAYS_KEY),
                           ("bio_backfill_retry_days", BIO_BACKFILL_RETRY_DAYS_KEY)):
            raw = request.form.get(field)
            if raw:
                try:
                    dashboard.repo.setIntSetting(key, int(raw), BACKFILL_RETRY_DAYS_MIN, BACKFILL_RETRY_DAYS_MAX)
                except (TypeError, ValueError):
                    pass
        return redirect(url_for("adminPage"))
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

    def adminSpotifySettings():
        """Admin-only: the Spotify Developer API backfill kill switch
        (missed-plays recovery and album/track metadata fetching)."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("adminPage")))
        if not dashboard.repo.isAdmin(username):
            abort(403)
        dashboard.repo.setSpotifyApiBackfillEnabled(request.form.get("spotify_backfill") == "1")
        return redirect(url_for("adminPage"))
    app.add_url_rule("/admin/spotify_settings", "adminSpotifySettings", adminSpotifySettings, methods=["POST"])

    def adminSkipSettings():
        """Admin-only: the instance-wide skip threshold (a plain seconds value
        or a percent of each track's duration). Saving recomputes plays.is_skip
        across every user's history via recomputeSkipFlags(), so all skip vs
        real-play stats reflect the new boundary immediately."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("adminPage")))
        if not dashboard.repo.isAdmin(username):
            abort(403)
        mode = request.form.get("skip_mode", SKIP_MODE_SECONDS)
        if mode not in (SKIP_MODE_SECONDS, SKIP_MODE_PERCENT):
            mode = SKIP_MODE_SECONDS
        try:
            value = int(request.form.get("skip_value", ""))
        except (TypeError, ValueError):
            return redirect(url_for("adminPage", error="Skip threshold must be a whole number."))
        dashboard.repo.setSkipThreshold(mode, value)   #< clamps to the mode's bounds
        dashboard.repo.recomputeSkipFlags()             #< self-commits; reclassifies every play
        # Completion complete-percent (live; no recompute needed) - the second
        # half of the completion pie. Lenient on a blank/bad value.
        raw = request.form.get("completion_complete_percent")
        if raw:
            try:
                dashboard.repo.setIntSetting(COMPLETION_COMPLETE_PERCENT_KEY, int(raw),
                                             COMPLETION_COMPLETE_PERCENT_MIN, COMPLETION_COMPLETE_PERCENT_MAX)
            except (TypeError, ValueError):
                pass
        return redirect(url_for("adminPage"))
    app.add_url_rule("/admin/skip_settings", "adminSkipSettings", adminSkipSettings, methods=["POST"])

    def adminBackupSettings():
        """Admin-only: automatic-backup interval (hours) and retention (count),
        0 to disable either. Read when the BackupWorker is constructed, so a
        change applies after the app restarts."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("adminPage")))
        if not dashboard.repo.isAdmin(username):
            abort(403)
        for field, key, lo, hi in (
            ("backup_interval_hours", BACKUP_INTERVAL_HOURS_KEY, BACKUP_INTERVAL_HOURS_MIN, BACKUP_INTERVAL_HOURS_MAX),
            ("backup_retention_count", BACKUP_RETENTION_COUNT_KEY, BACKUP_RETENTION_COUNT_MIN, BACKUP_RETENTION_COUNT_MAX),
        ):
            raw = request.form.get(field)
            if raw is None or raw == "":
                continue   #< allow "0" (disable); only skip a truly empty field
            try:
                dashboard.repo.setIntSetting(key, int(raw), lo, hi)
            except (TypeError, ValueError):
                pass
        return redirect(url_for("adminPage"))
    app.add_url_rule("/admin/backup_settings", "adminBackupSettings", adminBackupSettings, methods=["POST"])

    def adminCreateBackup():
        """Admin-only: trigger an immediate on-demand database backup. Runs
        unconditionally even if scheduled automatic backups are disabled.
        Returns JSON when requested via AJAX, or redirects to /admin."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("adminPage")))
        if not dashboard.repo.isAdmin(username):
            abort(403)

        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"

        backup_worker = getattr(dashboard, "backupWorker", None)
        if backup_worker is None:
            message = "Backup worker not available."
            if is_ajax:
                return jsonify(kind="error", message=message)
            return redirect(url_for("adminPage", error=message))

        try:
            backup_path = backup_worker.runBackup()
            filename = getattr(backup_path, "name", str(backup_path))
            message = f"Database snapshot created: {filename}"
            if is_ajax:
                return jsonify(kind="success", message=message)
            return redirect(url_for("adminPage", message=message))
        except Exception as e:
            logger.error("Manual database backup failed: %s", e)
            message = f"Backup failed: {e}"
            if is_ajax:
                return jsonify(kind="error", message=message)
            return redirect(url_for("adminPage", error=message))
    app.add_url_rule("/admin/create_backup", "adminCreateBackup", adminCreateBackup, methods=["POST"])

    def adminTuningSettings():
        """Admin-only: numeric tunables migrated out of code constants. The
        Discover artist count is read live per request; the worker pool sizes
        apply only after a restart (see Database.configureWorkerPools). Each
        value is clamped to its bounds; a blank/unparseable field is left as-is."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("adminPage")))
        if not dashboard.repo.isAdmin(username):
            abort(403)

        def _save(field, key, lo, hi):
            raw = request.form.get(field)
            if not raw:
                return
            try:
                dashboard.repo.setIntSetting(key, int(raw), lo, hi)
            except (TypeError, ValueError):
                pass

        _save("discover_artist_limit", DISCOVER_ARTIST_LIMIT_KEY, DISCOVER_ARTIST_LIMIT_MIN, DISCOVER_ARTIST_LIMIT_MAX)
        _save("image_download_workers", IMAGE_DOWNLOAD_WORKERS_KEY, WORKER_COUNT_MIN, WORKER_COUNT_MAX)
        _save("artist_bio_workers", ARTIST_BIO_FETCH_WORKERS_KEY, WORKER_COUNT_MIN, WORKER_COUNT_MAX)
        _save("album_bio_workers", ALBUM_BIO_FETCH_WORKERS_KEY, WORKER_COUNT_MIN, WORKER_COUNT_MAX)
        return redirect(url_for("adminPage"))
    app.add_url_rule("/admin/tuning_settings", "adminTuningSettings", adminTuningSettings, methods=["POST"])

    def adminRestart():
        """Admin-only: gracefully stop every worker and exit so a SUPERVISING
        launch script relaunches the process - the only way restart-only
        settings (worker pool sizes) take effect. Gated behind
        ALLOW_INSTANCE_RESTART so it can't be triggered on a bare, unsupervised
        process, which would just stop the app. The exit is deferred by
        INSTANCE_RESTART_DELAY_SECONDS so this response reaches the browser
        first; threading.Timer is the testable seam (no real os._exit under
        test, which patches it)."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("adminPage")))
        if not dashboard.repo.isAdmin(username):
            abort(403)
        if os.environ.get(ALLOW_INSTANCE_RESTART_ENV_VAR, "").lower() not in TRUTHY_ENV_VALUES:
            return redirect(url_for("adminPage",
                error="Instance restart is disabled. Set ALLOW_INSTANCE_RESTART=1 in a supervised launch to enable it."))

        def _gracefulExit():
            try:
                dashboard.shutdown()
            finally:
                os._exit(0)
        threading.Timer(INSTANCE_RESTART_DELAY_SECONDS, _gracefulExit).start()
        return redirect(url_for("adminPage",
            error="Restarting now - the app will be back in a few seconds if the process is supervised."))
    app.add_url_rule("/admin/restart", "adminRestart", adminRestart, methods=["POST"])

    def adminSetUserAdmin(username):
        """Admin-only: promote/demote a user's admin status. Refuses to
        demote the instance's last remaining admin - Repository.setUserAdmin
        otherwise happily allows zero admins, which would strand the
        instance with nobody able to reach any admin-gated surface."""
        email, actingUsername, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("adminPage")))
        if not dashboard.repo.isAdmin(actingUsername):
            abort(403)
        makeAdmin = request.form.get("make_admin") == "1"
        if not makeAdmin and len(dashboard.repo.getAdminUsernames()) <= 1:
            return redirect(url_for("adminPage", error="Cannot remove the instance's last admin."))
        dashboard.repo.setUserAdmin(username, makeAdmin)
        return redirect(url_for("adminPage"))
    app.add_url_rule("/admin/users/<username>/admin", "adminSetUserAdmin", adminSetUserAdmin, methods=["POST"])
