# -*- coding: utf-8 -*-
"""包装 wb_webtools 的本地抓取。

防 SSRF 的实际判定在 registry 的 fetch 分发点统一做（``guard_url``），
这里再调一次结构化出口，命中缓存时不会多走一次网络。
"""

from .. import ir
from .base import Provider, ProviderError


class LocalHttpProvider(Provider):
    name = "local-http"
    kind = frozenset({"fetch"})

    def fetch(self, req, ctx):
        import wb_webtools

        out = wb_webtools.web_fetch_result(
            req.get("url"),
            req.get("start_index") or 0,
            req.get("max_chars"),
        )
        if not out.get("ok"):
            raise ProviderError(out.get("error") or "fetch failed")
        return ir.result_item(
            title=out.get("url"),
            url=out.get("url"),
            content=out.get("content"),
            source=self.name,
            content_truncated=bool(out.get("truncated")),
            next_start_index=out.get("next_start_index"),
        )
