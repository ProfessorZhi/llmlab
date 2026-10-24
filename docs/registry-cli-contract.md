# Registry 与 llmlab CLI 契约

## 0. 一句话

用户在**自己的 Linux 账户**下跑 engine，用 `llmlab` 把 engine / model / deployment /
endpoint 登记进平台；登记、验证、共享是三件事；共享默认私有，只有显式授权过的
主体才看得见、才调得动；**一个 Endpoint 的 alias（`route_path`）是稳定的**，
换模型换的是它背后的 deployment —— 而**权重文件始终留在 owner 的 HOME 里、
Registry 从不打开它**。

一句话的边界：**重指向只改 Registry 这边的指向，它不会自动让 Gateway 生效**。
所以换完之后状态是 `pending_router_sync`，不是「已切换」。

## 1. 这是什么、不是什么

**是**：一个可实际跑通的**单节点、离线/回环、非生产**的 Registry REST API + CLI。
它有真实的 sqlite 持久化（带 schema 版本迁移，当前 v2）、行级 ACL、真实的
loopback 健康探测与协议验证、稳定 alias 的重指向、以及一个 364 条断言的测试套件。

**不是**：

| 不是 | 说明 |
|---|---|
| 生产部署 | 本仓库处于初始化阶段，不含常驻服务、systemd 单元、CI/CD。见 `docs/access-boundaries.md` §6 |
| Kubernetes / GPU 编排 | 没有资源池、没有抢占、没有调度器 |
| 模型文件管理器 | Registry **不**扫描、不读取、不迁移、不复制任何权重文件（§6.2） |
| Gateway 本体 | 不签发 per-user key、不写生产 Gateway 配置；只产出一份**只读**路由契约（§10） |
| 一个 UI | 本任务不做前端 |

## 2. 模块布局

```
backend/registry/
├── errors.py        契约错误（只有 contract_code，不认识 HTTP）
├── identity.py      受信目录：token -> principal/uid/scope；拒绝自报身份
├── authz.py         行级 ACL —— 可见性**只在这里**判定
├── models.py        Engine / Model / Deployment / Endpoint / EndpointRouting 与状态取值域
├── health.py        两根轴：传输可达 与 **协议是模型服务**；探测替身、聚合、UNKNOWN 的纪律
├── ownership.py     「这个引擎是谁的」：运维策略（谁能被登记）+ 所有权证明契约
├── urls.py          engine 目标 URL 的 loopback allowlist（SSRF 防线）+ 运维承认条目
├── weights.py       权重路径的字符串校验与打码（纯函数，不碰文件系统）
├── store.py         sqlite 存取与迁移（不含任何授权判断）；重指向的原子事务在这里
├── registry.py      用例层：把存储、授权、健康、所有权、网关接起来
├── gateway.py       只读路由契约 + 可注入 adapter（默认「没接」）
├── dto.py           对外 DTO 的形状（合成标记、null/unknown 词汇）
├── api.py           WSGI 传输层：路由、认证、错误码映射
├── devserver.py     临时 loopback 开发服务（前台进程，非常驻）
└── __main__.py      `python3 -m backend.registry`
cli/llmlab/          `llmlab` 命令（client / commands / entry / errors）
tests/registry/      247 条断言，全部离线（唯一开 socket 的是回环 E2E）
docs/registry-cli-contract.md   本文
pyproject.toml       最小打包文件：让 `llmlab` 可安装（零依赖）
```

分层是刻意的：`store.py` 不做授权，`authz.py` 不认识 SQL，`registry.py` 是唯一同时
认识「一行数据」与「谁能看这行」的地方。把授权塞进 SQL 的 `WHERE` 里（「顺手加个
`owner = ?`」）很容易在某条新查询上漏掉，从而静默泄漏行级数据 —— 所以可见性判定
**只有一处**，且它在 Python 里、对每一行都跑。

## 3. 三个动词：注册、验证、共享

平台简化设计里说「注册 != 启动 != 共享」。在这份契约里这不是一句话，是三个字段
加三条判定：

| 动词 | 结论字段 | 含义 |
|---|---|---|
| **注册** | `registered: true` | 元数据进了库。仅此而已 |
| **验证** | `verified` / `ready: true` | 一次**真实的**同主机 loopback 探测成功过 |
| **共享** | `visibility` + `shareable` | owner 明确授权给某些人或全部已审批用户 |

链式的强制顺序（每一级都校验上一级**真的**验证过，不靠用户声明）：

```
engine 探测通过 → engine.status = verified
                     ↓
deployment.verify 要求 engine 已 verified → deployment.status = ready
                     ↓
endpoint 建出来即探测 → endpoint.status = ready → 才允许 share / invoke
```

**未验证的 Endpoint 不可共享、不可调用**（`409 registry.not_verified`）。
没有配置任何观测源时，`verify` 会如实返回 `NOT_CONFIGURED` 并把状态**留在**
`registered` —— 它不会因为「用户说跑起来了」就变成 verified。

## 4. 身份与授权

### 4.1 owner 只能由凭据推出

`token -> principal / uid / linux_user / state` 来自服务端**受信目录**
（`identity.TokenDirectory` + `PrincipalDirectory`，进程启动时注入）。
客户端能自报的一切**都不参与授权**，而且这里选择**直接拒绝**而不是静默忽略：

* 请求头 `X-Owner` / `X-Uid` / `X-Role` / `X-Principal` / `X-Forwarded-User` … → `400 registry.identity_assertion_rejected`
* 请求体字段 `owner` / `uid` / `principal` / `role` / `is_admin` / `linux_user` … → 同上

静默忽略会让一次伪造尝试在日志里消失，用户也无从知道自己用错了客户端。

（唯一的例外：`invoke` 的**模型载荷**不做字段级检查 —— 那是要原样转给模型的
请求，里面出现 `role`（对话消息的字段）是正常的。身份在那条路径上本来也不参与判定。）

### 4.2 scope 命名空间

一律 `registry:` 前缀。每个动作要求**它自己**的 scope，互不代偿：

| scope | 授权的是什么 |
|---|---|
| `registry:read` | 读取自己名下的（以及被共享给自己的）资源 |
| `registry:write` | 登记/修改/验证自己名下的资源 |
| `registry:endpoint:share` | 把**自己**的 Endpoint 共享出去 |
| `registry:endpoint:invoke` | 调用**别人共享出来**的 Endpoint |
| `registry:admin:read` | 跨 owner 全局只读 |
| `registry:admin:manage` | 可授权管理：代替 owner 授予/收回共享 |

刻意分开的两处：`write` 与 `endpoint:share` 分开（写自己的元数据 vs 把服务开放给
别人，是两种不同的授权决定）；`admin:read` 与 `admin:manage` 分开（能看全局不等于
能改全局）。

### 4.3 为什么 403 与 404 必须分开

* **不可见** = `404 registry.not_found`。它与「根本不存在」**逐字节同形**
  （同一个 code、同一个句式，只有回显调用方自己传进来的那个 id 不同）。
  两者共用一个错误是**必须的**：一旦分开，端点就成了一个可以逐 id 枚举他人资源
  存在性的预言机。回归证据：`test_the_refusal_does_not_reveal_which_of_several_ids_exist`。
* **可见但无权操作** = `403 registry.access_denied`。例如受托人能看见（也就能调用）
  一个 Endpoint，但不能改它的 ACL、不能重新共享给别人。

因此判定顺序本身就是安全边界：先算可见性，再谈动作。

### 4.4 role 不产生任何权限

`roles` 只用于展示与审计。测试里有一条直接反证：
`test_a_role_of_admin_grants_nothing_by_itself` —— 一个 `roles={"admin"}` 但只有
`registry:read` 的主体，登记请求得到 `403`。

## 5. 共享语义

| 模式 | 谁看得见 |
|---|---|
| `private`（**默认**） | 只有 owner |
| `users` | owner + 显式列出的受托人 |
| `all_approved_platform_users` | owner + 受信目录里 `state=approved` 的全部主体 |

关键性质：

* **新建永远是 private。** `visibility` 字段在创建时被**拒绝**（走 share/unshare），
  这样就不会出现「创建一个开放的 Endpoint」这条路径。
* **受托人必须来自受信目录且已审批**，且不能是自己。
* **撤销是删除行，不是打标。** 留一行「已撤销」意味着每条读权限的查询都必须记得
  过滤它 —— 漏一次就是一次静默越权。没有行就没有权限，这是结构上的保证；
  撤销这件事由 `audit_log` 留痕（含 `grant` / `revoke` 逐条事件）。
* **撤销立即生效**：受托人在撤销后的**下一次**请求就已经看不见、也调不动。
* **不传递**：受托人不能把别人共享给他的 Endpoint 再共享出去（`403`）。
* **替换是原子的**：`share` 会用新集合整体替换旧集合，`share_endpoint` 的 `meta`
  里给出 `granted` / `revoked` 增量。
* 共享传递的是**服务**，不是 owner 的家目录：非 owner 看到的 engine 目标地址是
  **打码的**，model 的权重路径是**哈希指纹**（§6.2）。

## 6. 安全边界

### 6.1 engine 目标 URL：同主机 loopback allowlist（SSRF 防线）

接受的形态只有一种：`http://` + 字面 loopback + 端口在 allowlist 内。
实现是**严格解析后判定**（`urls.validate_engine_target_url`），不是字符串前缀匹配 ——
前缀匹配会放行 `http://127.0.0.1.evil.com/`、`http://2130706433/`、
`http://127.0.0.1@evil.com/` 这几种写法。

