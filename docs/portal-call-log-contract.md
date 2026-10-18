# 「我的调用」逐条日志契约（`atlas-calls/1`）

## 0. 一句话

`GET /api/atlas/calls` 返回**你自己**的逐条调用日志：只列 `principal == 会话主体`
的 key 的行，只含元数据（时间 / 状态 / Token / 排队与耗时 / `request_id`），
**永不**包含 prompt / completion 原文，未接线真实 Gateway 时明确标注 `demo`。

---

## 1. 为什么是第二条口径

`docs/atlas-data-contract.md` §10.1 把「没有『我的调用』视图」列为**有意保留**的
P1 缺口，并写明要加它必须满足三个条件。本模块逐条满足：

| §10.1 的要求 | 本契约的做法 |
| --- | --- |
| 另批一个口径（精确 `principal == subject`，或独立的 scope） | 两者都做：独立的 `atlas:calls:self:read` + 精确 `principal` 匹配 |
| 明确它与项目汇总的关系（子集？还是切换视图？） | **切换视图**。两条口径产出**不同 DTO、不同 schema_version**，互不派生（§6） |
| 不能以「顺手过滤 principal」混进项目汇总路径 | 新模块 `backend/atlas/calls.py`，`service.py` 的行归属逻辑一行未改 |

两条口径的**具体差集**在回归里有名有姓：
`test_the_two_rules_differ_by_exactly_the_colleague_rows` 断言
「只按项目归属」与「按 principal 归属」的差集**恰好**是同一个项目里同事的 key。

### 1.1 为什么还要一个独立 scope

`atlas:usage:read` 是「被授权项目的**汇总**」。把逐条明细挂在它下面有两个问题：

* 明细比汇总更接近原始观测 —— 逐条时间戳本身就是行为指纹，不该由一份为别的目的
  授予的 scope 顺带背书；
* 两者要回答的问题不同（「项目用量」vs「我发了什么」），各自需要一次**明确**的授权决定。

因此：持 `atlas:usage:read` 不会自动获得本视图，反之亦然。两条回归分别是
`test_the_aggregate_scope_is_not_enough` 与
`test_the_self_scope_does_not_open_the_aggregate`。

---

## 2. 模块布局

| 文件 | 职责 |
| --- | --- |
| `backend/atlas/calls.py` | 契约本体：过滤、排序、分页、正文屏蔽、出口自检。**纯函数，无 IO** |
| `backend/atlas/identity.py` | 新增 `SCOPE_CALLS_SELF_READ`、`OwnCallScope`、`resolve_own_call_scope`、`KeyDirectory.owned_by` |
| `backend/atlas/ingest.py` | 新增可选字段 `request_id`（见 §3.2） |
| `dev/atlas_calls.py` | 回环预览的接线：演示目录、合成 fixture、`SelfCallsGrant`、opt-in 注册表 |
| `dev/preview_auth.py` | 路由 `GET /api/atlas/calls` 与被把关的页面 `/calls` |
| `web/js/calls/contract.js` | 浏览器侧载荷校验（出口再设一次防） |
| `web/js/calls/render-log.js` | 纯字符串渲染：表格与各面板 |
| `web/js/calls/controller.js` | 筛选、一次 fetch、页面状态机 |
| `web/calls.html` + `web/css/calls.css` | 页面与样式（**不**是生成产物，见 §7） |

---

## 3. 输入契约

### 3.1 接受的字段

与 `docs/atlas-data-contract.md` §2.1 同一份元数据行，另加一个可选字段：

`ts,key_id,label,method,path,model,status,outcome,stream,usage,bytes_out,queue_ms,ttft_ms,upstream_ms,total_ms,request_id`

### 3.2 `request_id`（新增，纯增量）

* 类型：字符串，可缺省。缺省 / 空白 → `None`；**永不编造**。
* 上界 `MAX_REQUEST_ID_LEN = 128` 字符；超长、含控制字符（含换行）、非字符串 →
  该行计入 **malformed**，与其它坏行同处理。
