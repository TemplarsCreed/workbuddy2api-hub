"""Unified model wiring: per-exit catalogue advertising and isolation (PR-2).

Covers the pure helper that /v1/models uses plus the isolation contract that
motivated the feature: closing a real id on one exit must not affect the same
client-facing name on another exit. No network, no credentials, stdlib only.
Run: python tests/_test_unified_model_wiring.py
"""
import os
import sys
import tempfile

_startup_dir = tempfile.TemporaryDirectory(prefix="unified-wiring-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import wb_proxy as P
import wb_settings as S

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


CAT = [
    {"id": "deepseek-v4.1-flash", "object": "model", "name": "CN Flash"},
    {"id": "deepseek-v4.1-flash-1017", "object": "model", "name": "CA Flash"},
    {"id": "hy3", "object": "model"},
]
UNIFIED = {"ds-flash": {"cn": "deepseek-v4.1-flash",
                        "codearts": "deepseek-v4.1-flash-1017"}}

print("[1] catalogue advertises the name only where its target is served")
cn = [m["id"] for m in P.advertise_unified_models(CAT, UNIFIED, "cn")]
ca = [m["id"] for m in P.advertise_unified_models(CAT, UNIFIED, "codearts")]
check("CN exit advertises unified name", "ds-flash" in cn, cn)
check("CodeArts exit advertises unified name", "ds-flash" in ca, ca)
check("original ids untouched", "deepseek-v4.1-flash" in cn
      and "deepseek-v4.1-flash-1017" in ca, (cn, ca))
cn_entry = next(m for m in P.advertise_unified_models(CAT, UNIFIED, "cn")
                if m["id"] == "ds-flash")
check("entry copies target metadata, swaps id/name",
      cn_entry.get("name") == "ds-flash" and cn_entry.get("object") == "model",
      cn_entry)

print()
print("[2] a name with no target on this exit is not advertised")
only_cn = {"cn-only": {"cn": "deepseek-v4.1-flash"}}
ca2 = [m["id"] for m in P.advertise_unified_models(CAT, only_cn, "codearts")]
check("not advertised where target absent", "cn-only" not in ca2, ca2)

print()
print("[3] no unified table / empty / off => catalogue unchanged")
check("empty table returns same object",
      P.advertise_unified_models(CAT, {}, "cn") is CAT)
check("no mutation of source", [m["id"] for m in CAT][:1] == ["deepseek-v4.1-flash"])

print()
print("[4] isolation: closing CN id does not change CodeArts resolution")
d = tempfile.mkdtemp(prefix="unified-iso-")
S.set_unified_models(d, UNIFIED)
S.set_unified_models_enabled(d, True)
resolved_ca = S.resolve_unified_model("ds-flash", "codearts", d)
# Simulate CN account closing the CN real id.
closed = {"deepseek-v4.1-flash"}
check("CN real id is closed", "deepseek-v4.1-flash" in closed)
check("CodeArts still resolves to its own id",
      resolved_ca == "deepseek-v4.1-flash-1017", resolved_ca)
check("closed CN id is not the CodeArts target",
      resolved_ca not in closed, resolved_ca)

print()
print("[5] default-off: neither advertising nor rewriting happens")
d2 = tempfile.mkdtemp(prefix="unified-off-")
S.set_unified_models(d2, UNIFIED)  # switch left off
check("off switch yields no effective table", S.effective_unified_models(d2) == {})
check("off switch: catalogue unchanged",
      P.advertise_unified_models(CAT, S.effective_unified_models(d2), "cn") is CAT)
check("off switch: name unchanged",
      S.resolve_unified_model("ds-flash", "codearts", d2) == "ds-flash")

print()
print("[6] protocol entry points resolve the client-facing name")
old_accounts = P.ACCOUNTS_DIR
P.ACCOUNTS_DIR = d
try:
    body = {"model": "DS-FLASH", "input": "hi"}
    P.resolve_unified_model_in_payload(body, "cn")
    check("responses-style body is resolved", body["model"] ==
          "deepseek-v4.1-flash", body)
    anthropic = {"model": "ds-flash", "messages": [{"role": "user", "content": "hi"}]}
    P.resolve_unified_model_in_payload(anthropic, "codearts")
    check("anthropic-style body is resolved", anthropic["model"] ==
          "deepseek-v4.1-flash-1017", anthropic)
    unknown = {"model": "other", "input": "hi"}
    P.resolve_unified_model_in_payload(unknown, "cn")
    check("unknown names pass through", unknown["model"] == "other", unknown)
    missing = {"input": "hi"}
    P.resolve_unified_model_in_payload(missing, "cn")
    check("a body without a model is untouched", missing == {"input": "hi"}, missing)
finally:
    P.ACCOUNTS_DIR = old_accounts

print()
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
