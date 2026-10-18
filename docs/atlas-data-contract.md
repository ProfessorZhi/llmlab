# Atlas Gateway 数据适配与安全聚合契约

**状态：** 离线纯函数实现 + 未接线（no BFF wiring）。
**范围：** `backend/atlas/**`、`tests/backend/**`、本文。
**不在本契约内：** HTTP 监听端口、生产 Gateway、真实 `usage.jsonl`、SSO/RBAC 接线、UI。

这份文档与 `backend/atlas/dto.py` 的 `REQUIRED_TOP_LEVEL_FIELDS`、`UNKNOWN_COUNTER_FIELDS`
是同一份清单的两个副本；`validate_dto()` 在每次构建 DTO 时自检是否偏离。

---

## 0. 一句话

把 Gateway 的**元数据**行，按**服务端可核实**的身份与项目归属，压成一份
「看得见什么、看不见什么、哪些是测不到而不是 0」都可审计的聚合 DTO。

---

## 1. 模块布局

| 模块 | 职责 | 关键不变量 |
| --- | --- | --- |
| `backend/atlas/errors.py` | 失败模式 | 不绑定 HTTP 状态码，只有 `contract_code` |
| `backend/atlas/identity.py` | 受信 key 目录 + 授权解析 | 客户端自报字段永不参与授权 |
| `backend/atlas/ingest.py` | JSONL 解析与逐行校验 | 坏行拒绝并计数，正文类字段一律拒绝 |
| `backend/atlas/stats.py` | 分位数与累加器 | 分位数只从原始请求样本算 |
| `backend/atlas/service.py` | `build_atlas_dto()` 编排 + `validate_dto()`、`collect_authorized_records()` | 授权范围外的行既不计数也不展示 |
| `backend/atlas/aggregate.py` | `atlas-aggregate/1`：分组 / 模型分布 / 7×24 热度 / 趋势（见 §11） | 默认不接线 = UNKNOWN；热度图不补零 |
| `backend/atlas/dto.py` | DTO 字段清单与常量 | 字段清单的唯一真源 |
| `backend/atlas/corpus.py` | 接入 seam：受信目录文档加载 + Gateway 语料有界读取（见 §12） | 有界先于读取；失败 fail closed，不给部分结果 |

`errors.py` / `identity.py` / `ingest.py` / `stats.py` / `service.py` / `aggregate.py` /
`dto.py` 为**纯函数、无 IO**：不读文件、不开端口、不连网络。

`corpus.py` 是**唯一**例外。它按装配方给的路径读本地文件，因此单独承担两条纪律：
有界发生在读进内存**之前**，以及路径必须落在装配方指定的那棵树内（§12）。把它与
纯函数层分开记，是因为「本模块不读文件」这句话此前是对的，而接上 seam 之后**只对
一半模块**成立 —— 让一句不再成立的全局断言留在文档里，比缺一句话更误导。

---

## 2. 输入契约

### 2.1 被接受的字段

Gateway 既有 `usage.jsonl` 形状的**元数据**行：

```
ts, key_id, label, method, path, model, status, outcome, stream,
usage{input_tokens, output_tokens}, bytes_out,
queue_ms, ttft_ms, upstream_ms, total_ms
```

可选字段 `request_id`（字符串，≤ 128 字符，禁控制字符；缺省即 `None`）。
它**不参与**本文件的聚合与分组，只被「我的调用」逐条视图使用 ——
见 `docs/portal-call-log-contract.md` §3.2。

### 2.2 逐字段校验

| 字段 | 规则 | 违规后果 |
| --- | --- | --- |
| `ts` | ISO-8601 **且必须带时区偏移**；归一化到 UTC | `malformed_lines++` |
| `key_id` | 非空字符串（`strip()` 后） | `malformed_lines++` |
| `status` | `bool` 以外的 `int`，`100 ≤ s ≤ 599` | `malformed_lines++` |
| `outcome` | 严格白名单 `{success, failed, cancelled}` | `malformed_lines++` |
| `stream` | 必须是 JSON `bool`（`"true"`、`1`、`null` 都拒） | `malformed_lines++` |
| `queue_ms`/`ttft_ms`/`upstream_ms`/`total_ms` | `null` 或有限非负数；`bool` 不算数字 | `malformed_lines++` |
| `usage` | `null` 或对象；`input_tokens`/`output_tokens` 为 `null` 或非负 `int` | `malformed_lines++` |
| `bytes_out` | `null` 或非负 `int` | `malformed_lines++` |
| `label`/`method`/`path`/`model` | `null` 或字符串 | `malformed_lines++` |
| `request_id`（可选） | `null` 或字符串；`strip()` 后空即 `None`；长度 ≤ 128 且不含控制字符 | `malformed_lines++` |

**裸 `NaN` / `Infinity` 不是合法 JSON**，解析阶段就拒绝（不放行到统计）。

### 2.3 拒绝分类（互不重叠）

| 计数器 | 含义 |
| --- | --- |
| `malformed_lines` | 结构或类型不可信：坏 JSON、非对象、缺时区、越界 status、非白名单 outcome、负值/非有限耗时、类型错 |
| `content_field_rejected` | 行内出现 `prompt` / `completion` / `messages` / `content` / `text` 等**正文类**字段 |

正文类字段一律拒绝而不是忽略：一旦它们出现在输入里，就说明上游把不该来的数据送来了，
静默丢弃会掩盖问题。

**未知字段（例如伪造的 `x_project`、`X-Forwarded-User`）被静默忽略** —— 既不采信，
也不报错。它们不是合法契约的一部分，但也不构成拒绝理由。

### 2.4 滥用上限（`IngestLimits`）

| 参数 | 默认 |
| --- | --- |
| `max_bytes` | 32 MiB（整体） |
| `max_lines` | 200 000 |
| `max_line_bytes` | 64 KiB（单行超限按坏行处理，不整体失败） |

超 `max_bytes` / `max_lines` 直接抛 `InputLimitExceeded`，不返回部分结果。

