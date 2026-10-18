# 监控与日志数据后端契约（`backend/observability/**`）

## 0. 一句话

把「这台机器现在怎么样」与「每个人用了多少」拆成**两套来源、两套口径**的只读数据，
用一份稳定的 JSON DTO 交给 UI；**缺测一律 `null`（UNKNOWN），绝不以 `0` 代替**，
**整机/整卡读数与按人计量结构上不可互换**。

本模块**不创建 HTTP listener**（除 `dev/observability_preview.py` 的环回预览）、
**不接生产 Gateway / Prometheus / NVML**、**不读取任何真实日志**。

---

## 1. 模块布局

| 文件 | 职责 |
|---|---|
| `errors.py` | 契约错误与稳定 `contract_code`（不绑定 HTTP 状态码） |
| `dto.py` | 指标目录、读数（数值/枚举/文本）、新鲜度、来源声明 |
| `identity.py` | `RequesterPrincipal`、受信 key 目录、scope 与授权解析 |
| `windows.py` | 时间窗合法性与跨度上界、UTC 桶边界 |
| `percentiles.py` | 分位数（只从原始样本算） |
| `sources.py` | `MockMetricProvider` 与**固定表达式**的 Prometheus 只读 provider |
| `host.py` | 受限主机采集（生产不启用）、GPU 句柄注入、驱动状态 |
| `gateway.py` | Gateway 元数据行的解析、归属、按人累加 |
| `engines.py` | 引擎登记、可见性、健康判定 |
| `trust.py` | **受信来源白名单**与分类（谁可以被当作「已验证」） |
| `host_projection.py` | `/api/infra/summary` 投影的服务端严格校验 |
| `host_refresh.py` | 主机面板刷新契约的**可执行参考实现**（issue #38） |
| `service.py` | 四种 DTO 的组装与自检 |

外部配套：

* `dev/observability_preview.py` —— **环回、合成数据**的 WSGI 预览（Mock E2E 用）；
* `tests/observability/**` —— 隔离测试（无网络、无 GPU、无真实 `/proc` 依赖）。

---

## 2. 输入契约：Gateway 元数据行（`gateway.py`）

一行 JSON，字段沿用现有 Gateway `usage.jsonl` 形状：

```
ts, key_id, request_id?, label, method, path, model, endpoint?, status, outcome,
stream, usage{input_tokens,output_tokens}, bytes_out, queue_ms, ttft_ms,
upstream_ms, total_ms
```

### 2.1 三类字段，三种处理

| 类别 | 例子 | 处理 |
|---|---|---|
| **被接受的字段** | 上表所列 | 逐字段校验；不合法则该行计入 `malformed_lines` |
| **正文类字段** | `prompt` `completion` `messages` `content` `text` `input_text` `output_text` | **拒绝整行**并计入 `content_field_rejected`（**递归查找**，含嵌套对象与数组） |
| **自报身份字段** | `uid` `principal` `subject` `project` `x_project` `x_principal` `X-Forwarded-User` … | **读取后丢弃**（静默）。既不采信也不报错 |
| **其他未知字段** | `fixture` `retry_count` … | 静默忽略 |

自报身份字段为什么**静默**：报错等于告诉上游「你写这个字段我们看见了」，
而它的正确处理方式就是根本不存在。字段清单见
`gateway.SELF_REPORTED_IDENTITY_FIELDS`（存**归一化**形式：小写、连字符换下划线，
见 `normalise_field_name`），并会被记入 `IngestResult.self_reported_fields_seen`
用于诊断展示（**只**用于展示，不参与任何判定）。

归一化是必需的：HTTP 头习惯写 `X-Forwarded-User`，行内键习惯写 `x_forwarded_user`，
二者是同一个东西。少了这一步，连字符写法**仍然**不会被采信（未知字段一律忽略），
但「我们见过这个字段」这句诊断会变成假话 —— 而那正是运维用来确认「上游又加了什么头」
的依据。

正文类字段的查找是**递归**的：只查顶层的话，`{"usage": {"prompt": "…"}}` 这种写法会被
判成干净的行，于是「正文类字段 → 拒绝整行」这条不变量与它的计数被绕过，而那个计数
正是运维用来确认「上游有没有往元数据里塞正文」的依据。嵌套深度超过
`MAX_CONTENT_SCAN_DEPTH` 时**按拒绝处理** —— 一个深到看不清形状的结构不该被当作
「我检查过了，是干净的」。

### 2.2 归属只来自受信目录

`key_id -> uid/project/workload` **只能**来自 `identity.KeyDirectory`（运维经审批写入）。
归属判定的入参**只有** `key_id`；`label`、HTTP header、请求体字段一律不参与。
查不到就是「无法归属」，计入 `unknown_key`。

### 2.3 `request_id`

可选的不透明调用句柄，最长 128 字符，含控制字符即判坏行。
上界的存在是为了挡住「把正文塞进 `request_id` 当元数据」。

### 2.4 滥用上限（`IngestLimits`）

`max_bytes = 32 MiB`、`max_lines = 200_000`、`max_line_bytes = 64 KiB`。超限抛
`InputLimitExceeded`。

---

## 3. 身份与授权契约（`identity.py`）

### 3.1 scope

| scope | 授予什么 |
|---|---|
| `observability:self:read` | **任何**监控视图的最低门槛：本人的用量与本人被授权端点的状态 |
| `observability:host:read` | 整机/整卡**物理**读数与原生 UI 入口（不含他人用量） |
| `observability:admin:read` | 全体主体用量摘要、全部引擎、全部不可归属诊断 |

