"""
Responses API → Chat Completions 翻译层
路由: POST /v1/responses, POST /responses
将 OpenAI Responses API 请求翻译为 Chat Completions，响应翻译回 Responses API SSE
"""

import json
import logging
import uuid

import httpx

from .cache import MemoryCache
from .proxy import inject_reasoning, save_reasoning
from .stats import Stats

log = logging.getLogger("mimo-proxy")


def convert_tools(tools: list) -> list:
    """Responses API tools → Chat Completions tools

    兼容两种格式：
    - 嵌套: {"type": "function", "function": {"name": "x", ...}}
    - 扁平: {"type": "function", "name": "x", "description": "x", "parameters": {...}}
    """
    result = []
    for tool in tools:
        if not isinstance(tool, dict) or tool.get("type") != "function":
            continue
        name = tool.get("name", "")
        if "function" in tool:
            func = tool["function"]
            cc_func = {"name": func.get("name", name), "description": func.get("description", "")}
            if "parameters" in func:
                cc_func["parameters"] = _clean_schema(func["parameters"])
        else:
            cc_func = {"name": name, "description": tool.get("description", "")}
            if "parameters" in tool:
                cc_func["parameters"] = _clean_schema(tool["parameters"])
        result.append({"type": "function", "function": cc_func})
    return result


def _clean_schema(obj):
    """递归清除 JSON Schema 中不兼容的字段"""
    if not isinstance(obj, dict):
        return obj
    cleaned = {}
    for k, v in obj.items():
        if k in ("additionalProperties", "strict"):
            continue
        if isinstance(v, dict):
            cleaned[k] = _clean_schema(v)
        elif isinstance(v, list):
            cleaned[k] = [_clean_schema(i) if isinstance(i, dict) else i for i in v]
        else:
            cleaned[k] = v
    return cleaned


def convert_tool_choice(tc):
    """Responses API tool_choice → Chat Completions tool_choice"""
    if tc is None:
        return "auto"
    if isinstance(tc, str):
        return tc
    if isinstance(tc, dict) and tc.get("type") == "function":
        return {"type": "function", "function": {"name": tc.get("name", "")}}
    return "auto"


def extract_messages(data: dict) -> tuple[list, list, str]:
    """从 Responses API 请求提取 messages, tools, tool_choice"""
    tools = convert_tools(data.get("tools", []))
    tool_choice = convert_tool_choice(data.get("tool_choice"))

    if "input" not in data:
        if "messages" in data:
            return data["messages"], tools, tool_choice
        return [], tools, tool_choice

    inp = data["input"]
    if isinstance(inp, str):
        messages = []
        if data.get("instructions"):
            messages.append({"role": "system", "content": data["instructions"]})
        messages.append({"role": "user", "content": inp})
        return messages, tools, tool_choice

    if not isinstance(inp, list):
        return [], tools, tool_choice

    messages = []
    if data.get("instructions"):
        messages.append({"role": "system", "content": data["instructions"]})

    pending_tc = []
    pending_reasoning = ""

    def flush():
        nonlocal pending_tc, pending_reasoning
        if pending_tc:
            msg = {"role": "assistant", "content": "", "tool_calls": pending_tc}
            if pending_reasoning:
                msg["reasoning_content"] = pending_reasoning
            messages.append(msg)
            pending_tc = []
            pending_reasoning = ""

    for item in inp:
        if not isinstance(item, dict):
            continue
        t = item.get("type")

        # 兼容无 type 字段的格式：Codex CLI 可能省略 type
        if not t:
            if "role" in item:
                t = "message"
            elif "call_id" in item and "output" in item:
                t = "function_call_output"
            elif "call_id" in item or ("name" in item and "arguments" in item):
                t = "function_call"

        if t == "message":
            flush()
            role = {"developer": "system"}.get(item.get("role", "user"), item.get("role", "user"))
            content = item.get("content", "")
            if isinstance(content, list):
                texts = []
                for c in content:
                    if not isinstance(c, dict):
                        continue
                    ct = c.get("type")
                    if ct in ("text", "input_text", "output_text"):
                        txt = c.get("text", "")
                        if txt.strip():
                            texts.append(txt)
                if texts:
                    messages.append({"role": role, "content": "\n".join(texts)})
            elif isinstance(content, str) and content.strip():
                messages.append({"role": role, "content": content.strip()})

        elif t == "function_call":
            pending_tc.append({
                "id": item.get("call_id", ""),
                "type": "function",
                "function": {
                    "name": item.get("name", ""),
                    "arguments": item.get("arguments", ""),
                }
            })
            if item.get("reasoning_content") and not pending_reasoning:
                pending_reasoning = item["reasoning_content"]

        elif t == "function_call_output":
            flush()
            messages.append({
                "role": "tool",
                "tool_call_id": item.get("call_id", ""),
                "content": item.get("output", ""),
            })

    flush()

    # 重排：tool 消息紧跟对应的 assistant
    reordered = []
    i = 0
    while i < len(messages):
        msg = messages[i]
        if msg.get("role") == "assistant" and msg.get("tool_calls"):
            expected = {tc["id"] for tc in msg["tool_calls"]}
            tool_msgs = []
            sys_msgs = []
            j = i + 1
            while j < len(messages) and expected:
                nxt = messages[j]
                if nxt.get("role") == "tool" and nxt.get("tool_call_id") in expected:
                    expected.remove(nxt["tool_call_id"])
                    tool_msgs.append(nxt)
                elif nxt.get("role") in ("system", "developer"):
                    sys_msgs.append(nxt)
                else:
                    break
                j += 1
            reordered.extend(sys_msgs)
            reordered.append(msg)
            reordered.extend(tool_msgs)
            i = j
        else:
            reordered.append(msg)
            i += 1

    return reordered, tools, tool_choice


