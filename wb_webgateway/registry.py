# -*- coding: utf-8 -*-
"""provider 注册、选择与故障转移。

三条硬规则都收在这里，provider 自己不重复实现：

  * 选择：按配置里的 priority 升序，第一个成功即返回，失败继续下一个。
  * 冷却：provider 连续失败 3 次进 60 秒冷却；冷却只影响 web 通道。
  * SSRF：fetch 的 URL 守卫在分发点统一执行，HTTP 端点与协议注入两条
    路径都经过这里，模型输出的 URL 也无法绕开。

账号池由 wb_proxy 启动时注入，本模块不 import wb_proxy（避免循环导入）。
"""

import threading
import time

from . import ir
from .providers.base import FetchTargetError, ProviderError

MAX_CONSECUTIVE_FAILURES = 3
COOLDOWN_SECONDS = 60.0


class Registry(object):
    def __init__(self, pool=None, accounts_dir=None, log=None):
        self.pool = pool
        self.accounts_dir = accounts_dir
        self._log = log if callable(log) else (lambda *a, **k: None)
        self._providers = {}
        self._failures = {}
        self._cooldown_until = {}
        #: 账号级 web 冷却（uid -> 到期时间戳）。与 Account.cooldown_until
        #: 分开：搜索被限流不该让这个号在聊天里也消失。
        self.web_cooldowns = {}
        self._lock = threading.RLock()

    # -- 注册 ------------------------------------------------------------

    def register(self, provider):
        if provider is None or not getattr(provider, "name", ""):
            raise ValueError("provider needs a name")
        self._providers[provider.name] = provider
        return provider

    def get(self, name):
        return self._providers.get(name)

    def all(self):
        return list(self._providers.values())

    def health(self):
        now = time.time()
        out = []
        for provider in self._providers.values():
            info = provider.health()
            info["failures"] = self._failures.get(provider.name, 0)
            until = self._cooldown_until.get(provider.name) or 0
            info["cooling_seconds"] = max(0, int(until - now)) if until else 0
            out.append(info)
        return out

    # -- 账号冷却（workbuddy provider 用） --------------------------------

    def mark_account_cooling(self, uid, seconds=60.0):
        if not uid:
            return
        with self._lock:
            self.web_cooldowns[str(uid)] = time.time() + float(seconds)

    def account_cooling(self, uid):
        if not uid:
            return False
        return (self.web_cooldowns.get(str(uid)) or 0) > time.time()

    def cooled_uids(self):
        now = time.time()
        return set(uid for uid, until in self.web_cooldowns.items() if until > now)

    # -- 选择 ------------------------------------------------------------

    def candidates(self, kind, entries, wanted=None):
        """按配置挑出本次可用的 provider，顺序即尝试顺序。"""
        configured = {}
        for entry in entries or []:
            if isinstance(entry, dict) and entry.get("name"):
                configured[entry["name"]] = entry
        picked = []
        for provider in self._providers.values():
            if kind not in provider.kind:
                continue
            entry = configured.get(provider.name)
            if entry is None or not entry.get("enabled"):
                continue
            if wanted and provider.name not in wanted:
                continue
            picked.append((entry.get("priority", 10), provider.name, provider, entry))
        picked.sort(key=lambda row: (row[0], row[1]))
        return [(row[2], row[3]) for row in picked]

    def _record_failure(self, name):
        with self._lock:
            count = self._failures.get(name, 0) + 1
            self._failures[name] = count
            if count >= MAX_CONSECUTIVE_FAILURES:
                self._cooldown_until[name] = time.time() + COOLDOWN_SECONDS
                self._log("webgateway: provider %s cooling down after %d failures"
                          % (name, count), level="WARN")

    def _record_success(self, name):
        with self._lock:
            self._failures[name] = 0
            self._cooldown_until.pop(name, None)

    # -- 执行 ------------------------------------------------------------

    def search(self, req, cfg, ctx=None):
        started = time.time()
        response = ir.new_response(req.get("query"))
        for provider, entry in self.candidates("search", cfg.get("search_providers"),
                                               req.get("providers")):
            if self.cooling(provider.name):
                ir.note_error(response, provider.name, "cooling down")
                continue
            try:
                items = provider.search(req, self._provider_ctx(ctx, entry))
            except ProviderError as exc:
                ir.note_error(response, provider.name, str(exc))
                self._record_failure(provider.name)
                continue
            except Exception as exc:
                ir.note_error(response, provider.name,
                              "%s: %s" % (type(exc).__name__, exc))
                self._record_failure(provider.name)
                continue
            self._record_success(provider.name)
            ir.mark_provider(response, provider.name)
            response["results"] = list(items or [])
            break
        return ir.finalize(response, (time.time() - started) * 1000)

    def fetch(self, req, cfg, ctx=None):
        started = time.time()
        response = ir.new_response()
        url, problem = self.guard_url(req.get("url"))
        if problem:
            ir.note_error(response, "guard", problem)
            response["blocked"] = True
            return ir.finalize(response, (time.time() - started) * 1000)
        req = dict(req)
        req["url"] = url
        for provider, entry in self.candidates("fetch", cfg.get("fetch_providers"),
                                               req.get("providers")):
            if self.cooling(provider.name):
                ir.note_error(response, provider.name, "cooling down")
                continue
            try:
                item = provider.fetch(req, self._provider_ctx(ctx, entry))
            except FetchTargetError as exc:
                ir.note_error(response, provider.name, str(exc))
                response["errors"][-1]["code"] = exc.code
                continue
            except ProviderError as exc:
                ir.note_error(response, provider.name, str(exc))
                self._record_failure(provider.name)
                continue
            except Exception as exc:
                ir.note_error(response, provider.name,
                              "%s: %s" % (type(exc).__name__, exc))
                self._record_failure(provider.name)
                continue
            self._record_success(provider.name)
            ir.mark_provider(response, provider.name)
            response["results"] = [item]
            break
        return ir.finalize(response, (time.time() - started) * 1000)

    def search_and_fetch(self, req, cfg, ctx=None):
        """搜索后对前 fetch_top_n 条并发抓正文（这里串行，条数很小）。"""
        response = self.search(req, cfg, ctx)
        top = req.get("fetch_top_n")
        try:
            top = int(top) if top is not None else 0
        except (TypeError, ValueError):
            top = 0
        if top <= 0 or not response["results"]:
            return response
        for item in response["results"][:top]:
            if not item.get("url"):
                continue
            sub = {"url": item["url"], "max_chars": req.get("max_chars"),
                   "prompt": req.get("prompt")}
            got = self.fetch(sub, cfg, ctx)
            if got["results"]:
                page = got["results"][0]
                item["content"] = page.get("content")
                item["content_truncated"] = page.get("content_truncated")
                item["next_start_index"] = page.get("next_start_index")
            else:
                for err in got["errors"]:
                    response["errors"].append({
                        "provider": err.get("provider"),
                        "message": "fetch %s: %s" % (item["url"], err.get("message")),
                    })
        response["degraded"] = bool(response["providers"]) and bool(response["errors"])
        return response

    # -- 守卫 ------------------------------------------------------------

    @staticmethod
    def guard_url(url):
        """统一 URL 守卫。返回 (规范化 URL, 问题描述)。"""
        try:
            import wb_webtools

            return wb_webtools.guard_url(url, resolve=True)
        except Exception as exc:
            return "", "url guard failed (%s)" % type(exc).__name__

    def cooling(self, name):
        return (self._cooldown_until.get(name) or 0) > time.time()

    def _provider_ctx(self, ctx, entry):
        """provider 需要的运行时依赖：自己的配置、账号池、本 registry。"""
        merged = dict(ctx or {})
        merged["entry"] = entry
        merged["pool"] = self.pool
        merged["registry"] = self
        merged["web_cooled"] = self.cooled_uids()
        return merged