* 为什么要有上界：现有契约拒绝一切正文类字段，但如果 `request_id` 可以任意长，
  正文只是换了个字段名。**这条上界就是这个绕行的封堵**（回归：
  `test_an_over_long_id_is_a_bad_row`）。
* 为什么拒绝控制字符：日志是逐行 JSONL，一个带 `\n` 的 id 能让下游把一行读成两行。
* 它**不参与**授权、不参与归属，也不进任何汇总 DTO
  （回归：`test_the_aggregate_dto_gains_nothing_from_this_feature`）。

### 3.3 查询参数

`from` / `to` / `key` / `outcome` / `model` / `limit` / `before_ts` / `before_call_id`

* `from` / `to`：ISO-8601，**必须带显式时区**。缺省即服务端窗口
  （预览里是合成 fixture 的那一段）。窗口只能**收窄**，且沿用 `validate_window`
  的既有规则：半开区间 `[from, to)`、跨度上限 `MAX_WINDOW_HOURS`。
* `limit`：1–200（`MAX_LIMIT`），默认 50。
* `before_ts` / `before_call_id`：**必须成对**，是 keyset 游标（§5.4）。
* 未知参数一律忽略。`subject`、`principal`、`role`、`x_principal`、任意 `X-` 头
  **不是**本端点的参数，说什么都无效（回归：
  `test_a_forged_subject_parameter_changes_nothing`、`test_a_forged_header_changes_nothing`）。

---

## 4. 身份与授权契约

### 4.1 scope

| scope | 作用 |
| --- | --- |
| `atlas:calls:self:read` | 读取**自己**逐条调用日志的前置条件。`role == admin` **不**自动获得 |

### 4.2 两条输入，绝不混用（继承 §3.1 的规矩）

| | 来源 | 用途 |
| --- | --- | --- |
| `KeyDirectory` | 服务端经审批的配置 | **唯一**的 key 归属依据（`principal`） |
| `RequesterContext` | BFF 会话 | **唯一**的授权依据（`subject` / `scopes` / `authorized_projects`） |

请求行里的 `label`、`x_project`、`x_principal`、`X-Forwarded-User`、任意 header
**不参与**授权与归属，也**不出现**在明细里（回归：
`test_forged_identity_fields_in_the_row_have_zero_effect` 抹掉全部自报字段后
DTO 逐字节相同；`test_the_row_never_carries_the_client_supplied_label`）。

### 4.3 拒绝规则（全部抛 `AccessDenied`）

| 情形 | 结果 |
| --- | --- |
| 会话无 subject（`""` / `"   "`） | 拒绝 |
| 缺 `atlas:calls:self:read` | 拒绝 |
| `authorized_projects` 为空 | 拒绝 |
| 该主体名下**一把可见 key 都没有** | 拒绝（**不**返回空列表） |
| `requested_key` 不是本人可见 key 之一 | 拒绝 |

### 4.4 为什么「没有 key」是 403 而不是空列表

空列表会被读成「你这段时间没有调用」。真相是「你没有 key」。两者对调用方来说
正确的下一步都是「去找管理员」，所以这里合成**同一个**拒绝 —— 与汇总路径
「`authorized_projects` 为空即拒绝」同构。

### 4.5 为什么四种拒绝成因**不可区分**

未注册的 key、别人的 key、自己已吊销的 key、越出项目白名单的自己的 key ——
**同一个异常、同一句话、同一个状态码**（回归：`test_every_refusal_is_the_same_refusal`）。
一旦区分，端点就变成一个可以枚举他人 `key_id` 存在性的预言机。

### 4.6 判定顺序即安全边界（不得为了「顺手」调整）