拒绝清单（每一条都是一种真实的绕过手法，见 `test_ssrf_and_weights.py`）：

* 非 http(s)：`file://`（读本机文件系统）、`gopher://`、`ftp://`、`dict://`、`data:`、`unix://` …
* 外部主机与云元数据：`example.com`、`169.254.169.254`、`10.0.0.1`、`[fe80::1]`
* 绑定意图冒充地址：`0.0.0.0`、`[::]`
* 同一个 IP 的其它写法：`2130706433`、`0x7f000001`、`0177.0.0.1`
* userinfo 混淆：`user:pass@127.0.0.1`、`127.0.0.1:8000@evil.com`
* 名字长得像 loopback：`localhost.evil.com`、`127.0.0.1.evil.com`、`localhost.`
* 端口：allowlist 之外的（含 `22` / `80` / `443` / `3306`）与上游保留端口 `8080`
* 路径穿越：`..`、`%2e%2e`、反斜杠
* 形状问题：控制字符、空白、超长、缺 host

`localhost` 会被**归一化成 `127.0.0.1` 字面量**：连接路径上就不存在任何一次 DNS
解析，也就不存在解析结果被改写的可能（DNS rebinding）。

### 6.2 权重路径：受限登记引用

Registry 对权重路径只做三件事：**存下来、给 owner 看、按 owner 的意愿共享那个
Endpoint**。它**不** `stat`、**不**打开、**不**哈希内容、**不** `glob`、**不**复制、
**不**建软链、**不**把它交给 shell。

这不是「暂时没做」而是设计边界：权重文件在 owner 的 HOME 里，Registry 进程未必有
权限，也不该有。副作用正是想要的 —— **远端用户调用一个共享 Endpoint 时不需要读
权限**，文件权限完全留在 owner 与 engine 之间。

回归证据是一条**行为性**断言：`test_registering_a_nonexistent_weight_path_succeeds_without_any_fs_call`
把 `os.stat` / `os.lstat` / `os.path.exists` / `os.path.isdir` / `os.path.isfile` /
`glob.glob` / `os.scandir` / `builtins.open` 全部换成会抛异常的桩，然后注册一个指向
**确实不存在**的文件的路径 —— 必须成功。这证明的是「根本没有那条代码路径」，
而不是「这次没读」。

校验规则（纯字符串）：必须是绝对路径、无 `..` 段、无控制字符与 NUL、无 shell
元字符与空白。展示规则：非 owner 只得到 sha256 前 16 位指纹，路径本身不出现在响应里
（`/home/<user>/...` 本身就泄漏了「这台机器上有哪些用户、谁在跑什么」）。

`weight_present` **永远**是 `null`：Registry 没看过那个文件，所以「在不在」是
**未观测**，不是 `false`。

### 6.3 不自动暴露 `0.0.0.0:8080`

两处动作：

* `0.0.0.0` 被显式拒绝（它不是一个「地址」，是一个把服务暴露给整个内网的**绑定意图**）；
* `8080` 在端口 allowlist 里被**保留**（`RESERVED_UPSTREAM_PORTS`），即使运维显式
  把它加进 allowlist 也会先被拒 —— 与既有 `backend/portal/modules.py` 的
  `UPSTREAM_PORTS` 同向，避免两处约定漂移。用户自己的 engine 应当绑定高位端口。

**8080 有一个、且只有一个例外**（v2 新增，见 §18）：一条来自
**root/运维拥有的策略文件**、且**精确匹配** `(scheme, host, port, path_prefix)` 的
承认条目，配合 `engine_mode="system_existing"` 与 `registry:admin:manage` scope。
这条通路的存在理由是一个具体的事实：既有的上游推理服务（Strata）就在
`127.0.0.1:8080`。用户模式**永远**拿不到这个例外 —— 见
`test_a_plain_user_cannot_reach_8080_even_with_a_blessing_in_place`。

### 6.4 传输层：长度可信才读、明文不带凭据出本机

* 请求体只在 `Content-Length` **被可信地声明**时才读；`Transfer-Encoding`（含 chunked）
  一律 `411`。空字符串 `Content-Length` 视为「没有声明长度」（`wsgiref` 就是这么填的）。
* scheme 只取自 `wsgi.url_scheme`；`X-Forwarded-Proto` / `Forwarded` **完全被忽略**。
* 响应一律 `Cache-Control: no-store` + `X-Content-Type-Options: nosniff`（元数据是租户相关的）。
* CLI 侧：明文 `http://` **只允许连 loopback**；要连别的机器必须 `https://`。
  否则一次 `--api-base http://{{INTERNAL_HOST}}` 就会把 token 明文送进内网。
* `StorageError` 的消息**不外发**（里面有库路径与 schema 细节），只给 code 与 request id；
  打开 `debug_errors` 也不会让这类消息复活 —— 脱敏是错误类型自身的属性，不是运行模式的属性。

## 7. REST API 契约

前缀 `/api/v2/registry`。

### 7.1 路由表

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | **唯一**免认证的存活探针；不含任何租户数据 |
| GET | `/whoami` | 身份、scope、能力、可见计数 |
| GET | `/status` | 服务与各来源（观测源 / 网关 / 用量管道）的可用性 |
| GET | `/engines` · POST `/engines` | 列表 / 登记 |
| GET | `/engines/{id}` · POST `/engines/{id}/verify` | 单个 / 触发验证 |
| POST | `/engines/preflight` | **只读**登记预检（不写库、不写审计；需 `registry:write`），见 §18.3.1 |
| GET | `/models` · POST `/models` | 列表 / 登记 |
| GET | `/models/{id}` | 单个（模型没有探测路径，故无 verify） |
| GET | `/deployments` · POST `/deployments` | 列表 / 登记 |
| GET | `/deployments/{id}` · POST `/deployments/{id}/verify` | 单个 / 验证 |
| GET | `/endpoints` · POST `/endpoints` | 列表 / 登记（永远 private） |
| GET | `/endpoints/{id}` | 单个 |
| GET | `/endpoints/{id}/acl` | 授权名单 + 历史（**仅 owner 与管理员**） |
| GET | `/endpoints/{id}/routings` | 只追加的路由历史（从哪个模型换到哪个） |
| POST | `/endpoints/{id}/verify` | 验证 |
| POST | `/endpoints/{id}/share` | 共享（`mode` = `private`/`users`/`all_approved_platform_users`） |
| POST | `/endpoints/{id}/unshare` | 撤销（给 `grantee` 只撤一个；不给则全部收回） |
| POST | `/endpoints/{id}/repoint` | **把稳定 alias 指向另一个已就绪的 deployment**（§17） |
| POST | `/endpoints/{id}/rollback` | 回到上一个 deployment，走**同一套**检查（§17.6） |
| POST | `/endpoints/{id}/routing-applied` | 运维回执：路由器已按第 N 代配置生效（需 `admin:manage`）（§17.4） |
| POST | `/endpoints/{id}/invoke` | 经授权网关调用 |
| GET | `/usage/mine` | 自己的用量条目（未接入时 `NOT_CONFIGURED`） |
| GET | `/audit` | 审计（管理员看全部，普通主体只看自己） |
| GET | `/routing-config` | 只读路由契约（需 `registry:admin:read`） |
| GET | `/routing-snapshot` | 写给 Gateway 的 `llmlab.endpoint-routing/1` 快照（需 `registry:admin:read`）（§21） |

`acl` 是**读**（GET），其余动作是写（POST）；路径存在但方法不对给 `405`。

### 7.2 响应封套

成功：

```json
{
  "ok": true,
  "schema_version": "registry/1",
  "mode": "synthetic",
  "source": "registry_store",
  "synthetic": true,
  "data": { "...": "..." },
  "scope": { "principal": "bob", "uid": 1002, "scopes": ["..."], "roles": [], "is_admin_read": false },
  "notes": ["..."],
  "display_hints": ["SYNTHETIC: 本部署非生产，数据不代表真实运行状态。"]
}
```

失败：

```json
{"ok": false, "error": {"code": "registry.not_found", "message": "endpoint 'ep_x' not found"}}
```

`scope` 块原样回报本次的授权快照（含 `roles`），便于审计与前端如实展示。

### 7.3 错误码 → 状态码

映射**只**存在于 `api.CONTRACT_STATUS`（契约层不认识 HTTP，同 `backend.atlas.errors`
的纪律）：

| code | HTTP |
|---|---|
| `registry.invalid_request` / `registry.identity_assertion_rejected` | 400 |
| `registry.unauthenticated` | 401 |
| `registry.access_denied` | 403 |
| `registry.not_found` | 404 |
| `registry.method_not_allowed` | 405 |
| `registry.conflict` / `registry.not_verified` | 409 |
| `registry.revision_conflict` | 409 |
| `registry.ownership_unverified` | 409 |
| `registry.protocol_unverified` | 409 |
| `registry.length_required` | 411 |
| `registry.payload_too_large` | 413 |
| `registry.unsupported_media_type` | 415 |
| `registry.error` / `registry.internal_error` / `registry.storage_error` | 500 |
| `registry.upstream_unavailable` | 503 |
| `registry.service_offline` | 503 |

### 7.4 为什么传输层也用 `registry.*` 命名空间

