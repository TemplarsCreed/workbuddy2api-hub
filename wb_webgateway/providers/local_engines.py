# -*- coding: utf-8 -*-
"""包装 wb_webtools 的多引擎链。

wb_webtools 原本只回整段文字（模型直接读），结构化出口
``web_search_results()`` 是这次一起加的，两者共用同一套引擎、冷却与缓存。
"""

from .. import ir
from .base import Provider, ProviderError


class LocalEnginesProvider(Provider):
    name = "local-engines"
    kind = frozenset({"search"})

    def search(self, req, ctx):
        import wb_webtools

        out = wb_webtools.web_search_results(
            req.get("query"),
            req.get("max_results") or 10,
            req.get("allowed_domains"),
        )
        if not out.get("ok"):
            raise ProviderError(out.get("error") or "search failed")
        items = []
        for row in out.get("results") or []:
            items.append(ir.result_item(
                title=row.get("title"),
                url=row.get("url"),
                snippet=row.get("snippet"),
                published_date=row.get("published_date"),
                source=self.name,
            ))
        return items
