"""
日志缓冲 & 管理面板模块
"""

import logging
import os
import time
from pathlib import Path

import aiofiles


class LogBuffer(logging.Handler):
    """保留最近 N 条日志供面板展示"""

    def __init__(self, capacity: int = 200):
        super().__init__()
        self.capacity = capacity
        self.entries: list[dict] = []

    def emit(self, record: logging.LogRecord):
        self.entries.append({
            "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
            "level": record.levelname,
            "msg": self.format(record),
        })
        if len(self.entries) > self.capacity:
            self.entries = self.entries[-self.capacity:]


async def load_dashboard_html() -> str:
    """从外部模板文件加载仪表盘 HTML"""
    # 查找模板路径：优先环境变量 → 相对路径
    template_dir = os.getenv("MIMO_TEMPLATE_DIR", os.path.join(os.path.dirname(__file__), "..", "templates"))
    template_path = Path(template_dir) / "dashboard.html"

    if not template_path.exists():
        raise FileNotFoundError(
            f"仪表盘模板文件不存在: {template_path}\n"
            f"请确保 templates/dashboard.html 文件存在，或设置 MIMO_TEMPLATE_DIR 环境变量"
        )

    async with aiofiles.open(template_path, "r", encoding="utf-8") as f:
        return await f.read()


class DashboardRenderer:
    """延迟加载并缓存仪表盘 HTML"""

    def __init__(self):
        self._html: str | None = None

    async def render(self, **kwargs) -> str:
        if self._html is None:
            self._html = await load_dashboard_html()
        # 简单模板替换
        html = self._html
        for key, val in kwargs.items():
            html = html.replace(f"{{{{{key}}}}}", str(val))
        return html
