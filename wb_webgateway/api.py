# -*- coding: utf-8 -*-
"""三个能力端点的入参校验与序列化。

每个端点有独立的允许键集：写错字段名要报错，不能静默忽略——客户端
把 ``maxChars`` 写成驼峰、或把 ``query`` 发到 fetch 端点时，必须看到
400，而不是拿回一份默认行为的结果。
"""

PATHS = {
    "/v1/tools/search": "search",
    "/v1/tools/fetch": "fetch",
    "/v1/tools/search_fetch": "search_fetch",
}

_COMMON_SEARCH = {
    "query", "max_results", "language", "time_range", "categories", "engines",
    "safesearch", "pageno", "allowed_domains", "blocked_domains", "user_location",
    "providers",
}

_ALLOWED = {
    "search": _COMMON_SEARCH,
    "fetch": {"url", "max_chars", "prompt", "start_index", "providers"},
    "search_fetch": _COMMON_SEARCH | {"fetch_top_n", "max_chars", "prompt"},
}

_MAX_RESULTS_DEFAULT = 10


def _error(status, code, message):
    return status, {"error": {"code": code, "message": message}}


def _as_int(value, default, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _string_list(value):
    if not isinstance(value, (list, tuple)):
        return None
    return [str(x).strip() for x in value if str(x or "").strip()]


def _error_tuple(status, code, message):
    """check() 的两段返回：错误时回 (status, payload)，成功时回 (None, req)。"""
    return _error(status, code, message), None


def check(kind, body):
    """返回 (错误状态码与载荷, None) 或 (None, 规范化后的请求)。"""
    if not isinstance(body, dict):
        return _error_tuple(400, "invalid_body", "request body must be a JSON object")

    unknown = sorted(set(body) - _ALLOWED[kind])
    if unknown:
        hint = ""
        if kind == "fetch" and "query" in unknown:
            hint = " (did you mean /v1/tools/search?)"
        elif kind == "search" and "url" in unknown:
            hint = " (did you mean /v1/tools/fetch?)"
        return _error_tuple(400, "unexpected_field",
                      "unknown field(s): %s%s" % (", ".join(unknown), hint))

    allow = _string_list(body.get("allowed_domains"))
    block = _string_list(body.get("blocked_domains"))
    if allow and block:
        return _error_tuple(400, "conflicting_domains",
                      "allowed_domains and blocked_domains cannot be combined")

    if kind in ("search", "search_fetch"):
        query = str(body.get("query") or "").strip()
        if len(query) < 2:
            return _error_tuple(400, "invalid_query",
                          "query needs at least 2 characters")
    else:
        query = ""

    url = ""
    if kind == "fetch":
        url = str(body.get("url") or "").strip()
        if not url:
            return _error_tuple(400, "missing_url", "url is required")

    req = {
        "query": query,
        "url": url,
        "max_results": _as_int(body.get("max_results"), _MAX_RESULTS_DEFAULT, 1, 50),
        "max_chars": _as_int(body.get("max_chars"), 20000, 100, 100000),
        "start_index": _as_int(body.get("start_index"), 0, 0, 10 ** 9),
        "fetch_top_n": _as_int(body.get("fetch_top_n"), 3, 0, 10),
        "language": str(body.get("language") or "").strip() or None,
        "time_range": str(body.get("time_range") or "").strip() or None,
        "categories": _string_list(body.get("categories")),
        "engines": _string_list(body.get("engines")),
        "safesearch": body.get("safesearch"),
        "pageno": body.get("pageno"),
        "allowed_domains": allow,
        "blocked_domains": block,
        "user_location": body.get("user_location"),
        "prompt": str(body.get("prompt") or "").strip() or None,
        "providers": _string_list(body.get("providers")),
    }
    return None, req


def handle(path, body, ctx, registry, cfg):
    """返回 (status, payload)。调用方只负责把它写成 HTTP。"""
    kind = PATHS.get(path)
    if kind is None:
        return _error(404, "not_found", "unknown path")
    if not (cfg.get("endpoints") or {}).get(kind, False):
        return _error(404, "endpoint_disabled", "/v1/tools/%s is turned off" % kind)

    failed, req = check(kind, body)
    if failed:
        return failed

    if ctx is None:
        ctx = {}

    if kind == "search":
        result = registry.search(req, cfg, ctx)
    elif kind == "fetch":
        result = registry.fetch(req, cfg, ctx)
    else:
        result = registry.search_and_fetch(req, cfg, ctx)

    if result.get("blocked"):
        message = "; ".join(err.get("message") or "" for err in result.get("errors") or [])
        return _error(400, "blocked_url", message or "this URL is not allowed")

    ok = bool(result.get("providers"))
    if not ok:
        errors = result.get("errors") or []
        for error in errors:
            code = error.get("code")
            if code in ("target_policy_denied", "target_unreachable"):
                return _error(403 if code == "target_policy_denied" else 502,
                              code, error["message"])
        message = "; ".join(err.get("message") or "" for err in errors)
        return _error(502, "all_providers_failed",
                      message or "no web provider could serve this request")
    return 200, result
