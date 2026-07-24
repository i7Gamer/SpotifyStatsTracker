from __future__ import annotations
import time
from Database.queries._base import *  # noqa: F401,F403


def normalizeTag(tag: str | None) -> str:
    """Strip whitespace, convert to lowercase, and remove leading '#' characters."""
    if not tag:
        return ""
    cleaned = tag.strip().lower()
    while cleaned.startswith("#"):
        cleaned = cleaned[1:].strip()
    return cleaned


class TagQueries:
    """TagQueries: user tags data-access methods, mixed into Repository."""

    def addTag(self, username: str, tag: str, entity_type: str, entity_id: str) -> str | None:
        if entity_type not in ("track", "artist", "album"):
            raise ValueError(f"Invalid entity_type: {entity_type}")
        norm = normalizeTag(tag)
        if not norm:
            return None
        conn = self._conn()
        with conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO user_tags (username, tag, entity_type, entity_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (username, norm, entity_type, entity_id, time.time()),
            )
        return norm

    def removeTag(self, username: str, tag: str, entity_type: str, entity_id: str) -> bool:
        norm = normalizeTag(tag)
        if not norm:
            return False
        conn = self._conn()
        with conn:
            cur = conn.execute(
                "DELETE FROM user_tags WHERE username=? AND tag=? AND entity_type=? AND entity_id=?",
                (username, norm, entity_type, entity_id),
            )
            return cur.rowcount > 0

    def getTagsForEntity(self, username: str, entity_type: str, entity_id: str) -> list[str]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT tag FROM user_tags WHERE username=? AND entity_type=? AND entity_id=? ORDER BY tag ASC",
            (username, entity_type, entity_id),
        ).fetchall()
        return [r["tag"] for r in rows]

    def getUserTags(self, username: str) -> list[dict]:
        conn = self._conn()
        rows = conn.execute(
            """
            SELECT tag, COUNT(*) as cnt
            FROM user_tags
            WHERE username=?
            GROUP BY tag
            ORDER BY tag ASC
            """,
            (username,),
        ).fetchall()
        return [{"tag": r["tag"], "count": r["cnt"]} for r in rows]

    def getTaggedTrackIds(self, username: str, tags: list[str], match_mode: str = "any") -> list[str]:
        # dict.fromkeys dedups while preserving order, so a caller passing the
        # same tag twice fires one query, not two - and "all" mode's tag count
        # stays honest.
        norm_tags = list(dict.fromkeys(normalizeTag(t) for t in tags if normalizeTag(t)))
        if not norm_tags:
            return []

        conn = self._conn()

        # Expand each tag outward from the (small, indexed) set of tagged
        # entities rather than scanning the whole tracks catalog and testing
        # three OR conditions per row: a track matches if it is tagged directly,
        # its album is tagged, or any of its artists is tagged. UNION dedups, so
        # this returns the same set as the old DISTINCT-with-ORs. Each branch
        # rides an index (user_tags PK prefix, then tracks PK / idx_tracks_album
        # / idx_track_artists_artist). {tags} expands to one placeholder per tag.
        query = """
            SELECT t.id FROM tracks t
                JOIN user_tags ut ON ut.entity_id = t.id
                WHERE ut.username = ? AND ut.entity_type = 'track' AND ut.tag IN ({tags})
            UNION
            SELECT t.id FROM tracks t
                JOIN user_tags ut ON ut.entity_id = t.album_id
                WHERE ut.username = ? AND ut.entity_type = 'album' AND ut.tag IN ({tags})
            UNION
            SELECT ta.track_id FROM track_artists ta
                JOIN user_tags ut ON ut.entity_id = ta.artist_id
                WHERE ut.username = ? AND ut.entity_type = 'artist' AND ut.tag IN ({tags})
        """

        if match_mode == "all":
            # A track must match every tag, so each tag is resolved separately
            # (one query per tag, single placeholder) and the id sets intersected.
            single = query.format(tags="?")
            track_sets = [
                {r["id"] for r in conn.execute(single, (username, tag, username, tag, username, tag)).fetchall()}
                for tag in norm_tags
            ]
            return list(set.intersection(*track_sets))

        # "any": a track matching any tag qualifies, which is exactly the union
        # over all tags - so one query with `tag IN (...)` per branch does it.
        placeholders = ",".join("?" for _ in norm_tags)
        rows = conn.execute(
            query.format(tags=placeholders),
            (username, *norm_tags, username, *norm_tags, username, *norm_tags),
        ).fetchall()
        return [r["id"] for r in rows]

    def renameTag(self, username: str, old_tag: str, new_tag: str) -> int:
        old_norm = normalizeTag(old_tag)
        new_norm = normalizeTag(new_tag)
        if not old_norm or not new_norm or old_norm == new_norm:
            return 0
        conn = self._conn()
        with conn:
            # Update non-conflicting rows first
            cur1 = conn.execute(
                """
                UPDATE OR IGNORE user_tags
                SET tag = ?
                WHERE username = ? AND tag = ?
                """,
                (new_norm, username, old_norm),
            )
            # Delete remaining old_norm rows (which were duplicates of existing new_norm rows)
            cur2 = conn.execute(
                "DELETE FROM user_tags WHERE username = ? AND tag = ?",
                (username, old_norm),
            )
            return cur1.rowcount + cur2.rowcount

    def deleteTag(self, username: str, tag: str) -> int:
        norm = normalizeTag(tag)
        if not norm:
            return 0
        conn = self._conn()
        with conn:
            cur = conn.execute("DELETE FROM user_tags WHERE username = ? AND tag = ?", (username, norm))
            return cur.rowcount