三者**互相独立**：`host` 与 `admin` 都不蕴含对方。

### 3.2 `roles` 不产生任何权限

没有任何一条规则是「因为 `role == admin` 所以放行」。`roles` 只写进 `scope` 供审计。
不变量：把 `roles` 换成 `("admin","owner","superuser")` 而 scope 不变，**每一次行为判定
都必须相同**。回归：`test_usage_privacy.RoleDoesNotUnlockTest`。

### 3.3 拒绝规则（全部抛 `AccessDenied`）

1. 会话 uid 为空；
2. 缺 `observability:self:read`；
3. `requested_uid` 与会话 uid 不一致且不持 `admin` scope；
4. `requested_endpoint` 不在可见端点集合内；
5. 管理视图（`usage.admin`）而缺 `admin` scope。

### 3.4 拒绝是**统一**的（防预言机）

第 3、4 条的拒绝**不区分**成因：

* 「那是别人的 uid」与「那个 uid 不存在」是同一个拒绝；
* 「那个端点已登记但你无权」与「那个端点根本不存在」是同一个拒绝。

一旦区分，端点就变成可枚举他人 uid / 他人私有端点存在性的**预言机**。
回归：`test_identity_authz.ScopeResolutionTest.test_denial_does_not_distinguish_*`。

### 3.5 可见端点 = 授权白名单 ∩ 登记表

「被授权」是审批记录，「存在」是现实，取交集。于是已下架的端点不会靠白名单残留
继续出现在响应里。管理 scope 的可见集合是登记表全量 —— 这就是「Admin 可见 all」。

### 3.6 判定顺序即安全边界

`_attribute()` 的顺序是：**归属 -> 窗口 -> 吊销 -> 端点 -> 计数**。

前两步之前**不碰任何计数器**。因此：

> 对同一个 viewer，只要可见样本相同，整份 DTO 逐字节相同 —— 无论输入里额外塞进
> 多少其他主体的行、未注册 key、损坏 JSON、正文行、自报身份行、窗口外行。

回归：`test_usage_privacy.ByteInvarianceTest`（6 个断言，含 `timeline` 视图）。

---

## 4. 指标来源契约（`sources.py`）

### 4.1 Web 层提供不了 PromQL

`PrometheusProvider.read(metric_id)` 的**唯一**入参是 `metric_id`；表达式来自代码里写死的
`FIXED_EXPRESSIONS` 登记表。结构上没有「把用户字符串拼进查询」的路径 —— 不是靠校验，
是靠没有那个参数。回归：`test_sources.NoArbitraryPromqlTest`（对方法签名做断言）。

### 4.2 Web 层提供不了 URL

端点地址是**构造期**配置，且构造时即校验（`validate_base_url`）：

* scheme 必须是 `http` / `https`；
* 不得带 userinfo / query / fragment（凭据不进配置串）；
* host 必须是**环回**，或显式出现在 `allowed_hosts` 允许列表里。

**环回判定不做任何名字解析。** 判据只有两条：host 是 IP **字面量**且落在环回段，
或者它是唯一被承认的字面名字 `localhost`（`LOOPBACK_LITERAL_HOSTS`）。

早期实现调用 `getaddrinfo` 并按解析结果判断，于是：

* 校验路径上多了一次可阻塞的网络查询；
* 更要紧的是，任何一个 A 记录指向 `127.0.0.1` 的**外部**域名
  （`127.0.0.1.nip.io`、`localtest.me` 之类）都会被判成环回 —— 这是一类真实的
  SSRF 绕过。真正的判据应当是「**这就是本机**」，而不是「它说自己指向本机」。

一个普通域名要放行，只能靠运维**写死**在 `allowed_hosts` 里，不能由外部输入决定。

不满足即抛 `ConfigRejected` —— 一个指向任意外网地址的 provider **根本构造不出来**。
`PrometheusProvider` 默认 `enabled=False`：本任务不接生产 Prometheus，未显式打开时
一律如实报 `source_unavailable`。

### 4.3 归约必须显式声明

一条表达式必须声明 `reducer ∈ {single, sum, max, min, avg}`。

* `single` 却拿到多条序列 -> `ambiguous_series`（**不**取第一条，**不**求和）；
* 声明了归约 -> 应用它，并把质量标为 `partial`（因为已不是一个单一观测）。

**构成比值的一对读数必须同基。** `device.gpu.memory.used_bytes / total_bytes` 是一个
占用率，分子分母必须来自**同一个总体**（两者都是 `sum`，「本机全部卡的合计」）。
早先 `total` 用 `max`、`used` 用 `sum`：多卡机器上两卡各满一半就能算出「已用 > 总量」。
这是硬要求，不是风格偏好 —— 回归见 `test_sources` 的「同基」与「不超过 100%」两条。

### 4.4 不跟随重定向

`UrllibTransport` 用不跟随重定向的 opener。构造期校验管不住跳转：一个被指向的
（或已被攻陷的）Prometheus 只要回一个 `302 Location: http://169.254.169.254/…`，
默认的 `urllib` 就会去取那个地址 —— 环回策略在**请求期**被绕过。跳转一律当失败。

### 4.5 非有限值不是测量结果

