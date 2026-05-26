"""
Anthropic Messages → Chat Completions 翻译层
将 Anthropic 格式请求翻译为 Chat Completions，响应翻译回 Anthropic Messages SSE
上游只对接 Chat Completions，此模块处理协议转换 + reasoning 缓存
"""

import json
import logging
import uuid

from .cache import MemoryCache
from .proxy import save_reasoning
from .stats import Stats

log = logging.getLogger("mimo-proxy")


def extract_anthropic_messages(body: dict) -> tuple[list, list, dict | None]:
    """Anthropic Messages 请求 → (messages, tools, thinking_config)"""
    messages = []

    # system
    sys_content = body.get("system", "")
    if sys_content:
        if isinstance(sys_content, list):
            texts = [b.get("text", "") for b in sys_content if b.get("type") == "text"]
            sys_content = "\n".join(texts)
        if sys_content:
            messages.append({"role": "system", "content": sys_content})

    # messages
    for msg in body.get("messages", []):
        role = msg.get("role", "user")
        content = msg.get("content", "")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue

        if not isinstance(content, list):
            continue

        if role == "assistant":
            _translate_assistant_blocks(content, messages)
        elif role == "user":
            _translate_user_blocks(content, messages)

    tools = _convert_tools(body.get("tools", []))
    thinking = body.get("thinking")

    return messages, tools, thinking


def _convert_tools(tools: list) -> list:
    """Anthropic tools → Chat Completions tools"""
    result = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        func = {"name": t.get("name", ""), "description": t.get("description", "")}
        if "input_schema" in t:
            func["parameters"] = t["input_schema"]
        result.append({"type": "function", "function": func})
    return result


def _translate_assistant_blocks(blocks: list, messages: list):
    """Anthropic assistant content blocks → Chat Completions assistant message"""
    texts = []
    tool_calls = []

    for b in blocks:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            txt = b.get("text", "")
            if txt:
                texts.append(txt)
        elif btype == "tool_use":
            tool_calls.append({
                "id": b.get("id", ""),
                "type": "function",
                "function": {
                    "name": b.get("name", ""),
                    "arguments": json.dumps(b.get("input", {}), ensure_ascii=False),
                }
            })

    msg = {"role": "assistant", "content": "\n".join(texts) if texts else ""}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    messages.append(msg)


def _translate_user_blocks(blocks: list, messages: list):
    """Anthropic user content blocks → Chat Completions messages"""
    for b in blocks:
        if not isinstance(b, dict):
            continue
        btype = b.get("type")
        if btype == "text":
            txt = b.get("text", "")
            if txt:
                messages.append({"role": "user", "content": txt})
        elif btype == "tool_result":
            content = b.get("content", "")
            if isinstance(content, list):
                texts = [x.get("text", "") for x in content if x.get("type") == "text"]
                content = "\n".join(texts)
            messages.append({
                "role": "tool",
                "tool_call_id": b.get("tool_use_id", ""),
                "content": content or "",
            })


