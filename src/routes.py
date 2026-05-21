"""
路由定义模块
支持 OpenAI Chat Completions 协议和 Anthropic Messages 协议
"""

import asyncio
import json
import logging
from contextlib import asynccontextmanager

import httpx
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, StreamingResponse
from starlette.routing import Route

from .cache import MemoryCache, create_cache
from .config import AppConfig
from .dashboard import LogBuffer, DashboardRenderer
from .proxy import (
    inject_reasoning,
    save_reasoning,
    stream_proxy,
    inject_reasoning_anthropic,
    save_reasoning_anthropic,
    stream_proxy_anthropic,
)
from .stats import Stats

log = logging.getLogger("mimo-proxy")


class AppContext:
    """应用上下文，持有所有共享状态"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.stats = Stats()
        self.cache: MemoryCache = create_cache(
            max_size=config.cache.max_size,
            ttl=config.cache.ttl_seconds,
        )
        self.log_buffer = LogBuffer(capacity=config.dashboard.log_buffer_size)
        self.dashboard_renderer = DashboardRenderer()
        self.client: httpx.AsyncClient | None = None

    def get_client(self) -> httpx.AsyncClient:
        if self.client is None or self.client.is_closed:
            self.client = httpx.AsyncClient(
                timeout=httpx.Timeout(300, connect=30),
                follow_redirects=True,
            )
        return self.client


def create_routes(ctx: AppContext) -> list[Route]:
    """根据配置创建路由列表"""

    async def chat_completions(request: Request):
        """OpenAI 兼容协议"""
        ctx.stats.requests += 1
        client_ip = request.client.host if request.client else "?"
        log.info("")
        log.info("━━━ [OpenAI] 收到请求 from %s ━━━", client_ip)

        try:
            body = await request.json()
        except Exception:
            ctx.stats.failed += 1
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        model = body.get("model", "?")
        is_stream = body.get("stream", False)
        msg_count = len(body.get("messages", []))
        log.info("  📋 模型=%s | 消息数=%d | 流式=%s", model, msg_count, is_stream)

        messages = body.get("messages", [])
        inj, deg = inject_reasoning(messages, ctx.cache, ctx.stats)
        if inj or deg:
            log.info("  📊 [OpenAI] 注入=%d 条 | 降级=%d 条", inj, deg)
        else:
            log.info("  ✅ [OpenAI] 无需注入/降级")

        headers = {}
        if ctx.config.api_key:
            headers["authorization"] = f"Bearer {ctx.config.api_key}"
        elif auth := request.headers.get("authorization"):
            headers["authorization"] = auth

        upstream_base = ctx.config.upstream_api_base
        if "/anthropic" in upstream_base:
            upstream_base = upstream_base.replace("/anthropic", "")
        if upstream_base.endswith("/v1"):
            upstream_base = upstream_base[:-3]
        elif upstream_base.endswith("/v1/"):
            upstream_base = upstream_base[:-4]

        upstream = f"{upstream_base}/v1/chat/completions"
        log.info("  🎯 上游地址: %s", upstream)

        if is_stream:
            ctx.stats.stream += 1
            ctx.stats.success += 1
            return StreamingResponse(
                stream_proxy(upstream, headers, body, ctx.get_client(), ctx.cache, ctx.stats, ctx.config.retry),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        resp = None
        for attempt in range(ctx.config.retry.max_retries):
            try:
                resp = await ctx.get_client().post(upstream, headers=headers, json=body)
                if resp.status_code >= 500 and attempt < ctx.config.retry.max_retries - 1:
                    ctx.stats.retries += 1
                    log.warning("  ⚠️  [OpenAI] 上游 %d (尝试 %d), 即将重试...", resp.status_code, attempt + 1)
                    await asyncio.sleep(ctx.config.retry.backoff_base * (attempt + 1))
                    continue
                break
            except httpx.TimeoutException:
                ctx.stats.retries += 1
                if attempt < ctx.config.retry.max_retries - 1:
                    await asyncio.sleep(ctx.config.retry.backoff_base * (attempt + 1))
                    continue
                ctx.stats.failed += 1
                return JSONResponse({"error": "Timeout after retries"}, status_code=504)
            except Exception as e:
                ctx.stats.failed += 1
                return JSONResponse({"error": str(e)}, status_code=500)

        if resp is None:
            ctx.stats.failed += 1
            return JSONResponse({"error": "No response"}, status_code=502)

        if resp.status_code >= 400:
            ctx.stats.failed += 1
            log.warning("  ⚠️  [OpenAI] 上游错误 %d: %s", resp.status_code, resp.text[:200])
            return JSONResponse({"error": resp.text[:300]}, status_code=resp.status_code)

        data = resp.json()
        for ch in data.get("choices", []):
            msg = ch.get("message", {})
            if not msg.get("content") and not msg.get("tool_calls") and msg.get("reasoning_content"):
                msg["content"] = msg["reasoning_content"]
            save_reasoning(msg, ctx.cache)

        ctx.stats.success += 1
        log.info("  ✅ [OpenAI] 请求完成, 上行=%d 字符", len(json.dumps(body)))
        return JSONResponse(data)

    async def anthropic_messages(request: Request):
        """Anthropic 兼容协议"""
        ctx.stats.requests += 1
        client_ip = request.client.host if request.client else "?"
        log.info("")
        log.info("━━━ [Anthropic] 收到请求 from %s ━━━", client_ip)

        try:
            body = await request.json()
        except Exception:
            ctx.stats.failed += 1
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        model = body.get("model", "?")
        is_stream = body.get("stream", False)
        msg_count = len(body.get("messages", []))
        has_tools = "tools" in body
        has_thinking = body.get("thinking") is not None
        log.info("  📋 模型=%s | 消息数=%d | 流式=%s | 工具=%s | thinking=%s",
                 model, msg_count, is_stream, has_tools, has_thinking)

        messages = body.get("messages", [])
        inj, deg = inject_reasoning_anthropic(messages, ctx.cache, ctx.stats)
        if inj or deg:
            log.info("  📊 [Anthropic] 注入=%d 条 | 降级=%d 条", inj, deg)
        else:
            log.info("  ✅ [Anthropic] 无需注入/降级")

        headers = {}
        if ctx.config.api_key:
            headers["x-api-key"] = ctx.config.api_key
        else:
            for hdr in ("x-api-key", "api-key", "authorization", "anthropic-version"):
                if val := request.headers.get(hdr):
                    headers[hdr] = val

        upstream_base = ctx.config.upstream_api_base.rstrip("/")
        upstream_base = upstream_base.removesuffix("/v1")
        if "/anthropic" in upstream_base:
            upstream_base = upstream_base.replace("/anthropic", "")
            upstream = f"{upstream_base}/anthropic/v1/messages"
        else:
            upstream = f"{upstream_base}/v1/messages"

        log.info("  🎯 上游地址: %s", upstream)

        is_stream = body.get("stream", False)

        if is_stream:
            ctx.stats.stream += 1
            ctx.stats.success += 1
            return StreamingResponse(
                stream_proxy_anthropic(upstream, headers, body, ctx.get_client(), ctx.cache, ctx.stats, ctx.config.retry),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        resp = None
        for attempt in range(ctx.config.retry.max_retries):
            try:
                resp = await ctx.get_client().post(upstream, headers=headers, json=body)
                if resp.status_code >= 500 and attempt < ctx.config.retry.max_retries - 1:
                    ctx.stats.retries += 1
                    log.warning("  ⚠️  [Anthropic] 上游 %d (尝试 %d), 即将重试...", resp.status_code, attempt + 1)
                    await asyncio.sleep(ctx.config.retry.backoff_base * (attempt + 1))
                    continue
                break
            except httpx.TimeoutException:
                ctx.stats.retries += 1
                if attempt < ctx.config.retry.max_retries - 1:
                    await asyncio.sleep(ctx.config.retry.backoff_base * (attempt + 1))
                    continue
                ctx.stats.failed += 1
                return JSONResponse({"error": "Timeout after retries"}, status_code=504)
            except Exception as e:
                ctx.stats.failed += 1
                return JSONResponse({"error": str(e)}, status_code=500)

        if resp is None:
            ctx.stats.failed += 1
            return JSONResponse({"error": "No response"}, status_code=502)

        if resp.status_code >= 400:
            ctx.stats.failed += 1
            log.warning("  ⚠️  [Anthropic] 上游错误 %d: %s", resp.status_code, resp.text[:200])
            return JSONResponse({"error": resp.text[:300]}, status_code=resp.status_code)

        try:
            data = resp.json()
        except json.JSONDecodeError:
            ctx.stats.failed += 1
            log.error("  ❌ [Anthropic] 上游返回非 JSON: %s", resp.text[:300])
            return JSONResponse({"error": "上游返回非 JSON 响应"}, status_code=502)

        thinking_val = None
        content_blocks = data.get("content", [])
        if isinstance(content_blocks, list):
            for b in content_blocks:
                if b.get("type") == "thinking":
                    thinking_val = b.get("thinking")
                    break

        if thinking_val:
            non_thinking = [b for b in content_blocks if b.get("type") != "thinking"]
            synthetic = {
                "role": "assistant",
                "content": non_thinking,
                "thinking": thinking_val
            }
            save_reasoning_anthropic(synthetic, ctx.cache)

        ctx.stats.success += 1
        log.info("  ✅ [Anthropic] 请求完成, 上行=%d 字符", len(json.dumps(body)))
        return JSONResponse(data)

    async def list_models(request: Request):
        headers = {}
        if ctx.config.api_key:
            headers["authorization"] = f"Bearer {ctx.config.api_key}"
        elif auth := request.headers.get("authorization"):
            headers["authorization"] = auth
        elif api_key := request.headers.get("api-key"):
            headers["api-key"] = api_key

        upstream_base = ctx.config.upstream_api_base
        if upstream_base.endswith("/v1"):
            upstream_base = upstream_base[:-3]
        elif upstream_base.endswith("/v1/"):
            upstream_base = upstream_base[:-4]

        if "anthropic" in request.url.path:
            upstream = f"{upstream_base}/anthropic/v1/models"
        else:
            upstream = f"{upstream_base}/v1/models"

        try:
            resp = await ctx.get_client().get(upstream, headers=headers)
            return JSONResponse(resp.json(), status_code=resp.status_code)
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=502)

    async def api_stats(request: Request):
        return JSONResponse({
            "stats": ctx.stats.to_dict(),
            "cache": ctx.cache.info(),
            "upstream": ctx.config.upstream_api_base,
        })

    async def api_logs(request: Request):
        n = min(int(request.query_params.get("count", "50")), 200)
        return JSONResponse({"logs": ctx.log_buffer.entries[-n:]})

    async def api_cache_clear(request: Request):
        await ctx.cache.clear()
        log.info("  🗑️ 缓存已清空")
        return JSONResponse({"ok": True})

    async def dashboard(request: Request):
        try:
            html = await ctx.dashboard_renderer.render(
                HOST=ctx.config.server.host,
                PORT=ctx.config.server.port,
            )
            return HTMLResponse(html)
        except FileNotFoundError as e:
            return HTMLResponse(f"<h1>模板加载失败</h1><pre>{e}</pre>", status_code=500)

    async def root(request: Request):
        return JSONResponse({
            "status": "running",
            "service": "MiMo Reasoning Content Proxy (OSS)",
            "cache_size": ctx.cache.info().get("size", "?"),
            "upstream": ctx.config.upstream_api_base,
            "uptime": ctx.stats.uptime,
            "dashboard": f"http://127.0.0.1:{ctx.config.server.port}/dashboard",
        })

    routes = [
        Route("/", root),
        Route("/health", lambda r: JSONResponse({"ok": True})),
        Route("/v1/chat/completions", chat_completions, methods=["POST"]),
        Route("/chat/completions", chat_completions, methods=["POST"]),
        Route("/v1/messages", anthropic_messages, methods=["POST"]),
        Route("/messages", anthropic_messages, methods=["POST"]),
        Route("/v1/models", list_models),
        Route("/models", list_models),
        Route("/anthropic/v1/models", list_models),
    ]

    if ctx.config.dashboard.enabled:
        routes += [
            Route("/dashboard", dashboard),
            Route("/api/stats", api_stats),
            Route("/api/logs", api_logs),
            Route("/api/cache/clear", api_cache_clear, methods=["POST"]),
        ]

    return routes


def create_app(config: AppConfig) -> Starlette:
    """创建 Starlette 应用"""
    ctx = AppContext(config)

    ctx.log_buffer.setFormatter(logging.Formatter("%(message)s"))
    log.addHandler(ctx.log_buffer)

    routes = create_routes(ctx)

    @asynccontextmanager
    async def lifespan(app):
        ctx.client = httpx.AsyncClient(
            timeout=httpx.Timeout(300, connect=30),
            follow_redirects=True,
        )
        yield
        if ctx.client:
            await ctx.client.aclose()

    return Starlette(routes=routes, lifespan=lifespan)