"""
配置加载模块
支持 YAML 配置文件 + 环境变量覆盖
"""

import os
from pathlib import Path
from dataclasses import dataclass, field

import yaml


@dataclass
class CacheConfig:
    max_size: int = 2000
    ttl_seconds: int = 7200
    backend: str = "memory"  # "memory" | "sqlite" | "tiered"
    db_path: str = "cache.db"


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8899


@dataclass
class LoggingConfig:
    persistent: bool = False
    db_path: str = "./logs/mimo_proxy.db"
    retain_days: int = 7


@dataclass
class RetryConfig:
    max_retries: int = 3
    backoff_base: int = 2


@dataclass
class DashboardConfig:
    enabled: bool = True
    log_buffer_size: int = 200


@dataclass
class AppConfig:
    upstream_api_base: str = ""
    server: ServerConfig = field(default_factory=ServerConfig)
    cache: CacheConfig = field(default_factory=CacheConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    retry: RetryConfig = field(default_factory=RetryConfig)
    dashboard: DashboardConfig = field(default_factory=DashboardConfig)


def _deep_merge(base: dict, override: dict) -> dict:
    """递归合并字典，override 优先"""
    result = base.copy()
    for k, v in override.items():
        if k in result and isinstance(result[k], dict) and isinstance(v, dict):
            result[k] = _deep_merge(result[k], v)
        else:
            result[k] = v
    return result


def _apply_env_overrides(cfg_dict: dict) -> dict:
    """环境变量覆盖配置"""
    env_map = {
        "MIMO_API_BASE": "upstream_api_base",
        "MIMO_LISTEN_HOST": "server.host",
        "MIMO_LISTEN_PORT": ("server.port", int),
        "MIMO_CACHE_MAX_SIZE": ("cache.max_size", int),
        "MIMO_CACHE_TTL": ("cache.ttl_seconds", int),
        "MIMO_CACHE_BACKEND": "cache.backend",
        "MIMO_CACHE_DB_PATH": "cache.db_path",
        "MIMO_LOG_PERSISTENT": ("logging.persistent", lambda x: x.lower() in ("true", "1", "yes")),
        "MIMO_LOG_DB_PATH": "logging.db_path",
    }
    for env_key, path in env_map.items():
        val = os.getenv(env_key)
        if val is None:
            continue
        converter = None
        if isinstance(path, tuple):
            path, converter = path
        if converter:
            val = converter(val)
        keys = path.split(".")
        d = cfg_dict
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = val
    return cfg_dict


def load_config(config_path: str | None = None) -> AppConfig:
    """加载配置：YAML 文件 + 环境变量覆盖"""
    cfg_dict = {}

    if config_path is None:
        config_path = os.getenv("MIMO_CONFIG_FILE", "config.yaml")

    path = Path(config_path)
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            cfg_dict = yaml.safe_load(f) or {}
    else:
        print(f"[WARN] 配置文件不存在: {config_path}")
        print(f"[WARN] 将使用默认配置 + 环境变量")
        print(f"[HINT] 请复制 config.example.yaml 为 config.yaml 并修改")

    cfg_dict = _apply_env_overrides(cfg_dict)

    # 手动构建 dataclass（不支持递归 from_dict）
    server_cfg = ServerConfig(**cfg_dict.get("server", {}))
    cache_raw = cfg_dict.get("cache", {})
    cache_cfg = CacheConfig(**cache_raw)
    logging_cfg = LoggingConfig(**cfg_dict.get("logging", {}))
    retry_cfg = RetryConfig(**cfg_dict.get("retry", {}))
    dashboard_cfg = DashboardConfig(**cfg_dict.get("dashboard", {}))

    return AppConfig(
        upstream_api_base=cfg_dict.get("upstream_api_base", ""),
        server=server_cfg,
        cache=cache_cfg,
        logging=logging_cfg,
        retry=retry_cfg,
        dashboard=dashboard_cfg,
    )
