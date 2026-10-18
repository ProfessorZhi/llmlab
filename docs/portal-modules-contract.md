# Portal 模块目录契约（`portal-modules/1`）

**状态：** 离线纯函数实现 + 回环预览接线（preview-only，未接生产 BFF）。
**范围：** `backend/portal/**`、`dev/portal_modules.py`、`dev/preview_auth.py` 的
`_modules` 路由、`web/js/modules/**`、`tests/backend/test_portal_modules.py`、
`tests/integration_auth/test_preview_modules.py`、本文。
**不在本契约内：** 生产 HTTP 监听、真实 Gateway / NVML / Grafana / Prometheus /
EvalScope、SSO 会话来源、真实 Endpoint 目录、审计事件。

---

## 0. 一句话

`GET /api/atlas/modules` 返回一份**服务端判定**的模块目录：在当前这次已认证的会话
下，**每个模块能对这个主体说什么** —— 用一个状态（`available` / `unavailable` /
`disabled` / `unknown`）+ 一个稳定原因、只含有已声明可信来源的数值、
只含此刻真的能打开的**同源内部**入口。它的立场是 **fail-closed**：
前置条件没全部成立，就不声称可用；没有可信来源，就是 `null` + `unknown` 而不是 `0`；
本构建没有 NVML，所以每一个 `gpu_*` **永远是 null**。

---

## 1. 为什么是第三个契约

`docs/atlas-data-contract.md` 描述的是 **atlas 汇总**（`atlas-usage/1`），
`docs/portal-call-log-contract.md` 描述的是 **逐条调用**（`atlas-calls/1`）。
这两个都是**数据面**：它们回答「有哪些调用元数据、归谁、聚合成什么」。
本契约是**控制面**：它回答「这一层的模块/入口/可见性现在对你开不开放」，
不产出任何一行调用元数据。

| 契约 | 面 | 回答的问题 | `schema_version` | 端点 |
| --- | --- | --- | --- | --- |
| atlas 汇总 | 数据面（元数据归属与聚合） | 「被授权项目这一段的用量是多少」 | `atlas-usage/1` | `GET /api/atlas/usage` |
| calls 逐条 | 数据面（精确 `principal == subject`） | 「我发了什么调用」 | `atlas-calls/1` | `GET /api/atlas/calls` |
| modules（本契约） | 控制面（可用性、只读入口、可见性） | 「每个模块此刻能对我说什么」 | `portal-modules/1` | `GET /api/atlas/modules` |

三者**共用同一批信任原语**，但**不共用产出**：`backend/portal/__init__.py` 写明
Atlas 是数据面、本包是控制面，二者共用 `RequesterContext` / `KeyDirectory` /
`AccessDenied` 与 `Provenance`，而各自的 DTO 互不派生。

**控制面会回显数据面的 scope。** 本 DTO 把会话持有的 scope 原样回显给前端做审计，
而一个真实会话可能同时持有两个契约的 scope —— 所以 `KNOWN_SCOPES` 显式包含
**另一个契约**的 `atlas:calls:self:read`。自检要拦的是「拼错的 scope」，不是
「不属于本包的 scope」。本 DTO 还合成一个 `self_calls` 模块，把会话指进
`/atlas#calls`（即 calls 契约的那个视图）。

---

## 2. 模块布局

| 文件 | 职责 |
| --- | --- |
| `backend/portal/modules.py` | 契约本体：scope 常量、`MODULE_CATALOG`、`DeploymentFacts`、`EndpointRecord`、`ModuleRequest`、`build_modules_dto`、`validate_modules_dto`。**纯函数，无 IO** |
| `backend/portal/__init__.py` | 包的公开面：转出 `MODULES_SCHEMA_VERSION` / `DeploymentFacts` / `EndpointRecord` / `ModuleRequest` / `build_modules_dto` / `validate_modules_dto`，并载明 fail-closed 立场 |
| `dev/portal_modules.py` | 回环预览接线：`PREVIEW_FACTS`、`PortalModuleGrant(s)`、`build_endpoints`、`parse_visibility`、`build_payload` |
| `dev/preview_auth.py` | 路由 `GET /api/atlas/modules`（`_modules`）、查询参数读取（`_modules_query`）、`/calls` → `/atlas#calls` 的 302 |
| `web/js/modules/contract.js` | 浏览器侧载荷校验（出口再设一次防）；不含 DOM 与网络 |
| `web/js/modules/render.js` | 纯字符串渲染：模块卡、状态药丸、就绪清单、Endpoint 表、页面通告 |
| `web/js/modules/controller.js` | 模块层**唯一一次** `fetch` + 页面状态机 |
| `prototypes/v6/additions.html` | 模块面板标记（每块都带 fail-closed 默认文案）；由 `derive-from-v5.mjs` 拼进 05 外壳 |
| `tests/backend/test_portal_modules.py` | 纯函数 fail-closed 回归 |
| `tests/integration_auth/test_preview_modules.py` | 真实 HTTP 契约回归（真 `wsgiref` + 真 SQLite + 真 Session Cookie） |

`backend/portal/modules.py` 不联网、不读环境变量、不读真实日志、不启动监听。

---

## 3. 输入契约

### 3.1 scope（全部显式，`role` 不产生任何权限）

