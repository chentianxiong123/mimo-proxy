"""
路由定义模块
支持 OpenAI Chat Completions 协议和 Anthropic Messages 协议
"""

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
    UpstreamError,
)
from .stats import Stats
from .upstreams import UpstreamStore
from .responses import extract_messages, stream_responses_sse
from .anthropic_translate import extract_anthropic_messages, stream_anthropic_sse, _translate_cc_response_to_anthropic

log = logging.getLogger("mimo-proxy")


class AppContext:
    """应用上下文，持有所有共享状态"""

    def __init__(self, config: AppConfig):
        self.config = config
        self.stats = Stats()
        self.cache: MemoryCache = create_cache(
            backend=config.cache.backend,
            max_size=config.cache.max_size,
            ttl=config.cache.ttl_seconds,
            db_path=config.cache.db_path,
        )
        self.upstreams = UpstreamStore(db_path="upstreams.db")
        self.upstreams.seed(config.upstream_api_base)
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

    def get_active_upstream_url(self) -> str:
        u = self.upstreams.get_active()
        return u.url if u else self.config.upstream_api_base

    def get_upstream_url(self, model: str) -> str:
        u = self.upstreams.get_by_model(model)
        return u.url if u else self.config.upstream_api_base


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
        if auth := request.headers.get("authorization"):
            headers["authorization"] = auth

        upstream_base = ctx.get_upstream_url(model)
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
            gen = stream_proxy(upstream, headers, body, ctx.get_client(), ctx.cache, ctx.stats)
            try:
                first = await gen.__anext__()
            except StopAsyncIteration:
                ctx.stats.failed += 1
                return JSONResponse({"error": "empty stream"}, status_code=502)
            except UpstreamError as e:
                ctx.stats.failed += 1
                return JSONResponse({"error": e.body[:300]}, status_code=e.status_code)

            async def stream_with_first():
                yield first
                try:
                    async for chunk in gen:
                        yield chunk
                    ctx.stats.success += 1
                except UpstreamError:
                    ctx.stats.failed += 1

            return StreamingResponse(
                stream_with_first(),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        try:
            resp = await ctx.get_client().post(upstream, headers=headers, json=body)
        except httpx.TimeoutException:
            ctx.stats.failed += 1
            return JSONResponse({"error": {"message": "upstream timeout", "type": "timeout"}}, status_code=504)
        except Exception as e:
            ctx.stats.failed += 1
            return JSONResponse({"error": {"message": str(e), "type": "proxy_error"}}, status_code=502)

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
        """Anthropic 兼容协议 → 翻译为 Chat Completions 发送"""
        ctx.stats.requests += 1
        client_ip = request.client.host if request.client else "?"
        log.info("")
        log.info("━━━ [Anthropic→CC] 收到请求 from %s ━━━", client_ip)

        try:
            body = await request.json()
        except Exception:
            ctx.stats.failed += 1
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        model = body.get("model", "?")
        is_stream = body.get("stream", False)
        log.info("  📋 模型=%s | 流式=%s", model, is_stream)

        # 翻译成 Chat Completions
        messages, tools, thinking = extract_anthropic_messages(body)
        log.info("  🔄 [Anthropic→CC] 翻译完成: %d 条消息, %d 个工具", len(messages), len(tools))

        # reasoning 注入（用 OpenAI 的 inject_reasoning，因为已经是 CC 格式）
        if thinking:
            log.info("  🧠 客户端请求 thinking=%s", thinking)

        inj, deg = inject_reasoning(messages, ctx.cache, ctx.stats)
        if inj or deg:
            log.info("  📊 [Anthropic→CC] 注入=%d 条 | 降级=%d 条", inj, deg)

        headers = {}
        if auth := request.headers.get("authorization"):
            headers["authorization"] = auth

        upstream_base = ctx.get_upstream_url(model)
        if upstream_base.endswith("/v1"):
            upstream_base = upstream_base[:-3]
        elif upstream_base.endswith("/v1/"):
            upstream_base = upstream_base[:-4]
        upstream = f"{upstream_base}/v1/chat/completions"
        log.info("  🎯 上游地址: %s", upstream)

        # 构建 Chat Completions payload
        cc_body = {
            "model": model,
            "messages": messages,
            "stream": is_stream,
        }
        if tools:
            cc_body["tools"] = tools

        if is_stream:
            ctx.stats.stream += 1
            return StreamingResponse(
                stream_anthropic_sse(upstream, headers, cc_body, model, ctx.get_client(), ctx.cache, ctx.stats),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )

        # 非流式
        try:
            resp = await ctx.get_client().post(upstream, headers=headers, json=cc_body)
        except httpx.TimeoutException:
            ctx.stats.failed += 1
            return JSONResponse({"error": {"message": "upstream timeout", "type": "timeout"}}, status_code=504)
        except Exception as e:
            ctx.stats.failed += 1
            return JSONResponse({"error": {"message": str(e), "type": "proxy_error"}}, status_code=502)

        if resp.status_code >= 400:
            ctx.stats.failed += 1
            log.warning("  ⚠️  [Anthropic→CC] 上游错误 %d: %s", resp.status_code, resp.text[:200])
            return JSONResponse({"error": resp.text[:300]}, status_code=resp.status_code)

        try:
            data = resp.json()
        except json.JSONDecodeError:
            ctx.stats.failed += 1
            return JSONResponse({"error": "上游返回非 JSON"}, status_code=502)

        # 缓存 reasoning
        for ch in data.get("choices", []):
            msg = ch.get("message", {})
            save_reasoning(msg, ctx.cache)

        # 翻译回 Anthropic 格式
        output_items = _translate_cc_response_to_anthropic(data)
        ctx.stats.success += 1
        log.info("  ✅ [Anthropic→CC] 非流式完成")
        return JSONResponse(output_items)

    async def responses_api(request: Request):
        """OpenAI Responses API 兼容协议"""
        ctx.stats.requests += 1
        client_ip = request.client.host if request.client else "?"
        log.info("")
        log.info("━━━ [Responses] 收到请求 from %s ━━━", client_ip)

        try:
            body = await request.json()
        except Exception:
            ctx.stats.failed += 1
            return JSONResponse({"error": "Invalid JSON"}, status_code=400)

        messages, tools, tool_choice = extract_messages(body)
        model = body.get("model", "?")
        log.info("  📋 模型=%s | 消息数=%d | 工具=%d", model, len(messages), len(tools))

        inj, deg = inject_reasoning(messages, ctx.cache, ctx.stats)
        if inj or deg:
            log.info("  📊 [Responses] 注入=%d 条 | 降级=%d 条", inj, deg)

        headers = {}
        if auth := request.headers.get("authorization"):
            headers["authorization"] = auth

        upstream_base = ctx.get_upstream_url(model)
        if "/anthropic" in upstream_base:
            upstream_base = upstream_base.replace("/anthropic", "")
        if upstream_base.endswith("/v1"):
            upstream_base = upstream_base[:-3]
        elif upstream_base.endswith("/v1/"):
            upstream_base = upstream_base[:-4]
        upstream = f"{upstream_base}/v1/chat/completions"
        log.info("  🎯 上游地址: %s", upstream)

        ctx.stats.stream += 1
        return StreamingResponse(
            stream_responses_sse(upstream, headers, body, messages, tools, tool_choice, model, ctx.get_client(), ctx.cache, ctx.stats),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def list_models(request: Request):
        headers = {}
        if auth := request.headers.get("authorization"):
            headers["authorization"] = auth
        elif api_key := request.headers.get("api-key"):
            headers["api-key"] = api_key

        upstream_base = ctx.get_upstream_url(model)
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
            "upstream": ctx.get_active_upstream_url(),
        })

    async def api_logs(request: Request):
        n = min(int(request.query_params.get("count", "50")), 200)
        return JSONResponse({"logs": ctx.log_buffer.entries[-n:]})

    async def api_cache_clear(request: Request):
        await ctx.cache.clear()
        log.info("  🗑️ 缓存已清空")
        return JSONResponse({"ok": True})

    async def api_cache_config(request: Request):
        """GET: 获取缓存配置 | PUT: 更新缓存配置"""
        if request.method == "GET":
            return JSONResponse(ctx.cache.info())

        body = await request.json()
        cache = ctx.cache

        if "ttl" in body:
            ttl = int(body["ttl"])
            if hasattr(cache, "set_ttl"):
                cache.set_ttl(ttl)

        if "hot_max_size" in body:
            n = int(body["hot_max_size"])
            if hasattr(cache, "set_hot_max_size"):
                cache.set_hot_max_size(n)
            elif hasattr(cache, "set_max_size"):
                cache.set_max_size(n)

        if "cold_max_size" in body:
            n = int(body["cold_max_size"])
            if hasattr(cache, "set_cold_max_size"):
                cache.set_cold_max_size(n)

        log.info("  ⚙️ 缓存配置已更新")
        return JSONResponse(ctx.cache.info())

    async def api_upstreams(request: Request):
        """GET: 列出所有上游 | POST: 添加新上游"""
        if request.method == "GET":
            upstreams = ctx.upstreams.list_all()
            return JSONResponse({
                "upstreams": [u.to_dict() for u in upstreams],
                "active_id": next((u.id for u in upstreams if u.is_active), None),
            })

        body = await request.json()
        name = body.get("name", "").strip()
        url = body.get("url", "").strip()
        model = body.get("model", "").strip()
        if not url:
            return JSONResponse({"error": "url 不能为空"}, status_code=400)
        if not name:
            name = url.split("//")[-1].split("/")[0][:30]
        u = ctx.upstreams.add(name, url, model)
        log.info("  ➕ [Upstream] 添加上游: %s (%s) model=%s", name, url, model or "(任意)")
        return JSONResponse(u.to_dict())

    async def api_upstream_switch(request: Request):
        """PUT: 切换活跃上游"""
        body = await request.json()
        upstream_id = body.get("id")
        if not upstream_id:
            return JSONResponse({"error": "id 不能为空"}, status_code=400)
        if ctx.upstreams.switch(upstream_id):
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "上游不存在"}, status_code=404)

    async def api_upstream_delete(request: Request):
        """DELETE: 删除上游"""
        upstream_id = int(request.path_params["id"])
        if ctx.upstreams.delete(upstream_id):
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "不能删除当前激活的上游或上游不存在"}, status_code=400)

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
            "upstream": ctx.get_active_upstream_url(),
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
        Route("/v1/responses", responses_api, methods=["POST"]),
        Route("/responses", responses_api, methods=["POST"]),
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
            Route("/api/cache/config", api_cache_config, methods=["GET", "PUT"]),
            Route("/api/upstreams", api_upstreams, methods=["GET", "POST"]),
            Route("/api/upstreams/active", api_upstream_switch, methods=["PUT"]),
            Route("/api/upstreams/{id:int}", api_upstream_delete, methods=["DELETE"]),
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