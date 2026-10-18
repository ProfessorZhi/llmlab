# EvalScope 评测中心契约（`evaluation-center/1`）

**状态：** 平台层实现 + 回环预览（preview-only，未接生产 BFF）。
**范围：** `backend/evaluation/**`、`tests/evaluation/**`、`dev/evaluation_preview.py`、本文。
**不在本契约内：** EvalScope 本体（真实基准、真实 runner、命令行、HTTP 接口）、生产
HTTP 监听、GPU 探测来源、真实报告存储、Portal 前端渲染、`portal:evaluation:native` 入口。

> 本文不是"目标架构"，而是**此刻已经实现、已被测试固定**的契约。没做到的写在
> §12「本构建明确不做的事」里，而不是含糊过去。

---

## 0. 一句话

本模块**不做评测**。它回答四个问题，并保证答案可审计：

1. **谁**在提交评测（身份只来自服务端可信目录，绝不来自请求）；
2. **评什么**（评测类型 / 数据集 / 参数走封闭允许清单，自定义命令与外部地址被结构性排除）；
3. **结果给谁看**（owner/管理员可见，跨用户私有资源一律 404）；
4. **真实执行什么时候被允许**（合取式 GPU 准入；本部署一条都不成立，因此**从不**派发）。

---

## 1. 本部署的当前状态（如实）

| 项 | 状态 |
|---|---|
| 真实 EvalScope runner | **未接线**。适配器是桩，`run()` 一律 `RunnerUnavailable` |
| 真实派发 | **禁止**。`admission.allowed` 恒为 `false`，任务只进入队列 |
| GPU 事实 | **未观测**（`not_configured`）。本模块不读 GPU、不调 NVML、不跑 `nvidia-smi` |
| 结果 | **全部合成**。mock runner 是唯一 runner，每个结果 `synthetic=true` |
| 报告根目录 | **未接线**。报告引用不会被解析成路径 |
| EvalScope 原生界面 | **不开放**（`preview_build_has_no_real_backend`） |
| 数据集 | **平台自有的合成条目**。EvalScope 真实数据集名到本清单的映射尚未接线 |
| 生产部署 | **无**。只有 `dev/evaluation_preview.py` 的回环预览 |

准入的拒绝原因（本部署同时成立）：

```
admin_dispatch_disabled      管理员未开启真实派发
dispatch_scope_not_granted   该主体不持有 evaluation:dispatch:real
real_runner_not_available    没有可用的真实 runner（适配器根本没有装配）
adapter_version_unknown      不知道任何 EvalScope 适配器的版本
outside_time_window          不在维护时间窗内
gpu_facts_not_observed       显存占用未观测（不是 0，也不是"充足"）
```

`insufficient_gpu_headroom` **不**在这一组里，因为它与 `gpu_facts_not_observed` 互斥：
前者是"测到了但不够"，后者是"没测到"。

---

## 2. 模块布局

| 文件 | 职责 | 关键不变量 |
|---|---|---|
| `backend/evaluation/errors.py` | 失败模式 | 只带 `contract_code`，不决定 HTTP 状态码 |
| `backend/evaluation/identity.py` | 受信主体目录、scope、自报身份拒绝 | 身份只能从凭据推出；`role` 不产生权限 |
| `backend/evaluation/authz.py` | 行级可见性与写权限 | 「谁看得见什么」只在这一处决定 |
| `backend/evaluation/targets.py` | 受信评测目标目录 | 未授权 = 查无此目标；共享 ≠ 可压测 |
| `backend/evaluation/datasets.py` | 评测类型、允许清单、参数范围、安全预设 | 未知即拒绝；命令/路径/地址四类字段结构性排除 |
| `backend/evaluation/models.py` | 领域对象与显式状态机 | 终态永不离开；`synthetic` 无默认值 |
| `backend/evaluation/store.py` | SQLite 持久层 | 迁移不匹配即拒绝启动；审计只记 metadata |
| `backend/evaluation/limits.py` | 提交配额与时间窗 | 只拒绝，不"先收下以后再偷偷跑" |
| `backend/evaluation/admission.py` | GPU 准入 | 合取；未观测 ≠ 充足 |
| `backend/evaluation/runner.py` | mock runner + 适配器桩 | mock 只产出合成样本；`:9000` 是绊线 |
| `backend/evaluation/reports.py` | 报告引用 | 只读、相对、受限后缀；内容读取关闭 |
| `backend/evaluation/dto.py` | JobStatus / ResultSummary | 未测到是 `null`，不是 `0` |
| `backend/evaluation/service.py` | 编排 | 顺序即安全设计（见 §5） |
| `backend/evaluation/api.py` | 路由表 + WSGI | 契约错误 → 状态码的唯一映射处 |
| `backend/evaluation/openapi.py` | OpenAPI 文档 | 由路由表生成，双向漂移检测 |
| `backend/evaluation/cli.py` | CLI 数据契约 | 与 HTTP 共用同一个 service |

