"""
统计数据模块
"""

import time
from dataclasses import dataclass, field


@dataclass
class Stats:
    start_time: float = field(default_factory=time.time)
    requests: int = 0
    success: int = 0
    failed: int = 0
    stream: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    degraded: int = 0
    retries: int = 0

    @property
    def uptime(self) -> str:
        s = int(time.time() - self.start_time)
        days = s // 86400
        hours = (s % 86400) // 3600
        mins = (s % 3600) // 60
        parts = []
        if days:
            parts.append(f"{days}天")
        if hours:
            parts.append(f"{hours}时")
        if mins or not parts:
            parts.append(f"{mins}分")
        return "".join(parts)

    def to_dict(self) -> dict:
        hit_total = max(self.cache_hits + self.cache_misses, 1)
        return {
            "uptime": self.uptime,
            "requests": self.requests,
            "success": self.success,
            "failed": self.failed,
            "stream": self.stream,
            "cache_hits": self.cache_hits,
            "hit_rate": f"{self.cache_hits / hit_total * 100:.1f}%",
            "degraded": self.degraded,
            "retries": self.retries,
        }
