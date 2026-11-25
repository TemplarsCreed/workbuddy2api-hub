"""出口感知的统一模型别名：数据层与解析（PR-1）。

覆盖：归一化、开关默认关、按出口解析、未知名原样返回、表在关着时可编辑。
无网络、无凭据，纯标准库。运行：python tests/_test_unified_model.py
"""
import os
import sys
import tempfile

_startup_dir = tempfile.TemporaryDirectory(prefix="unified-model-")
os.environ["ACCOUNTS_DIR"] = _startup_dir.name
os.environ["WB_PROXY_USAGE_DIR"] = _startup_dir.name
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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


TABLE = {
    "DeepSeek-Flash": {
        "cn": "deepseek-v4.1-flash",
        "codearts": "deepseek-v4.1-flash-1017",
    },
    "Only-CN": {"cn": "glm-5.2"},
    "bad-no-target": {"cn": ""},
    "bad-unknown-realm": {"mars": "x"},
}

print("[1] normalization drops bad entries and lowercases names")
d = tempfile.mkdtemp(prefix="unified-clean-")
stored = S.set_unified_models(d, TABLE)
check("name is lowercased", "deepseek-flash" in stored, stored)
check("valid realm pairs kept",
      stored.get("deepseek-flash", {}).get("cn") == "deepseek-v4.1-flash"
      and stored.get("deepseek-flash", {}).get("codearts")
      == "deepseek-v4.1-flash-1017", stored)
check("empty target dropped", "bad-no-target" not in stored, stored)
check("unknown realm dropped", "bad-unknown-realm" not in stored, stored)
check("round-trips through reader", S.unified_models(d) == stored)

print()
print("[2] switch defaults off; table survives while off")
check("off by default", S.unified_models_enabled(d) is False)
check("off switch yields no effective table", S.effective_unified_models(d) == {})
check("stored table still readable while off",
      "deepseek-flash" in S.unified_models(d))
S.set_unified_models_enabled(d, True)
check("switch on exposes the table",
      "deepseek-flash" in S.effective_unified_models(d))
check("enabled flag persisted", S.unified_models_enabled(d) is True)

print()
print("[3] per-realm resolution")
check("cn resolves to CN id",
      S.resolve_unified_model("DEEPSEEK-FLASH", "cn", d)
      == "deepseek-v4.1-flash")
check("codearts resolves to CodeArts id",
      S.resolve_unified_model("deepseek-flash", "codearts", d)
      == "deepseek-v4.1-flash-1017")
check("unlisted realm falls back to original name",
      S.resolve_unified_model("deepseek-flash", "intl", d)
      == "deepseek-flash")
check("unknown name passes through",
      S.resolve_unified_model("ordinary-model", "cn", d) == "ordinary-model")

print()
print("[4] switch off resolves nothing")
S.set_unified_models_enabled(d, False)
check("off switch: name unchanged",
      S.resolve_unified_model("deepseek-flash", "cn", d) == "deepseek-flash")
check("empty realm: name unchanged",
      S.resolve_unified_model("deepseek-flash", "", d) == "deepseek-flash")
check("non-string name: returned as-is",
      S.resolve_unified_model(42, "cn", d) == 42)

print()
print("[5] malformed file never crashes the reader")
d2 = tempfile.mkdtemp(prefix="unified-bad-")
S.save(d2, {"unified_models": "not-a-table"})
check("malformed table reads as empty", S.unified_models(d2) == {})
check("resolution on malformed table is a no-op",
      S.resolve_unified_model("deepseek-flash", "cn", d2) == "deepseek-flash")

print()
print("RESULT: %d passed, %d failed" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