依赖方向单向：`api` / `cli` → `service` → 其余。`identity` / `models` / `datasets` 不依赖 IO。

---

## 3. 身份与 scope

身份来自**凭据 + 服务端可信目录**（`TokenDirectory` + `PrincipalDirectory`），由
运维/BFF 在服务启动时注入。客户端能自报的一切都**不参与授权**。

| scope | 打开什么 |
|---|---|
| `evaluation:read` | 读取**自己**名下的任务与结果 |
| `evaluation:submit` | 提交任务 |
| `evaluation:cancel` | 取消**自己**的任务 |
| `evaluation:retry` | 重试**自己**的任务 |
| `evaluation:report:read` | 拿到报告引用（知道"有结果"与"拿到路径"是两次决定） |
| `evaluation:admin:read` | 全局只读（alice 的运维视角） |
| `evaluation:admin:manage` | 代替 owner 取消/重试 |
| `evaluation:dispatch:real` | 允许派发到**真实** runner；**由准入判定强制执行**（本部署不授予任何主体） |

`role`（`platform_admin` / `member`）只用于展示与审计。代码里**不存在**「因为
`role == admin` 所以放行」的分支——`tests/evaluation/test_identity_and_rbac.py`
用一条反证断言固定了这件事。

**自报身份 = 整请求拒绝（400）**，不是"忽略那个多余字段"：

* header：`X-Owner` / `X-Role` / `X-Uid` / `X-Is-Admin` / `X-Project` / `X-Forwarded-User` …；
* body 字段：`owner` / `uid` / `role` / `roles` / `is_admin` / `project` / `scopes` …；
* 查询串：同上（一个查询参数里的 `project` 是一次伪造尝试）。

---

## 4. 可见性规则

| 情形 | 结果 |
|---|---|
| 持有 `evaluation:admin:read` / `evaluation:admin:manage` | 全局可见 |
| `job.owner == 主体` **且** 主体是该任务所属项目的成员 | 可见 |
| 其余（包括同项目的同伴、别人的项目、不存在的 id） | **404** |

「不存在」与「无权知道它存在」**共用 404**，且响应**逐字节同形**——否则 `GET /jobs/{id}`
就是一个可以逐 id 枚举他人评测任务存在性的预言机。这里的"同形"比"状态码相同"强：
两条路径必须给出同一个 `contract_code` **和**同一个 `message`，因此它们共用
`authz.job_not_available()` 这一个来源，而不是各自措辞。任务 id 也不是不可猜的
（幂等提交的 id 是 `sha256(owner ‖ project ‖ key)[:24]`），所以这条不是理论问题。
`GET` 与 `POST`（cancel / retry）四条路径都有"响应体**全等**"的回归断言。

写操作同理：

| 情形 | 结果 |
|---|---|
| 看不见任务 | 404 |
| 看得见、不是 owner、无 `admin:manage`（只可能是 `admin:read` 持有者） | 403 |
| 是 owner 但缺 `cancel` / `retry` scope | 403 |

