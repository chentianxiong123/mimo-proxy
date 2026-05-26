"""
上游管理模块
SQLite 持久化上游列表，支持切换、添加、删除
按模型名路由：每个上游声明它服务的模型名
"""

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import logging

log = logging.getLogger("mimo-proxy")


@dataclass
class Upstream:
    id: int
    name: str
    url: str
    model: str
    is_active: bool
    created_at: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "url": self.url,
            "model": self.model,
            "is_active": self.is_active,
            "created_at": self.created_at,
        }


class UpstreamStore:
    def __init__(self, db_path: str = "upstreams.db"):
        self._local = threading.local()
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS upstreams (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                url TEXT NOT NULL,
                model TEXT NOT NULL DEFAULT '',
                is_active INTEGER NOT NULL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_upstream_url ON upstreams(url)")
        conn.commit()
        self._migrate()

    def _migrate(self):
        """兼容旧表：添加 model 列（如果不存在）"""
        conn = self._conn()
        cols = [r[1] for r in conn.execute("PRAGMA table_info(upstreams)").fetchall()]
        if "model" not in cols:
            conn.execute("ALTER TABLE upstreams ADD COLUMN model TEXT NOT NULL DEFAULT ''")
            conn.commit()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.db_path, timeout=5)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
            self._local.conn.row_factory = sqlite3.Row
        return self._local.conn

    def seed(self, url: str):
        """如果表是空的，写入默认上游"""
        row = self._conn().execute("SELECT COUNT(*) as c FROM upstreams").fetchone()
        if row["c"] == 0:
            self._conn().execute(
                "INSERT INTO upstreams (name, url, is_active, created_at) VALUES (?, ?, 1, ?)",
                ("default", url, time.time())
            )
            self._conn().commit()
            log.info("  🌐 [Upstream] 初始化默认上游: %s", url)

    def get_active(self) -> Optional[Upstream]:
        row = self._conn().execute(
            "SELECT id, name, url, model, is_active, created_at FROM upstreams WHERE is_active = 1 LIMIT 1"
        ).fetchone()
        if row:
            return Upstream(**dict(row))
        return None

    def get_by_model(self, model: str) -> Optional[Upstream]:
        """按模型名精确匹配上游，未命中返回活跃上游"""
        row = self._conn().execute(
            "SELECT id, name, url, model, is_active, created_at FROM upstreams WHERE model = ? LIMIT 1",
            (model,)
        ).fetchone()
        if row:
            return Upstream(**dict(row))
        return self.get_active()

    def list_all(self) -> list[Upstream]:
        rows = self._conn().execute(
            "SELECT id, name, url, model, is_active, created_at FROM upstreams ORDER BY created_at ASC"
        ).fetchall()
        return [Upstream(**dict(r)) for r in rows]

    def add(self, name: str, url: str, model: str = "") -> Upstream:
        """添加新上游，不自动激活"""
        now = time.time()
        cur = self._conn().execute(
            "INSERT INTO upstreams (name, url, model, is_active, created_at) VALUES (?, ?, ?, 0, ?)",
            (name, url, model, now)
        )
        self._conn().commit()
        return Upstream(id=cur.lastrowid, name=name, url=url, model=model, is_active=False, created_at=now)

    def switch(self, upstream_id: int) -> bool:
        """切换活跃上游"""
        conn = self._conn()
        conn.execute("UPDATE upstreams SET is_active = 0")
        cur = conn.execute("UPDATE upstreams SET is_active = 1 WHERE id = ?", (upstream_id,))
        conn.commit()
        if cur.rowcount > 0:
            log.info("  🔄 [Upstream] 切换到上游 id=%d", upstream_id)
            return True
        return False

    def delete(self, upstream_id: int) -> bool:
        """删除上游（不能删当前激活的）"""
        row = self._conn().execute(
            "SELECT is_active FROM upstreams WHERE id = ?", (upstream_id,)
        ).fetchone()
        if not row or row["is_active"]:
            return False
        self._conn().execute("DELETE FROM upstreams WHERE id = ?", (upstream_id,))
        self._conn().commit()
        log.info("  🗑️ [Upstream] 删除上游 id=%d", upstream_id)
        return True