Prometheus 用 `NaN` / `+Inf` 表示「这一格没有数据」。它们既不被算成 `0`，也不被跳过，
而是报 `no_sample`。

### 4.6 状态量先归约再解释

`service.up` 用 `min` 归约（多采集目标里任何一个掉线即为 `down`）。实现上**先**归约
**再**把 `0/1` 映射成 `down/up`；只可能是 `0/1`，别的值一律 `ambiguous_series`。
（这是被 `test_sources.PrometheusReadTest.test_service_up_uses_min_so_any_target_down_wins`
抓出来的一处真实缺陷：早期实现对状态量直接取 `values[0]`，把 `min` 悄悄丢掉了。）

---

## 5. 主机采集契约（`host.py`）

### 5.1 生产不启用

`ProcHostCollector(enabled=False)` 是**默认**。此时每一项都是
`quality=unknown, reason=not_collected`，**不**是 `0`。本任务不接线采集器，
所以「未启用」是交付形态而非降级形态。

### 5.2 模块自身不碰 GPU

NVML 句柄必须由外部**注入**（`NvmlGpuReader(handle=...)`），`host.py` **不** import
`pynvml`。于是任何一次测试运行都不可能意外加载驱动或占用设备。

### 5.3 解析是纯函数

`parse_meminfo` / `parse_proc_stat` / `parse_loadavg` / `parse_nvidia_version` /
`cpu_utilization_percent` / `driver_status` 全部是纯函数，用合成文本充分测试。

两条具体纪律：

* `meminfo` 的**单位逐行读**，认不出的单位整行丢弃（宁可少一项，也不要一个乘以错系数的数字）；
* **一个计数器快照算不出 CPU 使用率** —— 首次采集如实报 `no_sample`。

### 5.4 GPU 驱动状态

`/proc/driver/nvidia/version` 给出**内核模块**版本；用户态版本由 NVML 提供。

取值域 `DRIVER_STATES = ("ok", "version_mismatch", "no_device", "permission_denied")`：

| 情况 | 状态 |
|---|---|
| 用户态**因权限**读不到（`userspace_error=permission_denied`） | `permission_denied` |
| 两边都读到、相同 | `ok` |
| 两边都读到、不同 | `version_mismatch` |
| 只有一边读到 | `ok`（**不**升格成 mismatch ——「没读到」不是「不一致」） |
| 两边都没有 | `no_device` |
| 两边都读不到**且读取器没报成因** | `quality=unknown`（**不**断言 `no_device`） |

`permission_denied` 需要读取器**显式**带成因过来（`NvmlGpuReader.error_kind()`，
成因按类别归类、不留原始异常文本）：NVML 打不开时只返回 `None`，与「这台机器没有
NVML」在返回值上无法区分。少了这一步，「只有一边读到 -> ok」会把一次权限问题静静地
判成「驱动健康」—— 而这正是本模块最不该有的失败方式。

同一份 `/proc/driver/nvidia/version` 里的编译器版本也会作为
`node.gpu.driver.compiler_version` 输出：解析出来就必须用上，否则那只是一次无意义的
parse。

`version_mismatch` 刻意单列：此时 NVML 往往**仍能打开设备并报出读数**，但读数不可信。
这不是 `down`，而是「看得到、不能信」。

`no_device` 与 `permission_denied` 也刻意分开：「这台机器没有卡」与「有卡但我读不到」
对运维是两件完全不同的事，合并会让一次权限问题看起来像一次硬件缺失。

### 5.5 GPU 数量不能用 `len(devices)`

没有读取能力时 `devices()` 返回空元组，`len` 是 `0` —— 那会把「我没有 NVML，说不上来」
渲染成「这台机器没有 GPU」。实现先问读取器「你数得出来吗」，数不出来即报
`unsupported`。

---

## 6. 口径分离：整机/整卡 vs 按人（核心）

**共享推理实例上，GPU 显存是整卡共享的，按用户切分只能是编造。**
所以按人计量**只**走 Gateway 已验证 key 的请求/token 口径。

### 6.1 两个作用域族，`metric_id` 前缀即边界

| 前缀 | 作用域 | 来源 | 单位 |
|---|---|---|---|
| `node.*` / `device.*` | `node` / `device` | 主机采集或 Prometheus | bytes / percent / celsius / count |
| `service.*` | `service` | 只读健康探测 | state |
| `engine.*` | `engine` | 引擎探测 | state / count / ms |
| `principal.*` | `principal` | Gateway 已验证 key 元数据 | **count / tokens / ms**（无 bytes） |

### 6.2 结构性保证（不是约定）

* 目录里**没有** `principal.*` 且单位为 `bytes` / `percent` / `celsius` 的指标；
* `MetricReading.from_metric()` 的 `scope` / `unit` / `physical` **一律取自目录**，
  调用方无从覆写；
* 读数**没有** `uid` 字段；`_validate_reading()` 明文拒绝「物理作用域的读数带 `uid`」；
* `device` 块（`DeviceBlock`）**没有** `uid` 字段；
* 用法视图里每一处计量都带 `billing_basis: "verified-key"`，且校验器**钉死**这个取值。

于是「把整卡利用率当成用户账单」不是「没人这么写」，而是**无法被表达**。

回归：`test_metric_semantics.py`（19 项）、`test_canaries.PerUserGpuMemoryCanaryTest`。

### 6.3 金丝雀为什么这么写