| scope | 打开什么 |
| --- | --- |
| `portal:modules:read` | 读取本模块目录的**前置条件**；本身**不打开任何入口** |
| `portal:engines:native` | 打开引擎原生控制台（Strata Chat / Monitor） |
| `portal:evaluation:native` | 打开 EvalScope 原生界面 |
| `portal:admin:read` | 查看整个部署范围的用户 / RBAC 视图（而不是只看自己的）。本构建的 `MODULE_CATALOG` **未**引用它 |
| `portal:endpoints:share` | 允许把 Endpoint **发布**为 Shared；缺省即 Private |
| `portal:observability:native` | 打开 Grafana / Prometheus 的原生只读大盘 |
| `atlas:calls:self:read` | **另一个契约**的 scope；本 DTO 用它合成 `self_calls` 模块，并随会话 scope 一并回显 |

`portal:modules:read` 与「进原生界面」的 scope **刻意分开**：前者是「告诉我有这么个
模块」，后者是「让我进到那个界面里」——两次不同的授权决定。`role` 只进 `roles`
（展示 / 审计用），代码里**不存在**「因为 `role == admin` 所以放行」的分支。

### 3.2 `visibility` 查询参数

* 取值域**精确**是两个小写词：`private` / `shared`（`VISIBILITY_MODES`）。
* `parse_visibility` **刻意不做** `strip()` / `lower()`：一个能被两种写法拼出来的值，
  会让「服务端到底收到了什么」变成一个需要猜的问题。不是这两个词之一 → `ValueError`
  → HTTP 400。
* 查询串里**只读** `QUERY_PARAMS = ("visibility",)`；其余一律忽略。
  `subject`、`role`、`x_principal`、任意 `X-` 头**不是**本端点的参数，
  说什么都无效。
* 同一个参数出现两次、或值为空 → 400（`_modules_query` 拒绝，而不是替调用方
  在两种含义里选一个）。

### 3.3 端点如何被盯住（只读 GET）

* `_modules` 在解析任何东西**之前**先判 `method != "GET"` → 405。写入请求
  **永远到不了**构建路径。
* 路由只在预览里：`dev/preview_auth.PreviewApplication.handle` 把
  `MODULES_PATH` 接到 `_modules`；该路径**不在**生产路由器
  `backend.auth.routes.ROUTES` 里（回归：`test_the_endpoint_is_absent_from_the_production_router`）。
* 响应是 JSON + `JSON_CSP`，带 `X-Content-Type-Options: nosniff` / `Cache-Control: no-store`。
* 载荷外层是 `{"ok": true, "data": {"observed_at": <ISO-Z>, "portal": <DTO>}}`；
  `observed_at` 是这份响应**被构建的时间**（服务端时钟）。

---

## 4. 授权与 fail-closed 判定

### 4.1 `resolve_module_scope`：会话与 scope 的解析

| 情形 | 结果 |
| --- | --- |
| `request` 不是 `ModuleRequest`、`requester` 不是 `RequesterContext` | `InvalidRequest` |
| 会话无 subject（`""` / `"   "`） | `AccessDenied`（`no_authenticated_session`） |
| 缺 `portal:modules:read` | `AccessDenied`（`missing_required_scope`） |
| `visibility` 不是 `private` / `shared` | `InvalidRequest` |

`role` 在任何一步都不参与。`role == admin` 且不带 scope，看到的仍是拒绝
（回归：`test_an_admin_role_is_not_a_fallback`）。

### 4.2 `(state, reason)` 规则（`_module_state`）

前置条件是**全部**成立才 `available`；否则按下面的顺序给出状态与原因：

```
1. 缺任一 requires_scopes           -> unavailable / missing_required_scope
2. 未满足的事实里有，且 spec.preview_only
                                    -> disabled    / preview_build_has_no_real_backend
3. 未满足里含 engine_is_strata 且引擎为空
                                    -> unknown     / not_configured_in_this_deployment
4. 未满足里含 engine_is_strata 且引擎非空非 strata
                                    -> unavailable / engine_is_not_strata
5. 未满足里含 gateway_attached      -> unavailable / gateway_not_attached
6. 其余未满足                       -> unavailable / spec.missing_reason
7. 全部成立                         -> available   / None
```

**带 scope 的判定（第 1 步）早于事实判定（第 2 步起）**：缺 scope 就是缺 scope，
不会因为「事实恰好也没满足」而换一个原因。

### 4.3 `MODULE_CATALOG` 的前置条件

| `module_id` | `requires_scopes` | `requires`（`FACT_CHECKS` 谓词名） | `preview_only` | `missing_reason` |
| --- | --- | --- | --- | --- |
| `engines` | （无） | `engine_is_strata`, `gateway_attached` | False | `engine_is_not_strata` |
| `playground` | （无） | `playground_backend_authorized` | **True** | `preview_build_has_no_real_backend` |
| `observability` | （无） | （无） | False | `not_configured_in_this_deployment` |
| `evaluation` | `{portal:evaluation:native}` | `evalscope_configured` | False | `not_configured_in_this_deployment` |
| `administration` | （无） | （无） | False | `not_configured_in_this_deployment` |

另有**合成**模块 `self_calls`（不在 `MODULE_CATALOG`，由 `build_modules_dto` 直接追加）：
持 `atlas:calls:self:read` 则 `available`，否则 `unavailable` + `missing_required_scope`。

`FACT_CHECKS` 把「需要的部署事实」映射成一个谓词，取名字而不是 lambda，以便静态检查
与自检遍历：

