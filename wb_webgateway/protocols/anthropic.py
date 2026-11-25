# -*- coding: utf-8 -*-
"""Anthropic Messages 的网页工具适配。

Claude Code 的 ``WebSearch`` 是 server-side tool：客户端把
``{"type": "web_search_20250305"}`` 放进 ``tools``，期望 API 端执行搜索并回
``server_tool_use`` + ``web_search_tool_result`` 两个原生内容块。

本网关的上游没有这个执行器，所以这里做两件事：

1. 预检索——从最后一条 user 消息取查询，先在网关本地执行，把结果作为
   system 段注入，模型这一轮就能用上；
2. 形状回填——响应里补出那两个原生块，客户端看到的与官方 API 一致，
   ``usage.server_tool_use.web_search_requests`` 也照实给。

之所以不做多轮循环：上游拿到的是一份纯文本 system，模型不会产出
``web_search`` 的 tool call，因此也不需要第二轮。代价是搜索时机由网关
决定（请求进来就搜），换来的是实现量与流式事件序列都稳定可控。
"""

import base64
import gzip
import json
import uuid

SEARCH_TYPES = ("web_search_20250305",)
FETCH_TYPES = ("web_fetch_20250910",)


def wants_web(tools):
    """客户端宣告了哪些 server-side 网页工具。没有就回 None。"""
    search = fetch = False
    options = None
    for tool in tools or []:
        if not isinstance(tool, dict):
            continue
        kind = str(tool.get("type") or "").strip()
        if kind in SEARCH_TYPES:
            search = True
            options = tool
        elif kind in FETCH_TYPES:
            fetch = True
    if not (search or fetch):
        return None
    return {"search": search, "fetch": fetch, "options": options or {}}


def last_user_text(messages):
    """最后一条 user 消息的纯文本。"""
    for msg in reversed(messages or []):
        if not isinstance(msg, dict) or str(msg.get("role")) != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts = []
            for block in content:
                if isinstance(block, dict) and str(block.get("type")) == "text":
                    parts.append(str(block.get("text") or ""))
                elif isinstance(block, str):
                    parts.append(block)
            return " ".join(p for p in parts if p).strip()
    return ""


def _system_text(payload):
    system = payload.get("system")
    if isinstance(system, str):
        return system
    if isinstance(system, list):
        parts = []
        for block in system:
            if isinstance(block, dict) and str(block.get("type")) == "text":
                parts.append(str(block.get("text") or ""))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(p for p in parts if p)
    return ""


def _strip_server_tools(payload):
    kept = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if str(tool.get("type") or "").strip() in SEARCH_TYPES + FETCH_TYPES:
            continue
        kept.append(tool)
    if kept:
        payload["tools"] = kept
    else:
        payload.pop("tools", None)
    return payload


def prepare(payload, registry, cfg, ctx=None):
    """回 ``(payload, meta)``。

    payload 已去掉 server tool 声明、并在 system 里加了检索结果；meta 为
    None 表示这次没接管（没有声明、查询太短、或搜索失败）。
    """
    if not isinstance(payload, dict):
        return payload, None
    wants = wants_web(payload.get("tools"))
    if not wants or not wants.get("search"):
        return payload, None

    query = last_user_text(payload.get("messages"))[:400].strip()
    if len(query) < 2:
        return payload, None

    options = wants.get("options") or {}
    req = {
        "query": query,
        "max_results": 5,
        "allowed_domains": options.get("allowed_domains"),
        "blocked_domains": options.get("blocked_domains"),
        "user_location": options.get("user_location"),
    }
    try:
        result = registry.search(req, cfg, ctx or {})
    except Exception:
        return payload, None
    rows = result.get("results") or []
    if not result.get("providers"):
        return payload, None

    import wb_webtools

    shaped = [{"title": r.get("title") or "", "url": r.get("url") or "",
               "snippet": r.get("snippet") or "",
               "page_age": r.get("published_date") or "",
               "encrypted_content": _encrypt(r.get("snippet") or r.get("content") or "")}
              for r in rows]
    note = wb_webtools.format_results(query, shaped) if shaped else ""
    if note:
        existing = _system_text(payload)
        head = ("Web search results for the user's question are below. "
                "Cite the sources you use.\n\n")
        payload["system"] = (existing + "\n\n" + head + note) if existing else (head + note)
    _strip_server_tools(payload)

    return payload, {
        "query": query,
        "results": shaped,
        "tool_use_id": "srvtoolu_%s" % uuid.uuid4().hex[:24],
        "provider": ",".join(result.get("providers") or []),
        "options": {
            "max_uses": options.get("max_uses"),
            "allowed_domains": options.get("allowed_domains"),
            "blocked_domains": options.get("blocked_domains"),
        },
    }


