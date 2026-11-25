# 网页搜索与抓取网关集成方案（feat）

> 状态：**方案稿 v3（已过三轮评审，评审判定「可通过」），未实现、未提交**。写成 `docs/WEBGATEWAY-PLAN.md` 是为了将来直接当 PR 描述的基础。
> 基线：main @ `5a95e48`（v2.1.3）。
> 取证来源：WorkBuddy 5.11.3 安装目录下的 `resources/app.asar.unpacked/cli/dist/codebuddy-lite-wb.mjs`（12.8 MB，内置 CodeBuddy CLI）、本仓库 `wb_webtools.py` / `wb_proxy.py` / `wb_accounts.py`、三个腾讯域名端点实测。

## 0. 一句话

在现有 chat 代理旁边**并列**加一层 web 网关：三个能力端点（搜索 / 抓取 / 搜索+抓取）+ 按协议分批交付的注入层。
执行层由可多选的 provider 承担（WorkBuddy 官方 `/agenttool` 接口 + 现有零依赖引擎链 + 可选外部服务），
账号选择复用现有池与 key 级白名单，**不新建账号体系、不新建记账表**；默认关闭。

## 1. 本机取证（已验证事实，2026-11-16）

| 事实 | 值 | 用途 |
| --- | --- | --- |
| 搜索端点 | `POST {domain}/agenttool/v1/search` | 独立接口，不走 chat completions |
| 抓取端点 | `POST {domain}/agenttool/v1/webfetch` | 同上 |
| 大陆域名 | `https://www.workbuddy.cn`（主）/ `https://copilot.tencent.com`（备） | 均实测 401，端点存在 |
| 国际域名 | `https://www.workbuddy.ai` | 实测 401；与 `REALM_CONFIGS['intl']['domain']` 一致 |
| 鉴权 | `Authorization: Bearer <accessToken>` | 无 token 返回 401 / APISIX |
| 搜索请求体 | `{query, type:"text2text", max_results:5, allowed_domains?, freshness?}` | `blocked_domains` 拼进 query 成 `-site:` |
| 搜索参数约束 | `query` ≥ 2 字符；`freshness` 匹配 `^(d[1-30]?|m[1-12]?|y[1-5]?)$` | Zod schema |
| 搜索响应 | `{code, msg, provider, results:[{title,url,snippet,site?,favicon?}], total_results, response_time_ms}` | `code` 非 0 即错误；`provider:"1"` = 元宝 |
| 抓取请求体 | `{url, format:"markdown", prompt, timeout:20, max_length:100000, model_name?}` | 服务端可做 AI 摘要 |
| 抓取响应 | `{code, msg, url, content, content_type, context.postprocess_context.extract_applied}` | `url` 为重定向后地址；`content` 为 Markdown |
| 超时 | 搜索 20 s；抓取 20 s；摘要提醒阈值 15 KB；正文上限 100 KB | 常量 `2e4` / `15e3` / `1e5` |
| 现有本地执行器 | `wb_webtools.py` `search()` / `fetch()` 返回**整段文本**，`_search_fanout()` 亦同 | 需新增结构化出口，不能直接复用 |
| 现有账号字段 | `Account.access_token` / `.domain` / `.realm` / `.uid` / `.proxy` | `wb_accounts.py:248` 已在用 `Bearer <access_token>` |
| 现有选择器 | `AccountPool.pick(realm, exclude, model, page, allowed, scope)` | 位置参数顺序如左；`realm=None` 全池；`allowed=None` 不限制，必须用关键字传参 |
| 现有出口 | `wb_accounts.urlopen(req, timeout, proxy)` | 走 keep-alive 池与 TLS 指纹桥 |
| 现有协议现状 | `/v1/responses` 有多轮代跑循环；`/v1/messages`、`/v1/chat/completions` 均为**单发透传** | 决定各协议的真实工作量 |
| 现有开关 | 全局布尔 `local_web_tools` | 新方案须兼容，不能静默关闭既有部署 |
| 服务器 new-api | `/agenttool/*` 不转发，返回 SPA 兜底页 | 网关必须直连腾讯域名 |