`canary_per_user_gpu_bytes()` 展示这条算术的致命性质：**别人多跑一点，「我的显存」就变少**。
它是**算术上自洽**的，所以看起来「公平」—— 这正是它危险的地方。

---

## 7. DTO 字段

四种 `kind`：`usage.self` / `usage.admin` / `engine.status` / `timeline`。

### 7.1 共用信封（每种都必须齐备）

```jsonc
{
  "kind": "usage.self",
  "source": "synthetic-fixture",     // 谁提供的数据
  "synthetic": true,                  // 是否合成；true 时 display_hints 必须明说
  "schema_version": "observability/1",
  "scope": { /* 见 7.2 */ },
  "window": { "from": "...Z", "to": "...Z", "bucket": "1h", "timezone": "UTC", "hours": 4 },
  "page": { "limit": 50, "offset": 0, "total": 3, "returned": 3, "has_more": false },
  "display_hints": ["..."],
  "notes": ["..."]
}
```

### 7.2 `scope` 块

```jsonc
{
  "uid": "u-1001",            // 会话身份
  "viewing_uid": "u-1001",    // 本次查看对象（管理主体可指定他人）
  "requested_uid": null,      // 请求里是否**显式**指定了（null = 没指定）
  "is_self_view": true,
  "roles": ["user"],          // 仅审计，不产生权限
  "scopes": ["observability:self:read"],
  "endpoints": ["model-shared-a"],
  "admin": false,
  "host_visible": false,
  "requested_endpoint": null,
  "cross_endpoint": false
}
```

### 7.3 读数信封（每个指标一份）

```jsonc
{
  "metric_id": "device.gpu.utilization_percent",
  "scope": "device",           // 取自目录
  "unit": "percent",           // 取自目录
  "physical": true,            // 派生自 scope
  "value": 42.0,               // 或 "state" / "text"，三者恰有其一
  "quality": "measured",       // measured | partial | unknown
  "display_quality": "stale",  // 上面三者 + stale
  "source": "nvml",
  "observed_at": "2026-10-15T11:59:00Z",
  "reason": null,              // 仅 unknown 时非 null，且必须是稳定原因码
  "freshness": {
    "age_seconds": 60.0,
    "stale": true,
    "clock_skew": false,
    "threshold_seconds": 60
  }
}
```

取值类型由 `metric_id` 在目录中的 `unit` **唯一决定**：`state` -> 枚举，`text` -> 文本，
其余 -> 数值。provider 无从选择，因此无法把状态量伪装成数值量绕过校验。

### 7.4 `null` 与 `0` 的分界

| 情形 | 表达 |
|---|---|
| 确实测得零 | 整数 `0` |
| 没测到（来源不可达 / 无样本 / 未采集 / 宿主不支持） | `value: null` + `reason` |
| 过期 | `quality` 保持 `measured`，`display_quality: "stale"` |
| 时钟偏斜（来源时间在未来） | `display_quality: "unknown"`（一个未来时间戳是数据质量问题，不是「很新」） |

`freshness.stale` **三态**：`true` / `false` / `null`（从未观测 —— `null` 而**不是** `false`，
因为 `false` 会断言「它是新的」）。`threshold_seconds: null` 表示该指标没有过期概念
（例如驱动版本号这种结构性事实）。

### 7.5 原因码（`reason`，稳定字符串）

`no_sample` / `source_unavailable` / `not_collected` / `not_authorized` /
`unsupported` / `clock_skew` / `ambiguous_series`

### 7.6 `unknown` 计数器族

| 族 | 字段 | 普通主体 | 管理主体 |
|---|---|---|---|
| **可归属**（本人可见范围内真数过） | `out_of_window` `revoked_key` `filtered_out` `out_of_scope_endpoint` `endpoint_missing` `outcome_status_mismatch` `latency_missing` `ttft_missing` | 整数（可以是 `0`） | 整数 |
| **不可归属**（结构上无法定位到任何主体） | `unknown_key` `malformed_lines` `content_field_rejected` | **`null`** | 整数 |

普通主体拿到的 `null` 是「无从测得」的唯一合法表达。写 `0` 等于宣称「我数过，是零」，
而前后两次 DTO 的差值就能据此读出他人活动量或源文件损坏情况。

### 7.7 `usage.admin` 专有块

```jsonc
{
  "billing_basis": "verified-key",
  "counters": { "calls": 6, "success": 5, "failed": 0, "cancelled": 0, "throttled_429": 1 },
  "principals": [ { "uid": "...", "counters": {...}, "latency": {...}, "queue": {...}, "tokens": {...} } ],
  "groups":     [ { "id": "model-shared-a", "display_name": "...", "counters": {...} } ],
  "engines":    [ /* engine.status 的条目 */ ],
  "host":       { "node": {...}, "driver": {...}, "devices": [ { "index": 0, "scope": "device", "readings": {...} } ] },
  "services":   { /* 只读健康探测 */ },
  "external_ui": { /* 见 §9 */ }
}
```

`principals` 只列出窗口内**有可归属调用**的主体。目录里存在但本窗口无调用的主体
**不出现**，也不以 `0` 出现 —— 「没有用量」与「没有数据」在数据上无法区分。

管理摘要若**显式**指定 `requested_uid`，则用量收窄到该主体（`usage.admin?requested_uid=…`）；
不指定则为全体摘要。二者由 `scope.requested_uid` 区分。

### 7.8 `timeline` 专有块