```
1. key 不在受信目录          -> 结构上不可归属：不计数、不展示
2. binding.principal != subject -> 别人的 key：静默丢弃，连计数都没有
3. binding.project 不在授权集   -> 静默丢弃
4. 已吊销（本人）              -> 计入 revoked_key_rows，不出现在明细
5. 被 requested_key 收窄掉     -> 静默丢弃
6. 窗口外                      -> window_excluded（本人数据，可如实告知）
7. outcome / model 过滤        -> 静默丢弃
8. 计入明细
```

**第 2 步必须早于第 6 步**：否则「你有 N 行别人的调用落在窗口外」会变成一个关于
他人活动量的侧信道。第 1 步之后的任何一步都**不产生**新的计数器。

---

## 5. DTO 字段

```jsonc
{
  "source": "synthetic-preview",
  "synthetic": true,
  "schema_version": "atlas-calls/1",     // 与 atlas-usage/1 刻意不同
  "transport": {                          // §5.1
    "mode": "demo", "gateway_attached": false,
    "synthetic": true, "label": "DEMO / 合成数据"
  },
  "scope": {                              // §5.2
    "subject": "local:…", "roles": [...], "scopes": [...], "projects": [...],
    "keys": ["key-alpha", "key-orphan"], "requested_key": null,
    "revoked_keys": ["key-retired"], "cross_key": true,
    "content_consent": "absent"
  },
  "window": { "from": "…Z", "to": "…Z" }, // 生效窗口（半开）
  "filters": { "outcome": "all", "model": null, "limit": 50, "before": null },
  "coverage": {                           // §5.3
    "rows": "measured", "tokens_unknown_rows": 2, "ttft_unknown_rows": 3,
    "queue_unknown_rows": 1, "request_id_absent_rows": 1
  },
  "counts": {                             // §5.4
    "returned": 5, "available": 5, "truncated": false, "next_before": null,
    "window_excluded": 1, "revoked_key_rows": 1, "unattributed_rows": 1
  },
  "content": {                            // §6
    "included": false, "consent": "absent", "reason": "consent_absent",
    "opt_in_available": true, "note": "…"
  },
  "calls": [ /* §5.5 */ ],
  "display_hints": [ "…" ],
  "notes": [ "…" ]
}
```

### 5.1 `transport`：绝不假称 live

`mode == "live"` **要求两个条件同时成立**：`synthetic == false` **且**调用方显式
声明 `gateway_attached = true`。预览里 `gateway_attached` 写死为 `False`，
没有任何运行参数能改变它（回归：`test_no_path_can_make_the_preview_claim_live`）。
出口自检会拒绝「合成数据 + live」与「live 但未声明已接线」两种组合。

### 5.2 `scope`

`keys` 是本次可见的 key 集合；`revoked_keys` 是**本人**已吊销的 key —— 它们对本人
不是秘密，所以如实告知，只是不出现在明细里。`content_consent` 只反映**授权状态**
（§6）。

### 5.3 `coverage`：可测性，不是 `quality`

刻意与汇总 DTO 的 `quality` **用不同的名字**：两者口径不同，共用一个词会让读者
以为可以互相比较。这里的每个数字都是「本页有多少行压根没记这个字段」——
一个**测得**的计数。

**分母是「本页实际返回的行」，而且这些行已经过 principal / 项目授权过滤。** 别人的行、
已吊销 key 的行、授权范围外的行都不进入分母 —— 否则覆盖率会变成一条跨用户的计数侧信道。
回归 `tests/atlas/test_isolation_failclosed.py::NullIsNotZeroTest`。

### 5.4 分页是 keyset，不是「截断后假装完整」

排序键是 `(ts, call_id)` **全序**，倒序（最新在前）。游标是上一页最后一行的这两个
值，**不含**：`(ts, call_id) < (before_ts, before_call_id)`。
`counts.truncated` 与 `counts.next_before` 如实告知还有多少。

为什么不用时间戳当游标：同一时刻可能并排着多条记录，以 `ts` 为界会把它们一起跳过
或一起重复。回归 `test_paging_visits_every_row_exactly_once` 就在一对同 `ts` 的行上
跑翻页，断言**不漏不重**。