def prepare_multi(payload):
    """多轮模式：把 server tool 换成网关自己的 function 定义，不预检索。

    模型据此自己决定搜什么、搜几次；循环与执行在 wb_proxy 侧完成。
    """
    wants = wants_web(payload.get("tools"))
    if not wants or not wants.get("search"):
        return payload, None
    import wb_webtools

    kept = []
    for tool in payload.get("tools") or []:
        if not isinstance(tool, dict):
            continue
        if str(tool.get("type") or "").strip() in SEARCH_TYPES + FETCH_TYPES:
            continue
        kept.append(tool)
    kept.append(wb_webtools.web_search_tool_def())
    payload["tools"] = kept
    options = wants.get("options") or {}
    try:
        max_uses = int(options.get("max_uses") or 5)
    except (TypeError, ValueError):
        max_uses = 5
    return payload, {"multi": True, "max_uses": max(1, min(10, max_uses)),
                     "allowed_domains": options.get("allowed_domains"),
                     "blocked_domains": options.get("blocked_domains")}


# -- 原生形状回填 -----------------------------------------------------------

def _encrypt(text):
    """官方字段 encrypted_content 的本地等价物：压缩 + base64，带来源前缀。

    客户端只透传这个字段，不解密；加前缀是为了在会话日志里能一眼看出它不是
    官方密文。空内容回空串，避免给客户端一个假的“有内容”信号。
    """
    raw = str(text or "").encode("utf-8")
    if not raw:
        return ""
    return "wbenc:" + base64.urlsafe_b64encode(gzip.compress(raw)).decode("ascii")


def server_tool_use_block(meta):
    options = meta.get("options") or {}
    payload = {"query": meta["query"]}
    for key in ("allowed_domains", "blocked_domains"):
        value = options.get(key)
        if value:
            payload[key] = value
    return {
        "type": "server_tool_use",
        "id": meta["tool_use_id"],
        "name": "web_search",
        "input": payload,
    }


def web_search_result_block(meta):
    content = []
    for row in meta.get("results") or []:
        item = {
            "type": "web_search_result",
            "title": row.get("title") or "",
            "url": row.get("url") or "",
        }
        if row.get("page_age"):
            item["page_age"] = row["page_age"]
        if row.get("encrypted_content"):
            item["encrypted_content"] = row["encrypted_content"]
        content.append(item)
    return {
        "type": "web_search_tool_result",
        "tool_use_id": meta["tool_use_id"],
        "content": content,
    }


def decorate_message(result, meta):
    """非流式：把两个原生块插到 content 最前，并补 usage。"""
    if not isinstance(result, dict) or not meta:
        return result
    blocks = [server_tool_use_block(meta), web_search_result_block(meta)]
    content = result.get("content")
    result["content"] = blocks + (list(content) if isinstance(content, list) else [])
    usage = result.get("usage")
    if not isinstance(usage, dict):
        usage = {}
        result["usage"] = usage
    usage["server_tool_use"] = {"web_search_requests": 1}
    return result


def stream_prefix_frames(meta, frame_factory):
    """流式：两个块的 start/delta/stop 帧。index 固定占 0 与 1。

    ``frame_factory`` 是 wb_proxy 的 ``anthropic_sse_frame``；
    调用方随后必须把后续事件的起始索引设成 2。
    """
    if not meta:
        return []
    tool_id = meta["tool_use_id"]
    payload = json.dumps({"query": meta["query"]}, ensure_ascii=False)
    return [
        frame_factory("content_block_start", {
            "index": 0,
            "content_block": {"type": "server_tool_use", "id": tool_id,
                              "name": "web_search", "input": {}},
        }),
        frame_factory("content_block_delta", {
            "index": 0,
            "delta": {"type": "input_json_delta", "partial_json": payload},
        }),
        frame_factory("content_block_stop", {"index": 0}),
        frame_factory("content_block_start", {
            "index": 1,
            "content_block": web_search_result_block(meta),
        }),
        frame_factory("content_block_stop", {"index": 1}),
    ]
