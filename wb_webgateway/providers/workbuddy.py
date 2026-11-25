# -*- coding: utf-8 -*-
"""WorkBuddy 官方 ``/agenttool`` 接口。

端点与请求体来自 WorkBuddy 5.11.3 内置 CodeBuddy CLI 的打包产物
（``cli/dist/codebuddy-lite-wb.mjs``，2026-11-16 反查）：

    POST {domain}/agenttool/v1/search
      {query, type:"text2text", max_results, allowed_domains?, freshness?}
      → {code, msg, provider, results[{title,url,snippet,site?,favicon?}],
         total_results, response_time_ms}

    POST {domain}/agenttool/v1/webfetch
      {url, format:"markdown", prompt, timeout, max_length, model_name?}
      → {code, msg, url, content, content_type,
         context.postprocess_context.extract_applied}

鉴权是账号自己的 accessToken，出口走 ``wb_accounts.urlopen``（账号代理 +
TLS 指纹桥），与聊天路径共用同一条出口，避免同一账号在两条通道上呈现
两种设备特征。

失败口径刻意保守：429 与 401 换号重试（预算 ``WB_WEB_ROTATE_MAX``，默认 1），
403 一律不换号直接交给 registry 的兜底——仓库对 403 有封号红线，宁可让
这一个请求失败，也不能触发设备连坐。
"""

import json
import os
import urllib.error
import urllib.request
import uuid

from .. import ir
from .base import FetchTargetError, Provider, ProviderError

SEARCH_PATH = "/agenttool/v1/search"
FETCH_PATH = "/agenttool/v1/webfetch"
DEFAULT_TIMEOUT_MS = 20000
FETCH_MAX_CHARS = 100000
_FETCH_TARGET_ERRORS = {
    "15021": ("target_policy_denied", "WorkBuddy cannot fetch this website under its routing policy (15021)"),
    "15022": ("target_unreachable", "WorkBuddy could not connect to the target website (15022)"),
}

#: time_range（能力端点的 day/week/month/year）→ 官方 freshness（dN/mN/yN）
TIME_RANGE_TO_FRESHNESS = {"day": "d1", "week": "d7", "month": "m1", "year": "y1"}


class _AccountRejected(Exception):
    """账号级别的失败（401 / 429 / 403），由 _call 决定是否换号。"""

    def __init__(self, status, message, retryable=True):
        super().__init__(message)
        self.status = int(status or 0)
        self.retryable = bool(retryable)


def _check_fetch_target_error(path, payload):
    if path != FETCH_PATH or not isinstance(payload, dict):
        return
    nested = payload.get("error")
    code = str(payload.get("code") or (nested.get("code") if isinstance(nested, dict) else ""))
    failure = _FETCH_TARGET_ERRORS.get(code)
    if failure:
        raise FetchTargetError(*failure)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Do not replay account credentials to a redirect target.

    ``urllib`` copies ``Authorization`` and custom headers onto a redirected
    request. The WorkBuddy provider sends an account access token, so a 3xx
    must be treated as a failed upstream response instead of being followed.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _opener_for_proxy(proxy):
    proxy = str(proxy or "").strip()
    if not proxy:
        return _OPENER
    return urllib.request.build_opener(
        _NoRedirect(),
        urllib.request.ProxyHandler({"http": proxy, "https": proxy}),
    )


def _rotate_budget():
    try:
        n = int(os.environ.get("WB_WEB_ROTATE_MAX", "1") or 1)
    except (TypeError, ValueError):
        n = 1
    return max(0, min(3, n))


def _allowed_uids(entry, ctx):
    """配置白名单 ∩ key 白名单；任一侧为 None 表示不限制。"""
    configured = entry.get("allowed_uids")
    configured_set = set(configured) if configured else None
    key_entry = (ctx or {}).get("key_entry")
    key_set = None
    if "allowed_accounts" in (ctx or {}):
        supplied = ctx.get("allowed_accounts")
        key_set = set(supplied) if supplied is not None else None
    elif key_entry:
        try:
            import wb_settings

            key_set = wb_settings.key_allowed_accounts(key_entry)
        except Exception:
            key_set = None
    if configured_set and key_set:
        return configured_set & key_set
    return configured_set or key_set


def _freshness(time_range, configured):
    text = str(configured or "").strip()
    if text:
        return text
    return TIME_RANGE_TO_FRESHNESS.get(str(time_range or "").strip().lower(), "")


def _headers(account):
    try:
        raw = account.headers(purpose="chat")
    except Exception:
        raw = {}
    if isinstance(raw, dict):
        headers = dict(raw)
    else:
        headers = {}
        for item in raw or []:
            try:
                name, value = item
            except (TypeError, ValueError):
                continue
            headers[str(name)] = str(value)
    headers.setdefault("Accept", "application/json")
    headers["Content-Type"] = "application/json; charset=utf-8"
    headers.setdefault("X-Requested-With", "XMLHttpRequest")
    headers["X-Request-ID"] = uuid.uuid4().hex
    return headers