## 2. 目标 API（Layer 1，能力端点）

三个端点，**per-endpoint 允许键集**，未知键 400。

| 方法 | 路径 | 允许的键 |
| --- | --- | --- |
| POST | `/v1/tools/search` | `query` `max_results` `language` `time_range` `categories` `engines` `safesearch` `pageno` `allowed_domains` `blocked_domains` `user_location` `providers` |
| POST | `/v1/tools/fetch` | `url` `max_chars` `prompt` `start_index` `allowed_domains` `blocked_domains` `providers` |
| POST | `/v1/tools/search_fetch` | `search` 的键 + `fetch_top_n` `max_chars` `prompt` |

互斥规则：

- `/v1/tools/search` 收到 `url` → 400，错误码 `unexpected_field`，提示改用 `/v1/tools/fetch`
- `/v1/tools/fetch` 收到 `query` → 400，同上
- `allowed_domains` 与 `blocked_domains` 同时出现 → 400，与 Anthropic、OpenAI 两家一致

响应：

```json
{
  "object": "web_search_result",
  "providers": ["workbuddy"],
  "query": "...",
  "results": [
    { "title": "...", "url": "...", "snippet": "...", "content": null,
      "site": "...", "favicon": "...", "source": "workbuddy",
      "content_truncated": false, "next_start_index": null }
  ],
  "elapsed_ms": 812,
  "degraded": false,
  "errors": []
}
```

- 顶层 `providers` 列出本次实际成功参与的 provider（修掉 v1 里 `provider` 单值 + `sources[]` 的语义重叠）
- `next_start_index` 非 null 表示正文被 `max_chars` 截断，调用方可带 `start_index` 续读
- `degraded=true` 表示主 provider 失败、由低优先级 provider 出结果

`max_results` 由请求体单点决定，未给时取 `10`；上游超过各自上限时截断，并在响应里回执实际条数。
`max_chars` 以字符计，默认 `20000`，上限 `100000`；与协议的 token 单位不换算，文档写明差异。

鉴权沿用现有 `_authorized()` 与 `wb_settings.match_api_key`。

## 2.5 伪模型（模型名路由）

搜索与抓取除了三个能力端点，还以「模型」形式暴露：客户端把 `model` 设成下面三个 id
即可直接拿到结果，不需要声明任何工具。

| model | 等价能力端点 | query 来源 | 调用上游模型 |
| --- | --- | --- | --- |
| `wb-search` | `/v1/tools/search` | 最后一条 user 消息的文本 | 否 |
| `wb-fetch` | `/v1/tools/fetch` | 消息里第一个 http(s) URL | 否 |
| `wb-search-fetch` | `/v1/tools/search_fetch` | 最后一条 user 消息的文本 | 否 |

- 入口：`/v1/chat/completions` 与 `/v1/messages` 都支持，`stream` 走一次性 SSE 收尾
- 响应正文：与 `wb_webtools._format_results()` 同款排版，模型可直接引用
- 结构化附带：Chat 放 `choices[0].message.web_results`，Messages 放
  `content[0].web_results`；两条路径都给同一份 IR
- 模型列表：`/v1/models` 追加三条固定条目，`owned_by` 为 `wb-webgateway`
- 记账：计入 web 通道，不进 model 用量表
- 关闭：`web_gateway.enabled=false` 时三条从 `/v1/models` 消失，请求返回 404

## 3. 架构与文件清单

