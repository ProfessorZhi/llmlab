# 评测报告来源契约（EvalScope / 历史 Benchmark）

> Lane：Evaluation/Reports [#48](https://github.com/ProfessorZhi/llm-lab-portal/issues/48)。
> 基线：`integration/v6-operations-linux-auth-20261016` @ `7fcc7d4fbbbf395b87e0e7251a4916856ec1e891`。
> 安全前置：[Issue #34](https://github.com/ProfessorZhi/llm-lab-portal/issues/34)。
> 本文只描述**代码契约**，不代表任何来源已接线、任何报告已产生或任何生产部署。

## 0. 一句话

旧 Portal 的 `/api/evaluation` 返回的是一条**历史 Benchmark 镜像 run**，EvalScope 的
`/api/v1/reports` 返回的是**另一种东西**。本契约把它们做成两个**永不混淆**的来源，
并按 owner UID / 项目 ACL 决定谁能看见哪一条；看不清归属的历史产物默认隐藏。

## 1. 归属与文件

| 文件 | 职责 |
|---|---|
| `backend/evaluation/provenance.py` | 来源词表、记录、快照、文本消毒、不透明引用 |
| `backend/evaluation/report_catalog.py` | 来源快照 ⇒ 按 ACL 过滤的只读目录（新鲜度、0 vs 未知） |
| `backend/evaluation/report_readers.py` | EvalScope `/api/v1/reports` 与旧镜像的只读适配器 |
| `backend/evaluation/authz.py` | `report_visibility` / `may_read_report_reference`（报告可见性） |
| `backend/evaluation/dto.py` | `render_report_entry` / `render_report_catalog` / `render_report_sources` |
| `backend/evaluation/service.py` | `reports` / `report_detail` / `report_sources` |
| `backend/evaluation/api.py` | 三条只读路由 |
| `backend/bff/evaluation_adapter.py` | BFF 薄接缝（主体桥接，不重写来源） |
| `tests/evaluation/test_report_*.py` | 正/负向回归 |

## 2. 来源（provenance）

```
SOURCE_EVALSCOPE        = "evalscope"          —— EvalScope 产出的报告
SOURCE_LEGACY_BENCHMARK = "legacy-benchmark"   —— 旧 Portal 镜像的 Benchmark run
```

**来源由适配器盖章，负载说了不算。** `records_from_evalscope_payload` 永远产出
`evalscope`；`records_from_legacy_payload` 永远产出 `legacy-benchmark`。负载里的
`source` / `kind` / `origin` 字段**不被读取**。快照在构造时强制
`record.source == snapshot.source`——一条 legacy 记录进不了 evalscope 快照（构造期
`ReportSourceMalformed`）。

这条是 Issue #34「旧 run 被当成 EvalScope 报告」的正面防线。

## 3. 来源状态：0 vs 未知

| 状态 | 含义 | DTO 里的条数 |
|---|---|---|
| `ok` | 读到了，至少一条 | 该来源的记录数（按 ACL 过滤后才是可见数） |
| `empty` | **问过了，真的是 0** | 0 |
| `unconfigured` | 本部署没接线 | **未知**（不是 0） |
| `unreachable` | 接了但这次没读到 / 负载畸形 | **未知**（不是 0） |
| `stale` | 最后一次成功读取超出新鲜度窗口 | 陈旧记录仍然列出，但被标记 |

构造期约束：`ok` 必须至少一条记录（真的 0 要用 `empty`）；`empty` / `unconfigured` /
`unreachable` 必须零条。因此「0」与「未知」不能互相冒充。

### 3.1 全局状态是跨租户侧信道：用户面必须走主体范围视图

`ok` 依构造**至少有一条报告**，`empty` 则一条都没有。因此把全局状态直接下发给一个
普通成员，就等于告诉他"别人的报告在不在"——即使条数已经按 UID 过滤。所以：

| 视图 | 用途 | 状态 |
|---|---|---|
| `ReportCatalog.pipeline_status()` | 运维/管理视角 | 全局：`ok`/`empty`/`stale`/… |
| `ReportCatalog.scoped_source_status(requester, projects)` | **唯一可下发的** | 主体范围 |

主体范围视图的规则：

* 有**可见记录** → 状态由**这些记录自己**的时间戳决定（`ok` / `stale`），
  `observed_at` 取这些记录里最新的一个（`observed_at`，退回 `created_at`）；
* 没有可见记录、来源未接线/不可达 → 如实报 `unconfigured` / `unreachable`（管道事实，
  与任何人的数据无关）；
* 没有可见记录、来源接线且可读 → 一律 `empty` + `reason=no_visible_reports_for_this_principal`。
  **不**区分"真的没有"与"有别人的"。

由此得到一条可断言的性质：**别人新增一份报告，我的 `/reports` 与 `/report-sources`
响应逐字节不变**（含 `observed_at` / `reason` / 空态）。`/reports` 里 `sources` 用的是
主体范围状态；全局 `pipeline_status()` 只对明确授权的管理视角开放。

### 3.2 新鲜度与未来时间戳

`ok` 快照的 `observed_at` 距 `now` 超过 `report_staleness_seconds`（默认 3600）即按
`stale` 处理；`ok` 但**没有** `observed_at` 同样按 `stale` 处理（"我很好但我不知道
什么时候"在新鲜度上等于不知道）。

时间戳**落在未来**时同样按陈旧处理：允许 `FUTURE_SKEW_SECONDS = 60` 的时钟偏斜，超出
即 `stale` + `reason=report_source_timestamp_is_in_the_future`。没有这条上界，一个远在
未来的 `observed_at` 会算出负 age，让一份快照**永远**新鲜。时间戳必须带时区。

### 3.3 空态

| 情形 | `empty_state` | 含义 |
|---|---|---|
| 全部来源未接线/不可达 | `value: null` | 未知，不是 0 |
| 至少一条来源接线且可读 | `value: 0`（`label: "0 份报告"`） | **主体范围**的计数；不代表来源里没有他人的报告 |

判据只来自主体范围状态，因此空态也不随他人数据变化。

## 4. 只读适配器

```python
EvalScopeReportsReader(
    single_tenant_owner=ReportOwner | None,      # 显式的单租户隔离声明
    owners={report_id: ReportOwner} | None,      # 或逐报告 id 的审核映射
    clock=..., base_url=None, transport=None,
)  -> GET /api/v1/reports          # 路径是常量，调用方改不掉

LegacyBenchmarkReader(
    audited_owners={run_id: ReportOwner},        # **逐 run** 审核过的映射
    clock=..., base_url=None, transport=None,
)  -> GET /api/legacy/benchmark/runs
```

* **上游固定。** 适配器在构造时拿到 base URL 与一个注入的 `transport`；HTTP 请求里的
  任何字段都进不到这里——因此「让服务端替我去读一个我指定的地址」在结构上不存在。
* **本部署 `transport=None`** ⇒ 两条来源都如实报 `unconfigured`。适配器**不发网络、
  不读磁盘**（静态扫描断言本包没有 `socket` / `http.client` / `subprocess`）。
* **归属必须被逐条核实，绝不能靠自报。** 见 §4.1。
* base URL 校验：必须绝对、`http(s)`、无内嵌凭据、无 query/fragment。

### 4.1 归属：只能逐条核验，不能整份盖章

| 来源 | 归属来源 | 未命中时 |
|---|---|---|
| EvalScope | `single_tenant_owner`（**显式的**单租户隔离声明）**或** `owners`（逐报告 id） | `unowned` |
| 旧镜像 | `audited_owners`，键是 **`run_id`** | `unowned` |

两条硬规则：

* **全局/共享列表不得整份盖章。** 只有当调用方**显式**声明该来源是单租户隔离实例
  （`single_tenant_owner`）时才允许整份指派；否则必须逐条命中 `owners` 的 id。一份混合
  多个 owner 的全局列表不会被"配了一个 owner"就全部归到他名下。同时声明两者 → 拒绝。
* **旧镜像自报的 `owner_uid` 不参与判定。** 用 UID 作键只能证明"这个 UID 在可信名单上"，
  **不证明这一条 run 属于它**；一条自报 `owner_uid` 记录会把未核实的历史产物指派给一个
  被允许的主体。因此归属按**逐 `run_id` 审核过的映射**决定，`owner_uid` / `owner` /
  `owner_principal` / `source` 字段一律不读。

## 5. 可见性（`authz.report_visibility`）

按顺序：

1. `owner is None`（归属无法核实）→ **默认对所有人隐藏**，包括只有 `evaluation:admin:read`
   的主体。仅当调用者持 `evaluation:admin:manage` **且**显式传 `include_unowned=true` 时可见。
2. 全局读者（`admin:read` 或 `admin:manage`）→ 可见全部**有归属**的报告。
3. 否则：必须同时 `owner.principal == requester.principal` **且** `owner.uid == requester.uid`
   **且** requester 是 `owner.project_id` 的成员。
4. 其余 → 拒绝。

预览/下载引用额外需要 `evaluation:report:read`（全局读者豁免）——见
`authz.may_read_report_reference`。

> **BFF 面的事实**：`backend/bff/scopes.py` 的角色只发 `evaluation:admin:read`，
> **从不**发 `admin:manage`。因此经 BFF 的 `include_unowned=true` 打不开——归属无法核实的
> 历史记录要显形，正确路径是 Issue #34 的**审核过的迁移映射**（让 reader 把它变成有归属）。

找不到、或找到了但不可见 → **同一条 404**（`no such evaluation report`）：区分二者会让
引用端点变成"这份报告存在吗"的预言机。

## 6. 报告记录 DTO

```jsonc
{
  "report_ref": "evrp-<24 hex>",      // 不透明：由 (source, 上游 id, 相对引用) 摘要而来
  "source": "evalscope",              // 适配器盖章
  "source_label": "EvalScope 报告",
  "owner_state": "owned",             // 或 "unowned"
  "owner": {"principal": "...", "uid": 1002, "project_id": "..."},  // unowned 时 null
  "visibility": "owner_and_project_member",
  "synthetic": false,
  "title": "...",                     // 经 safe_text 消毒（删除 <> 与控制字符）
  "observed_at": "...Z", "created_at": "...Z",
  "stale": false, "source_state": "ok",
  "dimensions": { "<name>": {"name","state","value","unit","note"} },
  "missing_dimensions": [{"name": "...", "reason": "..."}],
  "preview":  {"authorized": true, "content_read": false, "reason": "..."},
  "download": {"authorized": true, "content_read": false, "reason": "..."}
}
```

* **引用是不透明 id，不是路径。** DTO 里不出现绝对路径（回归测试对每条端点断言响应不含
  `/home/` `/srv/` `/opt/` `/tmp/` `/etc/` `/var/`）。
* **缺测不是 0。** 缺失/未取值的维度一律 `state != "known"` 且 `value: null`；
  `missing_dimensions` 区分「来源没提」(dimension_not_reported_by_source) 与
  「提了但没取值」(dimension_reported_without_a_value)。
* **内容永不经这个面出去。** `content_read` 恒为 `false`（`reports.py` 的 `read_bytes`
  一律 `NotConfigured`）。预览/下载的结论只说明"允不允许发起这次读取"。

维度词表：`quality` / `throughput_rps` / `first_token_ms` / `cold_start_ms` /
`concurrency` / `failure_rate`。

## 7. HTTP 端点（只读）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/evaluation/report-sources` | 每条来源的管道健康 + 该调用者可见条数 |
| GET | `/api/evaluation/reports` | 可见报告列表；`?include_unowned=true\|false`（非法值 400） |
| GET | `/api/evaluation/reports/{report_ref}` | 按不透明引用取一条 |

三条都需要 `evaluation:read`（匿名 401、缺 scope 403）。响应是标准 envelope
（`schema_version` / `mode` / `source` / `synthetic` / `scope` / `notes`），
`Cache-Control: no-store`。

## 8. 本部署的诚实状态

* 两条来源都**未接线**（`transport=None`）→ `unconfigured`，**不是** 0 份报告。
* 真实评测派发仍然**关闭**（`AdmissionPolicy.closed` + 未接线 adapter + 从不签发
  `evaluation:dispatch:real`）。本次改动**没有**打开任何派发路径，也没有接 GPU。
* **NOT DEPLOYED / NO RUNTIME VERIFICATION**：本契约只在 CPU-only 单元/环回测试中验证，
  未接生产 EvalScope、未挂载任何报告根、未改 PAM/Caddy/systemd。

## 9. 测试证据（CPU-only）

```
CUDA_VISIBLE_DEVICES='' python3 -m unittest discover -s tests/evaluation -t . -v
```

| 文件 | 覆盖 |
|---|---|
| `test_report_provenance.py` | 来源词表、构造期拒绝伪造路径、来源隔离、状态自洽、维度、`safe_text`、显式 `synthetic`、时区 |
| `test_report_catalog.py` | 跨 UID 隐藏、非成员拒绝、全局读者、**无归属默认隐藏**、lookup 404 同形、0 vs 未知、陈旧、**未来时间戳**、**主体范围状态不泄露他人存在性**、缺失维度、重复引用 |
| `test_report_readers.py` | 未接线/不可达 fail-closed、**误导性来源标记**、**逐 run 审核归属**、**伪造 owner_uid 不提权**、**单租户 vs 逐报告/混合列表**、伪造路径与非法 HTML、base URL 校验、固定路径 |
| `test_report_http.py` | 匿名 401、跨 UID 404 同形、不透明引用、`include_unowned` 解析、空态 0 vs 未知、**他人新增报告后成员响应逐字节不变**、响应无路径、真实回环 socket |
| `test_report_bff_adapter.py` | BFF 主体桥接、scope 不放宽、来源不被二次改写、派发仍关闭 |

（全部 CPU-only、无 GPU、无外网、不读真实报告/提示词/用量日志。）