| 谓词名 | 含义 |
| --- | --- |
| `gateway_attached` | `facts.gateway_attached` |
| `engine_is_strata` | `facts.is_strata()` |
| `grafana_configured` | `bool(facts.grafana_path)` |
| `prometheus_configured` | `bool(facts.prometheus_path)` |
| `evalscope_configured` | `bool(facts.evalscope_path)` |
| `has_gpu_source` | `"nvml-verified" in facts.attached_sources` |
| `playground_backend_authorized` | `facts.playground_backend_authorized` |

### 4.4 `disabled` 与 `unavailable` 不是同一件事

* `disabled` = **本构建永远没有**真实后端（`preview_only=True`）。`playground` 是
  `disabled`：它的按钮**不应该**看起来像「配一下就能用」。
* `unavailable` = **这次部署**没配（缺 scope、没接 Gateway、路径没配……）。

两者状态不同、原因不同（回归：`test_playground_is_disabled_not_merely_unavailable`
对 `test_a_missing_scope_makes_a_module_unavailable`）。`engines` 的「引擎未知」
（`unknown`）与「引擎不是 Strata」（`unavailable`）同样分开
（回归：`test_an_unknown_engine_is_unknown_not_a_wrong_engine` 对
`test_a_named_non_strata_engine_is_unavailable`）。

### 4.5 `available` 必须名副其实

出口自检 `validate_modules_dto` **拒绝**两类说谎的 `available`：

* 声称 `available` 却缺某个它自己声明的 `requires_scopes`
  （回归：`test_an_available_module_without_its_scope_is_refused`）；
* 声称 `available` 却 `metrics` / `links` / `data` 三者全空
  （回归：`test_an_available_module_with_nothing_to_show_is_refused`）。

反之，任何**非** `available` 的模块**必须**给出原因，否则也拒绝。

### 4.6 拒绝原因的稳定字符串

| 常量 | 取值 | 何时出现 |
| --- | --- | --- |
| `REASON_NO_SESSION` | `no_authenticated_session` | 会话无 subject |
| `REASON_MISSING_SCOPE` | `missing_required_scope` | 缺所需 scope |
| `REASON_ENGINE_NOT_STRATA` | `engine_is_not_strata` | 引擎非空且不是 Strata |
| `REASON_GATEWAY_NOT_ATTACHED` | `gateway_not_attached` | Gateway 未接线 |
| `REASON_NOT_CONFIGURED` | `not_configured_in_this_deployment` | 未配置 / 引擎未知 |
| `REASON_NO_AUTHORIZED_BACKEND` | `no_authorized_backend` | 已声明但本构建的 `_module_state` 未产出它；前端认识这个 token |
| `REASON_NO_VERIFIED_SOURCE` | `no_verified_metric_source` | 指标无来源 |
| `REASON_PREVIEW_ONLY` | `preview_build_has_no_real_backend` | `preview_only` 模块未满足事实 |

这些是**契约**，不是日志文案：测试断言它们，前端按它们分流
（`web/js/modules/contract.js` 的 `REASON_LABELS`；不认识的 token 原样显示，不吞掉）。

---

## 5. 入口（链接）契约

### 5.1 链接的判定与模块的判定是**两套**

模块 `available` **不**代表它的每个入口都开放。每个入口的判定是**独立**的，
在 `_module_links` 里单独算。下表的「实际门槛」列就是 `_module_links` 里**真正**
检查的条件；`requires` 列是随链接一起发出的完整前置说明文字。

| 模块 | 入口 | `requires` | 实际门槛 |
| --- | --- | --- | --- |
| `engines` | 原生 Chat `{gateway_path}/strata/chat` | `engine=strata + gateway=attached + portal:engines:native` | 引擎为 Strata **且** Gateway 已接线 **且** 持 `portal:engines:native` **且** 已配 `gateway_path` |
| `engines` | 原生 Monitor `{gateway_path}/strata/monitor` | 同上 | 同上 |
| `engines` | 原生 Chat / Monitor（**被挡住**） | `portal:engines:native` | 引擎为 Strata **且** Gateway 已接线，但**缺** `portal:engines:native` → 只发被挡条，**无地址** |
| `observability` | Grafana 只读大盘 `{grafana_path}` | `portal:observability:native + grafana=configured` | 已配 `grafana_path` |
| `observability` | Prometheus 查询 `{prometheus_path}` | `portal:observability:native + prometheus=configured` | 已配 `prometheus_path` |
| `evaluation` | EvalScope 原生界面 `{evalscope_path}` | `portal:evaluation:native + evalscope=configured` | 已配 `evalscope_path` |
| `self_calls` | 打开我的调用日志 `/atlas#calls` | `atlas:calls:self:read` | 常量发出（`kind: internal`）；模块状态由 scope 决定 |

`engines` 的原生入口是「模块前置条件 + 它自己的 scope」的 **AND**：多给一个
`portal:engines:native` 也开不了 —— 本构建没有 Strata、没有 Gateway
（集成回归：`test_an_extra_scope_does_not_open_a_link_the_deployment_cannot_serve`）。

`self_calls` 的链接指向**外壳里的另一个视图**（同源路径），与 Gateway 是否接线无关。
所以集成回归按 `kind` 而不是按「有没有链接」断言「没有 Gateway 就没有原生入口」
（`test_no_native_console_entry_exists_without_a_gateway`）。

