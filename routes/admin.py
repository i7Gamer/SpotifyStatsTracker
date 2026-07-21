"""Admin-only routes: the /admin console and its settings/action endpoints.

Extracted verbatim from app.py. Every handler is fully gated on
Repository.isAdmin. register(app, dashboard) wires them via app.add_url_rule
under their original endpoint names.
"""
import logging

from flask import render_template, redirect, request, url_for, abort

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

            # Per-user background worker statuses for the Worker Health panel
            spotify_api_worker = {"configured": has_api, "running": False}
            genre_worker = {"configured": has_lastfm_key, "running": False}
            album_bio_worker = {"configured": has_lastfm_key, "running": False}
            artist_bio_worker = {"configured": has_lastfm_key, "running": False}
            auto_importer_worker = {"configured": True, "running": False}
            wrapped_worker = {"configured": True, "running": False}

            if u_db is not None:
                try:
                    if hasattr(u_db, "getSpotifyApiWorkerStatus"):
                        st = u_db.getSpotifyApiWorkerStatus()
                        if isinstance(st, dict):
                            spotify_api_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running"))}
                except Exception as e:
                    logger.warning("Spotify API worker status lookup failed for %s: %s", u_username, e)

                if has_lastfm_key:
                    try:
                        workerStatus = u_db.getLastfmWorkerStatus()
                        if isinstance(workerStatus, dict):
                            genre_worker = {"configured": bool(workerStatus.get("configured")), "running": bool(workerStatus.get("running"))}
                    except Exception as e:
                        logger.warning("Last.fm worker status lookup failed for %s: %s", u_username, e)

                    try:
                        if hasattr(u_db, "getLastfmAlbumBiographyWorkerStatus"):
                            st = u_db.getLastfmAlbumBiographyWorkerStatus()
                            if isinstance(st, dict):
                                album_bio_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running"))}
                    except Exception as e:
                        logger.warning("Last.fm album bio worker status lookup failed for %s: %s", u_username, e)

                    try:
                        if hasattr(u_db, "getLastfmBiographyWorkerStatus"):
                            st = u_db.getLastfmBiographyWorkerStatus()
                            if isinstance(st, dict):
                                artist_bio_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running"))}
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
                            wrapped_worker = {"configured": bool(st.get("configured")), "running": bool(st.get("running"))}
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
                "plays_count": dashboard.repo.getPlaysCount(u_username),
                "skips_count": dashboard.repo.getSkipCount(u_username),
                "created_at": created_date_str,
            })

        listener_summary: dict[str, int] = {}
        for u in users_list:
            listener_summary[u["sync_status"]] = listener_summary.get(u["sync_status"], 0) + 1

        spotify_api_worker_summary = {"running": 0, "idle": 0, "no_key": 0}
        lastfm_worker_summary = {"running": 0, "idle": 0, "no_key": 0}
        lastfm_album_bio_worker_summary = {"running": 0, "idle": 0, "no_key": 0}
        lastfm_artist_bio_worker_summary = {"running": 0, "idle": 0, "no_key": 0}
        auto_importer_worker_summary = {"running": 0, "idle": 0}
        wrapped_worker_summary = {"running": 0, "idle": 0}

        for u in users_list:
            # Spotify API Backfill
            w = u["spotify_api_worker"]
            if not w["configured"]:
                spotify_api_worker_summary["no_key"] += 1
            elif w["running"]:
                spotify_api_worker_summary["running"] += 1
            else:
                spotify_api_worker_summary["idle"] += 1

            # Last.fm Genre
            w = u["genre_worker"]
            if not w["configured"]:
                lastfm_worker_summary["no_key"] += 1
            elif w["running"]:
                lastfm_worker_summary["running"] += 1
            else:
                lastfm_worker_summary["idle"] += 1

            # Last.fm Album Bio
            w = u["album_bio_worker"]
            if not w["configured"]:
                lastfm_album_bio_worker_summary["no_key"] += 1
            elif w["running"]:
                lastfm_album_bio_worker_summary["running"] += 1
            else:
                lastfm_album_bio_worker_summary["idle"] += 1

            # Last.fm Artist Bio
            w = u["artist_bio_worker"]
            if not w["configured"]:
                lastfm_artist_bio_worker_summary["no_key"] += 1
            elif w["running"]:
                lastfm_artist_bio_worker_summary["running"] += 1
            else:
                lastfm_artist_bio_worker_summary["idle"] += 1

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

        backup_worker_running = False
        if hasattr(dashboard, "backupWorker") and dashboard.backupWorker is not None:
            th = getattr(dashboard.backupWorker, "thread", None)
            if th is not None:
                backup_worker_running = th.is_alive()
            elif hasattr(dashboard.backupWorker, "is_alive"):
                backup_worker_running = dashboard.backupWorker.is_alive()

        backup_worker_summary = {"status": "RUNNING" if backup_worker_running else "INACTIVE"}

        return render_template(
            "admin.html",
            users_list=users_list,
            admin_count=len(dashboard.repo.getAdminUsernames()),
            spotify_backfill_enabled=dashboard.repo.isSpotifyApiBackfillEnabled(),
            lastfm_backfill_enabled=dashboard.repo.isLastfmGenreBackfillEnabled(),
            sharing_enabled=dashboard.repo.isDataSharingEnabled(),
            inherited_genres_enabled=dashboard.repo.isInheritedGenresEnabled(),
            listener_summary=listener_summary,
            spotify_api_worker_summary=spotify_api_worker_summary,
            lastfm_worker_summary=lastfm_worker_summary,
            lastfm_album_bio_worker_summary=lastfm_album_bio_worker_summary,
            lastfm_artist_bio_worker_summary=lastfm_artist_bio_worker_summary,
            auto_importer_worker_summary=auto_importer_worker_summary,
            wrapped_worker_summary=wrapped_worker_summary,
            backup_worker_summary=backup_worker_summary,
            catalog_genre_coverage=dashboard.repo.getCatalogGenreCoverage(),
            catalog_biography_coverage=dashboard.repo.getCatalogBiographyCoverage(),
            registration_counts=dashboard.repo.getRecentRegistrationCounts(),
            instance_share_counts=dashboard.repo.getInstanceShareCounts(),
            active_share_links_count=dashboard.repo.getActiveShareLinksCount(),
            error=request.args.get("error"),
            section="admin",
        )
    app.add_url_rule("/admin", "adminPage", adminPage, methods=["GET"])

    def adminUserSettings():
        """Admin-only: instance-wide toggles for data sharing (Compare +
        share requests), new user registration, and public Wrapped share
        links - see Database/repository.py's app_settings."""
        email, username, db = dashboard.get_current_user_or_redirect()
        if not email:
            return redirect(url_for("login", next=url_for("adminPage")))
        if not dashboard.repo.isAdmin(username):
            abort(403)
        # Unchecked checkboxes aren't submitted: absence means disable.
        dashboard.repo.setDataSharingEnabled(request.form.get("data_sharing") == "1")
        dashboard.repo.setRegistrationEnabled(request.form.get("registration") == "1")
        dashboard.repo.setShareLinksEnabled(request.form.get("share_links") == "1")
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