---

## 3. 身份与授权契约

### 3.1 两类输入，绝不混用

| | 来源 | 用途 |
| --- | --- | --- |
| `KeyDirectory`（`key_id → principal/project/workload`） | 服务端经审批的配置 | **唯一**的项目/工作负载归属依据 |
| `RequesterContext`（`subject/roles/scopes/authorized_projects`） | BFF 会话 | **唯一**的授权依据 |

客户端可控制的 `label`、`method`、`path`、`model`、任意 header、任意 body 字段
**不参与**授权判定，也**不参与**项目/工作负载分组。

### 3.2 scope

| scope | 作用 |
| --- | --- |
| `atlas:usage:read` | 进行任何用量聚合的**前置条件** |
| `atlas:usage:unattributed` | 额外允许看到「没有可信项目归属」的**调用**计数（`unknown.unknown_key` / `unattributed.project_calls`） |
| `atlas:usage:global-diagnostics` | 额外允许看到**源文件级**诊断（`unknown.malformed_lines`、`unknown.content_field_rejected` 及 `quality.calls` 的完整性口径） |

**为什么是两个 scope 而不是一个。** 它们回答的是两个不同的问题：

- `atlas:usage:unattributed`：「哪些请求归不了属」—— 未注册的 key、已吊销的 key；
- `atlas:usage:global-diagnostics`：「上游把什么不该来的东西写进了日志」—— 坏 JSON、
  正文类字段。

把两者合并会**用一个权限给另一类数据背书**：只看未归属量的主体会顺带拿到全局损坏
计数，而「未归属请求」与「源文件损坏」并不是同一件事。因此不合并。

`roles` **只用于展示与审计，不产生任何权限**。代码中不存在「因为 role == admin 所以放行」
的分支，**全局诊断也一样**：`role == "admin"` 但没带 scope，看到的仍是 `null` —— 见 §7 回归证据。

### 3.3 拒绝规则（全部抛 `AccessDenied`）

| 情形 | 结果 |
| --- | --- |
| 会话无 subject（`""` / `"   "`） | 拒绝 |
| 缺 `atlas:usage:read` | 拒绝 |
| `authorized_projects` 为空 | 拒绝 |
| 请求的项目不在白名单内 | 拒绝 |
| 客户端传 `requested_subject` 且 ≠ 会话 subject | 拒绝，**不降级、不借用他人身份、不回退 admin** |

### 3.4 授权范围外的行

来自未授权项目的行会被**静默丢弃**：既不计数也不展示。
这是刻意的 —— 一个「你有 N 行别人项目的数据被过滤了」的计数器本身就是侧信道。

### 3.5 判定顺序即安全边界

事件遍历的顺序是契约的一部分，**不得**为了「顺手」而调整：

```
1. directory.lookup(key_id)          —— 键不在受信目录 → 不可归属（全局诊断）
2. binding.project ∈ 授权项目集？     —— 否 → 静默丢弃，什么都不记
3. 是否落在窗口内？                   —— 否 → out_of_window（可归属）
4. 是否已吊销？                       —— 是 → revoked_key（可归属）
5. 是否被 model_filter 排除？          —— 是 → filtered_out（可归属）
6. 计入 visible / by_project / by_workload / by_hour / by_day
```

关键点：**第 2 步必须早于第 3、4 步**。若把窗口或吊销判定提到项目判定之前，
`out_of_window` / `revoked_key` 就会变成「全局」计数 —— 仅凭前后两次 DTO 的数字差值，
一个只有单项目授权的 viewer 就能读出其他租户的活动量。这正是基线 commit `73f2d15`
的 P0 缺陷形态。

第 1 步先于第 3 步则意味着：**窗口外的未注册 key 计入 `unknown_key`，而不是
`out_of_window`**。因为「归不了属」比「不在窗口内」更根本 —— 与 §4.5 的分类不冲突。

---

## 4. 聚合契约

### 4.1 「成功」的判定

```
success  ⇔  outcome == "success"  AND  200 ≤ status ≤ 299
```

两者不一致时计 `outcome_status_mismatch`，并按**不成功**处理（保守）。

### 4.2 延迟样本（p50 / p95）

- 只取 `成功` **且** `total_ms` 非 `null` **且** `total_ms > 0` 的**每条原始请求**。
- `429`、`failed`、`cancelled`、缺测一律排除。
- 算法：**线性插值 type 7**（`PERCENTILE_METHOD = "linear-interpolation-type7"`）。
  空样本 → `null`；单样本 → 该样本本身。

> **整体 p95 绝不等于各组 p95 的平均。** 整体分位数由所有可见原始样本合并后重算。
> 模块不提供「对 p95 求平均」的入口。回归用 fixture 证明：
> 各组 p95 = `[800, 2820, 700]`（均值 1440），整体 p95 = **2640**。

### 4.3 TTFT

- 只取 `成功` **且** `stream == true` **且** `ttft_ms` 非 `null` **且** `> 0`。
- 非流式成功请求：**排除**，计入 `ttft.excluded_non_stream`。
- 流式成功但无 `ttft_ms`：计入 `unknown.ttft_missing`。
- 无有效样本 → `p50_ms`/`p95_ms` 为 `null`，`quality.ttft = "unknown"`。

### 4.4 Tokens

- 只统计成功请求；缺 `input_tokens` / `output_tokens` 的行计入
  `tokens.input_missing` / `output_missing`，**不按 0 计入总和**。
- 一个都没有 → 总量为 `null`（不是 0），`quality.tokens = "unknown"`。

### 4.5 分组

- 项目分组只来自 `KeyDirectory` 中该 `key_id` 的 `project`。
- 工作负载分组只来自 `KeyDirectory` 中经审批的 `workload`。
- 受信项目下**没有**工作负载映射的调用 → 归入工作负载 id `"unattributed"`。
- 没有可信项目归属的调用（未注册 key / 已吊销 key）→ 计入
  `unknown.unknown_key` / `unknown.revoked_key`，并汇总为 `unattributed.project_calls`。