def _translate_cc_response_to_anthropic(data: dict) -> dict:
    """Chat Completions 响应 → Anthropic Messages 格式（非流式）"""
    choice = data.get("choices", [{}])[0]
    msg = choice.get("message", {})
    finish = choice.get("finish_reason", "stop")

    stop_reason_map = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use"}
    stop_reason = stop_reason_map.get(finish, "end_turn")

    content = []
    text = msg.get("content", "")
    if text:
        content.append({"type": "text", "text": text})

    for tc in msg.get("tool_calls", []):
        args = {}
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            pass
        content.append({
            "type": "tool_use",
            "id": tc.get("id", ""),
            "name": tc["function"]["name"],
            "input": args,
        })

    usage = data.get("usage", {})
    return {
        "id": f"msg_{uuid.uuid4().hex[:12]}",
        "type": "message",
        "role": "assistant",
        "content": content,
        "model": msg.get("model", data.get("model", "")),
        "stop_reason": stop_reason,
        "usage": {"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0)},
    }


async def stream_anthropic_sse(
    upstream: str,
    headers: dict,
    payload: dict,
    model: str,
    client,
    cache: MemoryCache,
    stats: Stats,
):
    """Chat Completions 流式响应 → Anthropic Messages SSE 事件"""

    def sse(event: str, d: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(d, ensure_ascii=False)}\n\n".encode()

    msg_id = f"msg_{uuid.uuid4().hex[:12]}"
    acc_content = ""
    acc_reasoning = ""
    acc_tc: list[dict] = []
    input_tokens = 0
    output_tokens = 0
    stop_reason = None

    yield sse("message_start", {
        "type": "message_start",
        "message": {
            "id": msg_id, "type": "message", "role": "assistant",
            "content": [], "model": model, "stop_reason": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}
        }
    })
    yield sse("ping", {"type": "ping"})

    text_idx = 0
    yield sse("content_block_start", {
        "type": "content_block_start", "index": text_idx,
        "content_block": {"type": "text", "text": ""}
    })

    try:
        async with client.stream("POST", upstream, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                err = (await resp.aread()).decode("utf-8", errors="replace")
                log.error("  ❌ [Anthropic→CC] 上游 %d: %s", resp.status_code, err[:200])
                yield sse("error", {"type": "error", "error": {"type": "api_error", "message": err[:200]}})
                return

            buf = ""
            async for raw_bytes in resp.aiter_bytes():
                buf += raw_bytes.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        continue
                    try:
                        chunk = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    usage = chunk.get("usage")
                    if usage:
                        input_tokens = usage.get("prompt_tokens", 0)
                        output_tokens = usage.get("completion_tokens", 0)

                    if "choices" not in chunk or not chunk["choices"]:
                        continue

                    delta = chunk["choices"][0].get("delta", {})
                    finish = chunk["choices"][0].get("finish_reason")

                    rc = delta.get("reasoning_content", "")
                    if rc:
                        acc_reasoning += rc

                    content = delta.get("content", "")
                    if content:
                        acc_content += content
                        yield sse("content_block_delta", {
                            "type": "content_block_delta", "index": text_idx,
                            "delta": {"type": "text_delta", "text": content}
                        })

                    for tc in delta.get("tool_calls", []):
                        idx = tc.get("index", 0)
                        while len(acc_tc) <= idx:
                            acc_tc.append({"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
                        if tc.get("id"):
                            acc_tc[idx]["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            acc_tc[idx]["function"]["name"] += fn["name"]
                        if fn.get("arguments"):
                            acc_tc[idx]["function"]["arguments"] += fn["arguments"]

                    if finish:
                        stop_reason_map = {
                            "stop": "end_turn", "length": "max_tokens",
                            "tool_calls": "tool_use", "function_call": "tool_use",
                        }
                        stop_reason = stop_reason_map.get(finish, "end_turn")

    except Exception as e:
        log.error("  ❌ [Anthropic→CC] 流错误: %s", e, exc_info=True)
        yield sse("error", {"type": "error", "error": {"type": "api_error", "message": str(e)}})
        return

    # 缓存 reasoning
    if acc_reasoning and (acc_content or acc_tc):
        save_reasoning({
            "role": "assistant", "content": acc_content,
            "tool_calls": acc_tc, "reasoning_content": acc_reasoning,
        }, cache)

    yield sse("content_block_stop", {"type": "content_block_stop", "index": text_idx})

    tc_start_idx = text_idx + 1
    for i, tc in enumerate(acc_tc):
        idx = tc_start_idx + i
        args = {}
        try:
            args = json.loads(tc["function"]["arguments"])
        except (json.JSONDecodeError, KeyError):
            pass
        yield sse("content_block_start", {
            "type": "content_block_start", "index": idx,
            "content_block": {"type": "tool_use", "id": tc["id"], "name": tc["function"]["name"], "input": {}}
        })
        yield sse("content_block_delta", {
            "type": "content_block_delta", "index": idx,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(args, ensure_ascii=False)}
        })
        yield sse("content_block_stop", {"type": "content_block_stop", "index": idx})

    if not stop_reason:
        stop_reason = "end_turn"
    yield sse("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"output_tokens": output_tokens}
    })
    yield sse("message_stop", {"type": "message_stop"})

    stats.success += 1
    log.info("  ✅ [Anthropic→CC] 流完成, text=%d chars, tools=%d, reasoning=%d chars",
             len(acc_content), len(acc_tc), len(acc_reasoning))