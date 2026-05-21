# MiMo Proxy

> 思维链缓存代理 · Chain-of-Thought Reasoning Cache Proxy

**MiMo Proxy** 是一个透明的 API 代理，专门解决多轮会话中思维链（reasoning / thinking）重复计算的问题。

它位于客户端与大模型 API 之间，自动**缓存**上一轮的 reasoning/thinking 内容，并在下一轮请求中**注入**回消息体，让模型能"记住自己刚才在想什么"，而无需重新思考。大幅节省 tokens，提升多轮交互的连贯性。

---

## 工作原理

```
客户端 ──→ MiMo Proxy ──→ 上游 API（OpenAI / Anthropic）
              │
              ├─ 首次请求：透传并缓存 reasoning/thinking
              └─ 后续请求：注入缓存的 reasoning/thinking → 再发往上游
```

### 支持协议

| 协议 | 端点 | 缓存字段 |
|------|------|----------|
| **OpenAI** | `/v1/chat/completions`, `/chat/completions` | `reasoning_content` |
| **Anthropic** | `/v1/messages`, `/messages` | `thinking` content block |

### 核心能力

- **思维链缓存** — 自动提取并缓存 assistant 消息中的 reasoning/thinking
- **智能注入** — 匹配 tool_call_id，将缓存的思维链精确注入到对应消息
- **优雅降级** — 无缓存时自动剥离 tool_calls 转为文本描述，避免 400 错误
- **流式/非流式全兼容** — SSE 流式转发同时实时累积 reasoning
- **双协议透明** — OpenAI 和 Anthropic 协议各自独立处理，互不干扰

---

## 快速开始

### 1. 安装

```bash
pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml 修改上游地址和密钥
```

所有配置项均可通过环境变量覆盖（优先级高于 YAML）：

```bash
set MIMO_API_KEY=sk-xxx
set MIMO_API_BASE=https://your-api.com/v1
python -m src.main
```

### 3. 启动

```bash
python -m src.main
```

或直接双击 `start.bat`。

启动后访问仪表盘：http://127.0.0.1:8899/dashboard

---

## 配置参考

| 环境变量 | 对应配置项 | 说明 |
|----------|-----------|------|
| `MIMO_API_KEY` | `api_key` | API 密钥（推荐用环境变量，避免写文件） |
| `MIMO_API_BASE` | `upstream_api_base` | 上游 API 地址 |
| `MIMO_LISTEN_HOST` | `server.host` | 监听地址（默认 0.0.0.0） |
| `MIMO_LISTEN_PORT` | `server.port` | 监听端口（默认 8899） |
| `MIMO_CACHE_MAX_SIZE` | `cache.max_size` | 最大缓存条目数 |
| `MIMO_CACHE_TTL` | `cache.ttl_seconds` | 缓存过期时间（秒） |
| `MIMO_CACHE_BACKEND` | `cache.backend` | 缓存后端（memory / redis） |
| `MIMO_REDIS_URL` | `cache.redis.url` | Redis 连接地址 |
| `MIMO_LOG_PERSISTENT` | `logging.persistent` | 是否持久化日志 |

---

## 项目结构

```
mimo-proxy/
├── src/
│   ├── main.py          # 入口 & 启动
│   ├── config.py        # 配置加载（YAML + 环境变量）
│   ├── routes.py        # 路由定义（OpenAI / Anthropic）
│   ├── proxy.py         # 核心代理逻辑（缓存、注入、流式转发）
│   ├── cache.py         # LRU + TTL 内存缓存
│   ├── dashboard.py     # 日志缓冲 & 仪表盘渲染
│   └── stats.py         # 请求统计
├── templates/
│   └── dashboard.html   # 中文仪表盘（深色主题）
├── config.example.yaml  # 配置示例
├── requirements.txt
├── start.bat
└── .gitignore
```

---

## License

MIT