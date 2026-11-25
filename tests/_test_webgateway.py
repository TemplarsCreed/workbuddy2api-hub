"""Web gateway: capability endpoints, provider selection and protocol shapes.

Sections:

  [1] per-endpoint key validation (unknown fields, mutual exclusion, SSRF)
  [2] provider selection, fallback and degraded marking
  [3] WorkBuddy request bodies (search / fetch) and account whitelist maths
  [4] Anthropic pre-search: tool removal, system injection, native blocks
  [5] pseudo models (model name routing) and /v1/models entries
  [6] settings normalisation ("all" -> None, [] -> None) and rejection

Everything is offline: providers are replaced with fakes, the account pool is
a stub. No network access required.
"""

import io
import json
import os
import sys
import tempfile
import urllib.error
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_webgateway as gw
from wb_webgateway import api, ir, models, settings
from wb_webgateway.providers.base import Provider, ProviderError
from wb_webgateway.protocols import anthropic as anth

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("  [PASS] %s" % name)
    else:
        FAIL += 1
        print("  [FAIL] %s -> %s" % (name, detail))


class FakeProvider(Provider):
    def __init__(self, name, kind, rows=None, fail=False):
        self.name = name
        self.kind = frozenset(kind)
        self.rows = rows or []
        self.fail = fail
        self.calls = 0

    def search(self, req, ctx):
        self.calls += 1
        if self.fail:
            raise ProviderError("boom")
        return self.rows


def make_cfg(*providers, kind="search"):
    entries = []
    for index, provider in enumerate(providers):
        entry = {"name": provider.name, "enabled": True, "priority": 10 + index,
                 "fallback": True}
        if kind == "search":
            entry.update({"realm": None, "allowed_uids": None, "max_results": 5,
                          "freshness": "", "timeout_ms": 20000})
        entries.append(entry)
    return {"enabled": True, "endpoints": {"search": True, "fetch": True,
                                           "search_fetch": True},
            "%s_providers" % kind: entries, "qps": 5, "inject_protocols": [],
            "inject_in_protocols": False}


print("[1] per-endpoint validation")
cfg = settings.defaults()
cfg["enabled"] = True
registry = gw.Registry(accounts_dir=None)

status, body = api.handle("/v1/tools/fetch", {"query": "x"}, {}, registry, cfg)
check("fetch rejects query", status == 400 and body["error"]["code"] == "unexpected_field", body)

status, body = api.handle("/v1/tools/search", {"query": "abc", "url": "http://a"}, {},
                          registry, cfg)
check("search rejects url", status == 400 and body["error"]["code"] == "unexpected_field", body)

status, body = api.handle("/v1/tools/search",
                          {"query": "abc", "allowed_domains": ["a.com"],
                           "blocked_domains": ["b.com"]}, {}, registry, cfg)
check("domain lists are mutually exclusive",
      status == 400 and body["error"]["code"] == "conflicting_domains", body)

status, body = api.handle("/v1/tools/search", {"query": "a"}, {}, registry, cfg)
check("query shorter than 2 characters is rejected",
      status == 400 and body["error"]["code"] == "invalid_query", body)

status, body = api.handle("/v1/tools/search", {"query": "abc", "maxChars": 10}, {},
                          registry, cfg)
check("unknown field is named in the error",
      status == 400 and "maxChars" in body["error"]["message"], body)

status, body = api.handle("/v1/tools/fetch", {"url": "http://169.254.169.254/"}, {},
                          registry, cfg)
check("private address is refused with 400 blocked_url",
      status == 400 and body["error"]["code"] == "blocked_url", body)

status, body = api.handle("/v1/tools/nope", {"query": "abc"}, {}, registry, cfg)
check("unknown path is 404", status == 404, body)

print()
print("[2] provider selection")
primary = FakeProvider("primary", {"search"}, [ir.result_item("t", "http://x")])
backup = FakeProvider("backup", {"search"}, [ir.result_item("t2", "http://y")])
registry = gw.Registry(accounts_dir=None)
registry.register(primary)
registry.register(backup)
cfg = make_cfg(primary, backup)
result = registry.search({"query": "abc", "max_results": 5}, cfg)
check("first provider wins", result["providers"] == ["primary"] and primary.calls == 1, result)
check("no errors when the first succeeds", result["errors"] == [], result)

