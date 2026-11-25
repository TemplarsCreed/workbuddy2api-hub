# -*- coding: utf-8 -*-
"""provider 的公共约定。

一个 provider 只负责“把请求送出去、把结果拿回来”，不做故障转移、不做冷却、
不做 SSRF 判定——这些都在 registry 里做，保证所有 provider 走同一套规则。
"""


class ProviderError(Exception):
    """一次 provider 调用失败；registry 记进 errors 后尝试下一个。"""

    def __init__(self, message, retryable=True):
        super().__init__(str(message or "provider failed"))
        self.retryable = bool(retryable)


class FetchTargetError(ProviderError):
    """A refusal or connection failure specific to the requested website."""

    def __init__(self, code, message):
        super().__init__(message, retryable=False)
        self.code = code


class Provider(object):
    """provider 基类。子类至少实现 search 或 fetch。"""

    name = ""
    kind = frozenset()

    def search(self, req, ctx):
        raise NotImplementedError

    def fetch(self, req, ctx):
        raise NotImplementedError

    def health(self):
        return {"name": self.name, "kind": sorted(self.kind)}