- **`unattributed.project_calls` 只对持有 `atlas:usage:unattributed` 的主体可见**；
  否则为 `null` 且 `unattributed.visible = false`（`validate_dto()` 强制这一点）。
- `unattributed.workload_calls` 是**可归属**的（只由本次授权项目的行累加），
  所以它**不**随 `visible = false` 变成 `null` —— 它数的是「你自己项目里没有经审批
  工作负载映射的调用」，与别人的数据无关。

**当前视图是「被授权项目汇总」，不是「我的调用」。** `GatewayKeyBinding.principal` 被记录
下来用于审计，但**不参与**行归属过滤：同一项目下属于其他 principal 的 key 也一并计入本视图。
需要个人视图时必须另行批准一个精确主体匹配的口径（或独立的 `*.self` scope），
**不得**靠悄悄加 principal 过滤来冒充 —— 见 §10 P1。

### 4.6 桶与覆盖率

- 桶起点一律为 UTC；`1h` 输出 `YYYY-MM-DDTHH:00:00Z`，`1d` 输出 `YYYY-MM-DD`。
- **只输出真实观测到的桶**，不补齐空桶。
- `buckets.expected_count` 是窗口内应有的桶数，`observed_count` 是实际观测到的。
  两者不等 → `quality.hourly = "partial"`；`observed_count == 0` → `"unknown"`。
- `observed_count == 0` 时 `hourly`/`daily` 均为 `[]`，并给出「保持空态、不渲染 7×24 热力图」
  的 hint。**绝不**用演示值冒充生产监控。
- 桶的统计与整体统计使用同一套授权过滤 —— 不存在「总量已过滤、桶没过滤」的口子。

### 4.7 可见性先于聚合：计数器的两族

`unknown` 里的九个计数器分成**两族**，区别是「能不能把它归属到本次可见的项目」：

| 族 | 字段 | 谁由它累加 | 普通 viewer 看到 |
| --- | --- | --- | --- |
| **可归属**（scoped） | `out_of_window`、`revoked_key`、`filtered_out`、`outcome_status_mismatch`、`latency_missing`、`ttft_missing` | 已经过 §3.5 第 2 步、确认属于本次授权项目的行 | 整数；`0` 是**真的数过**的零 |
| **不可归属**（unattributable） | `unknown_key`、`malformed_lines`、`content_field_rejected` | 结构上无法定位到任何项目的行 | **`null`**（无从测得），**绝不写 0 冒充** |

不可归属的那三个字段各自的解锁条件：

| 字段 | 解锁 scope |
| --- | --- |
| `unknown_key` | `atlas:usage:unattributed` |
| `malformed_lines`、`content_field_rejected` | `atlas:usage:global-diagnostics` |

随之而来的三项配套约束：

1. **`quality.calls` 不随全局坏行变化。** 该字段的语义是「源文件是否可用」——一个
   文件级属性。没有 `atlas:usage:global-diagnostics` 时它必须是 `"unknown"`：
   报 `"measured"` 等于断言「源文件没有坏行」，也就是把一个自己无从测得的 `0` 说成结论。
   有授权时才按坏行/正文行数给出 `"measured"` / `"partial"`。
2. **`quality.malformed_lines` / `quality.content_field_rejected` 与 `unknown` 里的同名项
   同门同锁**（`validate_dto()` 强制二者相等），不存在「一处藏了、另一处漏了」的口子。
3. **`display_hints` 不暗示被隐藏的东西。** 未授权时既不说「有 N 行坏行」，也不说
   「没有坏行」—— 只说这项测不了。授权时才给出计数文案。

`validate_dto()` 会**拒绝**任何「没有对应 scope 却带着数字」的 DTO，所以这条边界不是
靠自觉，而是每次构建都自检。

---

## 5. AtlasDTO 字段

```
{
  "source": "gateway",              // 由调用方通过 Provenance 显式声明
  "synthetic": false,               // 同上；无默认值，必须显式传
  "schema_version": "atlas-usage/1",
  "scope": {
    "subject": str, "roles": [str], "scopes": [str],
    "projects": [str],              // 本次实际生效的授权项目
    "unattributed_visible": bool,   // 持 atlas:usage:unattributed
    "global_diagnostics_visible": bool,  // 持 atlas:usage:global-diagnostics
    "cross_project": bool
  },
  "window": {"from": "…Z", "to": "…Z", "bucket": "1h"|"1d", "timezone": "UTC", "hours": int},
  "quality": {
    "calls": "measured"|"partial"|"unknown",  // 无全局诊断授权时恒为 "unknown"
    "latency": "measured"|"partial"|"unknown",
    "ttft":    "measured"|"partial"|"unknown",
    "tokens":  "measured"|"partial"|"unknown",
    "hourly":  "measured"|"partial"|"unknown",
    "malformed_lines": int|null,        // 与 unknown.malformed_lines 恒等
    "content_field_rejected": int|null, // 与 unknown.content_field_rejected 恒等
    "truncated": false
  },
  "totals": {"calls": int, "success": int, "failed": int, "cancelled": int,
             "throttled_429": int, "requests": int},
  "latency": {"p50_ms": num|null, "p95_ms": num|null, "samples": int,
              "basis": "raw-request-samples"},
  "ttft":    {"p50_ms": num|null, "p95_ms": num|null, "samples": int,
              "excluded_non_stream": int},
  "tokens":  {"input_total": int|null, "output_total": int|null,
              "input_missing": int, "output_missing": int},
  "groups": [                       // 按 id 升序，只含本次授权且有可见流量的项目
    {"id": str, "calls": int, "success": int, "failed": int, "cancelled": int,
     "throttled_429": int, "requests": int,
     "latency": {...}, "ttft": {...}, "tokens": {...},
     "workloads": [ {"id": str, …同上…} ]}
  ],
  "buckets": {"unit": "1h"|"1d", "expected_count": int, "observed_count": int,
              "hourly": [{"start": "…Z", …计数…, "latency": {...}, "ttft": {...}}],
              "daily":  [{"start": "YYYY-MM-DD", …同上…}]},
  "unknown": {
    // —— 可归属（scoped）：整数，0 表示确实数过且为零 ——
    "revoked_key": int,           // 可见项目里用了已吊销 key 的行
    "out_of_window": int,         // 可见项目里结构可信但落在窗口外的行
    "filtered_out": int,          // 被 model_filter 排除（作用于已授权的行）
    "outcome_status_mismatch": int,
    "latency_missing": int,       // 成功但无有效 total_ms
    "ttft_missing": int,          // 流式成功但无有效 ttft_ms
    // —— 不可归属（unattributable）：无授权即 null，绝不是 0 ——
    "unknown_key": int|null,           // key_id 不在受信目录（需 atlas:usage:unattributed）
    "malformed_lines": int|null,       // 需 atlas:usage:global-diagnostics
    "content_field_rejected": int|null // 需 atlas:usage:global-diagnostics
  },
  "unattributed": {"visible": bool, "workload_calls": int, "project_calls": int|null},
  "display_hints": [str],          // 供 UI 直接展示的口径/降级提示
  "notes": [str]                   // 口径说明（分位数基础、label 不参与等）
}
```