failing = FakeProvider("primary", {"search"}, fail=True)
registry = gw.Registry(accounts_dir=None)
registry.register(failing)
registry.register(backup)
cfg = make_cfg(failing, backup)
result = registry.search({"query": "abc", "max_results": 5}, cfg)
check("failover marks degraded", result["providers"] == ["backup"] and result["degraded"], result)
check("failure reason is kept", result["errors"][0]["provider"] == "primary", result)

registry = gw.Registry(accounts_dir=None)
registry.register(failing)
cfg = make_cfg(failing)
result = registry.search({"query": "abc", "max_results": 5}, cfg)
check("all providers failing leaves providers empty", result["providers"] == [], result)

print()
print("[3] WorkBuddy request bodies")
from wb_webgateway.providers import workbuddy as wbp

provider = wbp.WorkbuddyProvider()
body = provider._search_body({"query": "abc", "blocked_domains": ["a.com", "b.com"],
                             "max_results": 7}, {"max_results": 5})
check("blocked domains are folded into the query",
      body["query"] == "abc (-site:a.com -site:b.com)", body)
check("max_results comes from the provider config",
      body["max_results"] == 5, body)

body = provider._search_body({"query": "abc", "allowed_domains": ["good.com"],
                             "time_range": "week"}, {})
check("allowed domains are a separate field", body["allowed_domains"] == ["good.com"], body)
check("time_range maps to freshness", body["freshness"] == "d7", body)

body = provider._fetch_body({"url": "https://x.com", "max_chars": 30000})
check("fetch body uses markdown and the char cap",
      body["format"] == "markdown" and body["max_length"] == 30000, body)

check("whitelist intersection", wbp._allowed_uids(
    {"allowed_uids": ["u1", "u2"]}, {"key_entry": {"accounts": ["u2", "u3"]}}) == {"u2"})
check("explicit key allow-list is enforced", wbp._allowed_uids(
    {}, {"allowed_accounts": {"u2", "u3"}}) == {"u2", "u3"})
check("unrestricted key leaves the pool open", wbp._allowed_uids(
    {}, {"allowed_accounts": None}) is None)
check("no restriction on either side means None", wbp._allowed_uids({}, {}) is None)
check("freshness falls back to the provider setting",
      wbp._freshness("year", "d3") == "d3", wbp._freshness("year", "d3"))
check("WorkBuddy redirects are not followed", wbp._NoRedirect().redirect_request(
    None, None, 302, "Found", {}, "https://evil.example/") is None)


class PickSpyPool(object):
    def __init__(self):
        self.calls = []

    def pick(self, realm=None, exclude=None, allowed=None):
        self.calls.append({"realm": realm, "exclude": exclude, "allowed": allowed})
        return None


pool = PickSpyPool()
wbp.WorkbuddyProvider._pick(pool, {"realm": None, "allowed_uids": None},
                            {"web_cooled": {"u9"}}, {"u1"})
check("pick uses keyword arguments and merges cooled accounts",
      pool.calls == [{"realm": None, "exclude": {"u1", "u9"}, "allowed": None}],
      pool.calls)

print()
print("[4] Anthropic pre-search")
registry = gw.Registry(accounts_dir=None)
registry.register(FakeProvider("local-engines", {"search"},
                               [ir.result_item("T1", "http://a"), ir.result_item("T2", "http://b")]))
cfg = make_cfg(registry.get("local-engines"))
payload = {"model": "claude-x", "messages": [{"role": "user", "content": "how to x"}],
           "tools": [{"type": "web_search_20250305", "name": "web_search"}]}
new_payload, meta = anth.prepare(payload, registry, cfg)
check("server tool declaration is removed", "tools" not in new_payload, new_payload)
check("results are injected into the system prompt",
      "Search results for" in (new_payload.get("system") or ""), new_payload.get("system"))
check("meta carries the query and rows", meta["query"] == "how to x" and len(meta["results"]) == 2, meta)

result = anth.decorate_message({"content": [{"type": "text", "text": "hi"}],
                                "usage": {"input_tokens": 3}}, meta)
check("native blocks come first",
      [b["type"] for b in result["content"]] ==
      ["server_tool_use", "web_search_tool_result", "text"], result["content"])
check("usage reports the search count",
      result["usage"]["server_tool_use"]["web_search_requests"] == 1, result["usage"])

