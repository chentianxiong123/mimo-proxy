"""
缓存模块
支持 LRU + TTL 内存缓存 和 SQLite 持久化缓存
"""

import hashlib
import json
import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Optional

log = logging.getLogger("mimo-proxy")


class MemoryCache:
    """LRU + TTL 内存缓存，支持 tool_call_id 索引"""

    def __init__(self, max_size: int = 2000, ttl: int = 7200):
        self._data: OrderedDict[str, tuple[str, float]] = OrderedDict()
        self._tc_index: dict[str, str] = {}
        self.max_size = max_size
        self.ttl = ttl

    @property
    def size(self) -> int:
        return len(self._data)

    @staticmethod
    def msg_hash(msg: dict) -> str:
        """OpenAI 格式消息哈希"""
        content = msg.get("content") or ""
        tc = json.dumps(msg.get("tool_calls") or [], sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(f"{content}||{tc}".encode()).hexdigest()[:16]

    @staticmethod
    def msg_hash_anthropic(msg: dict) -> str:
        """Anthropic 格式消息哈希"""
        content = msg.get("content")
        text_parts = []
        tool_parts = []
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "text":
                    text_parts.append(block.get("text") or "")
                elif block.get("type") == "tool_use":
                    tool_parts.append(f"{block.get('name')}||{json.dumps(block.get('input'), sort_keys=True)}")
        elif isinstance(content, str):
            text_parts.append(content)
        return hashlib.sha256(f"{''.join(text_parts)}||{''.join(tool_parts)}".encode()).hexdigest()[:16]

    @staticmethod
    def tc_ids(msg: dict) -> list[str]:
        """OpenAI 格式 tool_call_ids"""
        return [t["id"] for t in msg.get("tool_calls") or [] if t.get("id")]

    @staticmethod
    def tc_ids_anthropic(msg: dict) -> list[str]:
        """Anthropic 格式 tool_call_ids"""
        content = msg.get("content")
        ids = []
        if isinstance(content, list):
            for block in content:
                if block.get("type") == "tool_use" and block.get("id"):
                    ids.append(block["id"])
        return ids

    def get(self, key: str) -> Optional[str]:
        if key in self._data:
            val, ts = self._data[key]
            if time.time() - ts < self.ttl:
                self._data.move_to_end(key)
                return val
            del self._data[key]
        return None

    def set(self, key: str, value: str, tool_call_ids: list[str] | None = None):
        if key in self._data:
            del self._data[key]
        self._data[key] = (value, time.time())
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)
        if tool_call_ids:
            for tid in tool_call_ids:
                self._tc_index[tid] = value

    def get_raw(self, key: str) -> tuple[str, float] | None:
        """返回 (value, timestamp) 或 None，不刷新 LRU"""
        if key in self._data:
            val, ts = self._data[key]
            if time.time() - ts < self.ttl:
                return (val, ts)
            del self._data[key]
        return None

    def set_raw(self, key: str, value: str):
        """直接写入，自动时间戳，执行 LRU 淘汰"""
        if key in self._data:
            del self._data[key]
        self._data[key] = (value, time.time())
        while len(self._data) > self.max_size:
            self._data.popitem(last=False)

    def delete(self, key: str):
        if key in self._data:
            del self._data[key]

    def set_tc_index(self, tool_call_ids: list[str], value: str):
        for tid in tool_call_ids:
            self._tc_index[tid] = value

    def get_tc_index(self, tool_call_id: str) -> str | None:
        return self._tc_index.get(tool_call_id)

    def clear(self):
        self._data.clear()
        self._tc_index.clear()

    def info(self) -> dict:
        return {"size": self.size, "max": self.max_size, "ttl": self.ttl, "tc_index": len(self._tc_index), "backend": "memory"}

    def set_max_size(self, n: int):
        self.max_size = n

    def set_ttl(self, n: int):
        self.ttl = n