### 5.2 被挡住的入口没有地址，但说明缺什么

缺 scope 时入口以**被挡住**的形式出现（`href: None`），带 `requires` 与
`blocked`（= `missing_required_scope`）。**关键是它没有地址** —— 页面上不可能
出现一个点了会失败的链接。出口自检强制「被挡条必须说明它需要什么」
（回归：`test_the_native_console_link_needs_its_own_scope` 断言 `href is None`
且 `requires == portal:engines:native`）。

### 5.3 同源 + 上游端口规则，以及**为什么**

两条规则一起构成「不绕过 Gateway / Proxy」：

1. 任何链接都必须是**同源内部路径**（以 `/` 开头，且不是协议相对的 `//`，
   也不含 `://`）。
2. 任何链接都**不得**指向上游推理端口（`UPSTREAM_PORTS = {"8080"}`）。

为什么用「逐字拒绝端口 token」而不是「解析 authority 再比端口」：一个**原样转发**
的代理会把上游写成**路径段**（`/proxy/127.0.0.1:8080/v1`），而「这个 `:8080` 是
authority 还是路径的一部分」正是会漏过去的地方。逐字拒绝这个 token 没有这个边界问题。
落点是 `_internal_link`（构造期拒绝）与 `validate_modules_dto`（出口再查一遍）。

### 5.4 规则的**确切边界**：`/8080/v1` 放行，`/x:8080` 拒绝

被拦的是「地址里写了一个端口」，**不是**「路径里有 8080」：

| href | 结果 | 理由 |
| --- | --- | --- |
| `/8080/v1` | **放行** | 只是本机服务器上的一个路径段；浏览器按同源请求它，路由到哪由服务端决定，**不构成**绕过 Gateway 的通道 |
| `/x:8080` | **拒绝** | 出现 `:8080` 这个上游端口 token，正是绕过的那一条 |
| `/proxy/127.0.0.1:8080/v1` | 拒绝 | 端口藏在路径段里，也必须拦 |
| `http://127.0.0.1:8080/v1`、`//evil.example/x`、`127.0.0.1:8080` | 拒绝 | 非 `/` 开头 / 协议相对 / 绝对 |

回归：`test_a_bare_numeric_path_segment_is_not_treated_as_a_port`（放行 `/8080/v1`）、
`test_a_link_at_an_upstream_port_is_refused_even_as_a_same_origin_path`（拒绝
`/x:8080`、`/proxy/127.0.0.1:8080/v1`）、
`test_a_link_may_never_leave_the_origin_or_reach_an_upstream_port`。

---

## 6. 数值契约

### 6.1 有来源才有数字

一个指标数值被允许出现在页面上，当且仅当它的 `source` 在**白名单**
`VERIFIED_METRIC_SOURCES` 里**且**该来源在 `DeploymentFacts.attached_sources` 里
被声明为已接线：

```
VERIFIED_METRIC_SOURCES = { "gateway-usage-export", "nvml-verified", "prometheus-readonly" }
```

这是**白名单**而不是黑名单：新增一个观测源必须改这里，也就是必须有人**显式**声明
「这个东西现在可信」。默认拒绝。

`DeploymentFacts.__post_init__` 在配置期就拦住三类畸形输入：

* `attached_sources` 里出现白名单外的来源 → `InvalidRequest`；
* 某个指标 cite 了白名单外、或**未声明已接线**的来源 → `InvalidRequest`；
* 指标的 value 不是数字（含 `bool`）→ `InvalidRequest`。

### 6.2 没有来源就是 `null` + `unknown`

`_metric` 对没有可信来源的指标返回：

```jsonc
{ "name": "...", "label": "...", "unit": "...",
  "value": null, "state": "unknown", "source": null,
  "reason": "no_verified_metric_source" }
```

**绝不用 `0` 或一个「看起来正常」的数顶替。** 出口自检强制这条
（回归：`test_every_metric_without_a_source_is_null_and_unknown`）。

### 6.3 GPU 规则（`GPU_METRIC_SOURCE`）

唯一被允许为 GPU 指标背书的观测来源是 `GPU_METRIC_SOURCE = "nvml-verified"`。
**本构建没有接线它**，所以本构建里每一个 `gpu_*` 都是 `null`。这条保证在**四个层次**
各自成立，任何一层被绕过还有下一层：

| 层 | 落点 | 做什么 |
| --- | --- | --- |
| 1. 事实校验 | `DeploymentFacts.__post_init__` | `attached_sources` / `metrics` 的来源必须属于白名单且已声明接线 |
| 2. 取值 | `_metric` | `gpu_*` 且未声明 `nvml-verified` → 强制 `has_source = False` → 只能得到 `null` / `unknown` |
| 3. 出口自检 | `validate_modules_dto` | 值非 `null` 的 `gpu_*`，其 `source` 必须是 `nvml-verified`，否则 `ContractError` |
| 4. 浏览器镜像 | `web/js/modules/contract.js` | `gpu_*` 带值即 `errors.push`，整页不渲染 |

规则是「**没有 NVML 来源就没有 GPU 数字**」，而不是「永远不许有 GPU 数字」：
一个**真的**接了 NVML 的部署必须能显示它；本构建（什么都没接）仍然不能。
回归用「接的是 prometheus 而不是 NVML」的配置证明这一点
（`test_gpu_metrics_are_never_a_number_in_this_build`、自检
`test_a_gpu_metric_with_a_value_is_refused`）。