```
wb_webgateway/
  __init__.py         对外导出 Registry 与 handle(path, body, ctx) -> (status, dict)
  ir.py               SearchResult / FetchResult / WebResponse 数据类
  registry.py         provider 注册、选择、兜底、健康与 web 冷却、账号依赖注入
  api.py              三端点的键校验、IR 序列化；依赖 registry 与 ir，不碰 HTTP
  models.py           伪模型：识别 `wb-search` / `wb-fetch` / `wb-search-fetch`，
                      抽 query/URL，编成 chat 与 messages 两种响应形状
  settings.py         配置 schema、默认值、规范化（all→None、[]→None）
  providers/
    base.py           Provider 抽象：name / kind / search() / fetch() / health()
    workbuddy.py      /agenttool/v1/search + /agenttool/v1/webfetch
    local_engines.py  包装新增的 wb_webtools.web_search_results()
    local_http.py     包装新增的 wb_webtools.web_fetch_result()
  protocols/
    anthropic.py      web_search_20250305 + web_fetch_20250910（本期）
    openai_responses.py hosted web_search + web_search_call 事件（本期，改造现有循环）
tests/
  _test_webgateway_api.py
  _test_webgateway_registry.py
  _test_webgateway_workbuddy.py
  _test_webgateway_anthropic.py
  _test_webgateway_responses.py
  _test_dashboard_webgateway.js
```

后续阶段（不在本期）：

```
  protocols/openai_chat.py     web_search_options（P4）
  protocols/gemini.py          googleSearch → groundingMetadata（P7）
  providers/searxng.py  searxncrawl.py  crawl4ai.py  scrapling.py
```

改动点：

| 文件 | 改动 | 规模 |
| --- | --- | --- |
| `wb_webtools.py` | 新增 `web_search_results()` / `web_fetch_result()` 两个结构化出口，复用现有引擎链与缓存；**不改现有函数** | 约 +40 行 |
| `wb_proxy.py` | 路由分发 + 在 `_ensure_pools()` 之后构造 `Registry(pool=POOL, ...)` 并调用 `wb_webgateway.install(registry)` + Anthropic 多轮循环 + Responses 循环改调 registry | 约 +260 行 |
| `wb_settings.py` | `web_gateway` 对象读写与规范化 | 约 +60 行 |
| `dashboard.html` | 新配置区块 + 三语 | 见 §7 |

依赖方向：`protocols/* → registry → providers/* → wb_webtools`；`api.py → registry, ir`。
**`registry` 的账号池由 `wb_proxy.main()` 注入**（`POOL` 是模块级全局，子包反向 import 会成循环且拿到 `None`）。
**URL 守卫在 registry 的 fetch 分发点统一执行**，三条 HTTP 端点与两条协议注入路径都经过它，provider 自身不再各自校验。

## 4. Provider 设计

### 4.1 统一接口

```python
class Provider:
    name: str
    kind: frozenset          # {"search"} / {"fetch"} / {"search","fetch"}
    def search(self, req) -> list[SearchResult]
    def fetch(self, req) -> FetchResult
    def health(self) -> dict
```

默认配置的构造入口：

```python
registry = wb_webgateway.Registry(pool=POOL, accounts_dir=ACCOUNTS_DIR, log=log)
wb_webgateway.install(registry)          # 只注册一次，之后 handle() 直接取用
status, body = wb_webgateway.handle(path, body, ctx)
# ctx = {"key_entry": ..., "key_id": ..., "realm": ..., "log": ...}
```

`handle()` 只返回 `(status, dict)` 纯数据；HTTP 序列化由 `wb_proxy` 现有的 `_json` / `_error` 完成，子包不重复实现。
`ctx` 携带身份，是 key 白名单与按 key 限流的唯一来源；registry 抛错时由 `handle()` 统一转成 4xx/5xx。

### 4.2 workbuddy

```python
account = pool.pick(realm=cfg.realm, allowed=allowed_for_key, exclude=web_cooled_uids)
resp = wb_accounts.urlopen(request, timeout=cfg.timeout_ms/1000, proxy=account.proxy)
```

| 配置键 | 默认 | 说明 |
| --- | --- | --- |
| `enabled` | false | 总开关 |
| `realm` | `null` | `null` / `cn` / `intl`；settings 层把 `"all"` 规范化为 `null` |
| `allowed_uids` | `null` | `null` 不限制；`[]` 同样规范化为 `null` |
| `max_results` | 5 | 上游默认；请求体给了就以请求体为准 |
| `freshness` | `""` | 空或 `dN`/`mN`/`yN` |
| `timeout_ms` | 20000 | 与官方客户端一致 |
| `priority` | 10 | 数值小的先试 |
| `fallback` | true | 失败时是否让下一个 provider 接手 |