「没有 token」与「token 无效」都答 `registry.unauthenticated`；路由层自己的错误
（405/411/413/415）也走同一命名空间。调用方不该需要知道一个 401 是「router 发现
没有头」还是「service 发现 token 无效」才能分支 —— 那是实现细节，不是契约。
有一条测试断言映射表覆盖了契约层声明的**每一个** code，防止出现永远发不出的状态。

### 7.5 幂等语义

与主流 API 相同的那一种：

* 同一个 `Idempotency-Key` + 同一份内容 → **重放**：返回原来那条，`idempotent_replay: true`，不新建；
* 同一个 `Idempotency-Key` + 不同内容 → `409`。悄悄返回旧结果会让调用方以为新请求生效了；
* 没有键、但同名同内容 → 也算重放（对「脚本被跑了两次」这种常见失手友好）；
* 没有键、同名不同内容 → `409`。

键的作用域是**每个主体**的（一个用户的键不遮挡另一个用户）；内容指纹对 JSON 键序不敏感。

## 8. DTO：状态、健康与 UNKNOWN

### 8.1 结论性字段

每份 DTO 都给「注册 / 验证 / 共享」各自的结论，而不是一个混合的 `status`：

* Engine：`status` ∈ `registered` / `verified` / `unavailable`，外加 `registered: bool`、`verified: bool`、`verification.status` ∈ `VERIFIED` / `UNVERIFIED` / `FAILED` / `NOT_CONFIGURED`
* Deployment：`status` ∈ `registered` / `ready` / `unavailable`，外加 `registered` / `ready` 两个布尔与 `readiness.note`
* Endpoint：`status` ∈ `registered` / `ready` / `unavailable`，外加 `registered` / `ready` / `shareable`
* Model：只有 `registered` —— 它没有探测路径，这一点是设计不是遗漏

### 8.2 健康词汇表

与 `backend/portal/modules.py` 保持同一套词汇：

| 取值 | 含义 |
|---|---|
| `healthy` | 探测到了，且干净通过 |
| `degraded` | 探测到了，但应答不对（例如 5xx） |
| `unreachable` | 探测没到达 |
| `unknown` | **没有观测过** |

`health.source` ∈ `loopback_probe` / `none`；`reason` 是稳定 token
（`probe_succeeded` / `probe_failed` / `never_probed` / `not_configured_in_this_deployment` /
`weight_file_is_a_reference_never_opened_by_registry`）。

聚合（`aggregate_health`）的规矩：`unknown` **不参与取最坏值**（它是「没有数据」，
不是一个比较级）；「一个健康 + 一个不可达」折成 `degraded`（一部分活着 ≠ 全挂）。

### 8.3 绝不伪造

* 不可达的探测**不能**带延迟 —— `ProbeResult` 与 `HealthObservation` 在构造时就拒绝
  「不可达 + 有 latency」这种组合（`InvalidRequest`）。这条纪律在**类型层**而不是
  在文档里，因为那是最容易在重构中流失的一处。
* 未观测的指标是 `value: null` + `state: "unknown"` + `source: null`，
  **不是** `0`。「没有可信来源，就是 null + unknown 而不是 0」。
* 有来源的数字必须声明来源（`health.metric()` 会拒绝「measured 但没 source」）。
* `MockGatewayAdapter` 显式标注 `synthetic: true` 并在 `notes` 里写明「这不是真实
  推理结果，也不是可用性证据」。**一个 mock 的答复永远不是「图像/推理可用」的证据。**

## 9. llmlab CLI 契约

### 9.1 命令面

```
llmlab whoami
llmlab status
llmlab engines     list | register --name N --target-url U [--protocol P] [--tag T] | get ID | verify ID
llmlab models      list | register --name N [--weight-path P] [--format F] | get ID
llmlab deployments list | register --name N --engine E --model M | get ID | verify ID
llmlab endpoints   list | register --name N --deployment D | get ID | verify ID
                   | share ID --mode users --grantee G | share ID --mode all-approved
                   | share ID --mode private
                   | unshare ID [--grantee G] | acl ID | routings ID
                   | repoint ID --deployment D [--expected-revision N] [--regrant G]
                   | rollback ID [--expected-revision N] [--regrant G]
                   | routing-applied ID --generation N
llmlab usage       --mine        （别名：llmlab logs --mine）
llmlab invoke      ID [--prompt TEXT | --payload JSON]
llmlab routing     config | snapshot      （只读；需 registry:admin:read，见 §21）
```

`repoint` / `rollback` 省略 `--expected-revision` 时会先读一次再用读到的值；
脚本要精确控制并发时显式传它。**不给 `--regrant` 就表示共享收回为 private** ——
被收回的人不会因为「他本来就有权限」而保留。

全局：`--json`（机器可读；**放在子命令前后都可以**）、`--token` / `--token-file`、
`--api-base`、`--timeout`。

`--json` 输出服务端信封**原样**，不加解释、不补默认值 —— 因此「JSON 里是 null、
表格里是 0」这种分歧在结构上不可能出现。

### 9.2 退出码

退出码是契约的一部分（脚本与 Agent 靠它分支，而不是解析人类文本）：

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 2 | 用法/配置错误（**含没有凭据**：fail closed，一个请求都不发） |
| 3 | 认证/授权失败（401 / 403） |
| 4 | 不存在**或**无权知道其存在（服务端刻意不区分） |
| 5 | 冲突（重复登记、幂等键被复用、未验证不可共享） |
| 6 | 上游不可达 / 未接线（503） |
| 7 | 传输层失败（连不上、超时、响应不是 JSON） |
| 8 | 服务端内部错误（5xx） |

### 9.3 凭据来源与 fail closed

**只**来自 Portal 签发的 token：`--token`、`LLMLAB_TOKEN`，或 `LLMLAB_TOKEN_FILE`
指向的文件。没有就是退出码 2，**且一个网络请求都不发**（有测试用请求计数器证明）。

token 文件必须是：**属主本人**的 `0600` **普通文件**，且**不是软链**。
三条检查各自对应一种真实的失手方式（软链指到别处、权限过宽被别人读走、
把一个目录/设备当文件读）。

CLI **不**读 Linux 账密、**不**调 `sudo`、**不**开远端 shell、**不**回落到
「先试试免密」。token 从不进日志、不进异常消息、不进 `--json` 输出。

`invoke` 只到**授权网关**：服务端先做行级 ACL，再由 Gateway adapter 转发；
CLI 不直连上游推理端口，也不持有 Gateway 的 key。

## 10. Gateway 边界

本模块提供三样东西：

1. **`build_routing_config()` —— 一份只读契约。** 描述「哪些 Endpoint 可以被路由、
   归谁、走哪个上游、对谁开放」。**只包含已 `ready` 且非 `private` 的 Endpoint**：
   未 ready 的上游可能根本不在监听（写进去等于让 Gateway 把流量送进空洞），
   private 的没有「可以路由给谁」的语义（写进去只会让这份文件变成一份
   「谁注册了什么」的清单）。契约里**不含任何凭据**：per-user key 的签发与校验
   属于 Gateway。本仓库不把它写进任何生产 Gateway。
2. **`GatewayAdapter` 协议** —— 真正把一次已授权的调用送出去的那个洞。三个实现，
   缺省永远是 fail-closed 的那一个：
   * `DetachedGatewayAdapter`（**缺省**）：本部署没有接网关，于是诚实地回
     `503 registry.upstream_unavailable` + `gateway_not_attached`。它不伪造 200。
   * `MockGatewayAdapter`（测试替身）：确定性、无网络、记录每次调用，并显式标注合成。
   * `TrustedGatewayAdapter`（**真实但可注入**，见 §21）：把已授权的调用转发给一个
     运维配置的回环 Gateway 基址。它**不**持有上游 key，也**不**判定调用者权限 ——
     它只把**调用者自己的** Gateway key 交出去，由 Gateway 用它自己保存的 key 记录
     独立判定（server-side ACL）。
3. **`build_routing_snapshot()` + `RouterAdapter`**（§21）：与并列仓库 `ProfessorZhi/llm-lab`
   Gateway（PR #20，schema `llmlab.endpoint-routing/1`）逐字段对齐的快照，以及一个
   可注入的「交付快照 + 读回生效代数」的 adapter。缺省 `DetachedRouterAdapter`
   什么都不做，于是缺省行为与「本仓库不写 Gateway」一字不差。

计量的顺序是刻意的：**先记账、再转发**。一次被授权的调用即便上游随后失败，
也已经计下了 —— 否则上游抖动会让计量丢事件。`/usage/mine` 因此把
`registry_recorded_invocations` 报为**有来源的**计数（`source: registry_audit`），
而 token / 延迟 / TTFT 一律 `null + unknown`：那些属于 Gateway 的计量管道，
本部署未接入。

## 11. 本机实测事实

任务描述里有一处需要**查证**而不是转抄的地方，实测结果如下：

| 事实 | 值 |
|---|---|
| `alice` | uid **1000**，基础设施管理员 |
| `bob` | uid **1002**（本会话运行者），组 `users`(100) + `llm-team`(1001) |
| `carol` | uid **1003** |
| `dave` | uid **1004** |
| **任务描述中的账号名** | **本机不存在**（`getent passwd` 无结果） |

因此代码与测试里用的是 `dave`(1004)，而不是任务描述里写的账号名。
把不存在的用户写进夹具，会让「实测」退化成「转抄」。

`devserver.HOST_USERS` 与 `tests/registry/support.py` 的 `HOST_USERS` 共用这份实测值。

