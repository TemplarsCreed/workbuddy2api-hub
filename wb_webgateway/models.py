# -*- coding: utf-8 -*-
"""伪模型：把搜索与抓取当成一个「模型」暴露。

客户端把 ``model`` 设成 ``wb-search`` / ``wb-fetch`` / ``wb-search-fetch``
时，请求不进上游模型，直接由本模块抽参数、调 registry、编成该协议的正常
响应。这样 Agent 不需要声明任何工具，也不必理解 server-side tool。
"""

import json
import re
import time
import uuid

MODEL_KINDS = {
    "wb-search": "search",
    "wb-fetch": "fetch",
    "wb-search-fetch": "search_fetch",
}

_URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")


def is_web_model(model):
    return str(model or "").strip() in MODEL_KINDS


def kind_for(model):
    return MODEL_KINDS.get(str(model or "").strip())


def model_entries():
    """给 /v1/models 追加的三条固定条目。"""
    out = []
    for model, kind in MODEL_KINDS.items():
        out.append({
            "id": model,
            "object": "model",
            "created": 1626777600,
            "owned_by": "wb-webgateway",
            "capabilities": {"web": kind},
        })
    return out


# -- 从请求里抽参数 ---------------------------------------------------------

def last_user_text(messages):
    """最后一条 user 消息的纯文本；兼容字符串与 content block 数组。"""
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or str(msg.get("role")) != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and str(block.get("type")) in ("text", "input_text"):
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block, str):
                    parts.append(block)
            return " ".join(p for p in parts if p).strip()
    return ""


def first_url(text):
    match = _URL_RE.search(str(text or ""))
    return match.group(0).rstrip(".,;:!?") if match else ""


def request_for(model, messages, extra=None):
    """把伪模型的调用翻译成 registry 的请求。"""
    kind = kind_for(model)
    text = last_user_text(messages)
    req = {"query": "", "url": "", "max_results": 10, "max_chars": 20000,
           "start_index": 0, "fetch_top_n": 3, "allowed_domains": None,
           "blocked_domains": None, "providers": None}
    if isinstance(extra, dict):
        for key in ("max_results", "max_chars", "fetch_top_n", "allowed_domains",
                    "blocked_domains", "providers", "language", "time_range"):
            if key in extra:
                req[key] = extra[key]
    if kind == "fetch":
        req["url"] = first_url(text)
    else:
        req["query"] = text
    return kind, req


# -- 结果排版 ---------------------------------------------------------------

def format_text(model, result):
    """给模型/用户读的文字；沿用 wb_webtools 的排版。"""
    import wb_webtools

    kind = kind_for(model)
    if kind == "fetch":
        rows = result.get("results") or []
        if not rows:
            return "Error: could not fetch the requested URL."
        page = rows[0]
        text = str(page.get("content") or "")
        tail = ""
        if page.get("next_start_index") is not None:
            tail = ("\n\n[Truncated at %d characters. Call again with start_index=%d to continue.]"
                    % (len(text), page["next_start_index"]))
        return "URL: %s\n\n%s%s" % (page.get("url") or "", text, tail)

    rows = result.get("results") or []
    if not rows:
        return "No results found for: %s" % (result.get("query") or "")
    shaped = [{"title": r.get("title") or "", "url": r.get("url") or "",
               "snippet": r.get("snippet") or ""} for r in rows]
    text = wb_webtools.format_results(result.get("query") or "", shaped)
    if kind == "search_fetch" and any(r.get("content") for r in rows):
        blocks = []
        for index, row in enumerate(rows, 1):
            block = "%d. %s\n%s" % (index, row.get("title") or "", row.get("url") or "")
            if row.get("content"):
                block += "\n\n" + str(row["content"])
            blocks.append(block)
        text = "Search results for: %s\n\n%s" % (result.get("query") or "", "\n\n---\n\n".join(blocks))
    return text


# -- 响应构造 ---------------------------------------------------------------

def chat_completion(model, result, text):
    return {
        "id": "chatcmpl-wbweb-%s" % uuid.uuid4().hex[:16],
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text,
                "web_results": result,
            },
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def messages_response(model, result, text):
    return {
        "id": "msg_wbweb_%s" % uuid.uuid4().hex[:16],
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": text, "web_results": result}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }


def sse_lines(payload, model):
    """伪模型的流式收尾：一条 delta、一条 finish、一条 [DONE]。"""
    chunk_id = payload["id"]
    head = {"id": chunk_id, "object": "chat.completion.chunk", "created": payload["created"],
            "model": model,
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": payload["choices"][0]["message"]["content"]},
                         "finish_reason": None}]}
    tail = {"id": chunk_id, "object": "chat.completion.chunk", "created": payload["created"],
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]}
    return ("data: " + json.dumps(head, ensure_ascii=False) + "\n\n"
            + "data: " + json.dumps(tail, ensure_ascii=False) + "\n\n"
            + "data: [DONE]\n\n")