### 5.1 `null` 与 `0` 的分界

| 情况 | 表达 | 理由 |
| --- | --- | --- |
| 窗口内确实没有请求 | `totals.calls = 0` | 这是**测得**的 0 |
| 没有可用延迟样本 | `latency.p50_ms = null` | 这是**无从测得**，不是 0 |
| 没有 token 数值 | `tokens.input_total = null` | 同上 |
| 无小时日志 | `buckets.hourly = []`, `quality.hourly = "unknown"` | 不伪造点阵 |
| 无权限看未归属量 | `unattributed.project_calls = null` | 隐藏，不是 0 |
| 可见项目里窗口外/已吊销的行 | `unknown.out_of_window`/`revoked_key = 0` | 这些行**数过**了，是测得的 0 |
| 无权限看全局原始诊断 | `unknown.unknown_key`/`malformed_lines`/`content_field_rejected = null` | 无从测得；写 0 就是伪造 |
| 无权限看源文件完整性 | `quality.calls = "unknown"` | 见 §4.7 |

`validate_dto()` 会**拒绝** `samples == 0` 却带着数值分位数的 DTO —— 这条不变量有回归覆盖。
它同样会**拒绝**「没有对应 scope 却带着数字」的不可归属计数，以及 `quality.calls != "unknown"`
却 `global_diagnostics_visible = false` 的 DTO。

### 5.2 `requests` 字段的口径（历史命名，有歧义）

`totals.*.requests` 与各 `groups[*].requests`、`groups[*].workloads[*].requests` 的值
**等于同一行的 `success`**，即「成功请求数」，**不是**请求总量。

| 想做 | 用哪个字段 |
| --- | --- |
| 请求总量（含失败/429/取消） | `calls` |
| 成功请求数 | `success`（或等价的 `requests`） |

字段名保留是为了不破坏既有消费方；**不要**把 `requests` 当成请求总量渲染。
`tests/backend/test_privacy_boundaries.py::RequestsFieldSemanticsTest` 钉死了这条口径。

---

## 6. 失败模式

| 异常 | `contract_code` | 触发 |
| --- | --- | --- |
| `AccessDenied` | `atlas.access_denied` | 身份/scope/项目白名单不通过 |
| `InputLimitExceeded` | `atlas.input_limit_exceeded` | 超字节/行数上限 |
| `InvalidWindow` | `atlas.invalid_window` | 缺时区、`to <= from`、超最大跨度 |
| `InvalidBucket` | `atlas.invalid_bucket` | 不支持的粒度 |
| `InvalidRequest` | `atlas.invalid_request` | 其余参数类型/取值错误；或 builder 与来源接线状态不匹配（见 §11.1） |
| `UnverifiedSource` | `atlas.unverified_source` | 声明**非合成**却不在受信来源白名单里（见 §11.1.1） |

不绑定 HTTP 状态码：本模块不是 HTTP 服务。

---

## 7. 安全不变量与回归证据

| 不变量 | 覆盖 |
| --- | --- |
| 跨项目泄漏 = 0 | `test_cross_project_leak_is_zero`：proj-a 视图里搜不到 `proj-b` / `eval-suite` / `key-gamma` / `u-2001` / `llm-c` |
| `label` 冒领影响 = 0 | `test_label_spoofing_has_zero_effect`：抹掉全部 label 后 DTO **逐字节相同** |
| 伪造 header 无效 | `test_forged_x_project_header_cannot_create_a_foreign_group` |
| 整体 p95 不可由各组 p95 推出 | `test_overall_p95_is_not_the_mean_of_group_p95`（2640 ≠ 1440） |
| TTFT 非流式被排除并计数 | `test_ttft_only_counts_successful_streaming_positive_values` |
| 429 被排除并单独计数 | `test_429_is_excluded_from_latency_and_counted_separately`、`test_429_with_duration_canary_is_caught` |
| UNKNOWN ≠ 0 | `test_empty_window_returns_unknown_not_zero`、`test_empty_window_has_no_heatmap_grid` |
| 无 admin 回退 | `test_admin_role_is_not_a_fallback` |
| 不自报身份 | `test_wrong_requested_subject_is_denied_not_downgraded` |
| **授权范围外的原始诊断计数为零泄漏** | `test_extra_foreign_and_corrupt_rows_do_not_change_the_dto_bytes`：同一 viewer，第二批额外塞进其他项目 / 未知 key / 坏 JSON / 正文行 / 窗口外请求后，**整份 DTO 逐字节相同**（含 `notes` / `display_hints` / `quality`） |
| 不可归属计数是 `null` 而非 `0` | `test_ordinary_viewer_gets_null_for_every_unattributable_counter`、`test_ordinary_viewer_gets_null_not_zero_in_the_quality_block` |
| 可归属计数是**测得**的 0 | `test_scoped_counters_are_measured_zero_not_null` |
| `quality.calls` 不随全局坏行变化 | `test_quality_calls_does_not_track_global_corruption` |
| `role=admin` 不换来全局诊断 | `test_admin_role_without_the_scope_still_sees_null` |
| 两个 scope 互不背书 | `test_unattributed_scope_alone_does_not_unlock_source_diagnostics`、`test_global_diagnostics_scope_alone_does_not_unlock_unattributed_request_counts` |
| 多项目授权 ≠ 全局日志 | `test_multi_project_viewer_is_still_denied_foreign_raw_diagnostics` |
| 可见项目自己的窗口外 / 已吊销行仍被计数 | `test_out_of_window_rows_of_the_visible_project_are_counted`、`test_revoked_key_of_the_visible_project_is_counted` |
| 视图口径是「项目汇总」而非「我的调用」 | `test_view_is_an_authorised_project_aggregate_not_a_personal_view` |