## 12. 如何运行

```bash
# 全部测试（离线；唯一的 socket 是回环 E2E，端口由内核分配）
python3 -m unittest discover -s tests/registry -t .

# 单个模块
python3 -m unittest tests.registry.test_cli_over_loopback

# 打印一份合成身份（token 只存在于那个进程的内存里）
python3 -m backend.registry print-identities --json

# 起一个**临时** loopback 开发服务（前台进程；Ctrl-C 结束）
python3 -m backend.registry serve --db /tmp/registry.sqlite --port 8000 \
    --probe-loopback --mock-gateway

# 打开生产姿态（所有权强制 + 协议验证），并加载一份运维策略
python3 -m backend.registry serve --db /tmp/registry.sqlite --port 8000 \
    --probe-loopback --mock-gateway --production-posture \
    --operator-policy /path/to/operator-targets.json

# CLI
export LLMLAB_TOKEN=<上面打印的 token>
python3 -m cli.llmlab --api-base http://127.0.0.1:8000 whoami
python3 -m cli.llmlab --api-base http://127.0.0.1:8000 engines register \
    --name my-engine --target-url http://127.0.0.1:9000/v1
python3 -m cli.llmlab --api-base http://127.0.0.1:8000 endpoints list --json
python3 -m cli.llmlab --api-base http://127.0.0.1:8000 routing snapshot --json

# 真实受信 Gateway 接线（全部本地；缺任何一个参数都拒绝启动，不退化成「没接」）
install -m 0600 /dev/stdin /tmp/llmlab-gateway-keys.json <<'JSON'
{"bob": "k_...", "dave": "k_..."}
JSON
python3 -m backend.registry serve --db /tmp/registry.sqlite --port 8000 \
    --probe-loopback \
    --trusted-gateway http://127.0.0.1:8200 \
    --gateway-keys /tmp/llmlab-gateway-keys.json \
    --router-snapshot /tmp/llmlab-routing.json \
    --router-status /tmp/llmlab-gateway-status.json \
    --engine-secret-ref eng_xxxx=/etc/llmlab/engines.env:ENGINE_BOB_KEY
```

`--probe-loopback` 默认**关闭**：不打开它时，所有健康都是 `UNKNOWN`、
没有任何东西能变成 `verified` —— 那正是「没有观测源」应该有的样子。
`--mock-gateway` 同理（默认未接线，`invoke` 诚实回 503）。

`--trusted-gateway` 只接受**回环 http**，且必须是 `--gateway-keys` 指向的
0600 属主文件；`--router-snapshot` 与 `--router-status` 必须成对出现。任一条不满足
都直接拒绝启动：一个「悄悄没接上」的进程会比一个起不来的进程更危险。
**这两条路径在本仓库只在 CPU-only 测试里被验证过，没有接到任何真实 Gateway。**

**安装（可选）**：`pip install -e .` 会装上 `llmlab` 命令。本机的 Python 没有
`pip` 模块，所以这条路径**没有在本仓库被实际验证过**；`python3 -m cli.llmlab`
是唯一被验证过的入口。

## 13. 安全不变量与回归证据

408 条断言（`python3 -m unittest discover -s tests/registry -t .` → `OK`），
按文件分布：

| 文件 | 条数 | 盯的是 |
|---|---|---|
| `test_identity_and_authz.py` | 62 | owner 由凭据推出、伪造身份被拒、默认私有、共享/撤销、管理员、scope、403 vs 404、不可见 = 404 |
| `test_ssrf_and_weights.py` | 28 | URL allowlist 的 40 种拒绝形态、权重路径校验、**零文件系统调用**的行为证明 |
| `test_health_and_readiness.py` | 33 | UNKNOWN 纪律、不可达无延迟、聚合、注册 != 就绪、不同上游各自保留健康 |
| `test_persistence_and_idempotency.py` | 34 | 迁移版本、**v1→v2 原地升级**、事务原子性、并发存取、幂等四态、授权行的删除语义、审计历史 |
| `test_api_transport.py` | 34 | **穷举**每个路由的认证、405/404、请求体纪律、封套、不泄漏内部细节 |
| `test_gateway_and_dto.py` | 27 | 只读路由契约的排除规则、调用替身、DTO 形状与合成标记 |
| `test_cli_over_loopback.py` | 40 | 真实子进程 + 真实回环 HTTP：fail closed、退出码、完整链、跨用户可见性、**repoint/rollback/routings** |
| `test_endpoint_repoint.py` | 39 | 稳定 alias、CAS/竞态、ACL 不带过去、跨 owner 拒绝、回滚走同一套检查、只追加历史、pending 不谎报 |
| `test_engine_ownership.py` | 40 | 运维策略加载与拒绝、8080 的唯一通路、`system_existing` 权限、所有权强制档 |
| `test_proc_net_ownership.py` | 57 | `/proc/net/tcp(6)` fixture 表解析（IPv4/IPv6/mapped/通配/非 LISTEN）、uid 证明的各失败原因、prover 选择、`/engines/preflight`、本进程回环监听的真实内核冒烟 |
| `test_verification_axes.py` | 27 | 传输 vs 协议两根轴、真回环模拟器的「正确/错误」上游、模型清单解析 |
| `test_trusted_gateway_and_router.py` | 44 | 受信 Gateway adapter 的三条 fail-closed 规则、PR #20 快照形状与校验、落后的代数/sha 被拒、repoint 的 pending↔applied、**A→B 全链与撤权**（§21） |

全仓范围：`python3 -m unittest discover -s tests -t .` → **2728 tests, OK (skipped=6)**，
即 `backend/bff`、atlas、auth、portal 在本次改动后全部仍然通过。

其中值得单独点名的几条：

* `test_every_route_except_the_probe_refuses_an_anonymous_caller` —— 按一份**显式清单**
  穷举全部 29 条路径。新增路由若忘了认证，这条会直接失败。
* `test_no_credential_exits_2_and_sends_nothing` —— 用请求计数器证明「一个请求都没发」。
* `test_registering_a_nonexistent_weight_path_succeeds_without_any_fs_call` —— 行为性证明。
* `test_the_refusal_does_not_reveal_which_of_several_ids_exist` —— 存在性预言机的反证。
* `test_a_role_of_admin_grants_nothing_by_itself` —— role 不产生权限。
* `test_revocation_takes_effect_on_the_very_next_invoke` —— 撤销立即生效。
* `test_a_deployment_without_a_gateway_says_so_instead_of_pretending` —— 没接就说没接。
* `test_two_racing_repoints_produce_exactly_one_winner` —— 两个线程抢同一个 revision，
  结果必然是 `["ok", "registry.revision_conflict"]`，落库 revision 只前进一格。
* `test_repoint_by_default_revokes_the_existing_share` —— 换后端不带旧授权。
* `test_a_plain_user_cannot_reach_8080_even_with_a_blessing_in_place` —— 运维的承认
  对普通用户路径**不**生效。
* `test_a_200_that_is_not_a_model_list_is_unverified` —— 2xx 不再等于「模型就绪」。
* `test_only_allowlisted_paths_are_requested` —— 协议验证只打白名单路径，不是通用探测器。

全部测试**不连接**真实 GPU、生产 Gateway、生产推理服务或真实用户数据。
所有凭据都是 `synthetic-` 前缀、进程内随机生成、不落盘。

## 14. 仍未接线 / 已知缺口

如实列出，不粉饰（v2 新增的缺口在 8–12 条，并见 §18.3 的 BLOCKED）：

0. **路由切换默认仍要运维动手才生效（v3 起改成「可注入」，缺省不变）。** 缺省部署
   仍然是 `DetachedRouterAdapter`：每次 repoint/rollback 都停在 `pending_router_sync`，
   直到运维消费 `routing-config` 并回执（§17.4）。v3 补上了 `RouterAdapter` 与
   `TrustedGatewayAdapter`（§21），但**缺省不配置就仍然什么都不发** —— 本仓库不持有
   Gateway 的凭据、也不改它的配置。要让 repoint 真正生效，需要运维显式配置一个
   RouterAdapter（快照目录 + `engine_secret_refs`）与调用者 key 目录。
1. **用量管道未接入。** `/usage/mine` 顶层是 `NOT_CONFIGURED`；token / 延迟 / TTFT
   永远是 `null + unknown`。唯一有来源的是 Registry 自己记录的**授权调用次数**
   （`source: registry_audit`），它**不等于** Gateway 的全部流量。
2. **默认端口 allowlist 偏宽。** `{8000, 8001, 8081, 9000, 9001}` 只是「常见高位端口」，
   不是从本机实际监听情况推出的。它同时也是「注册我的 engine」与「探测邻居的本机
   服务」之间**唯一**的边界（本机实测确实有其它服务在 `127.0.0.1:9000` 上监听）。
   生产部署应当把它收窄到实际使用的端口，或按部署分配端口段。
   测试里已经把 allowlist 收窄到只含测试自己起的那个端口。
3. **单节点、无 HA、无备份。** sqlite 文件就是全部状态；没有副本、没有迁移脚本的
   回滚路径（迁移只向前）。
4. **没有限流。** 只有请求体上限（256 KiB）与 `invoke` 载荷上限（64 KiB）。
   重复登记由幂等与唯一约束兜住，但**没有**速率限制。
5. **`endpoint.route_path` 的冲突处理是靠唯一索引。** 同一 owner 下重名会被
   `(owner, name)` 唯一索引先挡住；跨 owner 的路径冲突理论不可达（路径里含 owner），
   但真出现时只会得到一个 `409`，没有更聪明的处理。
