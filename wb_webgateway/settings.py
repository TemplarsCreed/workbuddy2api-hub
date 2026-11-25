# -*- coding: utf-8 -*-
"""web 网关配置的形状、默认值与校验。

存取落在 ``wb_settings``（与 ``local_web_tools`` 同一份 settings 文件、同一把锁），
本模块只负责把外部传进来的东西规范化成固定形状。

两条规范化规则来自评审：
  * ``realm`` 写 ``"all"`` 等价于 ``None``——AccountPool.pick 把非空 realm
    当成过滤器，传 ``"all"`` 会得到空快照，每个请求都 503。
  * ``allowed_uids`` 为空列表等价于 ``None``——池子里 ``[]`` 与 ``None``
    语义不同，``[]`` 表示“一个都不许”，同样会全量 503。
"""

import copy

REALM_VALUES = (None, "cn", "intl")
KIND_VALUES = ("search", "fetch")
PROVIDER_KINDS = ("search", "fetch")


def defaults():
    """首次写入面板时用的完整默认值。"""
    return copy.deepcopy({
        "enabled": False,
        "inject_in_protocols": False,
        "inject_protocols": [],
        "endpoints": {"search": True, "fetch": True, "search_fetch": True},
        "qps": 5,
        "web_search_mode": "multi",
        "search_providers": [
            {"name": "workbuddy", "enabled": False, "priority": 10, "fallback": True,
             "realm": None, "allowed_uids": None, "max_results": 5,
             "freshness": "", "timeout_ms": 20000},
            {"name": "local-engines", "enabled": True, "priority": 20, "fallback": True},
        ],
        "fetch_providers": [
            {"name": "workbuddy", "enabled": False, "priority": 10, "fallback": True},
            {"name": "local-http", "enabled": True, "priority": 20, "fallback": True},
        ],
    })


def _as_bool(value, default=False):
    return value if isinstance(value, bool) else default


def _as_int(value, default, low, high):
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(high, n))


def _normalize_realm(value):
    text = str(value or "").strip().lower()
    if text in ("", "all", "none", "null"):
        return None
    return text if text in ("cn", "intl") else None


def _normalize_uids(value):
    if not isinstance(value, (list, tuple)):
        return None
    out = [str(x).strip() for x in value if str(x or "").strip()]
    return out or None


def _normalize_provider(raw, kind, fallback_default):
    """单个 provider 条目的规范化；未知名字原样保留，交给 registry 忽略。"""
    entry = dict(raw) if isinstance(raw, dict) else {}
    name = str(entry.get("name") or "").strip()
    out = {
        "name": name,
        "enabled": _as_bool(entry.get("enabled"), False),
        "priority": _as_int(entry.get("priority"), 10, 0, 1000),
        "fallback": _as_bool(entry.get("fallback"), fallback_default),
    }
    if kind == "search":
        out.update({
            "realm": _normalize_realm(entry.get("realm")),
            "allowed_uids": _normalize_uids(entry.get("allowed_uids")),
            "max_results": _as_int(entry.get("max_results"), 5, 1, 50),
            "freshness": str(entry.get("freshness") or "").strip(),
            "timeout_ms": _as_int(entry.get("timeout_ms"), 20000, 1000, 60000),
        })
    return out


def normalize(raw):
    """把任意输入规范化成完整配置；缺字段一律补默认值。"""
    base = defaults()
    data = raw if isinstance(raw, dict) else {}

    out = {
        "enabled": _as_bool(data.get("enabled"), base["enabled"]),
        "inject_in_protocols": _as_bool(data.get("inject_in_protocols"),
                                        base["inject_in_protocols"]),
        "inject_protocols": [],
        "endpoints": dict(base["endpoints"]),
        "qps": _as_int(data.get("qps"), base["qps"], 1, 1000),
        "web_search_mode": (str(data.get("web_search_mode") or "multi").strip()
                            if str(data.get("web_search_mode") or "").strip() in ("multi", "pre")
                            else base["web_search_mode"]),
    }

    protocols = data.get("inject_protocols")
    if isinstance(protocols, (list, tuple)):
        seen = []
        for item in protocols:
            text = str(item or "").strip().lower()
            if text and text not in seen:
                seen.append(text)
        out["inject_protocols"] = seen

    endpoints = data.get("endpoints")
    if isinstance(endpoints, dict):
        for key in base["endpoints"]:
            if key in endpoints:
                out["endpoints"][key] = _as_bool(endpoints[key], base["endpoints"][key])

    for kind in PROVIDER_KINDS:
        key = "%s_providers" % kind
        items = data.get(key)
        if not isinstance(items, (list, tuple)):
            out[key] = copy.deepcopy(base[key])
            continue
        normalized = []
        for item in items:
            entry = _normalize_provider(item, kind, True)
            if entry["name"]:
                normalized.append(entry)
        out[key] = normalized or copy.deepcopy(base[key])
    return out


def validate(raw):
    """返回人类可读的错误列表；空列表表示可以保存。"""
    if not isinstance(raw, dict):
        return ["web_gateway 必须是一个对象"]
    errors = []
    for kind in PROVIDER_KINDS:
        items = raw.get("%s_providers" % kind)
        if items is None:
            continue
        if not isinstance(items, (list, tuple)):
            errors.append("%s_providers 必须是数组" % kind)
            continue
        for item in items:
            if not isinstance(item, dict):
                errors.append("%s_providers 的每一项必须是对象" % kind)
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                errors.append("%s_providers 存在没有 name 的条目" % kind)
            realm = item.get("realm")
            if kind == "search" and realm not in (None, "", "all", "cn", "intl"):
                errors.append("provider %s 的 realm 只能是 all / cn / intl" % name)
            freshness = str(item.get("freshness") or "")
            if freshness:
                import re
                if not re.match(r"^(?:d(?:[1-9]|[12]\d|30)?|m(?:[1-9]|1[0-2])?|y[1-5]?)$",
                                freshness):
                    errors.append("provider %s 的 freshness 不合法" % name)
    qps = raw.get("qps")
    if qps is not None:
        try:
            if int(qps) < 1:
                errors.append("qps 必须 ≥ 1")
        except (TypeError, ValueError):
            errors.append("qps 必须是整数")
    return errors