frames = anth.stream_prefix_frames(meta, lambda event, payload: (event, payload))
check("stream prefix covers both blocks",
      [f[0] for f in frames] == ["content_block_start", "content_block_delta",
                                 "content_block_stop", "content_block_start",
                                 "content_block_stop"], frames)
check("block indexes are 0 and 1", [f[1]["index"] for f in frames] == [0, 0, 0, 1, 1], frames)
check("no declaration means no takeover",
      anth.prepare({"messages": []}, registry, cfg)[1] is None)

print()
print("[5] pseudo models")
check("model ids are recognised",
      all(models.is_web_model(m) for m in ("wb-search", "wb-fetch", "wb-search-fetch")))
check("other models are not", not models.is_web_model("deepseek-v4.1"))

entries = models.model_entries()
check("three model entries are published",
      [e["id"] for e in entries] == ["wb-search", "wb-fetch", "wb-search-fetch"], entries)
check("entries are attributed to the gateway",
      all(e["owned_by"] == "wb-webgateway" for e in entries), entries)

kind, req = models.request_for("wb-fetch", [{"role": "user",
                                             "content": "read https://example.com/x please"}])
check("fetch model extracts the first URL", req["url"] == "https://example.com/x", req)
kind, req = models.request_for("wb-search", [{"role": "user", "content": "alpha beta"}])
check("search model uses the message text", req["query"] == "alpha beta", req)

text = models.format_text("wb-search", {"query": "q", "results": [
    {"title": "T", "url": "http://a", "snippet": "S"}]})
check("search text carries the citation footer", "Cite the sources" in text, text)

print()
print("[6] settings")
normalized = settings.normalize({"search_providers": [
    {"name": "workbuddy", "enabled": True, "realm": "all", "allowed_uids": []}]})
entry = normalized["search_providers"][0]
check("realm all normalises to None", entry["realm"] is None, entry)
check("empty whitelist normalises to None", entry["allowed_uids"] is None, entry)
check("missing providers are filled from defaults",
      len(normalized["fetch_providers"]) == 2, normalized["fetch_providers"])

check("invalid realm is rejected",
      any("realm" in e for e in settings.validate(
          {"search_providers": [{"name": "x", "realm": "mars"}]})))
check("invalid freshness is rejected",
      any("freshness" in e for e in settings.validate(
          {"search_providers": [{"name": "x", "freshness": "zz"}]})))
check("valid freshness passes",
      settings.validate({"search_providers": [{"name": "x", "freshness": "d7"}]}) == [])

with tempfile.TemporaryDirectory() as tmp:
    import wb_settings

    stored = wb_settings.set_web_gateway(tmp, {"enabled": True, "inject_protocols": ["anthropic"]})
    check("settings round-trip through the shared file",
          wb_settings.web_gateway(tmp)["enabled"] is True and
          stored["inject_protocols"] == ["anthropic"])
    try:
        wb_settings.set_web_gateway(tmp, {"qps": 0})
        check("non-positive qps is rejected", False)
    except ValueError:
        check("non-positive qps is rejected", True)

print()
print("[7] Gemini")
from wb_webgateway.protocols import gemini as gproto

registry = gw.Registry(accounts_dir=None)
registry.register(FakeProvider("local-engines", {"search"}, [ir.result_item("T1", "http://a")]))
cfg = make_cfg(registry.get("local-engines"))

gem = {"contents": [{"role": "user", "parts": [{"text": "how to x"}]}],
       "tools": [{"googleSearch": {}}],
       "generationConfig": {"temperature": 0.2, "maxOutputTokens": 50}}
new_gem, gmeta = gproto.prepare(gem, registry, cfg)
check("googleSearch is removed", "tools" not in new_gem, new_gem)
check("systemInstruction carries the results",
      "Search results for" in gproto._system_text(new_gem), new_gem)
chat = gproto.to_chat(new_gem, "gemini-x")
check("gemini contents map to chat messages",
      chat["messages"][-1] == {"role": "user", "content": "how to x"}, chat)
out = gproto.from_chat({"choices": [{"message": {"content": "answer"}}],
                        "usage": {"prompt_tokens": 4, "completion_tokens": 2}},
                       gmeta, "gemini-x")
check("gemini response carries grounding chunks",
      len(out["candidates"][0]["groundingMetadata"]["groundingChunks"]) == 1, out)