### 7.1 金丝雀（证明负测有牙齿）

`tests/backend/test_canaries.py` 里显式写出几种**错误但看似合理**的实现，并断言它们
**确实违反**上述不变量：

- `canary_workloads_from_label` —— 按 label 分组，会出现 `spoof-admin`、`../../etc/passwd`
- `canary_workloads_from_client_headers` —— 相信 `x_project`，会造出 `proj-b` 分组
- `canary_mean_of_p95` —— 得到 1440，与真实 2640 不符
- `canary_ttft_including_non_stream` —— 样本数被灌水到 11
- `canary_duration_of_every_row` —— 把 429 / 失败 / 跨项目的耗时都算进去（10 ≠ 5）
- `canary_unknown_as_zero` —— 空样本返回 `0.0`

### 7.2 变异检验（对生产代码，非金丝雀）

> **口径说明：** 下表是**原作者在基线 `73f2d15`、86 个用例的套件上**测得的。本分支
> （`fix/atlas-usage-privacy-boundaries-20261014`）**没有**重测这张表 —— 原始变异定义
> 没有记录下来，另起一套写法会得出不可比的数字，比留旧数字更误导。本分支只新增了
> §7.2.1 那条可直接复现的 P0 变异证据。

| 变异（基线套件，86 用例） | 变红用例数 |
| --- | --- |
| `resolve_authorized_scope` 增加 admin 回退 | 1 |
| `resolve_authorized_scope` 忽略 scope 检查 | 12 |
| `resolve_authorized_scope` 忽略项目白名单 | 12 |
| `resolve_authorized_scope` 采信客户端自报 subject | 1 |
| `percentile` 空样本返回 0 | 7 |

### 7.2.1 P0 收口的 failing-before 证据（本分支实测）

在基线 `73f2d15` 上**只补一个最小 API shim**（新增 `SCOPE_USAGE_GLOBAL_DIAGNOSTICS`
常量 + `scope.global_diagnostics_visible` 字段 + 导出，**不补任何过滤或门禁**），
再把**本分支完整的 125 个用例**原样放上去：

```
Ran 125 tests ... FAILED (failures=44)
```

**44 个断言失败，覆盖 27 个测试方法**，其中就包括核心验收
`test_extra_foreign_and_corrupt_rows_do_not_change_the_dto_bytes`
（基线上同一 viewer 加料后 `IDENTICAL: False`），以及全部 6 个
「`validate_dto` 必须拒绝越界 DTO」的门禁用例。补齐 §3.5 的顺序与 §4.7 的门禁后全绿。

全套 **125/125** 绿（含本分支新增的 39 个边界用例：38 个在
`tests/backend/test_privacy_boundaries.py`，1 个补进 `test_unknown_and_buckets.py`）。
**若上述变异不再让测试变红，则不得声称 READY。**

---

## 8. 仍未接线的部分（真实 BFF 待办）

本模块**不假装**以下已经就绪：

1. **`KeyDirectory` 的真实来源 —— 代码 seam 已交付，真实目录仍缺。**
   `backend/atlas/corpus.py` 现在提供经严格校验的加载器
   （`atlas-key-directory/1` 文档 → `KeyDirectory`，见 §12.1）：白名单字段、重复
   `key_id` 拒绝、`revoked` 必须是布尔、版本不符拒绝，全部 fail closed。
   但**这份文档从哪里产出**（运维审批、轮换与吊销的下发流程）尚未接线 ——
   Dell 上**仍然没有任何真实 key 目录**，当前所有目录都是合成的。
2. **`RequesterContext` 的真实来源。** 现在由调用方注入。生产必须来自 BFF 的 SSO
   会话解析（见 `docs/access-boundaries.md` §2.1 三类凭据互不共用），
   本模块不实现登录、不读 token。
3. **`usage.jsonl` 的读取与投递 —— 有界读取已交付，导出侧仍缺。**
   `FileCorpusReader`（§12.2）提供有界、受路径遏制的读取，取代了「调用方先读成
   字符串」这条唯一路径；尺寸门在 `stat` 上、先于 `open`，因此不再需要先把整个
   文件读进内存才能发现它太大。**Gateway 侧的导出/流式读取与背压策略**仍由 Dell
   现场决定（HOST_PENDING）。
4. **HTTP 传输层。** 没有 listener、没有路由、没有状态码映射、没有分页/缓存/ETag。
5. **展示时区。** DTO 一律 UTC；本地化显示是纯前端职责。
6. **工作负载定义的审批流程。** 「什么算 resume-parse」目前是测试里写死的字符串；
   真实语义需要产品侧审批的服务端映射，不能从 prompt/model 猜。
7. **审计与配额。** 谁在什么时候查了谁的数据，本模块不产出审计事件。
8. **真实语料的**份额/保留/回填**策略。** 有界读取只回答「这一份多大能读」，
   不回答「该读哪一份、读多久、缺失的那段怎么补」。

---

## 9. 如何运行

