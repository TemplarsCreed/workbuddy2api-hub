# -*- coding: utf-8 -*-
"""Gemini generateContent 的网页工具适配。

Gemini 的 ``tools: [{"googleSearch": {}}]`` 是服务端 grounding：客户端不能指定
查询，模型自己决定搜什么。本网关的上游没有 Gemini 通道，所以只能做本地降级：

1. 从最后一条 user 消息取查询，先在网关本地搜一次；
2. 结果写进 ``systemInstruction``，模型这一轮就能用上；
3. 把 ``googleSearch`` 从 tools 里摘掉，避免上游对不认识的工具报错；
4. 响应里补出 ``groundingMetadata``（``webSearchQueries`` / ``groundingChunks`` /
   ``groundingSupports``），客户端据此显示引用。

``webSearchQueries`` 会带上本地执行的标记，不冒充官方 grounding。
"""

import uuid
from urllib.parse import urlsplit

FINISH_REASON_ZH = "STOP"


def wants_web(body):
    for tool in body.get("tools") or []:
        if isinstance(tool, dict) and tool.get("googleSearch") is not None:
            return True
    if isinstance(body.get("tools"), dict) and body["tools"].get("googleSearch") is not None:
        return True
    return False


def _text_of_parts(parts):
    out = []
    for part in parts or []:
        if isinstance(part, dict) and isinstance(part.get("text"), str):
            out.append(part["text"])
    return "\n".join(p for p in out if p).strip()


def last_user_text(contents):
    for item in reversed(contents or []):
        if not isinstance(item, dict):
            continue
        if str(item.get("role") or "user") != "user":
            continue
        text = _text_of_parts(item.get("parts"))
        if text:
            return text
    return ""


def _strip_google_search(body):
    tools = body.get("tools")
    if isinstance(tools, dict):
        tools = [tools]
    if not isinstance(tools, list):
        return
    kept = []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("googleSearch") is not None:
            continue
        kept.append(tool)
    if kept:
        body["tools"] = kept
    else:
        body.pop("tools", None)


def _system_text(body):
    instruction = body.get("systemInstruction") or body.get("system_instruction")
    if isinstance(instruction, dict):
        return _text_of_parts(instruction.get("parts"))
    if isinstance(instruction, str):
        return instruction
    return ""


def _set_system_text(body, text):
    body["systemInstruction"] = {"parts": [{"text": text}]}
    body.pop("system_instruction", None)


def to_chat(body, model):
    """Gemini 请求 → Chat Completions 请求。"""
    messages = []
    system = _system_text(body)
    contents = body.get("contents")
    if isinstance(contents, dict):
        contents = [contents]
    for item in contents or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "user")
        text = _text_of_parts(item.get("parts"))
        if not text:
            continue
        messages.append({"role": "assistant" if role == "model" else "user",
                         "content": text})
    if system:
        messages.insert(0, {"role": "system", "content": system})

    chat = {"model": model, "messages": messages}
    config = body.get("generationConfig") or body.get("generation_config") or {}
    if isinstance(config, dict):
        if config.get("temperature") is not None:
            chat["temperature"] = config["temperature"]
        if config.get("topP") is not None:
            chat["top_p"] = config["topP"]
        if config.get("maxOutputTokens") is not None:
            chat["max_tokens"] = config["maxOutputTokens"]
        stops = config.get("stopSequences")
        if isinstance(stops, list) and stops:
            chat["stop"] = stops
    return chat


def prepare(body, registry, cfg, ctx=None):
    """回 ``(body, meta)``；meta 为 None 表示没接管。"""
    if not wants_web(body):
        return body, None
    query = last_user_text(body.get("contents"))[:400].strip()
    if len(query) < 2:
        return body, None
    try:
        result = registry.search({"query": query, "max_results": 5}, cfg, ctx or {})
    except Exception:
        return body, None
    if not result.get("providers"):
        return body, None

    import wb_webtools

    shaped = [{"title": row.get("title") or "", "url": row.get("url") or "",
               "snippet": row.get("snippet") or ""} for row in result.get("results") or []]
    if shaped:
        block = ("Web search results for the user's question are below. "
                 "Cite the sources you use.\n\n"
                 + wb_webtools.format_results(query, shaped))
        existing = _system_text(body)
        _set_system_text(body, (existing + "\n\n" + block) if existing else block)
    _strip_google_search(body)
    return body, {"query": query, "results": shaped,
                  "provider": ",".join(result.get("providers") or [])}


def _segments(text, count):
    """回答里 `[n]` 标记的位置。模型没标就整段都给出去。"""
    out = {}
    for index in range(1, count + 1):
        marker = "[%d]" % index
        pos = text.find(marker)
        if pos < 0:
            continue
        start = max(0, text.rfind(".", 0, pos) + 1)
        while start < pos and text[start] in (" ", "\n", "\r", "\t"):
            start += 1
        out[index] = (start, pos + len(marker), text[start:pos + len(marker)])
    return out


def _domain(url):
    try:
        return urlsplit(str(url or "")).hostname or ""
    except Exception:
        return ""


def _grounding(meta, answer_text=""):
    if not meta or not meta.get("results"):
        return None
    text = str(answer_text or "")
    marks = _segments(text, len(meta["results"]))
    chunks = []
    supports = []
    for position, row in enumerate(meta["results"]):
        index = position + 1
        web = {
            "uri": row.get("url") or "",
            "title": row.get("title") or "",
        }
        domain = _domain(row.get("url"))
        if domain:
            web["domain"] = domain
        if row.get("page_age"):
            web["publishedDate"] = row["page_age"]
        chunks.append({"web": web})
        if index in marks:
            start, end, snippet = marks[index]
        else:
            start, end, snippet = 0, len(text), text[:200]
        supports.append({
            "segment": {"partIndex": 0, "startIndex": start,
                        "endIndex": end, "text": snippet},
            "groundingChunkIndices": [position],
            "confidenceScores": [1.0],
        })
    return {
        "webSearchQueries": [meta.get("query") or ""],
        "groundingChunks": chunks,
        "groundingSupports": supports,
        "retrievalMetadata": {"source": "wb-webgateway"},
    }


def from_chat(chat, meta, model):
    """Chat Completions 响应 → Gemini GenerateContentResponse。"""
    choices = chat.get("choices") if isinstance(chat, dict) else None
    choice = (choices or [{}])[0] if choices else {}
    message = choice.get("message") if isinstance(choice, dict) else {}
    text = ""
    if isinstance(message, dict):
        text = str(message.get("content") or "")
    usage = chat.get("usage") if isinstance(chat, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    prompt_tokens = int(usage.get("prompt_tokens") or 0)
    completion_tokens = int(usage.get("completion_tokens") or 0)

    candidate = {
        "content": {"role": "model", "parts": [{"text": text}]},
        "finishReason": FINISH_REASON_ZH,
        "index": 0,
    }
    out = {
        "candidates": [candidate],
        "usageMetadata": {
            "promptTokenCount": prompt_tokens,
            "candidatesTokenCount": completion_tokens,
            "totalTokenCount": prompt_tokens + completion_tokens,
        },
        "modelVersion": model,
        "responseId": "wb-%s" % uuid.uuid4().hex[:16],
    }
    grounding = _grounding(meta, text)
    if grounding:
        candidate["groundingMetadata"] = grounding
        candidate["finishReason"] = FINISH_REASON_ZH
    return out


def sse_payload(response):
    """Gemini 的流式事件：整份响应一次发出。"""
    import json

    return "data: " + json.dumps(response, ensure_ascii=False) + "\n\n"