---

## 7. 可见性（Private / Shared）

### 7.1 默认私有

`_endpoint_block` 按会话主体算出可见 Endpoint。**默认 `private`**：

* `private` 只含 `record.principal == subject` 的记录；
* `shared` **额外**含**已授权项目内**（`record.project in authorized_projects`）
  且被运维标记为 `shared=True` 的记录。

每条 item 带 `owner`（`self` / `shared`）与 `scope`（`private` / `shared`）。

### 7.2 开关**不能**放宽任何东西

客户端只能在**服务端已经算好的这两个子集**之间切换。理由（见
`dev/portal_modules.py` 与 DTO 的 `notes`）：可见性是**服务端判定**，一个能被客户端
改宽的值就不是判定。出口自检强制「`shared` 的 item 不得出现在 `private` 的视图里」
（回归：`test_a_shared_endpoint_in_a_private_view_is_refused`）。

切换的具体效果（回归 `test_shared_adds_only_shared_endpoints_inside_my_projects`）：

* 自己的 Endpoint（无论 shared 与否）在两种视图里都在；
* 同事的、**未标记 shared** 的 Endpoint：两种视图里都不出现；
* **标记了 shared 但项目不在本人授权集**的 Endpoint：两种视图里都不出现；
* 切到 `shared` **永不**隐藏自己的 Endpoint
  （`test_switching_to_shared_never_hides_my_own_endpoints`）。

### 7.3 `can_publish_shared`

`can_publish_shared = SCOPE_ENDPOINTS_SHARE in requester.scopes`。它是「发布为
Shared 需要什么」的**独立事实**，与当前视图的可见性无关；端点目录里 `shared`
这一标记本身由**运维在服务端**写死（`EndpointRecord.shared`），不是客户端能改的
（回归：`test_publishing_shared_requires_its_own_scope`）。

---

## 8. DTO 字段

```jsonc
{
  "source": "synthetic-preview",       // 由调用方经 Provenance 显式声明
  "synthetic": true,                   // 同上；无默认值
  "schema_version": "portal-modules/1",// 与 atlas-usage/1、atlas-calls/1 都不同
  "transport": {                       // §9 传输诚实
    "mode": "demo",                    // "demo" | "live"
    "gateway_attached": false,
    "synthetic": true,
    "label": "DEMO / 合成数据"          // 或 "LIVE / 已接线"
  },
  "scope": {
    "subject": "local:…",
    "roles": ["…"],                    // 展示/审计用，不产生权限
    "scopes": ["portal:modules:read", "…"],   // 会话持有的 scope 原样回显
    "projects": ["proj-a"],            // 本次授权项目
    "visibility": "private",           // private | shared
    "visible_modules": ["administration", "observability", "…"]  // = 状态为 available 的 id，升序
  },
  "deployment": {
    "engine": null,                    // "strata" | "vllm" | … | null
    "gateway_attached": false,
    "attached_sources": [],            // 已声明为已接线的观测来源（升序）
    "native_console_ready": false,     // = engine 是 Strata 且 Gateway 已接线
    "facts": {                         // 每个具名前置条件的真值，供 UI 画就绪清单
      "engine_is_strata": false, "gateway_attached": false,
      "grafana_configured": false, "prometheus_configured": false,
      "evalscope_configured": false, "has_gpu_source": false,
      "playground_backend_authorized": false
    }
  },
  "endpoints": {                       // §7
    "visibility": "private",
    "can_publish_shared": false,       // = 持 portal:endpoints:share
    "items": [ {
      "endpoint_id": "ep-demo-1", "name": "my-chat",
      "project": "proj-a", "protocol": "OpenAI Chat", "model": "llm-a",
      "owner": "self",                 // "self" | "shared"
      "scope": "private"               // "private" | "shared"
    } ]
  },
  "modules": [ {                       // 按 id 升序；含合成的 self_calls
    "id": "engines",                   // 或 "playground"/"observability"/"evaluation"
                                       //    /"administration"/"self_calls"
    "title": "引擎与部署",
    "detail": "…",
    "state": "unknown",                // available | unavailable | disabled | unknown
    "reason": "not_configured_in_this_deployment",  // available 时为 null
    "requires_scopes": [],             // 本模块声明的 scope 前置（升序）
    "requires": ["engine_is_strata", "gateway_attached"],  // 声明的部署事实谓词名
    "data": { /* 模块自己声明的事实块，见下 */ },
    "metrics": [ {                     // §6
      "name": "gpu_vram_used_mib", "label": "GPU 显存占用", "unit": "MiB",
      "value": null, "state": "unknown", "source": null,
      "reason": "no_verified_metric_source"
    } ],
    "links": [ {                       // §5；普通条目
      "href": "/gw/strata/chat", "label": "原生 Chat", "kind": "native",
      "requires": "engine=strata + gateway=attached + portal:engines:native"
    }, {                               // 被挡住条目：无 href，说明缺什么
      "href": null, "label": "原生 Chat / Monitor", "kind": "native",
      "requires": "portal:engines:native", "blocked": "missing_required_scope"
    } ]
  } ],
  "display_hints": [ "…" ],            // 供 UI 直接展示的口径/降级提示
  "notes": [ "…" ]                     // 口径说明；出口自检要求非空
}
```

