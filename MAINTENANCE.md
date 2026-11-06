# 自维护说明（本 fork 专用）

上游 `ardeyouxipianyi/workbuddy2api-hub` 于 **2026-11-06 宣布停止维护**，同日发布最后一个版本 **v2.0.0**。
本 fork（`TemplarsCreed/workbuddy2api-hub`）以该版本为基线，改由自己维护。

| 项 | 值 |
| --- | --- |
| 基线提交 | `d2343964` `merge-release-v2.0.0`（2026-11-06T07:51:04Z） |
| 基线 tag | `baseline-v2.0.0`（已推到 origin） |
| remote | `origin` = 本 fork；`upstream` = 上游（只读参照，已无新提交） |
| 许可证 | MIT — 可自由修改/分发，保留原版权声明即可 |

## 改了什么，一眼看出来

```bash
git fetch --tags
git log --oneline baseline-v2.0.0..main          # 自有改动列表
git diff baseline-v2.0.0 --stat                  # 自有改动范围
```

不要在 `baseline-v2.0.0` 上提交；所有改动走 `main`（单一维护者够用，需要试验再开临时分支）。

## 改动前必读

- `AGENTS.md` → 指向 `CONTRIBUTING.md` 的「AI / 智能体贡献者硬性规则」，改代码前先读一遍，避免踩它列的坑。
- 版本号三处必须一致（发版门禁硬性要求）：`wb_proxy.py` 的 `"version"`、同文件下方 `server_version = "wb-proxy/x.y.z"`、`wrt/openwrt/workbuddy2api/Makefile` 的 `PKG_VERSION:=x.y.z`；且 tag 名必须等于源码版本。当前自用版本 **v2.0.1**。改漏一处，`release.yml` 的 version job 会直接红。
- README 顶部是「自用维护分支」口径（无兼容承诺），不要把上游的「停止维护 / 赞助商」声明加回来。
- `dashboard.html` 是 1.2MB 单文件前端，`wb_proxy.py` 是 1.2MB 主服务单文件；两者都是「大文件里改小地方」，编辑时用精确锚点，不要整文件重写。

## 回归测试（改动后必跑）

```bash
PYTHONUTF8=1 python tests/run_all.py --jobs 4 --logs suite-logs
python scripts/check_clean_checkout.py      # 套件不许往 checkout 里写文件
```

单个套件：`python tests/_test_account_proxy.py` 之类，直接跑。

**在受限沙箱 shell 里跑会假失败**：套件用 `tempfile.mkdtemp()` 建临时目录（模式 `0o700`，即仅属主可访问），而 DSH 受限令牌不是该目录的属主，于是 `os.makedirs`/`open` 全报
`PermissionError: [WinError 5] 拒绝访问`。实测 170 个套件因此假红、混淆真回归。
换 `TEMP`/`TMP` 指到工作区内也无效（目录本身的 ACL 就进不去）。要么在**完整访问**的 shell 里跑，要么以 `0o777` 手工建目录后改 `TMP` 指过去。
CI 矩阵（Linux 3.9 / Linux 3.12 / Windows 3.12，Node 20）见 `.github/workflows/tests.yml`；fork 的 Actions 默认可用，推 `main` 或 `v*` tag 就会跑。

## 上游若又发补丁（可能性低）

```powershell
powershell -ExecutionPolicy Bypass -File .\sync-upstream.ps1
```

脚本只做 fetch + 预演 merge；有冲突就停下，交给人处理。

## 发版与回滚

```bash
# 1. 改 wb_proxy.py 里的 version
# 2. 提交并打 tag（本地 tag 用 local- 前缀，避免和上游 v* 混淆）
git commit -am "chore: bump version to 2.0.1"
git tag -a local-v2.0.1 -m "local release 2.0.1"
git push origin main local-v2.0.1
```

回滚到基线：`git reset --hard baseline-v2.0.0`（会丢改动，先 `git branch backup-$(date +%Y%m%d)`）。

## 已知风险

- 上游 WorkBuddy 服务端在收紧对非官方客户端的识别（README 的 WARNING），协议随时可能变；本项目本质是协议模拟，**服务端一改就得改代码**，这正是必须自维护的原因。
- 原维护者不再处理 issue/PR，`docs/CHANGELOG.md` 里的历史修复记录只能当线索，不能再指望上游补丁。
- 账号风控、限流、封禁风险由使用者自负；本项目仅供协议研究与个人自用。