```jsonc
{
  "series": [ { "start": "...Z", "counters": {...}, "latency": {...}, "queue": {...}, "tokens": {...} } ],
  "coverage": {
    "unit": "1h", "expected_count": 4, "observed_count": 3, "missing_count": 1,
    "basis": "expected-buckets-within-window"
  }
}
```

**缺失的桶不补 0，也不插值** —— 它们根本不出现，只让 `quality.buckets` 变成 `partial`。

### 7.9 质量

* `quality.calls`：`unknown` 当且仅当窗口内**一条可见样本都没有**。
  一份空日志与「没有接线」在数据上无法区分，所以「0 次请求」不是一个结论。
  这与「确实数了、结果是零」是两件事（后者用整数 `0` 表达）。
* `quality.buckets`：`observed / expected` 全覆盖 -> `measured`；部分 -> `partial`；零 -> `unknown`。
* `quality.engines`：全部无结论 -> `unknown`；部分 -> `partial`；全部有结论 -> `measured`。
* `quality.host`：**由 `host` 里的读数推导**，不是「有没有传 `host_report`」。
  采集器未启用时 `host` 对象在、但每条都是 `not_collected` —— 此时整体必须是
  `unknown`，否则一屏 UNKNOWN 会被画成可信读数。`validate_admin_overview_dto`
  把这条钉死：手工改成 `measured` 会被拒。
* `quality.calls` 的规则**只有一处实现**（`UsageAccumulator.quality()`，
  `service._calls_quality` 委派过去）。契约规则有两个实现就迟早会漂移。

---

## 8. 时间窗与分页

### 8.1 窗口有界（不可协商）

时间窗是 Web 层唯一能放大的输入。每个桶粒度有**跨度上界**：

| 桶 | 最大跨度 | 上界（小时） | 最多点数 |
|---|---|---|---|
| `5m` | 24 小时 | 24 | 288 |
| `1h` | 7 天 | 168 | 168 |
| `1d` | 32 天 | 768 | 32 |

外加全局硬上限 `MAX_POINTS = 512`。超出即抛 `InvalidWindow`。

分页上限 `MAX_LIMIT = 200`。窗口上界与分页上限是同一类闸门：都让**返回体积**有界。

时间戳必须带**显式 UTC 偏移**（naive 一律拒绝 —— 无从判定属于哪个窗口）。
半开区间 `[from, to)`：恰好落在 `to` 的行属于下一个窗口。

桶起点一律在 **UTC** 下取整：DST 跨越时本地日历会给出 23/25 小时的「天」，
让「应有的桶数」取决于时区，覆盖率就不再可比。

### 8.2 分页

`limit ∈ [1, 200]`（默认 50）、`offset >= 0`。切片不变式：
`returned == min(limit, max(0, total - offset))`，`has_more == offset + returned < total`。
`offset` **可以**超过 `total` —— 那是一次合法的「翻到末尾之后」，结果是空页。

---

## 9. 原生 UI 的授权入口（Grafana / Prometheus / EvalScope）

**不直接开放任意 iframe。** `external_ui` 块给出的是**同源代理路径**：

```jsonc
{
  "policy": "same-origin-proxy-with-session-auth",
  "iframe_allowed": false,
  "iframe_reason": "跨源 iframe 无法由本服务施加授权，且会把上游会话/Cookie 暴露在平台页面的源下。",
  "entries": [
    { "id": "grafana",    "path": "/observability/ui/grafana/",    "requires": "observability:host:read",  "visible": true, "external_url": null },
    { "id": "prometheus", "path": "/observability/ui/prometheus/", "requires": "observability:host:read",  "visible": true, "external_url": null },
    { "id": "evalscope",  "path": "/observability/ui/evalscope/",  "requires": "observability:admin:read", "visible": true, "external_url": null }
  ]
}
```

三条硬规则（校验器强制执行）：

1. `iframe_allowed` **必须**是 `false`；
2. `external_url` **恒为** `null` —— 平台不下发任意外部地址，否则「谁能看哪块看板」
   就退化成一条可分享的裸链接；
3. 不可见的条目**不得**携带 `path`（连路径都不泄漏）。

Grafana 自身的匿名/组织权限仍由 Grafana 侧配置；本模块只负责**入口授权**。

---

## 10. 失败模式

本模块**不绑定** HTTP 状态码，只给出稳定的 `contract_code`；由 BFF 自行映射。
`dev/observability_preview.py` 的映射可作为参考：

| 异常 | `contract_code` | 建议状态 |
|---|---|---|
| `AccessDenied` | `observability.access_denied` | 403 |
| `InvalidWindow` | `observability.invalid_window` | 400 |
| `InvalidBucket` | `observability.invalid_bucket` | 400 |
| `InputLimitExceeded` | `observability.input_limit_exceeded` | 400 |
| `UnknownMetric` | `observability.unknown_metric` | 400 |
| `InvalidRequest` | `observability.invalid_request` | 400 |
| `SourceUnavailable` | `observability.source_unavailable` | 502（**不**折叠成 0） |
| `ConfigRejected` | `observability.config_rejected` | 500（构造期） |
| `ContractError` | `observability.contract_error` | 500 |

`UnknownMetric` **刻意**与 `AccessDenied` 分开：「本模块不提供这个指标」与「你没权限看」
是不同的失败，混成一条会让日志失去分辨力，也会给一个枚举他人指标的侧信道。

---