### 8.1 各模块的 `data`（服务端刚做出的判定本身）

`data` 装的是「既不是外部指标、也不是入口」的内容 —— 例如会话授权。它**不**混进
`metrics`，因为 `metrics` 的每一项都必须有可信观测来源，而会话授权不是观测。

| `module_id` | `data` |
| --- | --- |
| `administration` | `{ roles, scopes, projects, subject }`（全部升序 / 去空白） |
| `engines` | `{ engine, gateway_attached, native_console_ready }` |
| `observability` | `{ attached_sources }`（升序） |
| `playground` | 仅在可用状态下 `{ "backend": "authorized-gateway", "audited": true }`，否则 `{}` |
| `self_calls` | `{ "endpoint": "/api/atlas/calls", "view": "calls" }` |
| `evaluation` | `{}` |

### 8.2 `transport` 绝不假称 live

`mode == "live"` 要求 `facts.live`（即 `gateway_attached`）**且** `provenance.synthetic`
为假，两者同时成立。出口自检拒绝「合成数据 + live」与「live 但未声明已接线」。
预览里 `gateway_attached` 写死为假（`PREVIEW_FACTS`），没有任何运行参数能改变它
（回归：`test_a_synthetic_payload_is_demo_even_with_a_gateway_attached`、
`test_live_requires_a_real_source_and_an_attached_gateway`）。

### 8.3 `scope.visible_modules` 必须与模块状态一致

`visible_modules` 就是「状态为 `available`」的模块 id，升序。出口自检拒绝二者不一致
（回归：`test_a_visible_modules_list_that_disagrees_is_refused`）。

---

## 9. 传输层状态码与失败模式

`_modules` 的映射（与 `_calls` 同构）：

| 情形 | HTTP | `code` |
| --- | --- | --- |
| 非 GET | 405 | `method_not_allowed` |
| 无 / 过期 / 已注销会话 | 401 | 产品自己的 `not_authenticated` / `session_expired` |
| 账号被停用 | 403 | `account_disabled` |
| 登录了但无 server-side grant（**含 admin**） | 403 | `forbidden` |
| 参数重复 / 值为空 | 400 | `invalid_request` |
| `visibility` 不是 `private` / `shared` | 400 | `invalid_request` |
| 契约层 `AccessDenied`（如缺 `portal:modules:read`） | 403 | `forbidden` |
| 契约层 `InvalidRequest` | 400 | `invalid_request` |
| DTO 自检失败（`ContractError`） | 500 | `server_error`（不透露哪条不变量失败） |

**顺序是安全边界**：grant 检查（403）在**解析查询串之前**，所以没有 grant 的调用方
无论发什么，都学到同一件事（403）。会话检查又在 grant 检查之前，所以注销、过期、
停用一律按产品自己的分类回答。

---

## 10. 前端

### 10.1 面板与它们所在的视图

模块卡由 `web/js/modules/controller.js` 画进外壳提供的**每一个** `[data-module]`
面板 —— 这样 `models`、`api`、`observability`、`evaluation`、`admin` 五个视图读的是
**同一份一致的答案**，而不是五份可能互相打架的答案。`prototypes/v6/additions.html`
把五个面板分别拼进 V5 外壳的 `panel-models` / `panel-api` / `panel-observability` /
`panel-evaluation` / `panel-admin` 锚点：

| 面板 `data-module` | 视图 | fail-closed 默认文案（未确认会话前） |
| --- | --- | --- |
| `engines` | models | 「……授权确认之前，这里不显示引擎版本、显存或部署状态——尤其不会显示一个看起来正常的 GPU 数字」 |
| `playground` | api | 「Endpoint 的可见性由服务端按 principal × 项目判定，默认 **Private**；本页面只呈现判定结果，不接受客户端把它改成 Shared」 |
| `observability` | observability | 「没有配置可信数据源的指标一律显示 UNKNOWN，而不是 0」 |
| `evaluation` | evaluation | 「……未开放时这里说明原因，而不是给一个点了没反应的按钮」 |
| `administration` | admin | 「下面显示的是服务端按你的会话主体算出的可见范围……不是客户端选择的结果」 |

「我的调用」是外壳里的第八个视图（`#view-calls`，由 `#nav-top` / `#nav-mobile` 的
`data-page="calls"` 切入）；`/calls` 是它的**深链接**：`dev/preview_auth.py` 把
`/calls` 以 302 永久重定向到 `/atlas#calls`，所以视图只有一份文档，不会出现第二个
会漂移的副本（回归：`test_the_deep_link_redirects_into_the_shell`、
`test_the_old_standalone_document_is_gone`）。`/atlas`（`PROTECTED_PAGES`）是受会话
把关的外壳页，`/calls` 本身**不**在 `PROTECTED_PAGES` 里 —— 它只是一个重定向。

### 10.2 浏览器侧再设一次防（`contract.js`）

服务端已经跑过 `validate_modules_dto`，前端仍再查一遍，拒绝四类载荷：

1. **离开源**或**指向上游端口**的链接（`isInternalHref`：非 `/` 开头、`//` 开头、
   含 `://`、含 `:8080` → 拒绝）。载荷非法时**整页不渲染**，而不是「把地址抹掉再渲染」——
   让违规可见，而不是悄悄修好。