`allowed` 的实际取值 = `key_allowed_accounts(key_entry)` 与 `cfg.allowed_uids` 的**交集**（任一侧为 `None` 表示不限制）。这保证面板上限定「只用 A 号」的 key 无法通过 web 通道消耗 B 号额度。

失败处理（与聊天路径共用判据）：

| 上游返回 | 动作 |
| --- | --- |
| 401 | 换号重试，预算由 `WB_WEB_ROTATE_MAX`（默认 1）控制；预算用尽仍 401 则把该账号加入 `web_cooldowns[uid]` |
| 403 | 先走 `restriction_is_account_level()` 分流：封号则 `note_restricted` 并**停止换号**，同时触发 `restriction_hold_remaining(realm)` 检查；普通 403 按 `WB_WEB_ROTATE_MAX` 换号 |
| 429 | 写入 registry 的 `web_cooldowns[uid]`，按预算换号；**不写 `Account.cooldown_until`** |
| 5xx / 超时 | 按预算换号，仍失败交给 fallback provider |
| `code != 0` | 不重试，`msg` 与 request_id 进 `errors` |

换号预算用独立常量 `WB_WEB_ROTATE_MAX`（默认 1）。`RESTRICTED_403_ROTATE_MAX`（默认 0）继续只服务 11140 封号分支，两者不共用，避免调大 web 预算时放宽封号红线。

### 4.3 本地 provider

| provider | kind | 实现 | 生效字段 |
| --- | --- | --- | --- |
| `local-engines` | search | 新增 `wb_webtools.web_search_results(query, n)`，复用 `_chain_for()` 与 `_run_engine()` | `query` `max_results`、`allowed_domains`（过滤结果域名） |
| `local-http` | fetch | 新增 `wb_webtools.web_fetch_result(url, start, max_chars)`，复用 `fetch()` 的正文与分页 | `url` `max_chars` `start_index` |

`language` / `time_range` / `categories` / `engines` / `safesearch` / `pageno` 对本地 provider 不生效，响应里以 `ignored_fields` 列出，不静默丢弃。
`time_range`（`day/week/month/year`）映射到 workbuddy 的 `freshness`（`d1/d7/m1/y1`），映射表写进 `providers/workbuddy.py`。

### 4.4 选择算法

1. 取 `kind` 覆盖本次请求且 `enabled` 的 provider，与请求体 `providers` 取交集
2. 按 `priority` 升序尝试
3. 成功即返回；失败累积到 `errors`，继续下一个
4. 全部失败返回 502；部分成功时 `degraded=true`
5. 健康状态在 registry 内维护：连续失败 3 次进 `web_cooldowns` 60 s；冷却只影响 web 通道

## 5. 协议适配（Layer 2）

### 5.1 开关语义（修正 v1 的重叠问题）

真值表：

| `local_web_tools` | `web_gateway.enabled` | `inject_in_protocols` | 行为 |
| --- | --- | --- | --- |
| true | 任意 | 任意 | Responses 注入（**现状行为保留**） |
| false | true | true | 按 `inject_protocols` 列表注入 |
| false | true | false | 只有 `/v1/tools/*` 可用 |
| false | false | 任意 | 全部关闭，等同 v2.1.3 |

迁移规则：首次启动若检测到 `local_web_tools=true` 且 `web_gateway` 不存在，自动写入 `inject_in_protocols=true` 与 `inject_protocols:["responses"]`，保证升级不静默降级。

### 5.2 Anthropic Messages（本期）

现状：`/v1/messages` 是单发透传，`stream_messages_events` 只有 thinking / text / tool_use 三态，没有任何 `server_tool_use` 发射路径。本期需要新建：

