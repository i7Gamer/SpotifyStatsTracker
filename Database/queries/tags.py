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
        norm_tags = [normalizeTag(t) for t in tags if normalizeTag(t)]
        if not norm_tags:
            return []

        conn = self._conn()
        track_sets: list[set[str]] = []

        query = """
            SELECT DISTINCT t.id FROM tracks t
            WHERE t.id IN (
                SELECT entity_id FROM user_tags WHERE username = ? AND entity_type = 'track' AND tag = ?
            ) OR t.album_id IN (
                SELECT entity_id FROM user_tags WHERE username = ? AND entity_type = 'album' AND tag = ?
            ) OR EXISTS (
                SELECT 1 FROM track_artists ta WHERE ta.track_id = t.id AND ta.artist_id IN (
                    SELECT entity_id FROM user_tags WHERE username = ? AND entity_type = 'artist' AND tag = ?
                )
            )
        """

        for tag in norm_tags:
            rows = conn.execute(query, (username, tag, username, tag, username, tag)).fetchall()
            track_sets.append({r["id"] for r in rows})

        if not track_sets:
            return []

        if match_mode == "all":
            result_set = set.intersection(*track_sets)
        else:
            result_set = set.union(*track_sets)

        return list(result_set)

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