```bash
# 仓库根目录
python -m unittest discover -s tests/backend -t tests/backend
```

Python 3.11+，仅标准库，无第三方依赖，离线可跑。
fixtures 在 `tests/backend/fixtures/`，全部为合成数据（文件名与行内均标注），
已在 `.gitignore` 中按仓库既有惯例显式开例外。

---

## 10. 已知缺口（**故意不做**，需另行批准）

以下三条是独立 QA 审查后**明确保留**的缺口。它们不是「忘了修」，而是改动会扩大授权面
或改变口径，必须由主控/产品决定，不能由实现者顺手带上。

### 10.1 P1：没有「我的调用」视图，且 principal 不参与过滤

现状：`GatewayKeyBinding.principal` 只被记录，`build_atlas_dto` 的行归属**只**看
`binding.project ∈ authorized_projects`。因此**同一项目下其他 principal 的 key
会被一并计量**进你的视图。见 §4.5 与回归
`test_view_is_an_authorised_project_aggregate_not_a_personal_view`。

这是**有意的**：本模块视图的语义就是「被授权项目汇总」。要加个人视图，必须：

- 另批一个口径（例如精确 `principal == subject` 匹配，或独立的 `atlas:usage:self` scope），
- 明确它与项目汇总的关系（是子集？还是切换视图？），
- 不能以「顺手过滤一下 principal」的形式混进本项目汇总路径 —— 那会让同一 DTO 字段
  在不同调用下含义不同。

**2026-10-15 更新：这条缺口已按上述三个条件单独实现**，位于另一份契约
[`docs/portal-call-log-contract.md`](portal-call-log-contract.md)
（`atlas-calls/1`，scope `atlas:calls:self:read`，端点 `GET /api/atlas/calls`）。
它是**切换视图**而不是本 DTO 的一个子集：新的模块、新的 schema_version、新的
scope，本文件的 `build_atlas_dto` 与 `REQUIRED_TOP_LEVEL_FIELDS` **一行未改**。
回归 `test_the_aggregate_dto_gains_nothing_from_this_feature` 断言本汇总 DTO 里
不出现 `request_id` / `call_id`，`test_the_two_rules_differ_by_exactly_the_colleague_rows`
断言两条口径的差集**恰好**是同一项目下同事的 key。

### 10.2 P1：`atlas:usage:unattributed` 仍是跨租户可见性授权

`unknown.unknown_key` 数的是**整份输入**里未注册的 key。持有 `atlas:usage:unattributed`
的主体因此能看到一个跨租户的计数。这与该 scope 的既有定义一致（「没有可信项目归属的调用」），
但**上线前应做一次权限口径审查**：谁被授予、是否需要收窄到「仅可见项目范围内无法归属的调用」。

### 10.3 P2：`content_field_rejected` 无法按项目归属

带正文类字段的行在 `ingest._has_content_field()` 处就被拒绝，发生在身份解析**之前**，
所以它结构上没有项目。若将来希望「告诉某个项目它自己的行被拒绝了几次」，需要改
`ingest` 的契约（返回被拒行的 `key_id`）。**注意**：只归属「能归属的那部分」是行不通的 ——
剩下的不可归属部分仍会让整个计数随他人数据变化，反而重新打开同一个侧信道。


## 11. 聚合形状契约（`atlas-aggregate/1`，`backend/atlas/aggregate.py`）

### 11.0 为什么是**第二份**契约

`atlas-usage/1` 回答「这一段时间用了多少」，`atlas-aggregate/1` 回答「用量的形状长什么
样」：按项目/工作负载的构成、按模型的分布、按 星期几×小时 的 7×24 热度、延迟分位与
时间趋势。两者**共用同一套授权判定**（`service.collect_authorized_records()`），但
**不共用同一份 DTO**：字段多一份，消费方就多一个可以各自理解错的地方；而「哪些 0 是
数过的」这个问题，两份口径需要分别回答。新增一份契约比改一份在用的契约安全。

§3.5 的判定顺序被抽成一个函数，**只此一处实现** —— 因为聚合视图不止一种，而授权判定
的拷贝迟早会漂移，漂移的代价是一条跨租户侧信道。

### 11.1 默认不接线 = UNKNOWN

```python
AggregateSource(source="gateway-usage-export", synthetic=False)   # attached 默认 False
```

`attached` **没有默认 True**。未接线时 `build_unattached_atlas_aggregate()` 给出：

```jsonc
{ "attached": false, "state": "unknown",
  "quality": { "calls": "unknown", "latency": "unknown", "heatmap": "unknown", "models": "unknown" },
  "groups": [], "models": [], "heatmap": { "cells": [], "cells_observed": 0, "cells_expected": 0 },
  "trend": { "unit": "1h", "points": [] },
  "latency": { "p50_ms": null, "p95_ms": null, "samples": 0 } }
```

`validate_atlas_aggregate()` 把这条钉死：未接线却带着行、带着分位数、或报
`state: measured`，一律拒绝。

**授权仍然先于接线判定**：未接线的来源也必须先拒未授权调用者 —— 否则一个 200 的空壳
会把 403 藏起来。

#### 每个 builder 只对自己的接线状态负责

```
build_atlas_aggregate(...)            # 只接受 attached=True；否则 InvalidRequest
build_unattached_atlas_aggregate(...)  # 只接受 attached=False；否则 InvalidRequest
build_atlas_aggregate_dto(...)         # 按 source.attached 分派到上面两个，并自检
```

`build_atlas_aggregate()` 的产物**恒为** `attached: true, state: "measured"`。因此它
必须拒绝 `attached=False` 的来源：否则一个绕过 `build_atlas_aggregate_dto()` 分派、
直接调用它的调用方，会拿到一份自称「已接线、已测得」的 DTO —— 那正是 fail-closed 的
反面。这里刻意**不**静默降级成 UNKNOWN：静默降级会让「我调错了函数」看起来像
「这份数据是空的」。