| 项 | 内容 |
| --- | --- |
| 识别 | `tools[].type in ("web_search_20250305", "web_fetch_20250910")` |
| 注入时机 | **必须在 `anthropic_tools_to_chat()` 之前**。该转换对无 `input_schema` 的 server tool 会丢弃并把名字记进 `skipped`，随后注入一条「These Anthropic server tools are not available here: … Do not call them.」的系统提示；若在转换后替换，模型会同时看到函数定义与「不要调用」的指令，工具永不触发 |
| 替换 | 用 `install_tool_defs()` 的手法换成同名 client tool，并从 `skipped` 集合里剔除该名字 |
| 实现方式 | **双模式**，由 `web_search_mode` 选：`multi`（默认，原生）让模型自己决定搜什么、搜几次，网关执行 tool call 后回注再请求一轮；`pre` 走预检索（网关先搜一次、结果注入 system） |
| 流式发射 | `multi` 模式：`  _send_anthropic_result_sse()` 把完整消息编成 SSE 一次发完；`pre` 模式：`stream_messages_events(..., web_meta=...)` 在 message_start 后补两个块 |
| 结果字段 | `page_age`（来自引擎发布时间）、`encrypted_content`（gzip+base64，前缀 `wbenc:`）、`server_tool_use.input` 含 `allowed_domains` / `blocked_domains` |
| 参数映射 | `max_uses`（轮次上限）、`allowed_domains`、`blocked_domains`、`user_location`（后两者只透传给支持的 provider） |
| 计数 | 在 `_anthropic_usage()` 输出里增 `server_tool_use: {"web_search_requests": N}`，N 取本轮实际执行的 server tool 次数。Messages API 没有 `searchCount` 字段，客户端据此显示「Did N searches」 |

### 5.3 OpenAI Responses（本期，改造现有）

现状已有完整循环与 `web_search_call` 事件序列（`wb_proxy.py:21377` 附近），改动是把执行体从 `wb_webtools.execute()` 换成 `registry`。

| 项 | 内容 |
| --- | --- |
| 识别 | `tools[].type in ("web_search", "web_search_preview", "web_search_preview_2025_03_11")`；第三项是现有 `wb_webtools.py` 已支持的变体，改造时不能遗漏 |
| 参数 | `filters.allowed_domains`、`search_context_size`、`user_location` |
| 回注 | 保持现有 `response.web_search_call.in_progress → .searching → .completed` 序列 |

### 5.4 Chat Completions（已移除）

协议层没有服务端搜索标准，`web_search_options` 属于搜索模型、`tools[].type=="web_search"` 属于 hosted tool，两者都无法对齐出一份"原生"形状。2026-11-16 按用户要求删除这条注入路径：`protocols/openai_chat.py` 与前端勾选框一并移除。

`/v1/chat/completions` 上的三个伪模型（`wb-search` / `wb-fetch` / `wb-search-fetch`）不受影响，它们本来就不依赖协议注入。

### 5.5 Gemini generateContent（P7，已实现）

新建 `/v1beta/models/{model}:generateContent` 与 `:streamGenerateContent` 两条路由。流程：`googleSearch` 声明 → 本地预检索 → 结果写入 `systemInstruction` → 移除 `googleSearch` → 请求转成 Chat 后交给本进程 `/v1/chat/completions` → 响应转回 `GenerateContentResponse`。

`groundingMetadata` 含 `webSearchQueries` / `groundingChunks` / `groundingSupports`，每个 `web` 对象带 `domain` 与 `publishedDate`（有值时）。`segment` 的位置来自回答里的 `[n]` 标记——注入 system 时已要求模型按下标引用，解析出真实 `startIndex` / `endIndex` / `text`；模型没标时回退整段。

`retrievalMetadata.source` 标记为 `wb-webgateway`，不冒充官方 grounding。流式走一次性 SSE 收尾。

## 6. 账号、鉴权、限流与记账

- 账号池共用，不新建池；web 请求不绑会话亲和，用 `pick()` 而非 `pick_for_session()`
- 分权：`allowed = key 白名单 ∩ 配置白名单`，任一侧 `None` 表示不限制；key 身份取自 `handle(path, body, ctx)` 的 `ctx["key_entry"]`
- 冷却：registry 维护独立 `web_cooldowns[uid]`，不写 `Account` 状态，保证搜索失败不影响聊天
- 记账：web 通道**不进 model 用量表**（没有 model 维度），只记请求计数与账号归因，走 `log(..., tag="webtools")`；面板另设一个「网页工具调用」计数
- 限流：`WB_WEB_QPS`（默认 5），按 key 计
- 隐私：query 只进运行日志并截断 120 字符，不进 usage 表

