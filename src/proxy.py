"""
核心代理逻辑模块
支持 OpenAI 协议与 Anthropic 协议下的 reasoning 注入与流式/非流式代理
"""

import asyncio
import json
import logging

import httpx

from .cache import MemoryCache
from .stats import Stats
from .config import RetryConfig

log = logging.getLogger("mimo-proxy")


def inject_reasoning(messages: list[dict], cache: MemoryCache, stats: Stats) -> tuple[int, int]:
    """[OpenAI 协议] 注入缓存的 reasoning_content，无缓存则降级剥离 tool_calls"""
    injected = degraded = 0

    for i, msg in enumerate(messages):
        if msg.get("role") != "assistant" or not msg.get("tool_calls") or msg.get("reasoning_content"):
            continue

        cached = None
        h = cache.msg_hash(msg)
        cached_val = cache._data.get(h)
        if cached_val:
            import time
            val, ts = cached_val
            if time.time() - ts < cache.ttl:
                cached = val
            else:
                del cache._data[h]

        if not cached:
            for tid in cache.tc_ids(msg):
                if tid in cache._tc_index:
                    cached = cache._tc_index[tid]
                    break

        if cached:
            msg["reasoning_content"] = cached
            injected += 1
            stats.cache_hits += 1
            log.info("  ⚡ [OpenAI] 注入 reasoning 成功 → msg[%d] (%d 字符)", i, len(cached))
        else:
            ids = MemoryCache.tc_ids(msg)
            stats.cache_misses += 1
            log.warning("  ⚠️  [OpenAI] 无缓存 msg[%d] ids=%s → 降级处理", i, ids)

            content = msg.get("content") or ""
            summary = " ".join(
                f"[调用了 {tc.get('function', {}).get('name', '?')}]"
                for tc in msg.get("tool_calls") or []
            )
            msg["content"] = f"{content} {summary}".strip()
            del msg["tool_calls"]
            degraded += 1
            stats.degraded += 1

    return injected, degraded


def save_reasoning(msg: dict, cache: MemoryCache):
    """[OpenAI 协议] 缓存 assistant 消息中的 reasoning_content"""
    rc = msg.get("reasoning_content")
    if rc and msg.get("tool_calls"):
        h = cache.msg_hash(msg)
        tc_ids = cache.tc_ids(msg)
        import time as _time
        if h in cache._data:
            del cache._data[h]
        cache._data[h] = (rc, _time.time())
        while len(cache._data) > cache.max_size:
            cache._data.popitem(last=False)
        for tid in tc_ids:
            cache._tc_index[tid] = rc
        log.info("  💾 [OpenAI] 已缓存 reasoning (%d 字符), ids=%s", len(rc), tc_ids)