6. **`windows`/`fields` 之类过滤未实现。** 列表接口一次返回全部可见行；数据量大了
   需要分页。
7. **`portal:endpoints:share` 与 `registry:endpoint:share` 并存。** 既有
   `backend/portal/modules.py` 已有 `EndpointRecord` + `portal:endpoints:share` 的
   Private/Shared 语义。本子系统刻意用**独立命名空间**（`registry:*`）与独立
   schema 版本（`registry/1`），**没有**去改它。把两者折叠是一次显式的集成决定，
   见 §15。
8. **没有 CI。** 本仓库此刻没有任何 CI/CD（`docs/architecture.md` 已记录）。
9. **所有权自动证明是可选的，且真实主机验证待做。** 见 §18.3：
   `ProcNetOwnershipProver` 读全员可读的 `/proc/net/tcp(6)` 的 uid 列（不需要 root），
   只经 fixture 单测与本进程回环监听的冒烟验证；缺省仍是 fail-closed 的 `NoOwnershipProver`。
10. **`ownership_policy` / `require_protocol_verification` 默认关着**（§19.4）。
    生产必须用 `RegistryConfig.production_posture()`。
11. **端口归属仍未证明。** `registry:write` 的人依然可以登记**别人**在允许端口上
    跑的服务 —— 运维策略解决的是「承认既有服务」，不是「证明端口属于登记者」。
    收窄措施是端口 allowlist 与（开启后的）所有权强制。
12. **没有进程级生命周期。** 平台不 start/stop/restart 任何引擎，
    也没有 GPU 准入或配额（§20）。`running` 恒为 `null`。

## 15. 与活跃 V5 工作树的集成接口

活跃的 V5 工作树是 `feature/portal-self-usage-logs-20261015`
（`llm-lab-portal-user-logs-20261015`）。本子系统与它**零文件冲突**
（新分支从 `main` 派生，只新增 `backend/registry/**`、`cli/llmlab/**`、
`tests/registry/**`、`docs/registry-cli-contract.md` 与两个最小打包文件）。

### 15.1 Registry 需要 V5 提供什么（唯一的接缝）

Registry 需要的**只有一件事**：一个受信的
`token -> {principal, uid, linux_user, state, scopes}` 解析结果。
它已经按 V5 的形状实现好了对应物：

| Registry（本子系统） | V5 既有对应物 |
|---|---|
| `identity.RequesterContext`（principal/uid/linux_user/scopes/roles） | `atlas.identity.RequesterContext`（subject/roles/scopes/authorized_projects） |
| `identity.TokenDirectory` + `TokenBinding` | `atlas.identity.KeyDirectory` + `GatewayKeyBinding` |
| `identity.PrincipalDirectory` + `Principal` | 目前**没有**对应物（V5 的 subject 来自会话，没有 UID 目录） |
| `authz.AccessPolicy` | `resolve_authorized_scope` / `resolve_own_call_scope` |
| `registry:<x>` scope 命名空间 | `atlas:*` / `portal:*` |

对接方式（按代价从小到大）：

* **A. 进程内直连（最小改动，推荐先做）。** 在 V5 的 BFF 里，用会话解析出的
  `subject` 与 scope，构造 Registry 的 `RequesterContext` 后**直接调用
  `RegistryService` 的方法**（它返回的就是 DTO dict，和 `build_atlas_dto` 一样是
  纯函数式入口）。这条路**不需要** HTTP、不需要 token 目录。
  需要补的只有一件事：V5 侧要有一份 `subject -> uid` 的映射（`PrincipalDirectory`），
  否则 owner 的 UID 无从得出。
* **B. HTTP 桥接。** Registry 以 WSGI 应用挂在同一个进程里（`create_app(service)`），
  BFF 把已认证的请求转发过来。这条路需要 V5 侧把会话身份**降级**成一个
  Registry 的 token（即让 BFF 成为 Registry 的受信目录）。**不要**让浏览器直连
  Registry 的 `:8080`/任何端口。

### 15.2 本次交付里**没有**做的集成

* 没有改 `backend/portal/modules.py` 的 `EndpointRecord` 语义；没有把
  `portal:endpoints:share` 与 `registry:endpoint:share` 折叠。
  这需要一次显式决定（是让 Registry 成为唯一真相，还是让 modules 继续做展示层目录）。
* 没有把 Registry 挂进 `dev/preview_auth.py`；没有改 `web/**`。
* 没有消费 V5 的审计/用量管道（§14.1）。

### 15.3 本次改动对 `backend/bff` 的兼容性（**只增不改**）

`backend/bff/**` 不允许被本次改动碰，所以 Registry 的公开面是**只增**的：

* 保留并仍然导出的名字（`bff` 直接 import 的那些）：`models.VISIBILITY_*`、
  `dto.render_*`、`health.STATE_*`、`identity.FORBIDDEN_IDENTITY_HEADERS` /
  `RequesterContext` / `Principal` / `PrincipalDirectory` / `TokenDirectory`、
  `api.CONTRACT_STATUS` / `create_app`、`errors.*`、`store.Store`、
  `registry.RegistryConfig` / `RegistryService`、`gateway.*`、`health.StaticProber`；
* `RegistryService.__init__` 新增的两个参数（`protocol_prober` /
  `ownership_prover`）都有缺省值，`RegistryConfig` 新增的三个字段也都有缺省值 ——
  于是 `backend/bff/devserver.py` 那句 `RegistryService(..., config=RegistryConfig(mode="synthetic"), ...)`
  **一个字都不用改**，行为也不变；
* `Engine` / `Endpoint` 新增的 dataclass 字段全部带默认值，DTO 是**新增键**；
* `health.STATE_*` 的取值域与语义没动（协议轴是**另一个**字段）。

证据不是「我说兼容」，而是：`import backend.bff.identity / ops / sharing / app` 成功，
且全仓 `python3 -m unittest discover -s tests -t .` → **2728 tests, OK (skipped=6)**。

`bff` 若要**用**新能力（例如把「换模型」接到某个页面上），需要它那边显式接
`repoint`/`rollback`/`routings` 与新的 `verification`/`ownership` 字段 ——
那是它自己的决定，本次没有替它做（§15.2）。

### 15.4 建议的下一步

1. 由 V5 侧决定 A 还是 B，并在其上补一份 `subject -> uid` 的受信映射。
2. 决定 `EndpointRecord` 与 Registry `Endpoint` 的关系（建议：Registry 成为
   端点的真相来源，modules 只做展示投影）。
3. 把 Registry 的 `usage/mine` 与 V5 的调用日志契约（`docs/portal-call-log-contract.md`）
   对接 —— 两边的口径不同（Registry 数的是「经授权的调用次数」，V5 数的是
   gateway 日志行），合并前要先决定以哪一侧为准。

## 16. 合成数据声明

* 本部署的 `mode` 是 `synthetic`：**每一份**响应都带
  `"synthetic": true` 与一行 `SYNTHETIC:` 的 `display_hints`。
* `devserver` 生成的身份与 token 全部是 `synthetic-` 前缀、进程内随机、不落盘、
  进程退出即失效。它们**不是**生产凭据。
* `MockGatewayAdapter` 的答复显式标注 `synthetic: true` 并附注说明。
* 测试里没有任何真实用户数据、真实 API key、真实用量导出、prompt/completion 原文。
* 本子系统**不会**：读真实 API Key 或用户数据、停止或访问生产模型、
  修改生产 Gateway / systemd / Caddy / GPU 配置。

## 17. Endpoint 重指向：稳定 alias、原子切换、不带旧授权

### 17.1 一句话

`route_path` **不变**，变的是它背后的 `deployment_id`。这就是「停掉大的 Qwen、
换一个小的、把 A6000 让给 dave」在 Registry 这一层的落点 —— 调用方那边的
API 路径不动，模型换了。

### 17.2 三个字段讲的是一件事

| 字段 | 含义 |
|---|---|
| `route_path` | 稳定 alias。创建时定下，**永不改变** |
| `deployment_id` | 这个 alias 现在指向哪次部署（重指向改的就是它） |
| `revision` | 乐观并发的版本号。创建为 1，每次重指向 +1 |

### 17.3 CAS 与竞态

`repoint` **要求**调用方带上它读到的 `expected_revision`（没有默认值）。
不带就 `400`；带了但已经过期就 `409 registry.revision_conflict`，且**什么都不改**。

不给默认值是刻意的：默认值会让「读-改-写」的竞争悄悄过关，而这道 CAS 的全部
价值就在于强迫调用方先读一次。证据是一条**真开两个线程**的测试：
`test_two_racing_repoints_produce_exactly_one_winner` —— 结果必然是
`["ok", "registry.revision_conflict"]`，且落库 revision 只前进一格。

### 17.4 生效状态：三个取值，`applied` 只由回读证据挣来

一次重指向之后，Registry 这边改了、Gateway 那边还没改，状态就是：

```json
"router_sync": {
  "state": "pending_router_sync",
  "pending": true,
  "applied": false,
  "applied_generation": 0,
  "current_generation": 2,
  "evidence": null,
  "reason": "router_generation_lags_revision"
}
```

状态取值是**封闭的三个**，且它们回答的不是同一个问题：