**项目归属由服务端目录决定**（`ProjectDirectory` 的 `home_projects`），不是请求字段。
没有归属项目的主体 → 404（不凭空造一个项目，让提交落进谁也看不见的地方）。

---

## 5. 提交的顺序（这就是安全设计）

1. **scope**：缺 `evaluation:submit` 即拒。
2. **请求形状**：未知字段、身份字段、禁止字段（命令/路径/地址/正文）全部拒绝。
3. **规格**：`build_eval_spec` 把请求压成允许清单内的一份完整参数集，或拒绝。
4. **目标授权**：deployment 必须存在、就绪、且对调用者授权。未授权与不存在共用 404。
5. **压测**：对**他人共享**的目标，`throughput` / `latency` 默认拒绝（403）。
6. **幂等重放**：同 `(owner, project, key)` 且**同内容** → 返回原任务，不消耗配额；
   同键**不同内容** → 409（静默返回旧任务会让调用方以为评了 B 而实际拿到 A）。
7. **配额**：滑动窗口，先查后记。超限 → 429 + `Retry-After`（按最早事件精确计算）。
8. **准入**：真实派发必须全体条件成立。本部署一条都不成立 → `dispatch_state=refused`
   并记录原因；`synthetic=true`。
9. **审计**：只记 metadata（`detail` 的键走白名单，正文类字段进不去）。

---

## 6. HTTP 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/evaluation/health` | 适配器 / 准入 / 报告根状态（**不需要凭据**，`scope.principal = null`） |
| GET | `/api/evaluation/openapi.json` | 本契约的 OpenAPI 文档 |
| GET | `/api/evaluation/datasets` | 评测类型、允许清单、参数范围、安全预设 |
| GET | `/api/evaluation/admission` | 真实派发此刻为何被允许/被禁止 |
| GET | `/api/evaluation/modules` | V6 评测中心的入口授权（控制面） |
| GET | `/api/evaluation/monitoring` | 监控面聚合 |
| GET | `/api/evaluation/jobs` | 列出可见任务（`status` / `limit` / `offset`） |
| POST | `/api/evaluation/jobs` | 提交任务 |
| GET | `/api/evaluation/jobs/{id}` | 任务状态 |
| GET | `/api/evaluation/jobs/{id}/result` | 结果摘要与报告引用（`version` 可选，缺省为最新） |
| POST | `/api/evaluation/jobs/{id}/cancel` | 取消（幂等） |
| POST | `/api/evaluation/jobs/{id}/retry` | 重试（幂等） |
| POST | `/api/evaluation/jobs/{id}/mock-run` | **开发专用**：mock 产出一份合成结果 |

查询参数的取值域**由命中的那条路由决定**（`route.query_params`），不是一个全局集合：
这样"哪些参数有意义"、OpenAPI 文档与处理函数读的键三者不可能漂移。没被该路由声明的
参数会被忽略，但**不会**被假装采纳——`?version=99` 打在没声明 `version` 的路由上时，
不会悄悄返回最新版本。

`mock-run` 带 `x-dev-only`，**默认不注册**（`create_app(enable_mock_routes=False)`）；
只有回环预览打开它。生产装配不应打开。

错误映射（唯一真源：`api.CONTRACT_STATUS`）：400 非法/自报身份/报告路径 · 401 凭据问题 ·
403 不允许 · 404 不存在或无权知道 · 405 方法不对 · 409 状态冲突 · 429 配额 ·
501 未接线 · 503 runner 不可用 · 500 其他。`StorageError` **不外发消息**（含库路径与
schema 细节）。

---

## 7. 状态机

```
queued ──▶ running ──▶ completed
   │          │      └▶ failed
   └──────────┴──────▶ canceled
```

`completed` / `failed` / `canceled` 是**终态，永不离开**。非法迁移 → 409。

| 动作 | 规则 |
|---|---|
| **取消** | 活跃任务 → `canceled`。再次取消 → 200 + `already_canceled=true`（幂等）。终态（非 canceled）→ 409 |
| **重试** | 仅 `failed` / `canceled` 可重试，产生**新任务**（`attempt+1`、`retry_of`、原任务保留）。重复重试 → 同一个新任务（幂等键 `{原 id}#retry#{n}`） |

