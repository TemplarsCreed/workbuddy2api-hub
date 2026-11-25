# -*- coding: utf-8 -*-
"""web 网关对外入口。

``wb_proxy`` 只跟这里打交道：启动时构造并 ``install()`` 一个 Registry，
之后把三条能力端点与三个伪模型的请求交给 ``handle`` / ``handle_model``。
"""

import json

from . import api, ir, models, settings
from .providers.local_engines import LocalEnginesProvider
from .providers.local_http import LocalHttpProvider
from .registry import Registry

_REGISTRY = None


def build_registry(pool=None, accounts_dir=None, log=None):
    """构造一个装好内置 provider 的 registry。"""
    registry = Registry(pool=pool, accounts_dir=accounts_dir, log=log)
    registry.register(LocalEnginesProvider())
    registry.register(LocalHttpProvider())
    try:
        from .providers.workbuddy import WorkbuddyProvider

        registry.register(WorkbuddyProvider())
    except Exception:  # 可选依赖：没有 workbuddy provider 时其余照常工作
        pass
    return registry


def install(registry):
    global _REGISTRY
    _REGISTRY = registry
    return registry


def current():
    return _REGISTRY


def is_web_model(model):
    return models.is_web_model(model)


def web_model_entries():
    return models.model_entries()


def _registry_or_error():
    if _REGISTRY is None:
        return None, (503, {"error": {"code": "not_ready",
                                      "message": "web gateway is not initialised"}})
    return _REGISTRY, None


def _config(registry):
    if registry.accounts_dir:
        try:
            import wb_settings

            return wb_settings.web_gateway(registry.accounts_dir)
        except Exception:
            pass
    return settings.defaults()


def handle(path, body, ctx=None):
    """三条能力端点。返回 (status, payload) 纯数据。"""
    registry, failed = _registry_or_error()
    if failed:
        return failed
    cfg = _config(registry)
    if not cfg.get("enabled"):
        return 404, {"error": {"code": "endpoint_disabled",
                               "message": "web gateway is turned off"}}
    return api.handle(path, body, ctx, registry, cfg)


def _failure_text(kind, result):
    detail = "; ".join(err.get("message") or "" for err in result.get("errors") or [])
    return ("Error: web %s is temporarily unavailable (%s). "
            "Do not retry the same call right away." % (kind, detail or "no provider available"))


def execute_tool(name, arguments, ctx=None):
    """给协议层（Responses 循环）用的字符串出口。

    接管条件：网关已启用，且 ``responses`` 在 ``inject_protocols`` 里；
    或者旧开关 ``local_web_tools`` 打开（保持 v2.1.3 的行为）。不接管时
    返回 None，调用方回退到 ``wb_webtools.execute``。
    """
    registry, failed = _registry_or_error()
    if failed:
        return None
    cfg = _config(registry)
    protocols = cfg.get("inject_protocols") or []
    take_over = bool(cfg.get("enabled")) and "responses" in protocols
    if not take_over:
        try:
            import wb_settings

            take_over = bool(registry.accounts_dir) and wb_settings.local_web_tools(
                registry.accounts_dir)
        except Exception:
            take_over = False
    if not take_over:
        return None

    if isinstance(arguments, str):
        try:
            args = json.loads(arguments or "{}")
        except Exception:
            args = {}
    else:
        args = arguments
    if not isinstance(args, dict):
        args = {}

    name = str(name or "").strip()
    ctx = ctx or {}

    if name in ("web_search", "WebSearch"):
        query = str(args.get("query") or args.get("queries") or args.get("q") or "").strip()
        if len(query) < 2:
            return "Error: web_search needs a query of at least 2 characters."
        result = registry.search({
            "query": query,
            "max_results": args.get("numResults") or args.get("max_results") or 5,
            "allowed_domains": args.get("allowed_domains"),
            "blocked_domains": args.get("blocked_domains"),
        }, cfg, ctx)
        if not result.get("providers"):
            return _failure_text("search", result)
        return models.format_text("wb-search", result)

    if name in ("web_fetch", "WebFetch"):
        url = str(args.get("url") or "").strip()
        if not url:
            return "Error: web_fetch needs a url."
        result = registry.fetch({
            "url": url,
            "start_index": args.get("startIndex") or args.get("start_index") or 0,
        }, cfg, ctx)
        if not result.get("providers"):
            return _failure_text("fetch", result)
        return models.format_text("wb-fetch", result)

    return None


def prepare_anthropic(payload, ctx=None):
    """Messages 协议：判定开关并做预检索，返回 ``(payload, meta)``。"""
    registry, failed = _registry_or_error()
    if failed:
        return payload, None
    cfg = _config(registry)
    if not cfg.get("enabled"):
        return payload, None
    if "anthropic" not in (cfg.get("inject_protocols") or []):
        return payload, None
    from .protocols import anthropic as protocol

    if (cfg.get("web_search_mode") or "multi") == "multi":
        return protocol.prepare_multi(payload)
    return protocol.prepare(payload, registry, cfg, ctx or {})


def handle_model(model, body, protocol="chat", ctx=None):
    """伪模型。``protocol`` 取 ``chat`` 或 ``messages``。"""
    registry, failed = _registry_or_error()
    if failed:
        return failed
    cfg = _config(registry)
    if not cfg.get("enabled"):
        return 404, {"error": {"code": "model_not_found",
                               "message": "%s is not available" % model}}

    messages = body.get("messages") if isinstance(body, dict) else None
    kind, req = models.request_for(model, messages, body.get("web_options")
                                   if isinstance(body, dict) else None)

    if kind == "fetch":
        if not req.get("url"):
            return 400, {"error": {"code": "missing_url",
                                   "message": "no http(s) URL found in the request"}}
        result = registry.fetch(req, cfg, ctx)
    elif kind == "search":
        if len(req.get("query") or "") < 2:
            return 400, {"error": {"code": "invalid_query",
                                   "message": "query needs at least 2 characters"}}
        result = registry.search(req, cfg, ctx)
    else:
        if len(req.get("query") or "") < 2:
            return 400, {"error": {"code": "invalid_query",
                                   "message": "query needs at least 2 characters"}}
        result = registry.search_and_fetch(req, cfg, ctx)

    if not result.get("providers"):
        message = "; ".join(err.get("message") or "" for err in result.get("errors") or [])
        return 502, {"error": {"code": "all_providers_failed",
                               "message": message or "no web provider could serve this request"}}

    text = models.format_text(model, result)
    if protocol == "messages":
        return 200, models.messages_response(model, result, text)
    return 200, models.chat_completion(model, result, text)