| `state` | 含义 | `applied` |
| --- | --- | --- |
| `pending_router_sync` | 回执代数落后于当前 `revision` | `false` |
| `router_sync_attested` | 代数追平了，但**没有**外部证据 | `false` |
| `router_sync_applied` | Gateway 回读的 `revision` **与** `snapshot_sha256` 与本次快照逐位相等 | `true` |

`evidence` 说明这次追平是**凭什么**：

* `gateway_readback` —— `RouterAdapter` 读回 Gateway 自己发布的
  `status.json.routing.{revision,snapshot_sha256}`，两者都与本次导出的快照相等。
  这是唯一能产生 `router_sync_applied` 的证据；
* `operator_attestation` —— 运维调
  `POST /endpoints/{id}/routing-applied {"generation": N}` 声明配置已生效。
  它要求 `generation` 与当前 `revision` **精确相等**（落后的回执会让系统以为
  已经生效，那正是「谎报已切换」的成因），但请求里没有一个字节来自 Gateway。
  所以它挣到的是 `router_sync_attested`。

**`attested` 与 `applied` 是两个词，是有意的。** `docs/architecture.md` §5.2
规则 3 把「路由应用」的必要条件写成「Gateway 发布的 generation/revision 与
snapshot SHA 回执」；状态图上 `router_sync_applied` 那条边也只由
「回读 revision + snapshot SHA 完全一致」走出。如果运维的一个整数也能产出同一个
token，那么「已切流」这四个字就再也无法从 DTO 上分辨真假 —— 而它恰恰是这整条
状态机存在的理由。这两条路径从前共用一个 token，本次分开。

**这条状态机是本次交付里最容易被误读的部分**：一次成功的 `repoint` 意味着
「Registry 接受了这次切换」，**不**意味着「线上已经在跑新模型」；一次
`routing-applied` 回执意味着「运维说它生效了」，同样**不**意味着 Registry
验证过。

**理由字段的分层**：`router_sync.reason` 只说**行级**事实
（`router_generation_lags_revision`）。「本部署有没有配 RouterAdapter」、
「Gateway 回读为什么对不上」是**部署级**事实，由 service 层补进同一个块
（缺省部署下 `reason` 就是 `no_router_adapter_in_this_deployment`，
而一次失败的同步把精确原因放在 `meta.router_sync_report.reason`）。一条数据库
行不知道本部署装了什么，从前它却替部署回答了这个问题，于是同一份响应里同时
出现「没有 adapter」和「Gateway 代数落后」两句。

### 17.5 ACL：默认收回，绝不静默带走

一个 Endpoint 可能已经被共享出去了，而共享的对象是「这个 alias 后面的服务」。
换个后端却把名单留着，等于把一份为**旧模型**签发的授权静默转给新模型 ——
收件人从没同意过这件事。

所以：

* **默认**：重指向把共享收回成 `private`，并审计
  `revoke(reason=repoint_acl_reset)`；
* 要保留共享，必须**显式** `reauthorize` / `--regrant`，而这一步要
  `registry:endpoint:share` 这个**它自己的** scope（不是顺手借用重指向的权限）；
* 重新授权与路由切换在**同一个事务**里生效，中间没有窗口；
* 保留下来的人也各有一条 `grant(reason=repoint_reauthorization)` —— 因为
  授权集合是被**拆掉重建**的，只记增量会让审计看起来「ACL 没动过」。

审计里那条 `repoint` 事件带 `acl_carried_over: false` 与 `visibility_after`。

### 17.6 回滚不是特权通道

`rollback` 就是「再重指向一次」，走的是**同一套**检查：目标必须
ready、归属必须证明、revision 必须对得上、ACL 同样重置。
**历史里那个目标此刻不可用，回滚就该失败** —— 不因为「它以前行过」而放行。

### 17.7 历史只追加

`endpoint_routings` 表记录每一次路由状态（**包括创建时的初始那一行**，所以
「回到最初那个」也是可回滚的），每行带 `previous_deployment_id` /
`previous_model_id`，并解析出 `model_name` / `previous_model_name`。
旧行**永不删除**：换模型不抹掉旧模型的数据，调用记录仍然指着旧模型。

### 17.8 跨 owner 一律拒绝

目标的可见性用两套答案，因为它们**不是同一件事**：

* 不可见 -> `404`（与「不存在」逐字节同形，不做存在性预言机）；
* 可见（别人共享给我了）但属于别的 owner -> `403`。这时说「不存在」是撒谎；
  真话是「你确实看得见它，但它不是你的，不能挂到你的对外路径上」。

**即使是 `admin:manage` 也不能把两个 owner 混起来** ——
代管不等于可以把别人的算力挂到自己的路径上（`test_even_an_admin_cannot_mix_owners`）。

## 18. 引擎所有权与运维策略：8080 的唯一通路

### 18.1 两种登记模式

| 模式 | 谁可以 | 走哪个端口 allowlist | 所有权初值 |
|---|---|---|---|
| `user`（默认） | 任何有 `registry:write` 的人 | 普通 allowlist（**不含** 8080） | `ownership_unverified` |
| `system_existing` | 需要 `registry:admin:manage` | **运维策略**中精确匹配的条目 | `operator_verified` |

关键在「用哪个 allowlist」这一步：`user` 模式传进去的是**空** allowlist，
所以运维策略里的承认**不会**泄漏到普通用户的登记路径上 ——
`test_a_plain_user_cannot_reach_8080_even_with_a_blessing_in_place` 就是这条。

### 18.2 运维策略文件

一份 JSON，`schema: "registry-operator/1"`，由 root 或**运行本服务的账号**拥有，
不能被组/其他人写，不能是软链：

```json
{"schema": "registry-operator/1",
 "targets": [{"scheme": "http", "host": "127.0.0.1", "port": 8080,
              "path_prefix": "/", "label": "existing upstream (Strata)"}]}
```

匹配是**精确**的（scheme + host + port + 路径前缀），不做通配。而且**策略也不能
把目标放到回环之外** —— 这条边界没有例外。加载失败必须让进程起不来，
不能「悄悄没读到策略」。

### 18.3 所有权证明：`/proc/net/tcp(6)` 的 uid 列（可选，真实主机待验证）

`ownership.py` 里的 `OwnershipProver` 有三个实现：默认 fail-closed 的
`NoOwnershipProver`（永远说不出来）、显式标注的测试替身 `StaticOwnershipProver`，
以及**可选**的 `ProcNetOwnershipProver`。

上一轮把自动 uid 证明判为 BLOCKED，前提是「得读 `/proc/net/tcp` 拿 inode、再扫
`/proc/<pid>/fd` 反查进程」—— 后一步需要 root 与遍历其他用户的进程表。那个前提
错了一半：`/proc/net/tcp` 与 `/proc/net/tcp6` **全员可读**，且每一行已经带着
socket 属主的 `uid` 列、本地 `地址:端口`（主机字节序 hex）与状态（`0A` = LISTEN）。
不需要 inode 反查，不碰任何 `/proc/<pid>`。

判定：

1. 期望 uid = `pwd.getpwnam(<登记者 Linux 用户名>)`；用户名**只**来自受信目录
   （登记时是 `RequesterContext.linux_user`，verify 时是 engine owner 在
   `PrincipalDirectory` 里的那一行）。请求里的 `uid`/`owner`/`linux_user` 字段照旧 400。
2. 在 LISTEN 行里找绑定在该端口、能服务这个回环目标的 socket：精确地址
   （含 `::ffff:127.0.0.1` 这种 mapped 写法）、同族通配（`0.0.0.0` / `::`）、
   以及 `::` 之于 IPv4 目标（双栈；`IPV6_V6ONLY` 从表上看不出，所以按「可能接得到」算）。
   通配命中会记进 `detail`。绑在别的地址上的 socket 不算。
3. 结论（`ownership.reason`，机器可读）：

| reason | 含义 |
|---|---|
| `null`（`state=uid_verified`） | 能服务该目标的 socket 恰好一个 uid，且就是登记者 |
| `not_listening` | 没有能服务该目标的 LISTEN socket |
| `owner_mismatch:<uid>` | 恰好一个 uid，但不是登记者 |
| `ambiguous` | 能服务该目标的 socket 不止一个 uid |
| `unknown_user` | 没有受信用户名，或该用户在本机不存在 |
| `proc_net_unreadable` / `proc_net_unparseable` | 表读不到 / 有看不懂的行（**不跳过**：被跳过的那行可能正是冲突者） |

选择方式：`RegistryConfig(ownership_prover_kind="proc_net")`，或开发服务器
`--ownership-prover proc_net`。**缺省仍是 `none`**，于是既有部署与测试的行为不变。
显式注入的 `ownership_prover` 优先于配置。

诚实的边界：

* **只**由单元测试（注入的 fixture 表）与「本进程自己开的回环监听」冒烟验证过；
  在 Dell 上对真实引擎的验证**仍待做**。
* `/proc/net/*` 按网络命名空间：Registry 与引擎不在同一 netns 时读到的是另一张表，
  结论会是 `not_listening`（fail-closed，不会误放行）。
* 它证明的是「监听这个端口的 socket 属于谁」，不是「进程是哪个二进制」。

运维承认的既有服务仍走运维策略，且优先于任何 prover。

#### 18.3.1 登记预检：`POST /engines/preflight`

前端「登记模型服务」表单在提交前调用。**只读**：不写库、不写审计。需要
`registry:write`（它只服务于登记流程），目标解析与 `POST /engines` 共用同一个
helper（预检能过的目标，登记的目标校验也能过）。请求：