回归：`test_atlas_aggregate.DirectBuilderFailsClosedTest`（含一个穷举公开入口、
断言任何入口对 detached 来源都不可能产出 `measured` 的用例）。

#### 11.1.1 非合成来源必须在受信白名单里

`source` 与 `synthetic` 也是调用方传进来的。若只把它们原样抄进 DTO，「这是已验证来源」
就退化成了调用方自己说的一句话。因此 `AggregateSource` 在构造期就校验：

* `synthetic is False` → `source` 必须通过
  `backend.observability.trust.is_verified_source`（即 `gateway-usage-export` /
  `nvml-verified` / `prometheus-readonly` 三者之一），否则抛 `UnverifiedSource`；
* `synthetic is True` → 名字随意（它自报合成，不冒充）。

白名单是**跨模块复用**而不是在本模块再抄一份 —— 那份集合在仓库里已经有过三份手抄
副本，手抄的副本迟早会不一样。回归：
`test_atlas_aggregate.SourceTruthIsNotCallerControlledTest`（含「白名单与 observability
是同一份」一条）。

**仍未由本层验证的**：`attached` 本身。仓库里还没有真实的 Gateway 导出口适配器
（见 §8），所以「我接上了」目前仍是一个调用方断言。本层能保证的是：没声明接线的来源
绝不产出实测数字，且声明接线的来源必须自称一个**受信**的来源名。

### 11.2 空 ≠ 未知

| 情形 | 表达 |
|---|---|
| 已接线、窗口内确实没有请求 | `state: measured`，`totals.calls = 0`，`groups = []` |
| 没有延迟样本 | `latency.p50_ms/p95_ms = null`，`samples = 0` |
| 没观测到的小时 | 该小时**不出现**在 `trend.points` 里 |
| 没观测到的 (星期几, 小时) | 该格**不出现**在 `heatmap.cells` 里 |
| 未接线 | `state: unknown` + 全空（§11.1） |

**热度图不补零**：只输出真正观测到的格。把空格子画成 0 会把「那段时间没有数据」
渲染成「那段时间没有请求」。`cells_expected` 是**窗口内应有**的
`(星期几, 小时)` 去重数（不是恒为 168）：每格至少代表一次观测到的调用，
`0` 请求的格是**非法**的（`validate_atlas_aggregate` 拒绝）。

### 11.3 模型分布（客户端可控，仅描述性）

`models` 按上游日志里的 `model` 分组。**模型名是客户端可控的**，因此：

* 它**不参与**授权与归属（伪造 `model` 不会造出别人项目的分组，也不会多算一行）；
* 标签被**有界化**：去控制字符、截断到 64 字符、空/缺失归入 `<unreported>`；
* 条目数上限 `MAX_MODELS = 50`，其余并入 `<other>` 并把 `quality.models` 标为
  `partial`（榜内集合按「调用量降序、标签升序」确定，因此可复算）。

#### 11.3.1 真实模型名不得与哨兵桶碰撞

`<other>` 与 `<unreported>` 是**本模块自己**的哨兵桶名。模型名既然客户端可控，客户端
就可以把模型真的叫做 `<other>`。若不隔离，那个**真实存在**的模型会与哨兵共用一个桶，
并让 `reported` / `bucket` 两个标志位反过来描述它 —— 一个确实上报了的模型被标成
「没上报」，一个真实存在的模型被标成「被折叠的尾巴」。

做法与 URL 里 `%` 的转义同理：