def inject_reasoning_anthropic(messages: list[dict], cache: MemoryCache, stats: Stats) -> tuple[int, int]:
    """[Anthropic 协议] 注入缓存的 thinking 块，无缓存则降级剥离 tool_use 避免 400"""
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

        cached_val = cache._data.get(h)
        if cached_val:
            import time
            val, ts = cached_val
            if time.time() - ts < cache.ttl:
                cached = val
            else:
                del cache._data[h]

        if not cached:
            tc_ids = MemoryCache.tc_ids_anthropic(msg)
            for tid in tc_ids:
                if tid in cache._tc_index:
                    cached = cache._tc_index[tid]
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
            log.warning("  ⚠️  [Anthropic] 无缓存 msg[%d] → 降级处理 (剥离 tool_use)", i)

            new_content = []
            for b in content:
                if b.get("type") == "tool_use":
                    tname = b.get("name", "?")
                    tinput = json.dumps(b.get("input", {}), ensure_ascii=False)
                    new_content.append({
                        "type": "text",
                        "text": f"\n[调用了工具 {tname}，参数: {tinput}]\n"
                    })
                else:
                    new_content.append(b)
            msg["content"] = new_content
            degraded += 1
            stats.degraded += 1

    if degraded > 0:
        for msg in messages:
            if msg.get("role") == "user":
                u_content = msg.get("content")
                if isinstance(u_content, list):
                    new_u_content = []
                    for b in u_content:
                        if b.get("type") == "tool_result":
                            t_result = b.get("content") or ""
                            if isinstance(t_result, list):
                                t_result_str = "\n".join(x.get("text", "") for x in t_result if x.get("type") == "text")
                            else:
                                t_result_str = str(t_result)
                            new_u_content.append({
                                "type": "text",
                                "text": f"\n[工具返回]: {t_result_str}\n"
                            })
                        else:
                            new_u_content.append(b)
                    msg["content"] = new_u_content

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

        import time as _time
        if h in cache._data:
            del cache._data[h]
        cache._data[h] = (thinking, _time.time())
        while len(cache._data) > cache.max_size:
            cache._data.popitem(last=False)
        for tid in tc_ids:
            cache._tc_index[tid] = thinking
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
    retry_config: RetryConfig,
):
    """[OpenAI] 流式转发，同时累积 reasoning_content 用于缓存"""
    acc_content = ""
    acc_reasoning = ""
    acc_tc: list[dict] = []

    for attempt in range(retry_config.max_retries):
        try:
            async with client.stream("POST", upstream, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    err = (await resp.aread()).decode("utf-8", errors="replace")
                    log.warning("  ⚠️  [OpenAI] 上游返回 %d (尝试 %d): %s", resp.status_code, attempt + 1, err[:200])
                    if resp.status_code < 500:
                        yield _sse(err)
                        return
                    if attempt < retry_config.max_retries - 1:
                        await asyncio.sleep(retry_config.backoff_base * (attempt + 1))
                        continue
                    yield _sse(json.dumps({"error": {"message": err[:200], "code": "502"}}))
                    return

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
                return

        except httpx.TimeoutException as e:
            log.warning("  ⏰ [OpenAI] 流式超时 (尝试 %d): %s", attempt + 1, e)
            stats.retries += 1
            if attempt < retry_config.max_retries - 1:
                await asyncio.sleep(retry_config.backoff_base * (attempt + 1))
        except Exception as e:
            log.error("  ❌ [OpenAI] 流式错误: %s", e, exc_info=True)
            yield _sse(json.dumps({"error": str(e)}))
            return

    yield _sse(json.dumps({"error": {"message": "流式请求重试后仍然失败", "code": "502"}}))


async def stream_proxy_anthropic(
    upstream: str,
    headers: dict,
    body: dict,
    client: httpx.AsyncClient,
    cache: MemoryCache,
    stats: Stats,
    retry_config: RetryConfig,
):
    """[Anthropic] 流式转发，同时累积 thinking 用于缓存"""
    acc_thinking = ""
    blocks = []

    for attempt in range(retry_config.max_retries):
        try:
            async with client.stream("POST", upstream, headers=headers, json=body) as resp:
                if resp.status_code != 200:
                    err = (await resp.aread()).decode("utf-8", errors="replace")
                    log.warning("  ⚠️  [Anthropic] 上游返回 %d (尝试 %d): %s", resp.status_code, attempt + 1, err[:200])
                    if resp.status_code < 500:
                        yield _sse(err)
                        return
                    if attempt < retry_config.max_retries - 1:
                        await asyncio.sleep(retry_config.backoff_base * (attempt + 1))
                        continue
                    yield _sse(json.dumps({"error": {"message": err[:200], "code": "502"}}))
                    return

                log.info("  🔗 [Anthropic] 流式连接成功, 开始接收数据...")
                buf = ""
                async for chunk in resp.aiter_bytes():
                    buf += chunk.decode("utf-8", errors="replace")
                    while "\n" in buf:
                        line, buf = buf.split("\n", 1)
                        line = line.rstrip("\r")

                        if line.startswith("data: "):
                            payload = line[6:].strip()
                            if payload == "[DONE]":
                                has_tool_use = any(b.get("type") == "tool_use" for b in blocks)
                                if acc_thinking and has_tool_use:
                                    non_thinking_blocks = [b for b in blocks if b.get("type") != "thinking"]
                                    synthetic = {
                                        "role": "assistant",
                                        "content": non_thinking_blocks,
                                        "thinking": acc_thinking
                                    }
                                    save_reasoning_anthropic(synthetic, cache)
                                yield _sse("[DONE]")
                                continue

                            try:
                                chunk_data = json.loads(payload)
                                event_type = chunk_data.get("type")

                                if event_type == "content_block_start":
                                    idx = chunk_data.get("index", 0)
                                    block_info = chunk_data.get("content_block", {})
                                    btype = block_info.get("type")

                                    while len(blocks) <= idx:
                                        blocks.append({})

                                    if btype == "thinking":
                                        blocks[idx] = {"type": "thinking", "thinking": ""}
                                    elif btype == "text":
                                        blocks[idx] = {"type": "text", "text": ""}
                                    elif btype == "tool_use":
                                        blocks[idx] = {
                                            "type": "tool_use",
                                            "id": block_info.get("id", ""),
                                            "name": block_info.get("name", ""),
                                            "input_json": ""
                                        }

                                elif event_type == "content_block_delta":
                                    idx = chunk_data.get("index", 0)
                                    delta = chunk_data.get("delta", {})
                                    dtype = delta.get("type")

                                    while len(blocks) <= idx:
                                        blocks.append({})

                                    if dtype == "thinking_delta":
                                        t_val = delta.get("thinking", "")
                                        acc_thinking += t_val
                                        if "thinking" in blocks[idx]:
                                            blocks[idx]["thinking"] += t_val
                                    elif dtype == "text_delta":
                                        txt_val = delta.get("text", "")
                                        if "text" in blocks[idx]:
                                            blocks[idx]["text"] += txt_val
                                    elif dtype == "input_json_delta":
                                        js_val = delta.get("partial_json", "")
                                        if "input_json" in blocks[idx]:
                                            blocks[idx]["input_json"] += js_val

                                elif event_type == "message_stop":
                                    has_tool_use = any(b.get("type") == "tool_use" for b in blocks)
                                    if acc_thinking and has_tool_use:
                                        non_thinking_blocks = [b for b in blocks if b.get("type") != "thinking"]
                                        synthetic = {
                                            "role": "assistant",
                                            "content": non_thinking_blocks,
                                            "thinking": acc_thinking
                                        }
                                        save_reasoning_anthropic(synthetic, cache)

                            except Exception:
                                pass

                            yield _sse(payload)
                        elif line.strip() == "":
                            yield b"\n"
                        elif line.startswith(":"):
                            yield (line + "\n\n").encode()
                        else:
                            yield (line + "\n").encode()
                return

        except httpx.TimeoutException as e:
            log.warning("  ⏰ [Anthropic] 流式超时 (尝试 %d): %s", attempt + 1, e)
            stats.retries += 1
            if attempt < retry_config.max_retries - 1:
                await asyncio.sleep(retry_config.backoff_base * (attempt + 1))
        except Exception as e:
            log.error("  ❌ [Anthropic] 流式错误: %s", e, exc_info=True)
            yield _sse(json.dumps({"error": str(e)}))
            return

    yield _sse(json.dumps({"error": {"message": "Anthropic 流式请求重试后仍然失败", "code": "502"}}))