**`ts` 与游标必须保留亚秒。** DTO 的 `ts` 与 `next_before.ts` 都按 ISO-8601 输出，
**有小数秒就写小数秒**（`2026-10-14T00:00:02.500000Z`）；秒精度输入输出逐字节不变。
截断到秒不是格式偏好，它会同时打坏两件事：

* 排序用的是**真实**时刻、而出口自检用行里（截断后的）`ts` 重建排序键 —— 同一秒内的
  两行截断后时间相等，自检退化成按派生 `call_id` 比较，字典序为升时直接以
  `calls must be strictly ordered newest-first` 告败，**端点 500**；
* 游标会把最后一行的时刻写早，下一页从错误位置继续，把同一秒里更早的行**整段跳过**。

回归 `tests/atlas/test_cursor_edges.py`：亚秒分页边界与「逐字段相同的重复行」都必须
**每行恰好访问一次**。

### 5.5 一行调用

```jsonc
{
  "call_id": "call_…", "call_id_basis": "derived-from-metadata-fields",
  "request_id": "req-…",                 // 或 null —— 不编造
  "ts": "…Z", "key_id": "key-alpha", "project": "proj-a",
  "workload": "resume-parse",            // 取自服务端目录；无映射时 "unattributed"
  "model": "llm-a", "path": "/v1/chat/completions", "method": "POST",
  "stream": true, "status": 200, "outcome": "success",
  "tokens": { "input": 100, "output": 200, "total": 300 },
  "queue_ms": 5, "ttft_ms": 120, "upstream_ms": 800, "total_ms": 1000
}
```

* **没有 `label`**：它是客户端可自报的字段，汇总契约明确不让它参与分组
  （§3.1）。逐条视图若显示它，就等于凭空造出第二个更弱、且由调用方自己书写的
  身份来源。出口自检会拒绝携带它的行。
* `call_id` 是**派生**的稳定摘要（`sha256` 取前 16 位十六进制），同一条观测在任何
  一次构建里都得到同一个 id。它**不是**授权依据，也不冒充上游句柄 ——
  `call_id_basis` 就是这句声明。两行元数据逐字段相同时，后来者加序号后缀以保证
  页内唯一。
* `tokens`：`input` / `output` 任一为 `null`，`total` 就**必须是** `null`。
  用 0 补齐等于把「没测到」说成「测到 0」。出口自检与浏览器侧校验各查一遍。
* 缺失的延迟一律 `null`，前端渲染成 `—`。

---

## 6. 正文与 opt-in：为什么不返回正文

`content.included` **恒为 `false`**。这不是「这个用户没同意」，而是
**元数据管道里根本没有正文** —— `ingest` 在身份解析**之前**就拒绝一切正文类字段
（`prompt` / `completion` / `messages` / `text` / …）。

`content.consent` 是服务端记录在案的**授权状态**：

| 取值 | `content.reason` | 前端显示 |
| --- | --- | --- |
| `absent` | `consent_absent` | 未开启 |
| `withdrawn` | `consent_withdrawn` | 已撤回 |
| `granted` | `not_collected_by_metadata_pipeline` | 已开启，**但本管道不采集正文** |

三种状态下的 `calls` 明细**逐字节相同**（回归：
`test_granting_consent_does_not_change_the_rows_at_all`）。

**若要给用户看真实正文**，那是一条**另行设计**的通道，本契约刻意不预留入口：
它需要（1）正文另存且与元数据分离，（2）单独的 scope，（3）单独的用户 opt-in，
（4）逐次审计。把正文塞进本 DTO 的任何一个现有字段都会让上述四项一次消失。
§10 把这件事列为待办而不是已做。

---

## 7. 前端