2. **带值的 GPU 指标**：没有 NVML 来源就没有 GPU 数字。
3. 声称 `available` 却**无可展示内容**，或缺某个自己声明的 scope。
4. **合成数据自称 live**；以及 `live` 却未声明 gateway 已接线。

缺失值一路保持 `null` 到面板（渲染成 `—`），**永不**变成 `0`。

### 10.3 页面状态机（与服务器的回答 1:1）

| 服务器 | 页面状态 | 文案 / 行为 |
| --- | --- | --- |
| 200 且载荷合法 | `ready` | 画面板，每个各带自己的状态与原因 |
| 401 | `signed_out` | 「请先登录」（→ `/login?next=%2Fatlas`） |
| 403 | `denied` | 「未获得模块目录授权」→ 找管理员；**不**自动重试 |
| 其它 / 非 JSON / 契约违规 | `unknown` | 不显示任何状态或数字 + 「重试」 |

`STATE_CHANGING_STATUSES = [401, 403]` 单独先判：一个 403 页**不会**被改标签成
「没有模块」。契约违规时 `portal` 置空、整页不渲染 —— 半张模块列表看起来像权威的，
但其实错的。

### 10.4 唯一一次 `fetch` 的位置

模块层的唯一一次 `fetch` 在 `web/js/modules/controller.js`（`MODULES_ENDPOINT =
"/api/atlas/modules"`），**不是**在 `web/js/data/`。这与调用日志的**已记录例外**
同源：`web/README.md` §7.1 记录了「不要给 `web/js/data/` 之外的文件加网络调用」
这条规则的那一处有据例外（调用日志页），模块层沿用同一先例 —— 例外局部、有据、
只有一处 `fetch`。`render.js` 的每个函数都是 `(portal, moduleId) -> html 字符串`，
所以它能在 plain Node 下被测，而不必依赖这个 build 里没有的 Chrome；`contract.js`
不含 DOM 与网络。

---

## 11. 安全不变量与回归证据

| 不变量 | 覆盖 |
| --- | --- |
| 缺 `portal:modules:read` / 无会话一律拒绝 | `test_the_modules_scope_is_required`、`test_no_session_is_denied` |
| `role == admin` 不是回退 | `test_an_admin_role_is_not_a_fallback` |
| 未知 `visibility` 取值被拒 | `test_an_unknown_visibility_mode_is_rejected`、`test_an_unknown_visibility_is_400` |
| 非 `available` 必须说明原因 | `test_every_module_states_a_reason_when_it_is_not_available`、`test_every_module_either_works_or_says_why_not` |
| `disabled`（本构建永不）≠ `unavailable`（没配） | `test_playground_is_disabled_not_merely_unavailable`、`test_the_playground_is_disabled_and_says_it_is_this_build` |
| 引擎未知 ≠ 引擎不对 | `test_an_unknown_engine_is_unknown_not_a_wrong_engine` 对 `test_a_named_non_strata_engine_is_unavailable` |
| 「全部成立才 available」是 AND 不是 OR | `test_strata_without_a_gateway_is_unavailable`、`test_an_extra_scope_does_not_open_a_link_the_deployment_cannot_serve` |
| 缺 scope → `missing_required_scope`（不是别的） | `test_a_missing_scope_makes_a_module_unavailable`、`test_the_preview_reports_the_modules_a_bare_read_grant_cannot_open` |
| 入口只在**真能打开**时出现（模块 ≠ 入口） | `test_the_native_console_link_appears_only_when_everything_holds`、`test_no_native_console_link_without_strata`、`test_no_native_console_link_without_a_gateway` |
| 被挡条**无地址**且说明缺什么 | `test_the_native_console_link_needs_its_own_scope` |
| 链接永不离开源 / 永不指上游端口 | `test_a_link_may_never_leave_the_origin_or_reach_an_upstream_port`、`test_a_link_at_an_upstream_port_is_refused_even_as_a_same_origin_path`、`test_the_generated_dto_has_no_absolute_or_upstream_links_at_all`、`test_no_link_anywhere_is_absolute_or_names_an_upstream_port` |
| 规则边界：`/8080/v1` 放行、`/x:8080` 拒绝 | `test_a_bare_numeric_path_segment_is_not_treated_as_a_port`（对 `test_a_link_at_an_upstream_port_is_refused_even_as_a_same_origin_path`） |
| 没有来源就没有数字（`null` + unknown） | `test_every_metric_without_a_source_is_null_and_unknown` |
| **GPU 在本构建永远是 null**（四层） | `test_gpu_metrics_are_never_a_number_in_this_build`、`test_no_gpu_metric_is_ever_a_number`、`test_no_metric_carries_a_number_at_all_in_this_build` |
| 有来源才出数字 | `test_a_declared_verified_source_does_produce_a_number` |
| 未白名单 / 未声明接线的来源在配置期就被拒 | `test_a_metric_may_not_cite_an_unverified_source`、`test_a_metric_may_not_cite_a_source_that_is_not_attached`、`test_an_unknown_source_cannot_even_be_declared_attached` |
| 合成数据绝不自称 live | `test_the_demo_build_never_claims_to_be_live`、`test_the_preview_never_claims_to_be_live`、`test_a_synthetic_payload_is_demo_even_with_a_gateway_attached` |
| 默认 Private，看不到同事 | `test_the_default_view_is_private_and_holds_only_my_endpoints`、`test_private_never_shows_a_colleague`、`test_a_colleagues_endpoint_is_absent_from_the_private_view`、`test_the_client_cannot_widen_visibility_through_the_payload` |
| Shared 只在已授权项目内放宽，且不隐藏自己的 | `test_shared_adds_only_shared_endpoints_inside_my_projects`、`test_switching_to_shared_never_hides_my_own_endpoints`、`test_switching_to_shared_adds_shared_endpoints_in_my_projects_only` |
| 发布为 Shared 需独立 scope | `test_publishing_shared_requires_its_own_scope` |
| 伪造身份参数 / 头影响 = 0 | `test_forged_identity_input_changes_nothing` |
| 未知 `visibility` / 重复参数 = 400 | `test_a_repeated_visibility_parameter_is_400`、`test_an_unknown_visibility_is_400` |
| 每个授权账号只看自己的 subject | `test_each_granted_account_sees_only_its_own_subject` |
| 注销 / 吊销**立即** 401，停用 403 | `test_signing_out_takes_effect_immediately`、`test_a_revoked_session_is_401`、`test_a_disabled_account_is_403` |
| 无 grant 的 admin 也是 403 | `test_a_signed_in_admin_without_a_grant_is_403`、`test_a_member_without_a_grant_is_403` |
| 非 GET = 405 | `test_write_verbs_are_405` |
| 出口自检真的会失败（篡改即拒绝） | `test_an_available_module_without_its_scope_is_refused`、`test_an_available_module_with_nothing_to_show_is_refused`、`test_a_gpu_metric_with_a_value_is_refused`、`test_a_metric_with_an_unverified_source_is_refused`、`test_an_absolute_link_is_refused`、`test_a_link_to_an_upstream_port_is_refused`、`test_claiming_live_on_synthetic_data_is_refused`、`test_an_unknown_module_id_is_refused`、`test_a_visible_modules_list_that_disagrees_is_refused`、`test_a_shared_endpoint_in_a_private_view_is_refused`（基线 `test_the_baseline_passes`、`test_the_dto_is_json_serialisable`） |
| 生产路由器里没有这条路径 | `test_the_endpoint_is_absent_from_the_production_router`、`test_the_three_preview_endpoints_are_distinct_paths` |
| `self_calls` 链接指回外壳 | `test_the_self_calls_link_points_into_the_shell` |
| 深链接 302、旧独立文档已下线 | `test_the_deep_link_redirects_into_the_shell`、`test_the_old_standalone_document_is_gone` |
| JSON 响应头正确 | `test_response_headers_are_the_json_ones` |