```json
{"name": "qwen-local", "target_url": "http://127.0.0.1:8000/v1"}
```

（可选 `engine_mode`，规则与登记相同。）响应 `data`：

```json
{
  "schema": "registry-engine-preflight/1",
  "read_only": true,
  "name": "qwen-local",
  "target": {"scheme": "http", "host": "127.0.0.1", "port": 8000, "path": "/v1", "...": "..."},
  "checks": {
    "port_reachable": {"ok": true, "detail": "connected"},
    "ownership": {"ok": true, "detail": "verified", "evidence": {"state": "uid_verified", "...": "..."}},
    "openai_compat": {"ok": true, "detail": "model_list_verified"},
    "name_unique": {"ok": true, "detail": "available"}
  },
  "all_ok": true
}
```

`ok` 是三值：`true` 通过、`false` 查了没通过（`detail` 是原因 token，如
`connection_refused` / `owner_mismatch:1002` / `model_list_missing` /
`engine_name_taken`）、`null` **本部署没有这条来源**（`detail` 为
`not_configured_in_this_deployment` 或 `no_ownership_source_in_this_deployment`）。
`all_ok` 只有四个都是 `true` 才为真。`port_reachable` 只在挂了传输 prober 的部署
（`--probe-loopback`）才真的做回环 TCP connect；`openai_compat` 用协议 prober
（只读 `GET /v1/models`）。BFF 以会话身份镜像了这条路由与登记本身：
`POST /api/v2/registry/engines/preflight` 与 `POST /api/v2/registry/engines`
（`route_map` 的 `registry.engines.{preflight,register}`，CSRF + 同源，owner 只来自会话）。

#### 18.3.2 平台账户作为 owner（2026-10-21，Owner 选定方案 A）

平台账户（`backend.auth.accounts`，主体 id `account:<hex>`）与 Linux 账户相互独立：
一个人在自己常用的 Linux 用户下启动引擎，再用管理员创建的平台账户登记。于是对
`account:` 主体，所有权的含义变成「这个服务由谁在跑、归谁登记」：

| 情形 | 判定 | `ownership.state` |
|---|---|---|
| 该回环目标已被**另一个**主体登记 | 拒绝；预检 `ok=false`，登记 `409` | `ownership_unverified`，`reason=target_registered_by_another_account` |
| 管理员为该账户设了 `run_as`（用户页「限定 Linux 用户」） | 监听 uid 必须是那个 Linux 用户（同 §18.3 的 prove） | `uid_verified` 或失败原因 |
| 未设 `run_as` | 只观测：能服务该目标的 socket 恰好一个 uid 即可，记下 uid 与用户名 | `runtime_observed` |

* `runtime_observed` 属于 `OWNERSHIP_VERIFIED_STATES`；预检的 `detail` 为
  `runtime_observed`，`evidence.uid` / `evidence.linux_user` 是观测到的运行用户；
* account 名下 engine 的 `owner_uid` 写观测到的运行 uid（账户本身没有 uid；其余对象写
  `UID_UNMAPPED = -1`）；
* `PrincipalDirectory` 中 `kind="account"` 的主体：`linux_user` 是账户用户名（展示 /
  路由标签，不是主机账户），`uid=None`，`run_as` 可选；BFF 的
  `AccountAwareDirectory` 每次查询都读账户库，停用的账户即为 `suspended`；
* Linux 主体（`linux:<host>:<uid>`）的行为**不变**。

同一个目标只归一个主体这条门目前只对 `account:` owner 生效；Linux 主体本来就由 uid
比对隔开。

#### 18.3.3 本机发现：`GET /discovery`

只读候选清单（`backend/registry/local_discovery.py`），CLI `llmlab discover` 与网页「推理服务 / 模型库 → 本机发现」共用。需要 `registry:read`。

* **推理服务**：`/proc/net/tcp(6)` 中在回环或通配地址上 LISTEN、且端口在 `allowed_ports` 内的 socket（缺省 `{8000, 8001, 8081, 9000, 9001}`，BFF 用 `--allowed-port` 追加；不在列表里的端口既发现不了也登记不了）；每个端口只读 GET `/v1/models`（模型 id）与框架指纹（`/version` 带 `version` ⇒ vllm，`/get_server_info` 带 `tp_size`+`model_path` ⇒ sglang）。监听 uid 反查用户名作为 `run_as_user`。
* **模型**：配置了 `--model-root` 就在这些根目录里找；**没有配置时**自动查 `/data`、`/srv`、`$HOME` 这三个约定位置（`DEFAULT_MODEL_ROOTS`），`--no-model-scan` 可关闭。这是「自动」而不是「全盘」：全盘要跨 TB 级、跨用户目录、可能落在网络存储上。深度 ≤ 4、条目 ≤ 200000、候选 ≤ 200；不跟随软链出根；`node_modules` / `site-packages` / `venv` 之类的噪声目录不进去（它们能一口吃掉条目预算，把真正的模型挡在后面）；识别 `config.json`+权重文件的 HF 目录与 `.gguf` 文件，切分的 `…-00001-of-00002` 归成一个模型，`mmproj-*.gguf` 不算模型；只读 `config.json`（≤ 256 KiB），权重只看名字与大小。扫描被上限截断时 `problems` 里会出现 `model_walk_truncated:<root>`——**不静默截断**。
* 每个候选带 `registered` / `registered_by_me`；**没有写入路径**。登记仍走 `POST /engines` / `POST /models`（`llmlab discover --register PORT` / `--register-model PATH` 只是把候选填进这两个调用）。
* 本部署未装配发现器时返回 `configured: false`（不是空清单，也不是错误）。

### 18.4 强制档

`RegistryConfig.ownership_policy`：

* `advisory`（**缺省**）：记录所有权状态、在 DTO 里点名，但**不**阻断共享/调用；
* `enforced`：未证明即拒绝共享与调用（`409 registry.ownership_unverified`）。

`RegistryConfig.production_posture()` 一次把 `enforced` 与
`require_protocol_verification` 都打开。**生产应当用它**；
缺省是兼容档，理由见 §19.4。

## 19. 两根轴：传输可达 ≠ 协议是模型服务

### 19.1 被拆掉的那个等号

原判定是「2xx 即 healthy」，于是**任何一个**在回环端口上应答 200 的东西
（静态文件服务器、别的用户的服务、随便一个 daemon）都能被登记成「模型就绪」。
现在拆成两根独立的轴，各自有状态、各自出现在 DTO 里：

| 轴 | 结论 | 怎么看 |
|---|---|---|
| **transport** | `healthy` / `degraded` / `unreachable` / `unknown` | 一次 GET 到 engine 目标 |
| **protocol** | `verified` / `unverified` / `unknown` | 读 `/v1/models`（或 `/models`）并解析模型清单 |

`verification.verified` 的语义在一个函数里定义清楚（`health.build_verification`）：

* `require_protocol_verification=False`（兼容缺省）：传输可达即 `verified`，
  但 `protocol.verified` **仍然如实为 false**，且 `blockers` 会点名
  `protocol_unverified` —— 于是没有哪个前端能把它读成「模型已就绪」；
* `=True`：**两根轴都通过**才算 `verified`。

### 19.2 只走白名单路径

协议验证只请求固定表里的路径（`/v1/models`、`/models`），**不使用** engine
target 里用户给的 path —— 否则「协议验证」就变成了一个能指向本机任意路径的
通用探测器（`test_only_allowlisted_paths_are_requested` 就是这条）。

health 类路径（`/healthz` 等）**不足以**判定 `verified`：一个 200 只说明
「有东西在应答」。模型身份必须来自模型清单（每一条都有 `id`）。

### 19.3 真回环模拟器的两侧

`test_verification_axes.py` 用**本进程起的临时 loopback HTTP 服务**（端口由内核
分配）分别验证两侧：返回合法模型清单 -> `verified`；返回 HTML / 空洞 JSON /
`/healthz` 200 -> `unverified`。**没有 GPU、没有生产服务、没有真实用户进程。**

### 19.4 两个开关默认关着 —— 这是显式取舍，不是遗漏

`ownership_policy` 默认 `advisory`、`require_protocol_verification` 默认 `False`。
理由有一条具体的、可核实的约束：**`backend/bff` 用默认 `RegistryConfig()`
构造服务并调用 `share_endpoint`**（`backend/bff/devserver.py`），而本次任务
明确不允许修改 `backend/bff/**`。把缺省改成严格档会静默改掉一个不属于本模块的
对外路径 —— 那是一次未经审查的行为变更，而不是一次加固。

因此：开关与新轴**全部实现并测到**，缺省保持历史行为，
生产用 `RegistryConfig.production_posture()`，而这条取舍写在明面上。

## 20. 注册 ≠ 验证 ≠ 运行

这三件事在契约里是三个**独立**的字段，不是同一个 `status` 的三种取值：

| 事实 | 字段 | 谁能改变它 |
|---|---|---|
| 元数据进了库 | `registered: true` | 登记者 |
| 一次真实的探测成功过 | `verified` / `verified_transport` / `verified_protocol` | Registry 的探测（由本部署的 prober 提供） |
| **进程真的在跑** | `running` | **恒为 `null`** —— 平台不启动、也不托管进程 |
| 发现循环最近看见它在不在 | `state` | 自动接入（§4.1），仅在启用时；`null` = 还没被观测过 |
| 是不是这个部署声明的模型服务 | `verification.protocol` | 协议探测（只读模型清单） |
| 这个引擎的进程属于谁 | `ownership` | 运维策略，或（可选、真实主机待验证的）`/proc/net` uid 证明 |