class SQLiteCache:
    """SQLite 持久化缓存，重启后缓存不丢失，接口与 MemoryCache 一致"""

    # 复用 MemoryCache 的静态方法
    msg_hash = MemoryCache.msg_hash
    msg_hash_anthropic = MemoryCache.msg_hash_anthropic
    tc_ids = MemoryCache.tc_ids
    tc_ids_anthropic = MemoryCache.tc_ids_anthropic

    def __init__(self, db_path: str = "cache.db", ttl: int = 7200, max_size: int = 2000):
        self.db_path = db_path
        self.ttl = ttl
        self.max_size = max_size
        self._local = threading.local()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        conn = self._conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                ts REAL NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tc_index (
                tool_call_id TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_cache_ts ON cache(ts)")
        conn.commit()

    def _conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn"):
            self._local.conn = sqlite3.connect(self.db_path, timeout=5)
            self._local.conn.execute("PRAGMA journal_mode=WAL")
        return self._local.conn

    @property
    def size(self) -> int:
        row = self._conn().execute("SELECT COUNT(*) FROM cache").fetchone()
        return row[0] if row else 0

    def get(self, key: str) -> Optional[str]:
        row = self._conn().execute(
            "SELECT value, ts FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row:
            if time.time() - row[1] < self.ttl:
                return row[0]
            self._conn().execute("DELETE FROM cache WHERE key = ?", (key,))
            self._conn().commit()
        return None

    def set(self, key: str, value: str, tool_call_ids: list[str] | None = None):
        conn = self._conn()
        conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        conn.execute(
            "INSERT INTO cache (key, value, ts) VALUES (?, ?, ?)",
            (key, value, time.time())
        )
        if tool_call_ids:
            for tid in tool_call_ids:
                conn.execute(
                    "INSERT OR REPLACE INTO tc_index (tool_call_id, value) VALUES (?, ?)",
                    (tid, value)
                )
        conn.commit()
        self._evict()

    def get_raw(self, key: str) -> tuple[str, float] | None:
        row = self._conn().execute(
            "SELECT value, ts FROM cache WHERE key = ?", (key,)
        ).fetchone()
        if row:
            if time.time() - row[1] < self.ttl:
                return (row[0], row[1])
            self._conn().execute("DELETE FROM cache WHERE key = ?", (key,))
            self._conn().commit()
        return None

    def set_raw(self, key: str, value: str):
        conn = self._conn()
        conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        conn.execute(
            "INSERT INTO cache (key, value, ts) VALUES (?, ?, ?)",
            (key, value, time.time())
        )
        conn.commit()
        self._evict()

    def delete(self, key: str):
        self._conn().execute("DELETE FROM cache WHERE key = ?", (key,))
        self._conn().commit()

    def set_tc_index(self, tool_call_ids: list[str], value: str):
        conn = self._conn()
        for tid in tool_call_ids:
            conn.execute(
                "INSERT OR REPLACE INTO tc_index (tool_call_id, value) VALUES (?, ?)",
                (tid, value)
            )
        conn.commit()

    def get_tc_index(self, tool_call_id: str) -> str | None:
        row = self._conn().execute(
            "SELECT value FROM tc_index WHERE tool_call_id = ?",
            (tool_call_id,)
        ).fetchone()
        return row[0] if row else None

    def clear(self):
        conn = self._conn()
        conn.execute("DELETE FROM cache")
        conn.execute("DELETE FROM tc_index")
        conn.commit()

    def _evict(self):
        """淘汰最旧的条目，保持不超过 max_size"""
        conn = self._conn()
        count = conn.execute("SELECT COUNT(*) FROM cache").fetchone()[0]
        if count > self.max_size:
            conn.execute(
                "DELETE FROM cache WHERE key IN (SELECT key FROM cache ORDER BY ts ASC LIMIT ?)",
                (count - self.max_size,)
            )
            conn.commit()

    def info(self) -> dict:
        return {"size": self.size, "max": self.max_size, "ttl": self.ttl, "backend": "sqlite", "db": self.db_path}

    def set_max_size(self, n: int):
        self.max_size = n

    def set_ttl(self, n: int):
        self.ttl = n


class TieredCache:
    """两级缓存：内存(L1热层) + SQLite(L2冷层)，接口与 MemoryCache 一致"""

    msg_hash = MemoryCache.msg_hash
    msg_hash_anthropic = MemoryCache.msg_hash_anthropic
    tc_ids = MemoryCache.tc_ids
    tc_ids_anthropic = MemoryCache.tc_ids_anthropic

    def __init__(self, hot: MemoryCache, cold: SQLiteCache):
        self.hot = hot
        self.cold = cold
        self.ttl = hot.ttl
        self.max_size = hot.max_size

    @property
    def size(self) -> int:
        return self.cold.size

    def get(self, key: str) -> Optional[str]:
        val = self.hot.get(key)
        if val is not None:
            return val
        val = self.cold.get(key)
        if val is not None:
            self.hot.set(key, val)
        return val

    def set(self, key: str, value: str, tool_call_ids: list[str] | None = None):
        self.hot.set(key, value, tool_call_ids)
        self.cold.set(key, value, tool_call_ids)

    def get_raw(self, key: str) -> tuple[str, float] | None:
        result = self.hot.get_raw(key)
        if result is not None:
            return result
        result = self.cold.get_raw(key)
        if result is not None:
            self.hot.set_raw(key, result[0])
        return result

    def set_raw(self, key: str, value: str):
        self.hot.set_raw(key, value)
        self.cold.set_raw(key, value)

    def delete(self, key: str):
        self.hot.delete(key)
        self.cold.delete(key)

    def set_tc_index(self, tool_call_ids: list[str], value: str):
        self.hot.set_tc_index(tool_call_ids, value)
        self.cold.set_tc_index(tool_call_ids, value)

    def get_tc_index(self, tool_call_id: str) -> str | None:
        val = self.hot.get_tc_index(tool_call_id)
        if val is not None:
            return val
        val = self.cold.get_tc_index(tool_call_id)
        if val is not None:
            self.hot.set_tc_index([tool_call_id], val)
        return val

    def clear(self):
        self.hot.clear()
        self.cold.clear()

    def info(self) -> dict:
        return {
            "size": self.cold.size,
            "backend": "tiered",
            "hot": self.hot.info(),
            "cold": self.cold.info(),
        }

    def set_hot_max_size(self, n: int):
        self.hot.set_max_size(n)

    def set_cold_max_size(self, n: int):
        self.cold.set_max_size(n)

    def set_ttl(self, n: int):
        self.ttl = n
        self.hot.set_ttl(n)
        self.cold.set_ttl(n)


def create_cache(backend: str = "memory", **kwargs):
    ttl = kwargs.get("ttl", 7200)
    max_size = kwargs.get("max_size", 2000)

    if backend == "sqlite":
        return SQLiteCache(
            db_path=kwargs.get("db_path", "cache.db"),
            ttl=ttl,
            max_size=max_size,
        )
    if backend == "tiered":
        hot = MemoryCache(max_size=max_size, ttl=ttl)
        cold = SQLiteCache(
            db_path=kwargs.get("db_path", "cache.db"),
            ttl=ttl,
            max_size=max_size * 5,
        )
        return TieredCache(hot=hot, cold=cold)
    return MemoryCache(max_size=max_size, ttl=ttl)