* 页面：`/calls`（预览）→ `web/calls.html` + `web/css/calls.css` + `web/js/calls/*`。
* **为什么是独立页面**：`web/index.html` 与 `web/css/*.css` 是 `prototypes/v5/index.html`
  的**生成产物**，原型哈希被 `tests/atlas-data.test.js` 与
  `web/tools/extract-from-prototype.mjs --check` 钉死。往导航里加第八个 `.view`
  等于改原型加三处钉死的期望值 —— 那是改演示外壳，不是加调用日志。因此新页面
  纯增量：复用 `tokens.css` / `base.css` 的设计令牌与组件类，自带一个样式表，
  不动任何生成文件。代价是一个独立 URL，收益是既有页面零改动。
* **网络调用的位置**：`web/README.md` 要求网络调用只出现在 `web/js/data/`。
  本页的唯一一次 `fetch` 在 `web/js/calls/controller.js`。这是**有意偏离**：
  放进 `web/js/data/` 需要在 `.gitignore` 开第五条例外，而
  `tests/tracked-assets.test.js` 把「恰好四条前端例外」钉住了。偏离局部、有据、
  且只有一处 `fetch`。
* **可测性**：`render-log.js` 的每个函数都是 `(log) -> html 字符串`。这样决定
  读者看到什么的那段代码可以在 plain Node 下被测（`tests/calls-log.test.js`），
  而不必依赖这个 build 里不存在的 Chrome。`contract.js` 不含 DOM 与网络。
* **页面状态机**（与服务器的回答 1:1）：

  | 服务器 | 页面状态 | 文案 |
  | --- | --- | --- |
  | 200 且有行 | `ready` | 表格 |
  | 200 且无行 | `empty` | 「确实为空」 |
  | 401 | `signed_out` | 请先登录（→ `/login`） |
  | 403 | `denied` | 未获得授权（→ 找管理员） |
  | 其它 / 载荷非法 | `unknown` | 不显示任何数字 + 重试 |

* **浏览器侧再设一次防**：`validateCallLog` 拒绝 `content.included !== false`、
  拒绝「合成数据自称 live」、拒绝 `key_id` 不在 `scope.keys` 里的行、
  拒绝携带 `label` / `prompt` 等字段的行。载荷非法时**整页不渲染**，
  而不是渲染一半 —— 半张调用日志比一张明显为空的更糟。

---

## 8. 失败模式与状态码

| 情形 | HTTP | code |
| --- | --- | --- |
| 无 / 过期 / 已吊销会话 | 401 | 产品自己的 `not_authenticated` / `session_expired` |
| 账号被停用 | 403 | `account_disabled` |
| 无 self-grant | 403 | `forbidden` |
| 非本人的 / 不存在 / 已吊销的 key | 403 | `forbidden`（与上一条**同一响应**） |
| 参数非法（重复、空、非数字 `limit`、无时区时间） | 400 | `invalid_request` |
| 窗口反向 / 超跨度 | 400 | `invalid_request` |
| 输入超限 | 413 | `payload_too_large` |
| DTO 自检失败 | 500 | 不透露哪条不变量失败 |
| 非 GET | 405 | `method_not_allowed` |

---

## 9. 安全不变量与回归证据