## 11. 安全不变量与回归证据

| # | 不变量 | 回归 |
|---|---|---|
| 1 | 授权范围外的行**既不计数也不展示**；可见样本相同时 DTO 逐字节相同 | `test_usage_privacy.ByteInvarianceTest` |
| 2 | 不可归属的原始诊断对普通主体为 `null`，绝不 `0` | `NullVersusZeroTest` |
| 3 | `role` 不产生任何权限 | `RoleDoesNotUnlockTest` |
| 4 | 看不到的端点/引擎/私有模型**不存在**于响应里（不是 `0`） | `NoForeignNamesAnywhereTest`、`test_engines.VisibilityTest` |
| 5 | 过滤器不构成存在性预言机（统一拒绝） | `FilterOracleTest`、`test_identity_authz` |
| 6 | 归属只来自受信目录；自报 label/header/body 一律忽略 | `test_gateway_ingest.SelfReportedIdentityIsIgnoredTest` |
| 7 | Web 层提供不了 PromQL，也提供不了 URL | `test_sources.NoArbitraryPromqlTest` / `BaseUrlPolicyTest` |
| 8 | 时间窗有界、桶数有上限 | `test_windows`、`test_service_contract.WindowBoundaryInDtoTest` |
| 9 | 整机/整卡读数与按人计量结构上不可互换 | `test_metric_semantics`（19 项） |
| 10 | 缺测一律 `null`；`stale` 与 `clock_skew` 如实显示 | `test_readings_and_freshness` |
| 11 | 探测无结论是 `unknown`，**不**是 `down` | `test_engines.StatusFromProbeTest` |
| 12 | 不开放任意 iframe，不下发外部 URL | `test_service_contract.ExternalUiTest` |
| 13 | 合成数据必须在 `display_hints` 里自报 | `test_service_contract.EnvelopeTest` |

---

## 12. 金丝雀（证明负测有牙齿）

`tests/observability/test_canaries.py` 显式写出 5 种「错误但看似合理」的实现，
并断言它们**确实违反**上面声明的不变量：

1. **按 `label` 分组** —— 泄漏他人 label（`eval-suite` / `misc`）；
2. **相信 `x_principal` / `X-Forwarded-User`** —— 把他人的行算进我的计数；
3. **把「没问到」当 `down`** —— 一次采集故障看起来像一次线上事故；
4. **对 p95 求平均** —— 悄悄低估尾部延迟（`984.75` vs 真实 `1850.0`）；
5. **把整卡显存按调用占比切给个人** —— 别人多跑一点，「我的显存」就变少。

外加两处：把不可归属计数写成 `0` 会被校验器拒绝；越过窗口上界会产出远超 `MAX_POINTS`
的点数。

**如果哪天这些金丝雀不再违反不变量，说明不变量本身失效了 —— 此时不得声称 READY。**

---

## 13. 仍未接线的部分

| 项 | 现状 |
|---|---|
| Prometheus | provider 已实现（固定表达式、环回端点、注入 transport），但默认 `enabled=False`；生产启用需另行批准 |
| 主机投影产生方 | `HostProjectionReader`（§16.6）已实现并测试，但默认 `enabled=False`；未启用时 `read()` 回 `None` ⇒ BFF 503；生产启用需另行批准 |
| 主机采集 | `ProcHostCollector` 已实现并测试，但默认 `enabled=False` |
| NVML | `NvmlGpuReader` 已实现（句柄注入），生产需注入 `pynvml` 句柄 |
| 引擎探测 | 默认 `UnavailableEngineProbe` —— 全部引擎如实报 `unknown` |
| 服务健康 | 只读到 `service.up` 的表达式；未接采集目标 |
| BFF 接线 | 本模块只产出 DTO；`RequesterPrincipal` 与 `KeyDirectory` 需由 BFF 从会话与受信配置构造 |
| 同源代理 | `external_ui` 只**声明**策略；反向代理路由需在部署层实现 |

---

## 14. 如何运行

```bash
# 仓库根目录。全部为 CPU / 离线测试，不使用 GPU。
CUDA_VISIBLE_DEVICES='' python -m unittest discover -s tests/observability -t .

# 单文件
CUDA_VISIBLE_DEVICES='' python -m unittest tests.observability.test_usage_privacy

# 主机投影的生产读取器（Lane #74）
CUDA_VISIBLE_DEVICES='' python -m unittest tests.observability.test_host_source_reader

# 环回预览（合成数据；给 UI 做 Mock E2E）
python -m dev.observability_preview --as admin
curl -s http://127.0.0.1:8712/observability/usage/admin | python -m json.tool
```

预览的 `--as` 只接受 `DEMO_PRINCIPALS` 里的名字；`--bind` 只接受环回地址。
身份在**启动期**固定，URL 里给不出身份（`?uid=` / `?role=` / `?scope=` 一律 403）。

---

## 15. 已知缺口（**故意不做**，需另行批准）

### 15.1 没有「按项目」口径

本模块的按人计量以 `uid` 为轴（`KeyDirectory` 里记录了 `project`，但响应不按项目聚合）。
现有 `backend/atlas/**` 提供的是「被授权项目汇总」，两者是**不同口径**，尚未统一。
同一份数据、两种切法，各自需要一次明确的授权决定 —— 合并会重蹈
`atlas:usage:unattributed` 与 `atlas:usage:global-diagnostics` 当初分开的理由。