class WorkbuddyProvider(Provider):
    name = "workbuddy"
    kind = frozenset({"search", "fetch"})

    # -- 能力 ------------------------------------------------------------

    def search(self, req, ctx):
        entry = (ctx or {}).get("entry") or {}
        payload = self._call(SEARCH_PATH, self._search_body(req, entry), entry, ctx)
        items = []
        for row in payload.get("results") or []:
            if not isinstance(row, dict):
                continue
            items.append(ir.result_item(
                title=row.get("title"),
                url=row.get("url"),
                snippet=row.get("snippet"),
                site=row.get("site"),
                favicon=row.get("favicon"),
                source=self.name,
            ))
        if not items and payload.get("total_results"):
            raise ProviderError("upstream reported %s results but returned none"
                                % payload.get("total_results"))
        return items

    def fetch(self, req, ctx):
        entry = (ctx or {}).get("entry") or {}
        payload = self._call(FETCH_PATH, self._fetch_body(req), entry, ctx)
        content = str(payload.get("content") or "")
        if not content.strip():
            raise ProviderError("no readable content found on the page")
        try:
            limit = int(req.get("max_chars") or 20000)
        except (TypeError, ValueError):
            limit = 20000
        limit = max(1, min(FETCH_MAX_CHARS, limit))
        final_url = str(payload.get("url") or req.get("url") or "")
        truncated = len(content) > limit
        return ir.result_item(
            title=final_url,
            url=final_url,
            content=content[:limit],
            source=self.name,
            content_truncated=truncated,
            next_start_index=limit if truncated else None,
        )

    # -- 请求体 ----------------------------------------------------------

    @staticmethod
    def _search_body(req, entry):
        query = str(req.get("query") or "").strip()
        blocked = [str(x).strip() for x in (req.get("blocked_domains") or []) if str(x).strip()]
        if blocked:
            # 官方实现把 blocked_domains 拼进 query，而不是单独字段
            query = "%s (%s)" % (query, " ".join("-site:%s" % d for d in blocked))
        try:
            max_results = int(entry.get("max_results") or req.get("max_results") or 5)
        except (TypeError, ValueError):
            max_results = 5
        body = {
            "query": query,
            "type": "text2text",
            "max_results": max(1, min(50, max_results)),
        }
        allowed = [str(x).strip() for x in (req.get("allowed_domains") or []) if str(x).strip()]
        if allowed:
            body["allowed_domains"] = allowed
        freshness = _freshness(req.get("time_range"), entry.get("freshness"))
        if freshness:
            body["freshness"] = freshness
        return body

    @staticmethod
    def _fetch_body(req):
        try:
            limit = int(req.get("max_chars") or 20000)
        except (TypeError, ValueError):
            limit = 20000
        body = {
            "url": req.get("url"),
            "format": "markdown",
            "timeout": max(1, DEFAULT_TIMEOUT_MS // 1000),
            "max_length": max(1000, min(FETCH_MAX_CHARS, limit)),
        }
        if req.get("prompt"):
            body["prompt"] = req["prompt"]
        return body

    # -- 账号与出站 ------------------------------------------------------

    def _call(self, path, body, entry, ctx):
        pool = (ctx or {}).get("pool")
        if pool is None:
            raise ProviderError("no account pool is available", retryable=False)
        registry = (ctx or {}).get("registry")
        tried = set()
        last = None
        for _ in range(_rotate_budget() + 1):
            account = self._pick(pool, entry, ctx, tried)
            if account is None:
                break
            tried.add(getattr(account, "uid", "") or "")
            try:
                return self._post(account, path, body, entry)
            except _AccountRejected as exc:
                last = exc
                if exc.status == 429 and registry is not None:
                    registry.mark_account_cooling(getattr(account, "uid", ""))
                if not exc.retryable:
                    break
                continue
        if last is not None:
            raise ProviderError(str(last))
        raise ProviderError("no usable account is currently available", retryable=False)

    @staticmethod
    def _pick(pool, entry, ctx, tried):
        cooled = set((ctx or {}).get("web_cooled") or ())
        exclude = set(tried) | cooled
        try:
            return pool.pick(realm=entry.get("realm"), exclude=exclude,
                             allowed=_allowed_uids(entry, ctx))
        except TypeError:
            # 老版本 pick 的签名不同：退回只按 realm 选
            return pool.pick(realm=entry.get("realm"), exclude=exclude)

    @staticmethod
    def _post(account, path, body, entry):
        import wb_accounts

        domain = str(getattr(account, "domain", "") or "").strip()
        if not domain:
            raise ProviderError("account has no upstream domain", retryable=False)
        url = "https://%s%s" % (domain, path)
        try:
            timeout_ms = int(entry.get("timeout_ms") or DEFAULT_TIMEOUT_MS)
        except (TypeError, ValueError):
            timeout_ms = DEFAULT_TIMEOUT_MS
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=data, headers=_headers(account),
                                         method="POST")
        try:
            opener = _opener_for_proxy(getattr(account, "proxy", "") or "")
            with opener.open(request, timeout=timeout_ms / 1000.0) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            status = int(getattr(exc, "code", 0) or 0)
            detail = ""
            try:
                with exc:
                    detail = exc.read(4096).decode("utf-8", "replace").strip()
            except Exception:
                pass
            if path == FETCH_PATH and status not in (401, 429):
                try:
                    payload = json.loads(detail)
                except ValueError:
                    payload = None
                _check_fetch_target_error(path, payload)
            detail = detail[:200]
            raise _AccountRejected(
                status,
                "HTTP %s%s" % (status, (": " + detail) if detail else ""),
                retryable=status in (401, 429),
            )
        except Exception as exc:
            raise ProviderError("request failed (%s)" % type(exc).__name__)
        try:
            payload = json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            raise ProviderError("upstream returned non-JSON")
        if not isinstance(payload, dict):
            raise ProviderError("upstream returned an unexpected payload")
        _check_fetch_target_error(path, payload)
        code = payload.get("code")
        if code not in (0, None):
            raise ProviderError("upstream error %s: %s"
                                % (code, payload.get("msg") or "unknown"))
        return payload
