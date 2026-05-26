"""
核心代理逻辑模块
支持 OpenAI 协议与 Anthropic 协议下的 reasoning 注入与流式/非流式代理
"""

import json
import logging

import httpx

from .cache import MemoryCache
from .stats import Stats

log = logging.getLogger("mimo-proxy")


class UpstreamError(Exception):
    """上游返回非 200 状态码时抛出，携带原始状态码和错误体"""
    def __init__(self, status_code: int, body: str):
        self.status_code = status_code
        self.body = body
        super().__init__(f"upstream {status_code}")


def inject_reasoning(messages: list[dict], cache: MemoryCache, stats: Stats) -> tuple[int, int]:
    """[OpenAI 协议] 注入缓存的 reasoning_content，无缓存则填充空值保留 tool_calls"""
    injected = degraded = 0

    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant" or not msg.get("tool_calls") or msg.get("reasoning_content"):
            continue

        cached = None
        h = MemoryCache.msg_hash(msg)
        cached_raw = cache.get_raw(h)
        if cached_raw:
            cached = cached_raw[0]

        if not cached:
            for tid in MemoryCache.tc_ids(msg):
                rc = cache.get_tc_index(tid)
                if rc is not None:
                    cached = rc
                    break

        if cached:
            msg["reasoning_content"] = cached
            injected += 1
            stats.cache_hits += 1
            log.info("  ⚡ [OpenAI] 注入 reasoning 成功 → msg[%d] (%d 字符)", i, len(cached))
        else:
            ids = MemoryCache.tc_ids(msg)
            stats.cache_misses += 1
            log.warning("  ⚠️  [OpenAI] 无缓存 msg[%d] ids=%s → 填充空 reasoning", i, ids)
            msg["reasoning_content"] = ""
            degraded += 1
            stats.degraded += 1

    return injected, degraded


def save_reasoning(msg: dict, cache: MemoryCache):
    """[OpenAI 协议] 缓存 assistant 消息中的 reasoning_content"""
    rc = msg.get("reasoning_content")
    if rc and msg.get("tool_calls"):
        h = MemoryCache.msg_hash(msg)
        tc_ids = MemoryCache.tc_ids(msg)
        cache.delete(h)
        cache.set_raw(h, rc)
        cache.set_tc_index(tc_ids, rc)
        log.info("  💾 [OpenAI] 已缓存 reasoning (%d 字符), ids=%s", len(rc), tc_ids)


def inject_reasoning_anthropic(messages: list[dict], cache: MemoryCache, stats: Stats) -> tuple[int, int]:
    """[Anthropic 协议] 注入缓存的 thinking 块，无缓存则填充空 thinking 保留 tool_use"""
    injected = degraded = 0

    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant":
            continue

        content = msg.get("content")
        if not isinstance(content, list):
            continue

        has_tool_use = any(b.get("type") == "tool_use" for b in content)
        has_thinking = any(b.get("type") == "thinking" for b in content)

        if not has_tool_use or has_thinking:
            continue

        h = MemoryCache.msg_hash_anthropic(msg)
        cached = None
        cached_raw = cache.get_raw(h)
        if cached_raw:
            cached = cached_raw[0]

        if not cached:
            tc_ids = MemoryCache.tc_ids_anthropic(msg)
            for tid in tc_ids:
                rc = cache.get_tc_index(tid)
                if rc is not None:
                    cached = rc
                    break

        if cached:
            content_blocks = msg.get("content", [])
            if isinstance(content_blocks, list):
                thinking_block = {"type": "thinking", "thinking": cached}
                msg["content"] = [thinking_block] + content_blocks
            injected += 1
            stats.cache_hits += 1
            log.info("  ⚡ [Anthropic] 注入 thinking 成功 → msg[%d] (%d 字符), hash=%s", i, len(cached), h[:8])
        else:
            stats.cache_misses += 1
            log.warning("  ⚠️  [Anthropic] 无缓存 msg[%d] → 填充空 thinking", i)
            content_blocks = msg.get("content", [])
            if isinstance(content_blocks, list):
                thinking_block = {"type": "thinking", "thinking": ""}
                msg["content"] = [thinking_block] + content_blocks
            degraded += 1
            stats.degraded += 1

    return injected, degraded