### 15.2 服务健康只有可达性

`service.up` 只回答「探测目标是否可达」。没有 scrape 时长、队列深度、Prometheus 自身
的 TSDB 指标 —— 那些需要先确定采集目标，属于接线工作。

### 15.3 引擎探测不含副本级细节

`engine.replicas` 报的是「探测通的副本数」，没有逐副本状态、重启次数、OOM 计数。

### 15.4 没有告警、没有历史留存

本模块是**只读视图**：不写时序库、不做阈值判定、不发通知。
`observed_at` 是数据自身的时间戳，不保证服务端做过任何留存。

### 15.5 时间线不含主机读数

`timeline` 目前只覆盖按人用量。整机读数的时序需要先接 Prometheus（§13），
且必须走**独立的** provider 路径 —— 与按人计量混在一张序列图上正是 §6 要防的事。

### 15.6 `principal.errors` 的口径

定义为 `calls - success`（含被限流的 429 与主动取消）。这与「HTTP 5xx 计数」不同；
若 UI 需要区分，应分别展示 `throttled_429` 与 `failed`（两者都在 `counters` 里）。

---

## 16. 主机 LIVE 面板：受信来源、投影校验与刷新契约

本节覆盖 `trust.py`、`host_projection.py`、`host_refresh.py` 三个模块，以及
`tests/observability/fixtures/host_summary/**` 这份**给 UI Lane 的交接 fixture**。

背景：`/api/infra/summary` 由 `web/tools/verified-infra.mjs`（Node）产生，由
`web/js/infra/host-status.js`（浏览器）消费。此前「这份数字可不可信」的判断只存在于
一处 Node 代码和**渲染函数里的一行 `if`**：一个被改写、由旧版本工具生成或伪造的响应，
只要能过那一行 `if`，就会被画成 LIVE。

### 16.1 受信来源白名单（`trust.py`）

`VERIFIED_METRIC_SOURCES = {gateway-usage-export, nvml-verified, prometheus-readonly}`
此前在仓库里有**三份手抄副本**（`backend/portal/modules.py`、
`backend/bff/observability_adapter.py`、前端 `web/js/features/contract.js`），而数据的
**产地** `backend/observability` 一份都没有。`trust.py` 把它放回产地：

| 函数 | 语义 |
|---|---|
| `classify_source(name)` | → `verified` / `synthetic` / `unverified`；**不认识的一律 unverified** |
| `is_verified_source(name)` | 是否在白名单里（不看 synthetic 标志） |
| `may_present_as_live(source, synthetic=…)` | **唯一**的 LIVE 判据：`synthetic is False` **且** 来源在白名单里 |

三条要求：

1. **默认拒绝。** 白名单不是黑名单：新增可信来源必须有人显式改这份常量。
2. **合成 ≠ 来路不明。** `mock` / `synthetic-fixture` 有独立分类（`synthetic`），
   用于把「诚实的假数据」与「不知从哪来的数据」分开显示；两者都不是 LIVE。
3. **名字逐字匹配。** `PrometheusProvider.source` 曾经自称 `"prometheus"` —— 它不在
   任何白名单里，于是**按契约接好线的读数会被消费方全部静默拒绝**。现已改为
   `"prometheus-readonly"`；`tests/observability/test_source_trust.py` 对四处副本做
   奇偶校验，并单独钉住这一条。

`Provenance` 新增两个只读属性：`source_class` 与 `live_eligible`（后者即
`may_present_as_live` 的封装）。**构造校验未变严** —— 合成/未知名仍可被构造出来，
否则就无法如实描述它们。

### 16.2 投影校验（`host_projection.py`）

`validate_host_summary(payload, *, now, max_sample_age_s=90, future_tolerance_s=15)`
把一份 `/api/infra/summary` 响应判成 `HostSummaryVerdict`：

* **信封级**（任一条不过，**全部**指标降级 —— 不是只降出错的那个字段）：
  * `synthetic` 必须是 JSON `false`（`0` / `"false"` / 缺省都不算）；
  * `source` 必须逐字等于 `prometheus-readonly`；
  * `readonly` 必须为 `true`；`version` 必须为 `1`（`true` 不算，虽然 `True == 1`）；
  * 四个信封字段**缺任何一个** → `bad-shape`：一个不是投影的响应与一份「自称合成」的
    响应，排查方向完全不同，不能混成一句话；
  * `observed_at` 必须带时区，且不得超出容差地落在未来。
* **指标级**，`state == "known"` 时必须同时满足：
  * `value` 是有限数（`null`、`NaN`、`±Inf`、布尔、数字字符串全部不算）；
  * `unit` 与该键的定义一致（`gpu_temperature_celsius` 报 `ratio` 是数据错位）；
  * `bool` 单位的值只可能是 `0`/`1`，`0.4` 既不是 up 也不是 down；
  * `sample_at` 存在、可解析、**带时区**、不晚于「现在 + 容差」、不早于「现在 − 阈值」。

`sample_at` 是 LIVE 的**充要条件**：缺失 / naive / 未来 / 过期四种情形各有独立原因码
（`missing-sample-at` / `bad-sample-at` / `future-sample-at` / `stale-sample-at`）。
**确实测得的 `0` 是合法的 LIVE 值**，而任何没通过校验的指标 `value` 必须是 `null`。
未来时间戳是**时钟偏斜**，不是「很新」。