* 一个（去掉控制字符、截断后的）标签若等于哨兵值、或**以转义前缀 `\` 开头**，
  就整体加一个 `\` 前缀；
* 于是映射在**真实模型名**上是单射：真实标签永不可能等于哨兵值，哨兵也永不可能被冒充
  （连客户端写 `\<other>` 也会被再转义一次，得到 `\\<other>`）；
* `is_sentinel_model_label(label)` 是判断「这一行是不是哨兵桶」的权威入口。

`None` / 空串 / 全空白**刻意共用** `<unreported>`：它们是同一个事实 ——「上游没给模型名」。
拆成三个桶不是更精确，是把一次缺失说成三种。

`MAX_MODEL_LABEL_LEN = 64` 是**转义前**的上界；转义至多多出一个字符。截断发生在转义
**之前**，否则一个长标签被截回哨兵形状就会重新碰撞。

回归：`test_atlas_aggregate.ReservedModelNameCollisionTest`，覆盖两个字面哨兵名、
转义的单射性与二次转义、以及**「同时存在 >MAX_MODELS 个模型与一个真叫 `<other>` 的
模型」**这一组合条件。

### 11.4 热度图轴

`weekday_origin: "monday"`（与 `datetime.weekday()` 一致，0–6）、`timezone: "UTC"`、
`hour` 0–23。轴固定 UTC，不让「哪个小时」取决于时区。

### 11.5 DTO 顶层字段

```
source, synthetic, schema_version, attached, state, scope, window, quality,
totals, latency, groups, models, heatmap, trend, unknown, display_hints, notes
```

`groups[*].workloads[*]`、`totals`、`latency`、`unknown` 的形状与口径与 §5 一致
（含 `requests == success` 这条历史命名，见 §5.2）。

### 11.6 回归

`tests/backend/test_atlas_aggregate.py`（72 项）：默认不接线即 UNKNOWN、未接线仍拒
未授权、跨项目/跨 UID 在**所有新维度**上零泄漏（含「加料后 DTO 逐字节相同」）、
模型分布分桶/有界/标签净化、热度图不补零、`0 vs UNKNOWN`、趋势不补零、
校验器对篡改 DTO 的拒绝，以及两处评审收口：

* `DirectBuilderFailsClosedTest` —— 直接调用公开 builder 不能绕过接线判定（评审 A）；
* `SourceTruthIsNotCallerControlledTest` —— 非合成来源名必须来自受信白名单（评审 A）；
* `ReservedModelNameCollisionTest` —— 真实模型名不得与哨兵桶碰撞（评审 B）。

---

## 12. 接入 seam 契约（`backend/atlas/corpus.py`）

§8.1 与 §8.3 是这份契约长期自陈的两个缺口。本节记录补齐它们的那条 seam 的形状。
本模块**不** import `backend.bff`：依赖方向是 BFF 依赖 Atlas，BFF Lane 拥有
`backend/bff/atlas_adapter.py`，它把本节产出的三样东西填进自己的 `AtlasStack`。

### 12.1 受信目录文档 `atlas-key-directory/1`

```json
{
  "schema_version": "atlas-key-directory/1",
  "revision": "2026-10-17T00:00:00Z#1",
  "bindings": [
    {"key_id": "key-alpha", "principal": "u-1001", "project": "proj-a", "workload": "resume-parse"},
    {"key_id": "key-retired", "principal": "u-1001", "project": "proj-a", "revoked": true}
  ]
}
```

| 规则 | 拒绝原因码 |
| --- | --- |
| 顶层/绑定层字段**白名单**（多一个没审批过的字段即拒绝） | `source.invalid_document` |
| `schema_version` 必须精确等于 `atlas-key-directory/1` | `source.schema_mismatch` |
| `revision` 非空、有长度上界 | `source.invalid_document` |
| `key_id` / `principal` / `project` 非空且去空白后仍非空 | `source.invalid_document` |
| `workload` 缺省或非空字符串 | `source.invalid_document` |
| `revoked` 必须是**布尔**（`"true"` 这类真值字符串会让吊销的 key 复活） | `source.invalid_document` |
| `key_id` 重复 | `source.duplicate_key_id` |
| 绑定条数超 `DirectoryLimits.max_bindings` | `source.too_many_bindings` |

`revoked` 条目**保留**在目录里（轮换靠它表达），由 §3.5 的判定顺序决定它在各视图里
的去留。加载结果是 `LoadedKeyDirectory(revision, directory, key_count, revoked_count)`。

### 12.2 Gateway 语料读取

顺序**即**边界，任何一步不过就拒绝，不返回已读到的部分：

```
realpath 归属(root 之内)  ->  stat 尺寸(<= max_bytes)  ->  open 读取(<= max_bytes+1)
```

* 路径归属按 `realpath` 后的**包含关系**判断：root 内的符号链接若指向 root 外，
  与 `../` 穿越是同一条拒绝（`source.outside_root`）；
* 尺寸门在 `stat` 上，**先于** `open` —— 这正是「有界」区别于「读完再判断」之处；
  读取只取 `max_bytes + 1` 字节，多出来的那个字节是「stat 与 read 之间文件长大了」
  的证据（TOCTOU），此时同样拒绝（`source.too_large`）；
* `CorpusLimits.max_bytes` 默认与 §2.4 的 `IngestLimits.max_bytes` **同值**：两者不同
  会让 BFF 拿到的原文先被 ingest 以另一个上限拒掉，而没有人会发现。

`read()` 返回信封 `CorpusRead(available, reason, text, bytes_read)`，它**刻意区分**
两种在数据上无法区分、结论却相反的状态：

| 组合 | 含义 |
| --- | --- |
| `available=False, text=""` | 本部署**没有可用的语料**（接线问题） |
| `available=True, text=""` | 语料可用，但这份导出是**空的**（一个测量结果） |

`__call__`（喂给 BFF `gateway_text` 的那条 seam）在不可用时**抛**
`SourceUnavailable`，绝不返回空串 —— 静默的空串会把「没接线」渲染成「你最近没有调用」。

`identity` 参数被**显式丢弃**：语料文件由装配方选定，不由身份选。这一点写成代码事实
而不是靠「没写那行」。

### 12.3 装配工厂

`build_atlas_source(...) -> AtlasSource`。**目录先于语料判定**（归属依据比内容更根本），
任一条不可用即整条 `available=False`，且 `directory` 与 `gateway_text` 一并是 `None`：

**不给半套东西。** 只把目录置空、却把语料接上去，会让每一行都因为「key 不在受信目录」
而归不了属，最终渲染出一份看起来正常的空账本。

`gateway_attached` 只由装配方显式声明，工厂**不做任何推断** —— 尤其不因为「文件读得到」
就把它翻成真。读到一个文件，不等于那文件来自生产 Gateway。

### 12.4 重复观测与排序（本分支修掉的缺陷）

上游重试/双写会让同一个 `request_id`、乃至**逐字段完全相同**的行出现在语料里。这曾是
一条真实缺陷：排序用「基础摘要」而出口自检用「补过序号的 id」，补序号只会把 id 变大，
于是同一时刻的两行让自检读到一串**递增**的游标，整条请求以
`ContractError: calls must be strictly ordered newest-first` 收场 —— **端点 500**。

修复：**先给每条观测定下一个唯一 id，再拿这个 id 当排序键**。该二元组正是
`validate_self_calls_dto` 检查的那个键，因此「按它排序」与「它严格递减」不可能互相
矛盾。同一处还修掉了游标不自洽（`next_before.call_id` 是补过号的、过滤却用未补号的），
即翻页会漏行/重行。

逐条视图**不按 `request_id` 去重**：去重会把「上游交付了两次」这条事实删掉，而那是
运维需要看见的东西。`call_id`（派生值）负责 DOM key 唯一，`request_id`（上游原值）
负责对账。

### 12.5 回归

`tests/atlas/`（本 Lane 的独立测试包，与 `tests/backend/` 下既有的 Atlas 契约用例分开）：

* `test_key_directory_loader.py` —— 文档解析与有界加载，含「尺寸门先于读取」与
  符号链接逃逸；
* `test_corpus_reader.py` —— 有界读取、信封语义、`__call__` 不返回空串；
* `test_atlas_source.py` —— 工厂的可用性判决、不给半套、以及**端到端无 Dell 证明**
  （临时目录里的两份合成文件 -> 逐条视图只含本人授权行）；
* `test_duplicate_request_id.py` —— §12.4 的缺陷回归，**failing-before 证据**见该文件
  文档字符串。