**重试会按目录的当前状态重新授权**，这一步容易漏掉，而漏掉意味着一次被撤回的共享
仍然能继续生成新任务。因此重试会重新解析目标、重新判定授权与就绪、并重新检查压测许可，
快照也按**现在**的状态重建。

与之相对，**已经存在的任务不受目录变化影响**：它保留提交那一刻的授权快照
（谁的目标、什么权限），因为报告的可复现性取决于当时的事实。撤回共享会阻止**新**任务，
但不会改写历史。（`tests/evaluation/test_store_and_lifecycle.py` 对三种撤回情形各有一条回归。）

> 取消是**带条件写入**（`WHERE status = ?`），不是读-改-写。两个并发的取消请求中，
> 只有一个能把状态改掉，另一个走幂等分支。这一点由真实线程的测试固定
> （`ConcurrentTransitionTests`），而不是靠"应该没问题"。

`dispatch_state` 与任务状态**分开**：任务可以因策略原因一直停在 `queued`，而
`dispatch_state` 说明"到底有没有真的送去执行过"（`not_dispatched` / `dispatched` / `refused`）。

---

## 8. DTO 契约（V6 UI / Swagger）

顶层信封：`schema_version` / `mode` / `source` / **`synthetic`** / `data` / `scope` /
`notes` / `display_hints`。`synthetic` **没有默认值**——合成数据必须在每一份响应里自我声明。

`JobStatus`：`job_id`(`evj-…`) / `owner` / `project_id` / `status` + `status_label`
（排队中·运行中·已完成·失败·已取消）/ `terminal`·`active`·`cancellable`·`retryable` /
`synthetic` / `runner_kind` / `dispatch` / `target` / `result` / 时间戳 / `failure`。

`ResultSummary`：`available` / `synthetic` / `version` / `metrics` / `report` /
`reproducibility`（含 `params_hash`，可复现）/ `empty_state`。

两条硬约束，直接对应 `prototypes/v6` 评测中心里的两句话：

**「这不是评测失败，也不代表已有模型得分为 0。」**
→ 无结果时给 `empty_state`，其 `value` 是 `null`；指标 `state != known` 时 `value`
必须是 `null`。`dto.validate_dto()` 在**每次**构建时自检这两件事，并拒绝任何
正文类键（`prompt` / `content` / `image` / `usage` …）出现在 DTO 里。

**「未开放时这里说明原因，而不是给一个点了没反应的按钮。」**
→ `GET /modules` 的每一项能力都带 `state` + `reason`：

```json
{"name": "evalscope.native_ui", "state": "disabled",
 "reason": "preview_build_has_no_real_backend",
 "note": "原生 EvalScope 界面未开放：子路径代理与身份边界尚未接线。"}
```

状态取值域 `available` / `unavailable` / `disabled` / `unknown` 与原因措辞沿用
`docs/portal-modules-contract.md`（`portal-modules/1`），前端可用同一套渲染逻辑。

报告引用块永远带 `content_read: false` 与 `content_read_reason`：**内容不经这个面出去**
是一个写在 DTO 里的决定，不是前端要猜的事。

---

## 9. 准入（GPU）与 runner

准入是**合取**，任一条不成立就拒绝派发：管理员开关 ∧ **该主体持有
`evaluation:dispatch:real`** ∧ 真实 runner 可用 ∧ 版本已知且受支持 ∧ 在维护时间窗内 ∧
显存事实**被观测过** ∧ 有足够空闲显存。

其中主体级那一条（`evaluation:dispatch:real`）是**实现**而不只是文档：默认
`dispatch_scope_granted=False`，因此一个没声明自己持有它的调用者不会被当作持有它。
本部署不授予任何主体该 scope，所以即使运维打开了部署开关，仍然需要一次针对具体主体的
授权决定——而不是"开关一开就人人可派发"。