---

## 12. 仍未接线 / 已知缺口

1. **没有真实 Gateway / NVML / Grafana / Prometheus / EvalScope 接线。**
   预览的 `PREVIEW_FACTS = DeploymentFacts()` 是**空值即事实**：这台机器上确实
   没有接这些东西。它让每个模块落到它真实的状态上：`engines` → `unknown`（引擎未知）、
   `playground` → `disabled`（本构建没有、也不会有真实推理后端）、
   `evaluation` → `unavailable`、`observability` / `administration` 可用但指标全
   `UNKNOWN`。
2. **部署事实写死，没有任何运行参数能改变它们。** `PREVIEW_FACTS` 是模块级常量，
   请求路径与 CLI 都没有入口能改；`--portal-modules-grant` 只发
   `portal:modules:read`（`DEFAULT_GRANT_SCOPES`），多给一个 scope 是**进程内**
   配置动作，不是命令行动作。想让它变真，必须先真的接上那些东西并改这一行。
3. **没有审计事件。** 「谁在什么时候看了这个模块目录」本模块不产出。
4. **模块列表是静态配置。** `MODULE_CATALOG` 是写死的声明；新增模块必须在这里加一行，
   否则前端拿不到它，而且 `validate_modules_dto` 会拒绝一个含未知模块 id 的 DTO。
5. **UI 开关只在两个服务端算好的子集之间切换。** 客户端不能在服务端判定之外把可见性
   放宽到任何别的东西；`shared` 这一标记本身也由运维在服务端写死。
6. **Endpoint 目录是合成 fixture。** 预览里由 `dev.portal_modules.build_endpoints`
   生成一小撮虚构数据（`principal` 绑定到被授权的那个主体）；**没有真实 Endpoint 来源**，
   生产需要经审批的目录适配器。
7. **`self_calls` 模块是桥接用的静态条目**：它把会话指进 calls 契约的视图
   （`/atlas#calls`），自身不产出任何调用数据。

以上都不是「忘了做」，而是需要单独的接线 / 授权 / 产品决定。

---

## 13. 如何运行

```bash
# 后端（纯函数，离线）
python -m unittest discover -s tests/backend -t .

# 真实 HTTP 契约（真 wsgiref + 真 SQLite + 真 Session Cookie）
python -m unittest discover -s tests/integration_auth -t .

# 前端（plain Node，不需要浏览器）
npm test

# 回环预览：模块目录端点
python -m backend.auth.bootstrap init --state-dir <ABSOLUTE DIR>
python -m dev.preview_auth --local-http-dev --state-dir <ABSOLUTE DIR> \
    --portal-modules-grant admin
# GET http://127.0.0.1:<port>/api/atlas/modules
```

Python 3.11+，仅标准库。fixture 全部为合成数据，已在 `.gitignore` 中按仓库既有惯例
显式开例外。