async def stream_responses_sse(
    upstream: str,
    headers: dict,
    body: dict,
    messages: list,
    tools: list,
    tool_choice: str,
    model: str,
    client: httpx.AsyncClient,
    cache: MemoryCache,
    stats: Stats,
):
    """流式转发：Chat Completions SSE → Responses API SSE"""

    def sse(event: str, data: dict) -> bytes:
        return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()

    response_id = f"resp_{uuid.uuid4().hex[:12]}"

    # response.created
    yield sse("response.created", {
        "type": "response.created",
        "response": {"id": response_id, "object": "response", "status": "in_progress", "model": model, "output": [], "usage": None}
    })
    yield sse("response.in_progress", {
        "type": "response.in_progress",
        "response": {"id": response_id, "object": "response", "status": "in_progress", "model": model, "output": [], "usage": None}
    })

    payload = {"model": model, "messages": messages, "stream": True, "stream_options": {"include_usage": True}}
    if tools:
        payload["tools"] = tools
        if tool_choice != "auto":
            payload["tool_choice"] = tool_choice

    # 状态追踪
    text_item_id = f"item_{uuid.uuid4().hex[:12]}"
    full_text = ""
    full_reasoning = ""
    text_started = False
    has_text = False
    tc_acc = {}  # index → {id, name, arguments, item_id, started}
    input_tokens = 0
    output_tokens = 0
    seq = 0

    try:
        async with client.stream("POST", upstream, headers=headers, json=payload) as resp:
            if resp.status_code != 200:
                err = (await resp.aread()).decode("utf-8", errors="replace")
                log.error("  ❌ [Responses] 上游 %d: %s", resp.status_code, err[:200])
                yield sse("response.failed", {
                    "type": "response.failed",
                    "response": {"id": response_id, "object": "response", "status": "failed", "model": model,
                                 "error": {"message": err[:200]}, "output": [], "usage": None}
                })
                return

            buf = ""
            async for chunk in resp.aiter_bytes():
                buf += chunk.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.rstrip("\r")
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:].strip()
                    if raw == "[DONE]":
                        continue
                    try:
                        d = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    usage = d.get("usage")
                    if usage:
                        input_tokens = usage.get("prompt_tokens", 0)
                        output_tokens = usage.get("completion_tokens", 0)

                    if "error" in d:
                        yield sse("response.failed", {
                            "type": "response.failed",
                            "response": {"id": response_id, "object": "response", "status": "failed", "model": model,
                                         "error": d["error"], "output": [], "usage": None}
                        })
                        return

                    if "choices" not in d or not d["choices"]:
                        continue

                    delta = d["choices"][0].get("delta", {})

                    # reasoning 累积
                    rc = delta.get("reasoning_content", "")
                    if rc:
                        full_reasoning += rc

                    # 文本内容
                    content = delta.get("content", "")
                    if content:
                        if not text_started:
                            text_started = True
                            has_text = True
                            yield sse("response.output_item.added", {
                                "type": "response.output_item.added", "output_index": 0,
                                "item": {"id": text_item_id, "type": "message", "status": "in_progress", "role": "assistant", "content": []}
                            })
                            yield sse("response.content_part.added", {
                                "type": "response.content_part.added", "item_id": text_item_id,
                                "output_index": 0, "content_index": 0,
                                "part": {"type": "text", "text": ""}
                            })
                        full_text += content
                        seq += 1
                        yield sse("response.output_text.delta", {
                            "type": "response.output_text.delta", "delta": content,
                            "item_id": text_item_id, "output_index": 0, "content_index": 0, "sequence_number": seq
                        })

                    # 工具调用
                    for tc in (delta.get("tool_calls") or []):
                        idx = tc.get("index", 0)
                        if idx not in tc_acc:
                            tc_acc[idx] = {"id": "", "name": "", "arguments": "", "item_id": f"item_{uuid.uuid4().hex[:12]}", "started": False}
                        acc = tc_acc[idx]
                        if tc.get("id"):
                            acc["id"] = tc["id"]
                        fn = tc.get("function", {})
                        if fn.get("name"):
                            acc["name"] = fn["name"]
                        args_delta = fn.get("arguments", "")
                        if args_delta:
                            acc["arguments"] += args_delta
                            out_idx = (1 if has_text else 0) + sorted(tc_acc.keys()).index(idx)
                            if not acc["started"]:
                                acc["started"] = True
                                yield sse("response.output_item.added", {
                                    "type": "response.output_item.added", "output_index": out_idx,
                                    "item": {"id": acc["item_id"], "type": "function_call", "status": "in_progress",
                                             "call_id": acc["id"], "name": acc["name"], "arguments": ""}
                                })
                            yield sse("response.function_call_arguments.delta", {
                                "type": "response.function_call_arguments.delta",
                                "item_id": acc["item_id"], "output_index": out_idx, "delta": args_delta
                            })

            # ===== 流结束：缓存 reasoning =====
            if full_reasoning and (full_text or tc_acc):
                synthetic = {
                    "role": "assistant",
                    "content": full_text,
                    "tool_calls": [
                        {"id": tc["id"], "type": "function", "function": {"name": tc["name"], "arguments": tc["arguments"]}}
                        for tc in (tc_acc[i] for i in sorted(tc_acc.keys()))
                    ] if tc_acc else [],
                    "reasoning_content": full_reasoning,
                }
                save_reasoning(synthetic, cache)

            # ===== 发出完成事件 =====
            output_items = []

            if has_text:
                yield sse("response.output_text.done", {
                    "type": "response.output_text.done", "text": full_text,
                    "item_id": text_item_id, "output_index": 0, "content_index": 0
                })
                yield sse("response.content_part.done", {
                    "type": "response.content_part.done", "item_id": text_item_id,
                    "output_index": 0, "content_index": 0,
                    "part": {"type": "text", "text": full_text}
                })
                text_item = {"id": text_item_id, "type": "message", "status": "completed", "role": "assistant",
                             "content": [{"type": "text", "text": full_text}]}
                if full_reasoning:
                    text_item["reasoning_content"] = full_reasoning
                yield sse("response.output_item.done", {"type": "response.output_item.done", "output_index": 0, "item": text_item})
                output_items.append(text_item)

            for idx in sorted(tc_acc.keys()):
                acc = tc_acc[idx]
                out_idx = (1 if has_text else 0) + sorted(tc_acc.keys()).index(idx)
                yield sse("response.function_call_arguments.done", {
                    "type": "response.function_call_arguments.done",
                    "item_id": acc["item_id"], "output_index": out_idx, "arguments": acc["arguments"]
                })
                func_item = {"id": acc["item_id"], "type": "function_call", "status": "completed",
                             "call_id": acc["id"], "name": acc["name"], "arguments": acc["arguments"]}
                if full_reasoning:
                    func_item["reasoning_content"] = full_reasoning
                yield sse("response.output_item.done", {"type": "response.output_item.done", "output_index": out_idx, "item": func_item})
                output_items.append(func_item)

            # response.completed
            yield sse("response.completed", {
                "type": "response.completed",
                "response": {
                    "id": response_id, "object": "response", "status": "completed", "model": model,
                    "output": output_items,
                    "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens, "total_tokens": input_tokens + output_tokens}
                }
            })
            stats.success += 1

    except httpx.TimeoutException:
        log.error("  ❌ [Responses] 上游超时")
        yield sse("response.failed", {
            "type": "response.failed",
            "response": {"id": response_id, "object": "response", "status": "failed", "model": model,
                         "error": {"message": "upstream timeout"}, "output": [], "usage": None}
        })
    except Exception as e:
        log.error("  ❌ [Responses] 错误: %s", e, exc_info=True)
        yield sse("response.failed", {
            "type": "response.failed",
            "response": {"id": response_id, "object": "response", "status": "failed", "model": model,
                         "error": {"message": str(e)}, "output": [], "usage": None}
        })
