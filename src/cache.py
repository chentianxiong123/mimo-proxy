"""
缓存模块
支持 LRU + TTL 内存缓存
"""

import hashlib
import json
import logging
import time
from collections import OrderedDict
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

    def clear(self):
        self._data.clear()
        self._tc_index.clear()

    def info(self) -> dict:
        return {"size": self.size, "max": self.max_size, "ttl": self.ttl, "tc_index": len(self._tc_index), "backend": "memory"}


def create_cache(backend: str = "memory", **kwargs):
    return MemoryCache(
        max_size=kwargs.get("max_size", 2000),
        ttl=kwargs.get("ttl", 7200)
    )