check("usage maps to usageMetadata",
      out["usageMetadata"]["totalTokenCount"] == 6, out)
check("no googleSearch means no takeover",
      gproto.prepare({"contents": []}, registry, cfg)[1] is None)

print()
print("[8] Anthropic multi-round preparation")
payload = {"model": "m", "messages": [{"role": "user", "content": "q1"}],
           "tools": [{"type": "web_search_20250305", "max_uses": 3}]}
new_payload, multi_meta = anth.prepare_multi(payload)
check("server tool becomes a function definition",
      any(t.get("name") == "web_search" for t in new_payload["tools"]),
      new_payload.get("tools"))
check("multi meta carries max_uses", multi_meta["max_uses"] == 3, multi_meta)
check("multi mode does not pre-search", "results" not in multi_meta, multi_meta)
check("max_uses is clamped", anth.prepare_multi(
    {"model": "m", "messages": [], "tools": [{"type": "web_search_20250305",
                                              "max_uses": 99}]})[1]["max_uses"] == 10)

print()
print("[9] WorkBuddy website-specific refusals do not penalize the pool")
account = SimpleNamespace(uid="u1", domain="example.test")
pool = SimpleNamespace(pick=mock.Mock(return_value=account))
target_registry = gw.Registry(pool=pool, accounts_dir=None)
target_registry.register(provider)
target_cfg = make_cfg(provider, kind="fetch")
for upstream_code, status_wanted, code_wanted in (
    (15021, 403, "target_policy_denied"),
    (15022, 502, "target_unreachable"),
):
    for http_error in (False, True):
        encoded = json.dumps({"error": {"code": upstream_code}}).encode("utf-8")
        opener = mock.Mock()
        if http_error:
            opener.open.side_effect = urllib.error.HTTPError(
                "https://example.test/agenttool/v1/webfetch", 502, "bad gateway", {},
                io.BytesIO(encoded))
        else:
            opener.open.return_value = io.BytesIO(encoded)
        pool.pick.reset_mock()
        with mock.patch.object(target_registry, "guard_url", return_value=("https://public.test", None)), \
                mock.patch.object(wbp, "_opener_for_proxy", return_value=opener):
            status, body = api.handle("/v1/tools/fetch", {"url": "https://public.test"},
                                      {}, target_registry, target_cfg)
        check("target error %d via HTTP error=%s is classified" % (upstream_code, http_error),
              status == status_wanted and body["error"]["code"] == code_wanted
              and pool.pick.call_count == 1 and opener.open.call_count == 1, (status, body))
check("target failures do not cool the provider", not target_registry.cooling("workbuddy"))

print()
print("[10] public web tool results are recorded without request content")
from _isolated_dirs import isolated_data_dirs
_TEST_DATA = isolated_data_dirs("wb-webgateway-usage-")
import wb_proxy as proxy

for path, status in (("/v1/tools/search", 200), ("/v1/tools/fetch", 403),
                     ("/v1/tools/search_fetch", 200)):
    rows = []
    reply_headers = []
    handler = SimpleNamespace(
        _authorized=lambda: True, _key_id=lambda: "fixture-key-id",
        _request_realm=lambda: "cn", _json_extra_headers=None,
        _payload_or_error=lambda: {"query": "private query", "url": "https://private.test"},
    )
    handler._json = lambda code, body: reply_headers.extend(handler._json_extra_headers)
    response = {"error": {"code": "target_policy_denied", "message": "private URL"}} if status >= 400 else {}
    with mock.patch.object(gw, "handle", return_value=(status, response)), \
            mock.patch.object(proxy, "_persist_usage", side_effect=lambda row, label: rows.append(row)):
        proxy.Handler._route_web_gateway(handler, path)
    row = rows[0] if rows else {}
    check("web usage row and response share request id: " + path,
          len(rows) == 1 and row.get("request_id") == dict(reply_headers).get("X-Request-Id")
          and row.get("request_id") and row.get("status") == status
          and row.get("kind") == row.get("cost_policy") == "web"
          and row.get("key") == "fixture-key-id" and row.get("credit") == 0
          and row.get("realm") == "cn" and row.get("elapsed_ms", -1) >= 0, row)
    check("web usage excludes request content", "private" not in json.dumps(row)
          and handler._json_extra_headers is None, row)

print()
print("PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
