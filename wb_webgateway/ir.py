# -*- coding: utf-8 -*-
"""web 网关的统一结果模型。

搜索与抓取各有多个 provider，拿到的形状不一样：WorkBuddy 回
``{code, provider, results, total_results}``，本地引擎回自己的 dict，
外部服务又各有一套。这里只做最低限度的归一——谁执行、拿到什么、
失败原因——协议层再把这套结果编成各自的原生形状。

约定：本模块只产 dict，不抛例外，不做 IO。
"""

OBJECT_SEARCH = "web_search_result"


def new_response(query=None):
    """一次调用的空响应骨架。"""
    return {
        "object": OBJECT_SEARCH,
        "providers": [],
        "query": query,
        "results": [],
        "elapsed_ms": 0,
        "degraded": False,
        "errors": [],
    }


def result_item(title, url, snippet=None, content=None, site=None, favicon=None,
                source=None, published_date=None,
                content_truncated=False, next_start_index=None):
    """单条结果的统一形状；空字段一律给 null，便于客户端判空。"""
    return {
        "title": str(title or "").strip() or str(url or ""),
        "url": str(url or "").strip(),
        "snippet": snippet or None,
        "content": content or None,
        "site": site or None,
        "favicon": favicon or None,
        "published_date": published_date or None,
        "source": source or None,
        "content_truncated": bool(content_truncated),
        "next_start_index": next_start_index,
    }


def note_error(response, provider, message):
    """把一次 provider 失败记进响应的 errors，供调用方判断 degraded。"""
    response["errors"].append({
        "provider": str(provider or ""),
        "message": str(message or "unknown error"),
    })
    return response


def mark_provider(response, name):
    """记录一个成功参与的 provider（去重，保持顺序）。"""
    name = str(name or "")
    if name and name not in response["providers"]:
        response["providers"].append(name)
    return response


def finalize(response, elapsed_ms):
    """收尾：算出 degraded，并把耗时写进响应。"""
    response["elapsed_ms"] = int(elapsed_ms or 0)
    response["degraded"] = bool(response["providers"]) and bool(response["errors"])
    return response