`GpuFacts` 在构造时强制两个方向：`observed=false` 时 `free_mib` / `total_mib` 必须是
`None`（"没测到"与"等于 0"必须在类型层面分开），而 `observed=true` 时两者都**必须**
有值——一份"观测到了但什么数都没有"的事实是自相矛盾的，留着它会开一个洞：准入里
"显存够不够"那一条会因为 `free_mib is None` 而被跳过。准入处因此**再**判一次。

**runner 层不做任何执行**：

* `MockRunner` —— 唯一能出结果的 runner。`run()` 里 `synthetic=True` 是**常量**；
  `synthetic_only=True`、`is_real=False`。它不读文件、不联网、不加载模型。
* `EvalScopeHttpAdapter` / `EvalScopeCliAdapter` —— **桩**。`run()` 一律
  `RunnerUnavailable`。只回答版本检查与能力检测，且答案来自**注入的事实**，不是真实探测。
  这里没有 HTTP 客户端、没有 `subprocess`、**没有任何命令行字符串**——猜一条命令行是
  本模块唯一能产生的、看起来像进展的错误动作。

两道结构性防线：

1. `runner_facts_from()` 要求 `available and not synthetic_only` —— 一个只产合成样本的
   runner **永远不算**真实 runner；
2. `service.admission_decision()` 会在装配的 runner 不是真实 runner 时，把 runner 事实
   **整体**替换成"没有接线"（`adapter_kind` 与 `adapter_version` 都是 `None`）。
   **即使有人注入了乐观的 runner 事实**，装了 mock 的部署仍然拿不到真实派发
   （`tests/evaluation/test_admission_and_runner.py` 有专门的反证）。

第 2 条里"整体替换"而不是"只改一个布尔量"是有意的：装 mock 的部署**没有**任何真实适配器，
所以它的 kind 与 version 是"不存在"，而不是"mock 自称的 1.0.0"。把 mock 的版本号当作
`adapter_version` 报出去，会让运维以为真有一个 EvalScope 1.0.0 适配器在位。mock 自身的
信息走 `capabilities()["adapter"]`，不挤进"真实适配器"的位置。

`parse_runner_base_url()` 的**绊线**：只接受 http/https + 环回 + 无 userinfo，且
**端口 9000 一律拒绝**（生产 EvalScope）。`tests/evaluation/test_static_guarantees.py`
断言字符串 `9000` 在整个包里只出现在 `runner.py`。

---

## 10. 报告引用

三层：

1. **形状**（纯字符串，不碰文件系统）——拒绝绝对路径、`..`、`.`、空段、反斜杠、URL、
   冒号、控制字符、超长、后缀不在 `{json,jsonl,csv,txt,md}` 内、段首 `-`。**不做任何
   "尽力修正"**：修正路径正是目录穿越得以成立的那一步。
2. **落点**（需要根目录）——`realpath` 之后必须仍在根目录内（挡软链接逃逸），且最终
   文件本身不是软链接。
3. **内容**——`read_bytes()` 一律 `NotConfigured`。本部署没有接线；接线必须先有意地
   改掉这里。

引用的摘要 `reference_sha256` 算的是**规范化后的引用字符串**，不是文件内容（算内容就得
读文件）。DTO 里**不出现**绝对路径——`tests/evaluation/test_reports.py` 对每一个端点断言
响应里不含 `/home/` / `/tmp/` / `/srv/` / `/opt/`。

无报告权限时，区块渲染成「存在但未授权」（`ref: null`, `reason:
report_reference_not_authorized`），而不是 `null` —— UI 才能把"没有报告"与"你没权限看"
分开。（这条是回归测试：DTO 层曾经有一个"顺手补上引用"的回退分支，会绕过授权。）

---

## 11. 测试与验证

```
CUDA_VISIBLE_DEVICES='' nice -n 10 python3 -m unittest discover -s tests/evaluation -t . -v
```

