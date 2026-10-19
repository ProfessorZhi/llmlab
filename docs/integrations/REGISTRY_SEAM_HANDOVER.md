# Registry → BFF seam：Lane #76 交给同源 BFF 的东西

> Owner: Registry / Gateway [#76](https://github.com/ProfessorZhi/llm-lab-portal/issues/76)。
> Baseline: `docs/v1-production-architecture-agent-orchestration-20261017` @
> `5ea80535a3ccdde63281314324195cd727a94700`。
> Companion: [`BFF_BATCH1_SEAMS.md`](BFF_BATCH1_SEAMS.md)（BFF 侧逐来源的同一张表）。

本文件只补 BFF 那一侧文档没有**替 Registry 说**的部分：Registry 交出去的是哪个
对象、它承诺什么、以及本轮动过的 DTO 契约。

## 1. 交给谁、交什么

| 来源 | 注入点 | 由谁提供 | 那个对象是什么 |
| --- | --- | --- | --- |
| `registry` | `OpsApplication(service=...)` | Registry [#76] | **`RegistryService` 本身**，不是一层包装 |

BFF 对同一个对象只调用这几个入口：

```text
service.policy(requester)                      -> AccessPolicy
policy.visible_endpoints() / visible_deployments()
service.store.grantees(endpoint_id)
service.principals.approved_principals() / lookup(name)
service.share_endpoint(...) / unshare_endpoint(...)
```

`requester` 是 **`backend.registry.identity.RequesterContext`** —— BFF 的
`backend/bff/identity.py` 直接 import 这个类来构造会话主体。**不要**为 BFF 另造
一个同名的 `RequesterContext`：两套同名类型是这条 seam 最容易悄悄漂移的地方。

**BFF 只读 `visible_*`，不重新判定任何一行 ACL。** 这条声明是关于 Registry 的性质，
所以它由 Registry 这边的 `tests/registry/test_bff_seam.py` 执行：那个文件钉住
可调用的形状，以及「私有不可见 / 共享后可见 / 撤权后下一次不可见 / 他人私有不进
可见集合」这四条语义。BFF 侧另有自己的 route_map 与 ops 测试。

## 2. 本轮 DTO 契约变更（`CONTRACT_CHANGE`，需 Integrator 过目）

**变更面：endpoint 视图里的 `router_sync` 块。** 前端 `web/` **不消费**这个块
（全仓 grep 为空），CLI 与 BFF 的 `ops.py` 也都不读它的 `state`；消费方是 CLI 的
`endpoints repoint` / `endpoints routing-applied` 渲染与 `meta`。改动是
**加法为主**，但**有一个取值换了含义**，所以记在这里而不是埋在 diff 里。

### 2.1 状态取值：三个，且 `applied` 只由回读证据挣来

| `state` | 含义 | `applied` |
| --- | --- | --- |
| `pending_router_sync` | 回执代数落后于当前 `revision` | `false` |
| `router_sync_attested` | 代数追平，但**没有**外部证据（运维声明） | `false` |
| `router_sync_applied` | Gateway 回读的 `revision` **与** `snapshot_sha256` 与本次快照逐位相等 | `true` |

**为什么改**：从前运维一句
`POST /endpoints/{id}/routing-applied {"generation": N}`（只有一个整数，没有一个
字节来自 Gateway）会产出与 `FileRouterAdapter` 逐位回读**完全相同**的
`router_sync_applied`。而 `docs/architecture.md` §5.2 规则 3 把路由应用的必要条件
写成「Gateway 发布的 generation/revision 与 **snapshot SHA** 回执」，状态图上
`router_sync_applied` 那条边也只由「回读 revision + snapshot SHA 完全一致」走出。
两条证据强度不同的路径共用一个 token，等于让一个整数买到「已切流」四个字。

新增字段：`applied`（bool）、`evidence`（`gateway_readback` /
`operator_attestation` / `null`）、`adapter`（pending 且已装配 adapter 时）。

**迁移影响**：`router_ack_evidence` 是 v3 新增列，迁移前落库的行是 `NULL` ——
一律读作 `router_sync_attested`，**不**读作 `router_sync_applied`。历史行不被提拔，
因为没有人替它们证明过。若某个消费者把 `state == "router_sync_applied"` 当作
「这个 endpoint 在切换后从未动过」的判据，`NULL` 行会让它变成 `attested` ——
这是刻意的方向：缺证据时不宣称已切流。

### 2.2 `router_sync.reason` 只说行级事实

`Endpoint.router_sync_block()` 是一张**数据库行**的视图，它从前却断言了
「本部署没有配 RouterAdapter」这件**部署级**事实。后果是一份同响应自相矛盾的 DTO：

```text
data.router_sync.reason       = "no_router_adapter_in_this_deployment"   # 假的
meta.router_sync_report.reason = "gateway_revision_mismatch"             # 真的
```

现在行级视图只报 `router_generation_lags_revision`；部署级事实由
`RegistryService._decorate_router_sync()` 补（缺省部署下就是
`no_router_adapter_in_this_deployment`，与从前一致）。**缺省部署的 JSON 形状没有
变化**；变的只是「装了 adapter 但回读对不上」这条路径，它从前在骗人。

### 2.3 ACK 只发给**这次快照里发布了**的路由（Wave-5）

`_attempt_router_sync` 从前只做 revision 算术：RouterAdapter 说 `published=True`
之后，把所有 `revision <= applied_revision` 且仍 pending 的 Endpoint 一起回执。
它没有问「这条 Endpoint 在这次快照里吗」。

`build_routing_snapshot` 会诚实地排除它发不出去的行（`no_secret_ref`、
`engine_not_published`、`deployment_missing`、`owner_unknown`……），并把理由写进
`excluded`。**被排除的 Endpoint Gateway 从来没收到过**，给它记
`router_generation = revision` 就是凭空的「已切流」。

实测（修复前）：快照 `endpoints == []` 时，一次 repoint 仍回执 2 条 Endpoint，
其中一条读出 `state=router_sync_applied`、`evidence=gateway_readback`、`applied=true`。

现在回执前先取 `{entry["endpoint_id"] for entry in snapshot["endpoints"]}`，
不在里面的**一律不回执**。`meta.router_sync_report.acked_endpoints` 因此第一次
等于「这次真的被 Gateway 加载了的那批」。

**给测试作者的连带结论**：`Harness` 默认没有 `engine_secret_refs`，快照是**空的**。
在空快照上让 `ConfirmingRouterAdapter` 返回 `published=True` 什么也证明不了 ——
Wave-5 之前有两条测试正是这样空转的。要证明「一条真实路由被回执」，先调
`Harness.publish_engine()`，并用 `Harness.make_sibling_deployment()` 做 repoint 目标
（复用同一个 engine，否则被测 Endpoint 会恰好掉出快照）。

### 2.4 回滚警告：v3 之后**不能只回滚代码**

`Store._migrate` 在 `user_version > SCHEMA_VERSION` 时直接拒绝打开
（`refusing to run against an unknown schema`），不半读。所以把 Registry 回滚到
v3 之前的代码，**必须连同数据库一起回滚**（恢复迁移前的备份）；只回滚代码会得到
一个启动即拒绝的进程。

这是刻意 fail-closed 的方向，但它必须是一次**有准备**的回滚。v3 只加了一列
`router_ack_evidence`，回滚到 v2 代码时不读它、也不影响路由判定 —— 但 v2 代码
读不了带 v3 `user_version` 的文件。

### 2.5 顺带修掉的一条不成立的说法

`registry._reject_identity_fields` 的 docstring 曾说 ``invoke`` 的载荷刻意豁免身份
字段检查（因为 ``role`` 是对话字段）。实际不成立：传输层
`api._dispatch` 在认证之后对**所有**路由（含 invoke）调用
`identity.reject_identity_assertions`，它与 `FORBIDDEN_IDENTITY_FIELDS` 命中即 400。
改的是注释，不是行为 —— 把保护层放松成注释描述的样子会是**削弱**。

## 2A. SEAM-2：受信 token introspection（Wave-9）

### 2A.1 这条缝原来为什么是断的

`resolve_requester` 对第二个参数做 `isinstance(tokens, TokenDirectory)` 硬校验。
IAM 的真实服务是 `backend/auth/tokens.ScopedTokenService`，不是 `TokenDirectory`，
所以 Registry 模块的 HTTP 层接不上它。

把 IAM token 复制进第二个 `TokenDirectory` 能绕过，但那是**第二套 token 账本**：
撤销、到期、主体停用都要在两边各同步一次，漏掉任何一次都表现为「已撤销的凭据
仍然有效」。本模块**不复制数据**，只问签发方。

### 2A.2 现在的形状

```python
authority = TrustedTokenIntrospector(
    scoped_token_service,          # backend/auth/tokens.ScopedTokenService
    principals=principal_directory,
    clock=time.time,               # 与签发方同一个时钟（默认墙钟）
)
RegistryService(store=..., tokens=authority, principals=principal_directory, ...)
```

`resolve_requester` 接受**两种**权威，且只接受这两种：

| 权威 | 行为 |
| --- | --- |
| `TokenDirectory` | 既有内存等值表路径，**逐字未变**（CLI 的既有测试继续成立） |
| `TrustedTokenIntrospector` | 问签发方，校验回答，再与同一个 `PrincipalDirectory` 对账 |
| 任何其它对象 | `TokenAuthorityUnavailable` → **503**，绝不合成主体 |

`TrustedTokenIntrospector` 的校验面：

* `active` 为真；`principal` / `linux_user` 非空串；`uid` 非负整数（拒绝 bool）；
* 主体状态 approved；
* `scopes` / `roles` 是字符串集合；每条 scope 的**形状**合法（见 §2A.5）；
  scope 按 namespace 分流：`registry:` 里未知的让整条 token 失败关闭，其它命名
  空间的合法 scope 丢弃但保留 token；
* 回答若带 `expires_at`，按本层时钟再判一次（签发方已判；纵深防御）；
* 回答里的 `principal` / `uid` / `linux_user` 必须与**同一个** `PrincipalDirectory`
  一致 —— 两条通道对「这个 uid 是谁」不可能给出两个答案。

**它不读 token。** token 是不透明的；本层没有 JWT 解码、没有自签校验、没有第二条
查表路径。一个长得像 JSON claims 的字符串只是一个未知凭据 ——「用一个字符串给
自己签发管理员」在这条路径上不可表示。

**它不能被客户端提供。** 包装的对象是启动时由部署注入的服务端对象；
`resolve_requester` 只认 `TrustedTokenIntrospector` 这个**类**，不是任何有同名
方法的鸭子。一个任意对象当权威 → 503。

### 2A.3 状态码

| 情形 | 状态 | `contract_code` |
| --- | --- | --- |
| 缺省部署（空目录） | 401 | `registry.unauthenticated` |
| 未知 / 过期 / 已撤销 / 主体停用或移除 | 401 | `registry.unauthenticated` |
| 回答与受信目录不符 / 未知 **registry** scope / 形状不对 | 401 | `registry.unauthenticated` |
| 合法 token 但没有任何 registry scope | **403** | `registry.access_denied` |
| 权威无法回答（抛异常） | **503** | `registry.token_authority_unavailable` |
| 配了一个不受信的对象 | **503** | `registry.token_authority_unavailable` |

503 是刻意的：「这台服务不知道该问谁」换任何凭据都不会变好，让运维去查配置，
而不是让调用方以为自己的登录有问题。

### 2A.4 给 QA / Integrator 的**测试反转指引**（不改 #114 的文件）

PR [#114](https://github.com/ProfessorZhi/llm-lab-portal/pull/114) 在当前基线上钉住了
「Registry 拒绝**原始** IAM 服务」。这条断言在实现本 PR 之后**仍然成立**
（原始 `ScopedTokenService` 依旧不是受信权威，只是现在多了一条显式适配的路径）。

当 QA 要证明 SEAM-2 已闭合时，**由 #114 的 owner** 做如下替换（本 Lane 不改其文件）：

```text
删掉/改名： SeamInventoryTest.test_the_registry_module_refuses_the_iam_token_service
替换为：     test_the_registry_accepts_the_iam_service_through_the_trusted_adapter
             authority = TrustedTokenIntrospector(iam, principals=h.principals, clock=...)
             h.service.tokens = authority ; h.app.tokens = authority
             → 用 B 的真实 IAM token 打 GET /api/v2/ops，断言 200 且 subject == B
保留：       把「原始 ScopedTokenService 仍被拒」作为一条 503 负测
             （TokenAuthorityUnavailable），它证明的不是缝没开，而是缝只对受信适配器开。
```

同时 V1-07 的「B 经**同源 HTTP** 调用」仍受 **SEAM-1**（BFF 不挂 invoke）限制，
那个反转属于 BFF owner，不在本 PR 范围。

### 2A.5 scope 词表：只消费 registry 自己的六个（D3，**已修**）

`TrustedTokenIntrospector._validate` 原要求回答里的 `scopes ⊆ KNOWN_SCOPES`，
而 `KNOWN_SCOPES` 只有 registry 的六个。真实 IAM token 会携带
`atlas:calls:self:read` / `observability:self:read` / `portal:modules:read` 等
**其它模块的合法 scope**，于是**整条 token 被拒**（`Unauthenticated`）——
Registry 侧什么都做不了。这是 Wave-5 union QA 的独立结论（PR #121 的 D3）。

**现在的规则**（按 namespace 分流，不扩容词表）：

| 输入的 scope | 结果 |
| --- | --- |
| 形状非法（大小写、空段、空白、控制字符、段数越界） | **整条 token 401** |
| `registry:` 命名空间、且是已知六项之一 | 保留，参与授权 |
| `registry:` 命名空间、但本部署不认识 | **整条 token 401**（不猜、也不扩权） |
| 其它命名空间（`atlas:` / `observability:` / `portal:` …） | **保留 token，丢弃该 scope** |

丢弃是**收窄**：外模块的 scope 本来就不产生任何 Registry 权限。反过来把整条 token
拒掉，是把「Registry 不认识这个 scope」误判成「这个凭据无效」。`RequesterContext.scopes`
因此只含 registry scope —— 审计与授权看到的是**本模块真正会消费**的那一份。

形状规则与签发方 `backend/auth/tokens._SCOPE_RE` 逐字相同，但**不 import**
`backend.auth`（Registry 不依赖 IAM 包）。两份规则的一致性由
`tests/registry/test_iam_scope_vocabulary.py` 用同一份语料对照钉住，防止漂移。

**仍然 fail closed**：未知的 `registry:*`、畸形 scope、过期/撤销/停用、与受信目录
不符的 principal/uid/linux_user，一律 401；签发方不可用仍 503。没有 registry scope
的操作得到 **403**（身份有效、权限不足），不是 401。

（同批 QA 的 BFF 侧 D4 —— 同源 invoke 对只有 `Authorization`、没有 session 的请求
也要求 CSRF → 403 —— 仍是 BFF owner 的入口契约问题，不在本模块。）

## 2B. `invoke` 的唯一权威路由门（P0，Wave-10）

### 2B.1 缺陷

`RegistryService.invoke` 从前只检查 ``endpoint.status == ready``，然后直接
``self.gateway.forward(...)``。``ready`` 只证明**Endpoint 验证过**，不证明**路由生效了**。
于是下面这些端点都会被转发：

* 刚创建、从未被路由过的（``router_generation=0 < revision=1``）；
* 刚 repoint、Router 还没回读的（``pending_router_sync``）；
* 运维用 ``{"generation": N}`` 声明过、但**没有任何 Gateway 回读**的
  （``router_sync_attested``）；
* 被 `build_routing_snapshot` 排除、Gateway 从未收到过的；
* 本部署根本没配 RouterAdapter 的；
* DB 说已生效、但 Router **此刻**报的是另一代的。

这与 `docs/architecture.md` §5.2 规则 3 和 V1-03 相悖：人工声明不能冒充 applied。

### 2B.2 现在的门

`invoke` 在**任何 Gateway 调用之前**依次检查：

1. 可见性（不可见 → 404，与不存在同形）；
2. ``status == ready``（→ 409 ``registry.not_verified``）；
3. **Gateway 是否接线**（没接 → 503 ``registry.upstream_unavailable``，
   消息含 ``gateway_not_attached``）——部署事实，不是凭据/路由问题；
4. 所有权/协议两根轴（既有顺序保留）；
5. **路由是否真的生效**（``_require_route_applied``）：
   * ``router_sync_state() == router_sync_applied``（排除 pending / attested）；
   * ``router_ack_evidence == gateway_readback``；
   * 本部署有 attached RouterAdapter；
   * ``router.applied_revision()`` **此刻**等于该 Endpoint 的 ``revision``；
   * 该 Endpoint 出现在**当前可导出的快照**里；
6. 载荷校验 → 审计 → 转发。

拒绝一律 ``409 registry.not_verified``，消息只含状态词。**被拒的调用不会被计量**，
也不会有任何字节到达 Gateway。

**有序的代价**：第 5 步每次 invoke 都会重新导出一次快照并读一次 Router 的
``applied_revision()``（`FileRouterAdapter` 是一次 `status.json` 读取）。对当前规模
（单机、几十条路由）可以接受；若将来成为热点，应当在**不削弱**上述判据的前提下
加缓存，而不是删掉其中任一条。

### 2B.3 其它 Lane 的 fixture 影响（必须由各自 owner 处理）

本门禁会让「没有路由确认也 invoke」的 fixture 从 200 变成 409。**这是预期的**，
不是回归；不要通过删断言或跳过测试来让它变绿。

本 PR 已更新 `tests/registry/**` 下的全部相关 fixture（新增
`Harness.confirm_route()` 与 `Harness.acknowledge_route()`）。**跨 Lane 的已知影响**：

| 文件 | 测试 | 需要的动作 |
| --- | --- | --- |
| `tests/bff/test_cross_module_e2e.py` | `test_sharing_to_a_uid_gives_b_visibility_and_invocation` | 在 invoke 前确认路由（见下） |
| `tests/bff/test_two_user_acl.py` | `test_b_can_then_invoke_it_and_the_mock_gateway_answers` | 同上 |

（`tests/bff` 归 BFF owner；本 Lane 不改。）

**在任意 fixture 里补齐一次路由确认**（不依赖 `tests/registry` 的 helper）：

```python
endpoint = store.endpoint_by_id(endpoint_id)
deployment = store.deployment_by_id(endpoint.deployment_id)
service.config = dataclasses.replace(
    service.config,
    engine_secret_refs={deployment.engine_id: {"file": "/etc/llmlab/engines.env", "var": "K"}},
)
service.router = <一个 publish()/applied_revision() 一致的 Router 替身>
store.ack_router_generation(
    endpoint_id, generation=endpoint.revision,
    actor_principal="system:router-sync", actor_uid=0,
    at=service._now(), evidence="gateway_readback",
)
```

更接近真实路径的做法是 repoint 到一个同 engine 的兄弟 deployment，让
`_attempt_router_sync` 自己完成回执；两条路都要求**快照非空**（engine 有 secret_ref），
否则回执会落空。

## 3. 未做、也不声称的（`HOST_PENDING`）

- **没有任何 Dell 现场事实。** 本轮全部是 CPU-only Linux 开发主机上的合成证据。
- RouterAdapter 的真实 `status.json` 发布、真实的 Gateway 进程、真实的 ACL 执行、
  真实的 key 目录：**都没有**。回环上的「Gateway」是**测试替身**，它证明的是
  「代码会照着 Gateway 的判定做决定」，不是「Gateway 真的这么判定」。
- 上真机还需要两件**本轮不做**的事：一个运维建好的、Gateway 读得到且 Registry 组
  可写的快照目录，以及一份 `engine_id -> {file, var}` 的 `engine_secret_refs` 配置。
- `/api/v2/registry/*` **仍是模块级 WSGI**，未被同源挂载（`tests/bff/test_route_map.py`
  继续把它钉在 404）。本轮没有为它发明任何浏览器路由。
- 没有新增任何 Session / Principal / ACL 概念；没有第二套身份通道。

## 4. 复现

CPU-only Linux 开发主机，无 GPU、无网络到 Dell、不安装任何东西：

```text
python3 -m unittest discover -s tests/registry -t . -p 'test_*.py'
  → Ran 522 tests ... OK        exit 0

python3 -m unittest discover -s tests/bff -t . -p 'test_*.py'
  → Ran 298 tests ... OK        exit 0
```

`tests/registry/test_bff_seam.py` 是本文件第 1 节那条声明的执行体；
`tests/registry/test_router_sync_evidence.py` 是第 2 节那条契约的执行体；
`tests/registry/test_router_ack_binding.py` 是 §2.3 那条回执绑定的执行体；
`tests/registry/test_schema_v3_rollback.py` 是 §2.4 那条回滚警告的执行体；
`tests/registry/test_v1_06_07_registry_acceptance.py` 与
`tests/registry/test_cross_uid_redaction.py` 是 V1-06 / V1-07 的 Registry 侧验收
（CPU-only、合成、**不声称 REAL**）；
`tests/registry/test_iam_introspection_seam.py` 是 §2A 那条受信 introspection 契约的
执行体（27 条）；`tests/registry/test_iam_scope_vocabulary.py` 是 §2A.5 那条 scope
词表规则的执行体（20 条）。