def save_reasoning_anthropic(msg: dict, cache: MemoryCache):
    """[Anthropic 协议] 缓存 assistant 消息中的 thinking"""
    thinking = msg.get("thinking")
    content = msg.get("content")

    has_tool_use = False
    if isinstance(content, list):
        has_tool_use = any(b.get("type") == "tool_use" for b in content)

    if thinking and has_tool_use:
        h = MemoryCache.msg_hash_anthropic(msg)
        tc_ids = MemoryCache.tc_ids_anthropic(msg)
        cache.delete(h)
        cache.set_raw(h, thinking)
        cache.set_tc_index(tc_ids, thinking)
        log.info("  💾 [Anthropic] 已缓存 thinking (%d 字符), hash=%s, ids=%s", len(thinking), h[:8], tc_ids)


def _sse(data: str) -> bytes:
    return f"data: {data}\n\n".encode()


async def stream_proxy(
    upstream: str,
    headers: dict,
    body: dict,
    client: httpx.AsyncClient,
    cache: MemoryCache,
    stats: Stats,
):
    """[OpenAI] 流式转发，上游错误原样透传（不重试，不改状态码）"""
    acc_content = ""
    acc_reasoning = ""
    acc_tc: list[dict] = []

    try:
        async with client.stream("POST", upstream, headers=headers, json=body) as resp:
            if resp.status_code != 200:
                err = (await resp.aread()).decode("utf-8", errors="replace")
                log.warning("  ⚠️  [OpenAI] 上游返回 %d: %s", resp.status_code, err[:200])
                raise UpstreamError(resp.status_code, err)

            log.info("  🔗 [OpenAI] 流式连接成功, 开始接收数据...")
            buf = ""
            async for chunk in resp.aiter_bytes():
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")

                    if line.startswith("data: "):
                        payload = line[6:].strip()
                        if payload == "[DONE]":
                            if acc_reasoning and (acc_content or acc_tc):
                                synthetic = {
                                    "role": "assistant",
                                    "content": acc_content,
                                    "tool_calls": acc_tc,
                                    "reasoning_content": acc_reasoning,
                                }
                                save_reasoning(synthetic, cache)
                            yield _sse("[DONE]")
                            continue

                        try:
                            chunk_data = json.loads(payload)
                            delta = chunk_data.get("choices", [{}])[0].get("delta", {})
                            if v := delta.get("reasoning_content"):
                                acc_reasoning += v
                            if v := delta.get("content"):
                                acc_content += v
                            for tc in delta.get("tool_calls") or []:
                                idx = tc.get("index", 0)
                                while len(acc_tc) <= idx:
                                    acc_tc.append({
                                        "id": "", "type": "function",
                                        "function": {"name": "", "arguments": ""}
                                    })
                                if tc.get("id"):
                                    acc_tc[idx]["id"] = tc["id"]
                                fn = tc.get("function", {})
                                if fn.get("name"):
                                    acc_tc[idx]["function"]["name"] += fn["name"]
                                if fn.get("arguments"):
                                    acc_tc[idx]["function"]["arguments"] += fn["arguments"]
                        except (json.JSONDecodeError, IndexError, KeyError):
                            pass
                        yield _sse(payload)
                    elif line.strip() == "":
                        yield b"\n"
                    elif line.startswith(":"):
                        yield (line + "\n\n").encode()
                    else:
                        yield (line + "\n").encode()

    except UpstreamError:
        raise
    except httpx.TimeoutException as e:
        log.warning("  ⏰ [OpenAI] 流式超时: %s", e)
        raise UpstreamError(504, str(e))
    except Exception as e:
        log.error("  ❌ [OpenAI] 流式错误: %s", e, exc_info=True)
        raise UpstreamError(502, str(e))


