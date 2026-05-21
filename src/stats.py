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
        for unit, div in [("天", 86400), ("时", 3600), ("分", 60)]:
            if s >= div:
                return f"{s // div}{unit}{s % div // 60}分"
        return f"{s}秒"

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