## 7. 前端改造

`dashboard.html` 新增「网络工具」区块：

1. **能力开关**：`web_gateway.enabled`、三端点各自开关、`inject_in_protocols` + `inject_protocols` 多选（本期 `responses`、`anthropic`）
2. **搜索后端**：`workbuddy` / `local-engines` 多选，各自展开配置
3. **抓取后端**：`workbuddy` / `local-http` 多选
4. **账号池策略**（仅 workbuddy 选中时显示）：realm 三档（全池 / 仅国内 / 仅国际）+ 账号多选列表（realm、剩余配额、web 冷却状态）

每个 provider 行右侧显示健康状态。现有「本地网络工具」区块保留，文案改为「协议内注入（Responses）」并指向新键。

三语文案同步 `zh-cn` / `zh-tw` / `en`。保存接口 `POST /settings/save` 扩展 `web_gateway` 对象，旧键 `local_web_tools` 保持可读可写。

## 8. 配置项与默认值

```json
{
  "web_gateway": {
    "enabled": false,
    "inject_in_protocols": false,
    "inject_protocols": [],
    "endpoints": { "search": true, "fetch": true, "search_fetch": true },
    "qps": 5,
    "web_search_mode": "multi",
    "search_providers": [
      { "name": "workbuddy", "enabled": false, "priority": 10, "fallback": true,
        "realm": null, "allowed_uids": null, "max_results": 5, "freshness": "", "timeout_ms": 20000 },
      { "name": "local-engines", "enabled": true, "priority": 20, "fallback": true }
    ],
    "fetch_providers": [
      { "name": "workbuddy", "enabled": false, "priority": 10, "fallback": true },
      { "name": "local-http", "enabled": true, "priority": 20, "fallback": true }
    ]
  }
}
```

非法值保存时拒绝并回原因。`realm` 接受 `all|cn|intl` 但写入时规范化为 `null|cn|intl`。

## 9. 测试计划

| 用例 | 断言 |
| --- | --- |
| `/v1/tools/search` 正常 | 200，`results[].url` 非空，`providers` 非空 |
| `query` 少于 2 字符 | 400 `invalid_query` |
| `/v1/tools/search` 带 `url` | 400 `unexpected_field` |
| `/v1/tools/fetch` 带 `query` | 400 `unexpected_field` |
| `allowed_domains` + `blocked_domains` 同现 | 400 `conflicting_domains` |
| 未知字段 | 400，列出未知键名 |
| provider 全失败 | 502，`errors` 非空 |
| 主失败 + 兜底成功 | 200，`degraded=true` |
| `search_fetch` | `results[].content` 非空，`fetch_top_n` 生效 |
| `max_chars` 截断 | `content_truncated=true`，`next_start_index` 非空 |
| workbuddy 401 | 按 `WB_WEB_ROTATE_MAX` 换号后成功；预算用尽仍 401 时进 `web_cooldowns` |
| workbuddy 403 封号 | 不换号，账号 `note_restricted` |
| workbuddy 429 | `web_cooldowns` 生效，`Account.cooldown_until` 不变 |
| key 白名单 | 限定 A 号的 key 在 B 号为唯一可用时返回 503 |
| `realm=null` | 全池轮换，不出现空快照 503 |
| Anthropic 注入 | 请求体 server tool 被替换，回注含 `tool_use_id` |
| Anthropic 系统提示 | 转换后的请求里「not available here … Do not call them」不含 `web_search` / `web_fetch` |
| Anthropic 用量 | `usage.server_tool_use.web_search_requests` 等于本轮实际执行次数 |
| Responses 注入 | 事件序列完整；`web_search`、`web_search_preview`、`web_search_preview_2025_03_11` 三种声明都识别 |
| SSRF 守卫 | 协议注入路径请求 `http://169.254.169.254/` 时被 registry 拒绝，不落到任何 provider |
| `local_web_tools=true` 迁移 | 升级后注入仍生效 |
| 全部关闭 | 与 v2.1.3 行为一致 |
| `model=wb-search` | `/v1/chat/completions` 返回 assistant 文本 + `message.web_results`，不触发上游调用 |
| `model=wb-fetch` | 从消息里抽到 URL 并抓取；无 URL 时 400 `missing_url` |
| `model=wb-search-fetch` | 每条结果带 `content` |
| `/v1/models` | 开启时含三条伪模型，关闭时不含 |

