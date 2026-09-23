"""
Database management for TowerCast using SQLite.
Tracks episode states: discovered, pending, queued, ready, skipped.
"""
import sqlite3
import os
import json
from datetime import datetime, timezone
from typing import List, Dict, Optional, Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    published_at TEXT,
    duration INTEGER,
    thumbnail_url TEXT,
    description TEXT,
    status TEXT NOT NULL,  -- 'pending', 'queued', 'downloading', 'ready', 'skipped', 'failed'
    audio_filename TEXT,
    file_size INTEGER DEFAULT 0,
    chapters_json TEXT,
    matched_keyword TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_status ON episodes(status);
CREATE INDEX IF NOT EXISTS idx_episodes_published ON episodes(published_at DESC);
"""

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self.init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def init_db(self):
        conn = self._get_conn()
        try:
            with conn:
                conn.executescript(SCHEMA)
        finally:
            conn.close()

    def get_episode(self, video_id: str) -> Optional[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            cur = conn.execute("SELECT * FROM episodes WHERE id = ?", (video_id,))
            row = cur.fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def upsert_discovered_episode(
        self,
        video_id: str,
        title: str,
        url: str,
        published_at: Optional[str],
        duration: Optional[int],
        thumbnail_url: Optional[str],
        status: str,
        matched_keyword: Optional[str] = None,
        description: Optional[str] = None
    ) -> bool:
        """
        Inserts new episode if not seen yet.
        Returns True if inserted, False if already exists.
        """
        now = datetime.now(timezone.utc).isoformat()
        conn = self._get_conn()
        try:
            with conn:
                existing = conn.execute("SELECT status FROM episodes WHERE id = ?", (video_id,)).fetchone()
                if existing:
                    return False
                conn.execute(
                    """
                    INSERT INTO episodes (
                        id, title, url, published_at, duration, thumbnail_url,
                        description, status, matched_keyword, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        video_id, title, url, published_at, duration, thumbnail_url,
                        description, status, matched_keyword, now, now
                    )
                )
                return True
        finally:
            conn.close()

    def mark_status(self, video_id: str, status: str, **kwargs):
        now = datetime.now(timezone.utc).isoformat()
        fields = ["status = ?", "updated_at = ?"]
        values = [status, now]
        for k, v in kwargs.items():
            if k == "chapters" and isinstance(v, (list, dict)):
                fields.append("chapters_json = ?")
                values.append(json.dumps(v))
            else:
                fields.append(f"{k} = ?")
                values.append(v)
        values.append(video_id)
        conn = self._get_conn()
        try:
            with conn:
                conn.execute(f"UPDATE episodes SET {', '.join(fields)} WHERE id = ?", values)
        finally:
            conn.close()

    def get_episodes_by_status(self, status: str, limit: int = 100) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "SELECT * FROM episodes WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                (status, limit)
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def get_pending_episodes(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.get_episodes_by_status("pending", limit)

    def get_queued_episodes(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self.get_episodes_by_status("queued", limit)

    def get_ready_episodes(self, limit: int = 100) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "SELECT * FROM episodes WHERE status = 'ready' ORDER BY COALESCE(published_at, created_at) DESC LIMIT ?",
                (limit,)
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def get_all_episodes(self, limit: int = 100) -> List[Dict[str, Any]]:
        conn = self._get_conn()
        try:
            cur = conn.execute(
                "SELECT * FROM episodes ORDER BY COALESCE(published_at, created_at) DESC LIMIT ?",
                (limit,)
            )
            return [dict(r) for r in cur.fetchall()]
        finally:
            conn.close()

    def queue_episode(self, video_id: str) -> bool:
        ep = self.get_episode(video_id)
        if ep and ep["status"] != "ready":
            self.mark_status(video_id, "queued")
            return True
        return False

    def skip_episode(self, video_id: str) -> bool:
        ep = self.get_episode(video_id)
        if ep and ep["status"] != "ready":
            self.mark_status(video_id, "skipped")
            return True
        return False

    def delete_episode(self, video_id: str):
        conn = self._get_conn()
        try:
            with conn:
                conn.execute("DELETE FROM episodes WHERE id = ?", (video_id,))
        finally:
            conn.close()
