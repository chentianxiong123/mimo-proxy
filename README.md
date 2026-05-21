# MiMo Proxy

> 解决 MiMo 模型多轮会话中 tool_calls 与 reasoning 不匹配导致的 400 错误

MiMo 模型在响应中会同时返回 `tool_calls` 和 `reasoning_content`（OpenAI 协议）/ `thinking`（Anthropic 协议）。但在多轮会话中，客户端发送的 assistant 消息只有 `tool_calls` 而缺少对应的 `reasoning_content`，上游 API 会因此返回 **400 Bad Request**。

**MiMo Proxy** 位于客户端与上游之间，自动缓存上一轮响应中的 reasoning/thinking，并在下一轮请求中**注入**回对应的 assistant 消息，让请求格式符合上游要求。当无缓存可用时，自动将 tool_calls 降级为文本描述，彻底避免 400。

---

## 工作原理

```
                    首次请求                         后续请求
客户端 ─── req (无 reasoning) ──→  代理  ──→  上游
                                          ↑
                                    缓存 reasoning ←─── 响应

客户端 ─── req (无 reasoning) ──→  代理  ──→  req (注入 reasoning) ──→ 上游
                                     ↑
                              从缓存取出 reasoning
```

### 请求处理（注入 reasoning 避免 400）

| 场景 | 协议 | 行为 |
|------|------|------|
| **有缓存** | OpenAI | 注入 `reasoning_content` 到 assistant 消息 |
| **有缓存** | Anthropic | 插入 `thinking` block 到消息 content 中 |
| **无缓存（降级）** | OpenAI | 剥离 `tool_calls`，替换为 `[调用了 xxx]` 文本 |
| **无缓存（降级）** | Anthropic | `tool_use` 转为文本描述，对应 `tool_result` 也转为文本 |

### 响应处理（缓存 reasoning）

| 协议 | 缓存来源 |
|------|---------|
| **OpenAI** | 非流式：`choices[].message.reasoning_content`；流式：累积 `delta.reasoning_content` |
| **Anthropic** | 非流式：`content[].thinking`；流式：累积 `delta.thinking_delta` |

---

## 快速开始

### 1. 安装

```bash
pip install -r requirements.txt
```

### 2. 配置

```bash
cp config.example.yaml config.yaml
# 编辑 config.yaml 修改上游地址
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