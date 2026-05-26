# MiMo Proxy

> 多协议推理链缓存代理 — 让 DeepSeek/MiMo 的 reasoning_content 与 tool_calls 在多轮会话中不再断裂

MiMo/DeepSeek 模型在响应中会同时返回 `tool_calls` 和 `reasoning_content`。但在多轮会话中，客户端发送的 assistant 消息只有 `tool_calls` 而缺少 `reasoning_content`，上游 API 会因此返回 **400 Bad Request**。

**MiMo Proxy** 位于客户端与上游之间，自动缓存推理链（reasoning/thinking），在下一轮请求中注入回对应消息。缓存采用两级架构（内存 + SQLite），重启不丢失。支持三种协议入口，统一翻译为 Chat Completions 发往上游。

---

## 核心特性

### 多协议翻译
三条路由，统一输出 Chat Completions：

| 路由 | 输入协议 | 用途 |
|------|---------|------|
| `/v1/chat/completions` | Chat Completions | Claude Code 等标准客户端直连 |
| `/v1/responses` | Responses API | Codex CLI 使用，自动翻译为 CC |
| `/v1/messages` | Anthropic Messages | Anthropic SDK 客户端，自动翻译为 CC |

### 两级缓存（Tiered Cache）
```
查询 → 内存(L1)命中 → 直接返回
       ↓ miss
     SQLite(L2)命中 → 提升到内存 → 返回
       ↓ miss
     填充空 reasoning → tool_calls 完整保留
```
- **内存层**：LRU 热缓存，微秒级访问
- **SQLite 层**：WAL 模式，每次写入即时落盘，突然断电不丢数据
- **自动提升**：冷数据命中后提升到内存，热数据自然积累

### 模型路由
每个上游声明它服务的模型名。请求按 `model` 字段精确匹配，未命中回退到默认上游。一个代理同时服务 MiMo 和 DeepSeek。

### 错误透传
上游返回 4xx/5xx 时，代理原样透传原始状态码给下游，不做内部重试。客户端（如 Claude Code）按自己的指数退避策略正常重连。

### 透明代理
密钥由下游客户端透传，代理不管理密钥，不做认证。

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

### 请求处理

| 场景 | 行为 |
|------|------|
| **有缓存** | 注入 `reasoning_content`（OpenAI）/ `thinking` block（Anthropic）到 assistant 消息 |
| **无缓存** | 填充空 reasoning 字段，保留 `tool_calls` / `tool_use` 完整，协议不断裂 |

### 响应处理

缓存 assistant 响应中的 `reasoning_content`（CC 流/非流）或 `thinking`（Anthropic 流/非流），供下一轮请求注入。

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

### 3. 启动

```bash
python -m src.main
# 或双击 start.bat
```

启动后访问仪表盘：http://127.0.0.1:8899/dashboard

---

## 仪表盘

深色主题 Web 面板，实时刷新（5 秒），提供：

- **运行统计**：请求/成功/失败/流式/缓存命中率/降级
- **上游管理**：添加/切换/删除上游，按模型名路由，即时生效
- **缓存设置**：内存上限、DB 上限、TTL，运行时热修改
- **日志查看**：彩色日志流，自动滚动

---

## 配置参考

| 环境变量 | 对应配置项 | 说明 |
|----------|-----------|------|
| `MIMO_API_BASE` | `upstream_api_base` | 默认上游 API 地址 |
| `MIMO_LISTEN_HOST` | `server.host` | 监听地址（默认 0.0.0.0） |
| `MIMO_LISTEN_PORT` | `server.port` | 监听端口（默认 8899） |
| `MIMO_CACHE_MAX_SIZE` | `cache.max_size` | 内存缓存条目上限 |
| `MIMO_CACHE_TTL` | `cache.ttl_seconds` | 缓存过期时间（秒） |
| `MIMO_CACHE_BACKEND` | `cache.backend` | 缓存后端（memory / sqlite / tiered） |
| `MIMO_CACHE_DB_PATH` | `cache.db_path` | SQLite 缓存数据库路径 |
| `MIMO_LOG_PERSISTENT` | `logging.persistent` | 是否持久化日志 |

---

## 项目结构

```
mimo-proxy/
├── src/
│   ├── main.py              # 入口 & 启动
│   ├── config.py            # 配置加载（YAML + 环境变量）
│   ├── routes.py            # 路由定义 & 请求分发
│   ├── proxy.py             # 核心代理（reasoning 注入、流式转发、错误透传）
│   ├── responses.py         # Responses API → Chat Completions 翻译
│   ├── anthropic_translate.py  # Anthropic Messages → Chat Completions 翻译
│   ├── cache.py             # 两级缓存（Memory + SQLite + Tiered）
│   ├── upstreams.py         # 上游管理（模型路由、SQLite 持久化）
│   ├── dashboard.py         # 日志缓冲 & 仪表盘渲染
│   └── stats.py             # 请求统计
├── templates/
│   └── dashboard.html       # 中文仪表盘（深色主题、单文件无依赖）
├── config.example.yaml
├── requirements.txt
├── start.bat
└── .gitignore
```

---

## License

MIT