前端用例 `tests/_test_dashboard_webgateway.js`：区块渲染、三语键齐全、保存载荷形状。

## 10. 分期交付与回滚

| 阶段 | 内容 | 独立验证 |
| --- | --- | --- |
| **P1** ✅ | `wb_webgateway/` 骨架、IR、registry、`api.py`、`models.py` 伪模型、`local-engines` / `local-http`、`wb_webtools` 两个结构化出口、`/v1/models` 追加三条 | 已实现：三端点与三个伪模型均实测通过 |
| **P2** ✅ | `workbuddy` provider（search + fetch）、账号池与 key 白名单、web 冷却 | 已实现：请求体与账号筛选有单测；端到端需在带账号的部署上验证 |
| **P3** ✅ | Anthropic Messages 预检索 + 原生内容块（含流式） | 已实现：注入链路实测（本机无账号，响应停在选号） |
| **P4** ✅ | Responses 循环改调 registry（`local_web_tools=true` 兼容） | 已实现：`_execute_web_tool()` 优先 registry，未接管时回退原执行器 |
| **P5** ✅ | 前端区块 + 三语 + 迁移逻辑 | 已实现开关、注入协议、后端多选、realm 三档、账号多选列表（全选 = 不限，部分选中写入 `allowed_uids`，全不选阻止保存） |
| **P6** ✗ | Chat Completions 注入 | 已按用户要求删除（协议无可对齐的原生形状） |
| P7（后续） | Gemini 适配 | 需先有 Gemini 上游通道 |
| P8（后续） | searxng / searxncrawl / crawl4ai / scrapling | 按需启用 |

每期单独提交，可单独 revert。全局关闭开关：`web_gateway.enabled=false` 且 `local_web_tools` 保持原值。

## 11. 风险

| 风险 | 影响 | 对策 |
| --- | --- | --- |
| `/agenttool/*` 为未公开接口 | 上游改版即失效 | 集中在 `providers/workbuddy.py`；失败降级到 `local-engines` |
| 搜索走账号可能触发风控 | 账号被限 | 与聊天共用出口、UA、TLS 指纹；403 走封号分流；搜索 QPS 限制 |
| SSRF | 任一 fetch provider 都可能被指向内网，且协议注入路径的 URL 来自模型输出 | 校验下沉到 **registry 的 fetch 分发点**（唯一必经之处，HTTP 端点与协议注入都经过）；`wb_webtools._guard_url` 改名公开为 `guard_url()`，`api.py` 不直接引用 |
| 协议注入改动大 | P3/P4 排期风险 | 分期、每期独立可验证；Anthropic 循环参照 Responses 现成实现 |
| 前端三处开关 | 配置歧义 | 真值表 + 迁移继承 |

## 12. 验收标准

1. 只开 `local-engines` 时 `/v1/tools/search` 返回结果，耗时 < 30 s
2. 开 `workbuddy` 后返回 `providers:["workbuddy"]` 的结果，`realm=null` 时国内与国际版账号都参与轮换
3. 限定 A 号的 key 无法使用 B 号搜索（503 且文案说明原因）
4. Claude Code 指向本网关时 `WebSearch` 可见、返回结果、UI 计数正确
5. `local_web_tools=true` 的既有部署升级后 Responses 注入不失效
6. 全部关闭时行为与 v2.1.3 一致
7. `tests/_test_webgateway_*.py` 与 `tests/_test_dashboard_webgateway.js` 全部通过