| 不变量 | 覆盖 |
| --- | --- |
| 只列本人的 key | `test_only_own_keys_appear`、`test_only_own_keys_are_listed` |
| 同项目同事的 key 不出现 | `test_a_colleague_key_in_the_same_project_never_appears`、金丝雀 `test_canary_project_only_attribution_does_leak_a_colleague` |
| 跨用户 key 403 | `test_a_colleagues_key_is_403`、`test_the_grant_is_for_one_subject_and_everyone_else_is_403` |
| 注销 / 吊销**立即** 401 | `test_signing_out_takes_effect_immediately`、`test_a_revoked_session_is_401` |
| 四种拒绝不可区分 | `test_every_refusal_is_the_same_refusal` |
| `label` 与伪造头影响 = 0 | `test_forged_identity_fields_in_the_row_have_zero_effect`、`test_the_row_never_carries_the_client_supplied_label` |
| 未注册的 key 连计数都没有 | `test_an_unregistered_key_is_not_even_counted` |
| 正文金丝雀到不了线上 | `test_the_prompt_canary_never_reaches_the_wire`、`test_the_prompt_canary_row_is_absent` |
| `content.included` 恒为 false | `test_content_is_never_included_in_any_consent_state`、金丝雀 `test_canary_a_content_bearing_dto_is_refused` |
| opt-in 只改状态 | `test_opt_in_changes_the_status_and_nothing_else` |
| 合成数据绝不自称 live | `test_synthetic_data_is_demo_even_when_the_gateway_is_claimed_attached`、`test_no_path_can_make_the_preview_claim_live` |
| 缺失值不写 0 | `test_a_row_without_usage_has_null_tokens_not_zero`、金丝雀 `test_canary_zero_fallback_does_produce_fake_zeros` |
| `request_id` 不编造 | `test_a_missing_request_id_is_null_and_never_invented` |
| 翻页不漏不重（含同 `ts` 的并列行） | `test_paging_visits_every_row_exactly_once` |
| 两条口径互不派生 | `test_the_two_schema_versions_differ`、`test_the_self_scope_does_not_open_the_aggregate`、`test_the_aggregate_dto_gains_nothing_from_this_feature` |
| 线上载荷 == 契约构建器输出 | `test_the_body_is_exactly_the_contract_builder_output` |
| 生产路由器里没有这条路径 | `test_the_endpoint_is_absent_from_the_production_router` |
| 页面与数据同源把关 | `test_a_signed_in_admin_without_the_grant_gets_403`、`test_the_page_is_not_reachable_at_its_file_name` |
| 出口自检真的会失败 | `tests/backend/test_self_calls.py::SelfCheckTest`（14 条篡改用例） |

---

## 10. 仍未接线 / 已知缺口

1. **`KeyDirectory` 没有真实来源。** 与 §8.1 同：生产需要经审批的配置/DB 适配器
   与吊销流程。预览里是 `dev/atlas_calls.build_directory` 生成的一小撮虚构数据。
2. **`RequesterContext` 来自预览的会话解析**，不是生产 BFF。生产必须来自 SSO 会话。
3. **真实 `usage.jsonl` 的读取与投递**未实现：本模块只接受调用方给的**字符串**。
4. **分页只支持向后翻**（older）。向前翻需要一个方向参数与一次反向排序，
   当前没有需求就不做 —— 也不假装支持。
5. **没有审计事件。** 「谁在什么时候看了自己的哪一段调用」本模块不产出。
6. **正文通道未实现**（§6）。本契约刻意没有为它留入口。
7. **演示只支持一个 self-grant 主体。** 一份 fixture 只覆盖一组 key；两个主体共享
   目录会让其中一人看到别人的 key，各自生成一份又会让归属判定失效。配置期拒绝。
8. **前端只覆盖 `web/` 的新页面**，没有接入 `web/index.html` 的导航（见 §7 的
   生成产物约束）。

以上都不是「忘了做」，而是需要单独的授权/产品决定。

---

## 11. 如何运行

```bash
# 后端（纯函数，离线）
python -m unittest discover -s tests/backend -t .

# 真实 HTTP 契约（真 wsgiref + 真 SQLite + 真 Session Cookie）
python -m unittest discover -s tests/integration_auth -t .

# 前端（plain Node，不需要浏览器）
npm test

# 回环预览：自己的调用日志页面
python -m backend.auth.bootstrap init --state-dir <ABSOLUTE DIR>
python -m dev.preview_auth --local-http-dev --state-dir <ABSOLUTE DIR> \
    --calls-self-grant admin
# 打开 http://127.0.0.1:<port>/calls
```

Python 3.11+，仅标准库。fixture 全部为合成数据（文件名与行内均带 `SYNTHETIC`
标注），已在 `.gitignore` 中按仓库既有惯例显式开例外。