| 模块 | 覆盖 |
|---|---|
| `test_identity_and_rbac.py` | 凭据/自报身份/跨项目可见性/管理员/取消授权/**不存在与无权知道响应体全等**（含「任务描述中的账号名在本机不存在」的断言） |
| `test_datasets_and_params.py` | 允许清单、参数范围、预设固定性、四类禁止字段、共享目标压测分离 |
| `test_store_and_lifecycle.py` | 状态机、幂等（提交/取消/重试）、**并发取消恰好一次迁移**、配额与时间窗、页大小上限、结果版本追加、审计白名单、迁移拒绝、重试不接受字段 |
| `test_admission_and_runner.py` | 逐道准入闸门、mock 结构性合成、适配器桩、`:9000` 绊线、版本门 |
| `test_reports.py` | 路径形状/软链接逃逸/**同前缀兄弟目录**/根目录为 `/` 的拒绝/内容关闭/DTO 无绝对路径 |
| `test_dto_contract.py` | 信封、空态非 0、状态标签一致性、模块门、OpenAPI 双向漂移 |
| `test_http_integration.py` | **真实 socket**：状态码、header、405/400/409、预览 Host 校验 |
| `test_cli_contract.py` | 命令集、退出码、参数保守解析、CLI 不是授权旁路、凭据不回显 |
| `test_static_guarantees.py` | `ast` 扫描：无网络/子进程/GPU 库/写盘、无跨包 import、无真实路径 |

测试自身的三条约束：所有进程以 `CUDA_VISIBLE_DEVICES=''` 运行；唯一的真实 socket 是本机
回环上的一次 `wsgiref`；**不读**任何真实报告、提示词、图像或用量日志（由
`test_static_guarantees.TestSuiteHygieneTests` 以 AST 扫描保证）。

---

## 12. 本构建明确不做的事

* **不实现 EvalScope**：不猜命令行、不发 HTTP、不调用生产 `:9000`。
* **不派发真实评测**：GPU 已满；准入默认全关。
* **不加载模型、不压测、不读 GPU**：`admission` 只消费注入的事实。
* **不读报告内容**：`read_bytes()` 关闭。
* **不接生产部署**：没有 systemd / Caddy / 端口监听；预览只绑环回。
* **不迁移模型与引擎属主**、**不改** `backend/registry/**`、`backend/auth_linux/**`、
  `backend/observability/**`、`web/**`、`dev/preview_auth.py`。
* **不写 Claude memory**、不碰生产 `/srv`、`/opt`、`.ssh` 或任何凭据。

---

## 13. 接线待办（下一步，不在本轮）

| 待办 | 前决条件 | 失败时的行为（fail closed） |
|---|---|---|
| 主体/项目目录 | BFF 把 `backend.auth_linux` 的受信主体接进来 | 目录为空 → 每个提交 404 |
| 目标目录 | `backend.registry` 的 deployment 目录 | 目录为空 → 每个提交 404 |
| GPU 只读事实 | 运维提供只读探测来源 | `not_configured` → 准入拒绝 |
| 真实 runner | EvalScope 版本与能力事实 + 维护时间窗 + 管理员开启 | `RunnerUnavailable` → 不派发 |
| 报告根目录 | 只读挂载点 + 保留 `read_bytes` 关闭 | 引用为 `not_configured` |
| `portal:evaluation:native` 入口 | 子路径代理与身份边界验证 | `disabled` + 原因 |
| EvalScope 真实数据集映射 | 允许清单 → 真实 slug 的映射表 | 未映射的真实基准名 = 不在清单里 → 400 |

**`identity` 是与 `backend.registry.identity` 同一份契约的两份实现**：本包的上游基线
`main` 里还没有 `backend` 包，跨包 import 会让"registry 尚未合并"变成"评测中心无法启动"。
合并时应由其中一份成为真源，另一份改为 import；`RequesterContext.from_foreign()` 已经是
鸭子类型适配器，接线不需要转换层（`tests/evaluation/test_static_guarantees.py` 断言了
本包**不**import `backend.registry` / `backend.auth_linux`）。
