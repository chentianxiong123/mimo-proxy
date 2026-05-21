"""
MiMo Reasoning Content Proxy

"""

import logging
import os
import socket
import sys
from pathlib import Path

import uvicorn


def check_prerequisites():
    """检查运行前置条件"""
    errors = []

    config_path = os.getenv("MIMO_CONFIG_FILE", "config.yaml")
    if not Path(config_path).exists():
        errors.append(f"配置文件不存在: {config_path}")
        errors.append("请复制 config.example.yaml 为 config.yaml 并修改配置")

    template_dir = os.getenv("MIMO_TEMPLATE_DIR", os.path.join(os.path.dirname(__file__), "..", "templates"))
    template_path = Path(template_dir) / "dashboard.html"
    if not template_path.exists():
        errors.append(f"仪表盘模板不存在: {template_path}")

    missing_deps = []
    for dep in ["httpx", "starlette", "uvicorn", "yaml", "aiofiles"]:
        try:
            __import__(dep)
        except ImportError:
            if dep == "yaml":
                try:
                    __import__("yaml")
                except ImportError:
                    missing_deps.append("pyyaml")
            else:
                missing_deps.append(dep)

    if missing_deps:
        errors.append(f"缺少依赖: {', '.join(missing_deps)}")
        errors.append("请运行: pip install -r requirements.txt")

    return errors


def main():
    print("""
╔══════════════════════════════════════════════════╗
║        MiMo Reasoning Content Proxy             ║
║          思维链缓存代理 · 开源版                    ║
╚══════════════════════════════════════════════════╝
""")

    errors = check_prerequisites()
    if errors:
        print("❌ 启动检查失败:\n")
        for e in errors:
            print(f"  • {e}")
        print("\n请按照提示完成配置后重试。")
        print("完整部署指南请参考项目文档。")
        sys.exit(1)

    from .config import load_config
    config = load_config()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 压制 httpx 的冗长请求日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    from .routes import create_app
    app = create_app(config)

    local_ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        local_ip = s.getsockname()[0]
        s.close()
    except Exception:
        pass

    print(f"""
════════════════════════════════════════════════
  服务信息
════════════════════════════════════════════════
  🔵 Anthropic API:  http://{local_ip}:{config.server.port}/v1/messages
  🟢 OpenAI API:     http://{local_ip}:{config.server.port}/v1/chat/completions
  📊 仪表盘:         http://{local_ip}:{config.server.port}/dashboard
  ⚡ 上游:            {config.upstream_api_base}
  💾 缓存:            memory (最大={config.cache.max_size}, TTL={config.cache.ttl_seconds}秒)
════════════════════════════════════════════════
  启动完成，等待请求...
""")

    uvicorn.run(app, host=config.server.host, port=config.server.port, log_level="info")


if __name__ == "__main__":
    main()