**多条 Model 记录 ≠ 同时真实运行。** 一个 principal 可以登记多个 Model、
多个 Deployment、多个 Endpoint（没有数量上限，唯一的约束是「同一 owner 下名字
不重复」），但「同时运行着几个」这件事平台**不管**：它既不启动也不托管进程。
所以 `running` 是 `null` 而不是 `true`/`false`。

`state` 与 `running` 不是同一件事，也不能互相代替：

* `running` 回答「平台管不管得着这个进程」——管不着，恒为 `null`。
* `state` 回答「发现循环最近一次看到它在不在」——`attached` / `offline` /
  `seen`，未启用自动接入或还没扫到时为 `null`。

`state=offline` **不删除**登记：服务停了两轮之后行还在，只是调用返回
`registry.service_offline`（503）；服务回来时复用同一行、同一路由。这是「停掉」与
「丢弃」的区别——丢弃只发生在从未识别成功的端口上。

`service_offline` 与 `upstream_unavailable` 都是 503，但读的人要做的事不同：
前者是「你的进程没在跑，去看你自己的服务」，后者是「转发失败或本部署没接网关」。
这一条判断**永远**生效（不是可选的部署策略），并且在任何 Gateway 调用之前完成 ——
一个注定失败的转发不该被记成一次已授权的调用。`state` 为 `null`（没有观测源）时
不做这一判断：不知道不等于不在。

**自动接入的路由名**（§4.2）在接入那一刻定下来，存在 `launch_json` 里：模型 id
规范化（`[A-Za-z0-9._:/-]`），与已占用的名字冲突时追加 `@<用户名>`。`private`
的服务不占名字。定下来就不再变——守护进程重启、服务名字冲突都只影响之后接入的
服务，与默认可见性同一条纪律。

**自动接入同时登记模型。** 引擎在 `/v1/models` 里报出的每个模型名都会落成一行
`models`（`source=auto`，owner 就是这个端口所属的 principal），引擎的
`observed_models` 列出 `{serving_id, model_id}` 的对应关系。同一条纪律：只**补空**，
不覆盖——已存在的手工登记行会被复用，它的 `weight_path` 不会被观测值抹掉；同名
但不同 owner 的模型各自成行；同一 owner 下两个端口服务同名模型共用一行。

这些行的 `weight_path` / `format` 是 `null`，不是猜出来的值：发现循环拿到的是
**名字**，不是权重文件的位置。`Qwen/Qwen3-4B` 这类带斜杠的 id 会把 `/` 换成 `-`
（Registry 名称不允许路径分隔符）。模型行不随引擎下线删除，理由与引擎行相同。

自动接入的服务**目前不会**被发布到 Gateway 快照：`build_routing_snapshot` 要求
engine 有 `secret_ref`，且路由挂在 Endpoint 上，而观测到的服务既没有上游密钥配置、
也还没有 Model / Deployment / Endpoint 链。快照的 `excluded` 会如实列出原因。

真正的 start/stop/restart、CUDA 加载与 GPU 准入属于一个**独立的受限 owner-agent**，
不在这条路径上（也超出了本仓库的部署边界：`docs/access-boundaries.md` §6
禁止常驻服务与 systemd 单元）。

**Mock 通过 ≠ 真实切换成功。** 缺省部署里所有的 `invoke` 都经过
`MockGatewayAdapter` 或 `DetachedGatewayAdapter`，所有的健康/协议/所有权结论
在测试里都来自注入的夹具。它们证明的是**代码会照着结论做判断**，
不是「某个真实端口上跑着一个真实模型」。§21 补上了第三条路
（`TrustedGatewayAdapter` + `RouterAdapter`），但它仍然是**可注入的接线**：
在有人把回环基址与调用者 key 目录真正配置好之前，缺省依旧什么都不发、也不声称。

## 21. 受信 Gateway adapter 与路由同步（v3）

> v3 新增。这一节回答审计文档 P0-04 与 Lane #46 交付 2：把 `DetachedGatewayAdapter`
> 之外那条**真实但可注入**的调用路径，以及「repoint 之后线上到底生效了没有」这条
> 外部证据链接上。**没有**接线到任何生产 Gateway，也没有读取任何真实凭据。

### 21.1 三条 fail-closed 规则（`TrustedGatewayAdapter`）

1. **基址只允许回环 http，且不得是保留的上游端口。** `https://…`、任何非回环地址、
   `http://127.0.0.1:8080`、带 userinfo / query / 非空路径的 URL，在**构造时**就被拒
   （`_normalize_gateway_base`）。网关是唯一该跟上游说话的东西，Registry 绝不直连它。
2. **调用者没有自己的 Gateway key 就一个请求都不发。** key 由 `GatewayKeyResolver`
   提供（缺省 `NoGatewayKeys`，永远 `None`）。没有 key → `503 gateway_key_unavailable`，
   且**零 socket**（有测试用回环替身的调用计数证明）。
3. **Gateway 的 403 如实翻成 `registry.access_denied`（`gateway_acl_denied`），不降级。**
   这就是 server-side ACL：能不能调，最终由 Gateway 用自己保存的 key 记录判定，
   本模块无权替它说「可以」。

`forward` 的签名**只增**了一个关键字参数 `requester`（由服务端凭据推出的
`RequesterContext`，**不是**客户端自报的东西）。既有 adapter 给它一个缺省即可，
`backend/bff` 一个字都不用改。

### 21.2 路由快照与代数回执（`router.py`）

* `build_routing_snapshot()` 导出的形状与 Gateway PR #20 的
  `llmlab.endpoint-routing/1` **逐字段对齐**：`engines[]`（`upstream` +
  `secret_ref`，只点名文件与变量、**绝不携带值**）、`endpoints[]`
  （`model_alias` = 稳定 `route_path`、`model_id` = 引擎内部名、`owner_uid` =
  平台用户名、`acl.kind` ∈ `private|users|all`、`state`）。
* **没有 `secret_ref` 的 engine 整个被排除**（连带它的 endpoint），并记进
  `excluded`。缺省 `engine_secret_refs` 是空的 —— 于是缺省下发布的是**空快照**，
  不配置就不发，这就是 fail-closed。
* `FileRouterAdapter` 用**规范字节**原子写入（临时文件 + `os.replace`），再读回
  Gateway **自己发布的** `status.json.routing.{revision, snapshot_sha256}`：
  * sha 与代数**逐位相等**才算 `published=True`；
  * 落后的代数 → `snapshot_revision_mismatch`；sha 不符 → `snapshot_digest_mismatch`；
  * `routing: null`（Gateway 没加载任何快照）→ `snapshot_not_loaded_by_gateway`；
  * 状态文件缺失/不可解析 → `gateway_status_unreadable`。
  四种失败**各自有理由**，都不是笼统的「失败」。
* 服务侧：`repoint` / `rollback` 之后，只有在 adapter 拿到上述**外部证据**时才会
  把落后的 Endpoint `ack` 到新代数（审计主体是机器主体 `system:router-sync`，
  **不是**发起 repoint 的人）。缺省（`DetachedRouterAdapter`）下，`meta.router_sync`
  恒为 `pending_router_sync`，理由 `no_router_adapter_in_this_deployment` ——
  **Registry 里改一行，绝不等于线上路由改了**。

### 21.3 新增的只读入口

* `GET /api/v2/registry/routing-snapshot`（需 `registry:admin:read`）：导出上面那份快照。
* CLI：`llmlab routing snapshot`（同一份快照）、`llmlab routing config`（本仓库自己的
  `registry-routing/1` 只读契约）。两者都**只读**。

### 21.4 本轮验证了什么、没验证什么

**CPU-only 合成证据**（`tests/registry/test_trusted_gateway_and_router.py`，44 条）：

* A(bob) 注册/验证/共享给 B(dave) → B 经 `TrustedGatewayAdapter` 调用**成功**，
  token 数来自 Gateway 的 `usage`（不是编的），结果照抄 Gateway 自称的 `synthetic`；
* 「浏览器/CLI 拿不到原始上游」是**可证伪的**：engine target 指向一台**诱饵**服务，
  整条链跑完诱饵命中数恒为 0；
* A 撤权后 B 的下一次调用在 Registry 侧就被挡（404，与不存在同形），且**不再向
  Gateway 发第二个请求**；
* Gateway 的 ACL 是**最终判定**：让 Gateway 加载一份旧快照（endpoint 仍 private），
  Registry 说 B 可以也会被 Gateway 403 挡住；
* repoint：缺省 detached → 停在 `pending_router_sync`；stale adapter → 同样停在
  pending（落后的代数被拒）；confirming adapter → `router_sync_applied` 且落库
  `router_generation` 前进、审计主体为 `system:router-sync`。

**没验证、也不声称的**：真实的 Gateway 进程、真实的 ACL 执行、真实的 `status.json`
发布、真实的 key 目录。回环上的「Gateway」是**测试替身**，它证明的是「代码会照着
Gateway 的判定做决定」。上真机还需要：一个运维建好的、Gateway 读得到且 Registry
组可写的快照目录，以及一份 `engine_id -> {file, var}` 的 `engine_secret_refs` 配置。
这两件事**都不在本轮做**（`NOT DEPLOYED / NO RUNTIME VERIFICATION`）。