服务列表同理：`state: known` 时 `up` 必须是真布尔 —— 字符串 `"false"` 是 truthy 的，
把它当可达性会把「挂了」静默读成「在」。

### 16.3 刷新契约（`host_refresh.py`，issue #38）

`web/js/infra/host-status.js` 目前只在挂载时请求一次。本模块是那份修复的**可执行
参考状态机**（UI Lane 的移植目标，不是第二个实现），不 import 任何 IO，时间与请求
全部注入。七条规则：

| # | 规则 | 为什么 |
|---|---|---|
| 1 | 同时最多一个在途请求 | 慢响应期间叠第二个请求，两个响应会竞争同一块 UI 状态 |
| 2 | `hidden` 期间不发请求 | 后台不空转；不产生无界定时器 |
| 3 | 恢复可见且已越过间隔 → **立刻**刷新 | 而不是再等满一个周期 |
| 4 | **每次渲染**重算年龄，超过 `max_sample_age_s` 即清空 | 「挂起一夜」的解药；不依赖任何新响应到达 |
| 5 | 失败 / 契约不符 / **时间回拨** 一律清旧值 | 一个陈旧但看起来是数字的面板比 UNKNOWN 危险得多 |
| 6 | `seq` 单调；迟到的回包不许覆盖更新的结果 | 竞态 |
| 7 | 一次响应原子替换整份快照 | 首屏数字与底部健康读数必须来自同一批 |

失败原因是**分开的**：`not-yet-fetched`（没试过）/ `request-failed`（超时、断连、
401/403/404）/ `contract-violation` / `expired` / `clock-rollback`。混成一句话会让
运维查错方向。

### 16.4 交接到 UI Lane 的 fixture

`tests/observability/fixtures/host_summary/` 下有 10 个**自描述**用例，每个形如：

```jsonc
{ "note": "…", "now": "2026-10-16T12:00:00Z",
  "expect": { "accepted": true, "any_live": true, "live_keys": [...],
              "envelope_reason": null, "metric_reasons": [], "display_state": "live" },
  "payload": { /* /api/infra/summary 响应，或一个失败响应 */ } }
```

`tests/observability/test_host_refresh.py` 逐个断言校验器的判定与文件里的 `expect`
相同；UI Lane 的 JS 测试可以读**同一批文件**断言 `hostStatusState` / `hostStatusBody`。
一份 fixture、一个期望答案、两个消费方 —— 这就是交接的意义。覆盖：新鲜、
部分未知、**测得的 0**、缺 `sample_at`、未来 `sample_at`、`synthetic:true`、
错误来源、过期、offline、404。

**这些文件不要在 UI Lane 里改。** 把它们改成与实现一致，就不再是证据。

### 16.5 仍不做的事

* `host_projection.py` / `host_refresh.py` **不发请求、不开端口**；`/api/infra/summary`
  这条**路由**归部署层（BFF）。
* 服务端**也**能产生投影了（§16.6）：`sources.py` 的 `HostProjectionReader` 用固定
  Strata 指标采出同一份形状。此前这一侧只有校验、没有产生方，于是 BFF 的
  `HostSummaryStack(read=...)` 没有可注入的真实来源。Node 的 `verified-infra.mjs`
  仍是预览端的产生方，两者被一条**漂移回归**绑在一起。
* 浏览器端的刷新循环本身仍在 UI Lane（`web/js/infra/host-status.js`）；本模块交付的
  是它的契约、边界条件与共享 fixture。

### 16.6 服务端产生方：`sources.py` 的 `HostProjectionReader`（Lane #74）

| 关注点 | 做法 |
|---|---|
| 指标名 | 六个 `llmlab_strata_*`，写死在 `STRATA_HOST_METRICS`；与 `verified-infra.mjs` 的 `METRICS` 有**逐字**漂移回归 |
| 查询 | `read()` **无参数**（请求侧给不出 PromQL / URL）；端点构造期校验环回 / 白名单；**不跟随重定向** |
| 归约 | 一律 `max`。`sum` 在**构造期**即被拒绝 —— 求和会拼出一个没有任何一张卡有过的机器级数字 |
| 标签 | **一概不读**。`uid` / `gpu` / `model` 结构上不可能出现在产物里（不拼造归属） |
| 单位 | `bytes → mib`（`1 MiB` 的倒数）；单位表取自 `HOST_METRIC_UNITS`，产生方不另立一份 |
| `null ≠ 0` | 无样本 / 非有限 / 过期 / 未来 一律 `unknown` + `value=None`；**量到的 0** 才是 `0` |
| 过期 / 未来 | 样本年龄 `> 90s` 判 `stale`（并保留 `age_s`）；领先 `> 15s` 的样本被丢弃，不许它「刷新」一份过期聚合 |
| 服务健康 | `strata` 取自 `llmlab_strata_up`（**不取**它那个带 `api_key` 的 `/health`）；其余四个探 **HTTP 状态码** —— 连得上不等于就绪 |
| `enabled=False` | `read()` 回 `None` ⇒ BFF 503「未接线」；与「接线了但没数据」（200 逐条 UNKNOWN）**分开** |
| 注入 | `host_summary_reader(enabled=True)` 返回**零参 callable**，直接喂 `HostSummaryStack(read=...)` |

产物直接交给 `validate_host_summary` 判定 —— 产生方**不**产出一份自己校验器不接受
的东西。启用是显式的部署决定：默认 `enabled=False`。
