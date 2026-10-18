# V1-68 离线纵切门（Issue #68）

> **状态：`OFFLINE_ACCEPTANCE`（仅离线；不含任何现场事实）**
> Owner: Registry / cross-Lane test · Issue [#68](https://github.com/ProfessorZhi/llm-lab-portal/issues/68) ·
> 总控 [#80](https://github.com/ProfessorZhi/llm-lab-portal/issues/80)
>
> **公开集成候选（本文件当前版本）**
> Base: `integration/v1-wave14-green-20261018` @
> `5ea33c942d857283378b1c4d3ce9b95786f54b97`（PUBLIC `ProfessorZhi/llm-lab-portal`）
> 叠加：`tests/v1_vertical/**`（28 + 17 = **45 tests**）
> 结果：**45/45 OK · exit 0**（本文件与用例均为 test-only 增量）
>
> **历史（Wave-5 / #121，保留为证据链）**
> `test/v1-68-offline-gate-wave5-20261018` @ `b964545`（stacked on #114 `ee69e19`）。
> 当时在 #114+#115+#116 的 TEMP union 上为 `13 ok + 4 DEPENDENCY_PENDING`；
> 那 4 项依赖在 wave14 候选上均已落地，见 §3.1。
>
> **Wave-4（历史记录，#114 原始版本）**
> Base: `integration/v1-wave8-bff-events-20261018` @ `4137737`（PR #112）
> `tests/v1_vertical/test_v1_68_offline_vertical.py` · **28 tests · exit 0**

## 1. 一句话结论

`V1-04..V1-09` 要求的**离线纵向链路**现在从**同一个同源 HTTP 入口**走通：
B 用自己由 IAM 签发、并由受信 introspector 确认的凭据 → 经 `POST
/api/v2/ops/endpoints/{id}/invoke`（会话+CSRF 或 scoped token）→ Registry 的行级
ACL/就绪/**Gateway 回读 applied 门** → 到回环 synthetic Gateway 拿到 `request_id`；
A 撤权后 B 下一次被拒且 Gateway 计数不增；C 不可枚举；Calls 只显示自己的行、
未测得为 null。两条历史装配缝（BFF invoke、Registry token 权威）都已闭合。

**这仍只是离线验收**：没有任何 Dell/PAM/真实 Gateway/Strata 现场事实，见 §6。

## 2. 真的 / 假的 分界

| 环节 | 本文件里是什么 | 性质 |
| --- | --- | --- |
| scoped token 签发 / introspect / 撤销 | `backend/auth/tokens.ScopedTokenService` | **真代码** |
| token → 主体（BFF） | `ScopedTokenPrincipalSource`（`enabled=True`） | **真代码** |
| 会话、CSRF、origin、身份字段守卫 | `OpsApplication` 真实路由与 preflight | **真代码** |
| 登记 / 验证 / 共享 / 撤销 / repoint CAS | `RegistryService` 真实状态机 | **真代码** |
| 路由 ACK（revision + snapshot 绑定） | `RegistryService._attempt_router_sync` + `ConfirmingRouterAdapter` | **真代码 + 测试替身 Router** |
| Gateway 转发 / key 解析 / request_id | `TrustedGatewayAdapter` → 回环 `SyntheticGateway` | **真代码 + 测试替身 Gateway** |
| Calls 归属 / 过滤 / null 口径 | `backend/atlas/calls.build_self_calls_dto` | **真代码** |
| Calls 语料 | 合成 JSONL（`source=synthetic-fixture`） | 合成 |
| PAM | `tests/bff/support` 的合成 broker | 替身 |
| 主机投影 / 观测 | harness 的合成 fixture | 合成 |

**没有出站网络**：唯一的网络是 `127.0.0.1` 上的 `SyntheticGateway`。
`transport.synthetic is True` / `gateway_attached is False` 是被断言的。

## 3. 两个阻断的装配缝

### 3.0 Wave-5 复测：源补丁已出现，缝的**形状**变了

#114 记录的两条 absence 断言**继续成立**（它们是历史事实），但它们指向的缺口已经有
源补丁。本门禁在**独立 TEMP union**里按精确 SHA 组合它们，不改任何 owner 代码：

| 缝 | 源补丁 | 精确 SHA | 当前状态 |
| --- | --- | --- | --- |
| SEAM-1 | BFF #115 | `7d294313994220d6e3001c54877cfb69884e062e` | **已闭合**：`POST /api/v2/ops/endpoints/{id}/invoke` 存在，会话+CSRF 可用 |
| SEAM-1 目录 | BFF #120 | `cece9338cc10439558d01b9187419977bfe8c089` | 第 20 条路由登记（catalogue） |
| SEAM-2 | Registry #116 | `769825b8017c8a8f48266ad9ad1f76836fad4d58` | **已闭合**（wave14 候选上词表亦已对齐）：受信 introspector 可用，真实 IAM token（含 portal scope）可被确认 |
| 集成（局部） | #119 | `a8ecfc210cb07825b4325a83f7b0965faca6524a` | Registry seam 的局部集成候选（本门禁未依赖它） |

> `#114` 的旧断言**没有删除**：`test_the_same_origin_bff_has_no_invoke_route` 仍钉
> `/api/v2/registry/*` → 404（新入口在 `/api/v2/ops/*`，不是把旧前缀挂上）；
> `test_the_registry_module_refuses_the_iam_token_service` 仍成立，并且 Wave-5 新增
> 一条更强版本断言它现在是 **503 语义**（`TokenAuthorityUnavailable`），而不是笼统的
> `InvalidRequest`。

### SEAM-1 · 同源 BFF 没有 invoke 路由（BFF #79）—— **Wave-4 时**

`backend/bff/route_map.py` 不挂载 `/api/v2/registry/*`；`tests/bff/test_route_map.py`
把它钉在 404。所以浏览器同源入口**没有**「B 调用 A 共享的 Endpoint」这条路。
本文件里 B 的调用走 **Registry 服务层**，主体/scopes 来自真实 IAM introspection。

* **测试证据**：`SeamInventoryTest.test_the_same_origin_bff_has_no_invoke_route`
* **需要的动作**：BFF owner 决定是否提供一个**同源、会话/CSRF 守卫、有界**的
  invoke 路由（或明确「invoke 永不同源」并写进契约）。在此之前 V1-07 的
  「经浏览器同源调用」**不成立**。

### SEAM-2 · Registry 模块拒绝 IAM token 服务（Registry #76）—— **Wave-4 时**

`backend/registry/identity.resolve_requester` 对 `tokens` 做
`isinstance(TokenDirectory)` 硬校验。因此 Registry 的 WSGI 应用**无法**接受
`backend/auth/tokens.ScopedTokenService`；把 IAM token 复制进第二个
`TokenDirectory` 就是**第二套 token 账本**，本文件明确不做。

* **测试证据**：`SeamInventoryTest.test_the_registry_module_refuses_the_iam_token_service`
* **需要的动作**：Registry owner 提供一个「受信 introspector」装配点
  （与 `ScopedTokenPrincipalSource` 同形），或者在 BFF 同源层统一 token 通道并让
  invoke 走 BFF（与 SEAM-1 一起解）。

两条缝都**不影响**本文件已通过的断言，但它们决定 V1-07 能否标 complete。

### 3.1 Wave-5 的四个依赖：历史待落地 → 在 wave14 候选上已闭合

Wave-5 的正向测试把「缝闭合之后应当成立什么」写成了可执行断言。它们**从未**被
`skip` / `expectedFailure` 掩盖；依赖未落地时它们就是红的。

| # | 用例 | 曾待落地的依赖 | 历史实测（#114 union） | 现在（`5ea33c9`） |
| --- | --- | --- | --- | --- |
| D1 | `test_invoke_is_refused_while_the_route_is_only_pending` | Registry #76 P0（router-applied 硬门） | 无门；pending 仍 200 | **已闭合**：`NotVerified` 且 Gateway 计数不增 |
| D2 | `test_an_attested_ack_is_not_applied_and_does_not_open_invoke` | 同上 | attested 仍放行 | **已闭合**：attested 被拒 |
| D3 | `test_the_trusted_introspector_confirms_the_iam_token` | Registry #116 scope 词表 | portal scope 使整条 token 被拒 | **已闭合**：词表对齐，introspect 成功 |
| D4 | `test_b_invokes_with_their_own_scoped_token` | BFF #115 写前置 | Bearer 也要求 session CSRF → 403 | **已闭合**：token 可调用 |

隔离变量仍然保留：`test_the_trusted_introspector_accepts_a_registry_scoped_iam_token`
用只含 registry scope 的 token 通过 —— 它证明的是 introspector **机制**本身工作，
与词表对齐问题彼此独立。

**夹具修正（test-only）**：正向路径现在先用候选既有的
`Harness.acknowledge_route()`（写下 `router_sync_applied + gateway_readback +
可导出快照` 的同一行）再调用；**负例**（pending / attested / stale / foreign UID /
撤销）不经过这个 helper，仍从各自的初始状态出发，并断言零 Gateway 调用。

## 4. V1-04..09 的离线覆盖

| 验收项 | 测试类 | 覆盖到的 |
| --- | --- | --- |
| V1-04 身份 | `V104IdentityBoundaryTest` | 匿名 401（五个数据路由）；伪造 `X-UID`/`X-Owner`/`X-Role` → 400；无 CSRF 写 → 403 且无副作用；stale Host → 421；Idempotency-Key 重放返回同一对象；repoint revision 不符 → 409 且不改 |
| V1-05 路由就绪 | `V105RouteReadinessTest` | registered / ready / pending_router_sync / applied 四轴分离；ACK 必须绑定**本次快照里发布了的那一条**；detached adapter 对照仍 pending；替身回读成功不写 LIVE |
| V1-06 登记与可见性 | `V10607ShareInvokeRevokeTest` | A 私有登记；B 看不到（owned/shared 皆空）；C 看不到 |
| V1-07 共享/调用/撤权 | `V10607ShareInvokeRevokeTest` | B 用自己的 scoped token 得到 requester（无 admin scope）；经 `TrustedGatewayAdapter` 调用成功，`result.request_id` 存在，替身看到的是 **B 的 key**；撤权后 B 下一次 NotFound 且**不再**打 Gateway；repoint 默认收回旧授权 |
| V1-08 Calls 归属 | `V10809CallsAtlasIsolationTest` | B 只看到 `key-dave`；A 只看到 `key-bob`；两人 request_id 集合无交集；C 的行不可见 |
| V1-09 Atlas 聚合口径 | `V10809CallsAtlasIsolationTest` | 未测量行 `tokens.{input,output,total}` / `ttft_ms` / `queue_ms` / `total_ms` 全为 **null**（不是 0）；`coverage.*_unknown_rows` 计数；`transport.mode=demo` |
| 未配置依赖 | `FailClosedUnconfiguredTest` | detached Gateway → invoke `UpstreamUnavailable`；无观测源 → `/api/v2/ops/usage` 503；无 Linux auth → 登录 503 `linux_auth_not_configured`；无 Atlas → `/api/atlas/calls` 503 |

### 4.1 Wave-5 新增覆盖（`test_v1_68_wave5_invoke.py`）

| 验收点 | 用例类 | 在 TEMP union 上的结果 |
| --- | --- | --- |
| 同源 invoke 正向（会话+CSRF） | `SameOriginInvokePositiveTest` | 200；`portal-invoke/1`；`request_id` 由 Gateway 路径产生；替身看到 **B 的 key** |
| 响应收窄 | `test_the_response_withholds_model_body_and_echo` | `result` 无 `body` / `echo` |
| 旧前缀仍不挂 | `test_the_sanctioned_route_is_under_ops_and_the_legacy_prefix_stays_unmounted` | `/api/v2/ops/...` → 200；`/api/v2/registry/...` → 404 |
| 无 CSRF / 跨 Origin | `test_a_write_without_csrf_…` / `test_a_cross_origin_…` | 403，Gateway 计数不变 |
| 过大 payload | `test_an_oversized_payload_is_413_…` | 413 `payload_too_large` |
| 伪造 uid/owner/role（头 + 查询串） | `test_forged_identity_headers_and_query_are_400` | 400 |
| C 不可枚举 | `test_c_cannot_enumerate_or_invoke` | 404 `registry.not_found` |
| 撤权后下一次拒绝 | `test_revoking_the_grant_…` | 404 + Gateway 计数不增 + grantees 清空 |
| **applied 正向** | `test_invoke_succeeds_after_an_applied_gateway_readback` | `router_sync_applied` + `evidence=gateway_readback` + 调用成功 |
| **applied 负例 ×2** | `StrictAppliedReadbackGateTest` | pending / attested 均被拒且零 Gateway 调用 |
| 受信 introspector（registry scope） | `test_the_trusted_introspector_accepts_…` | ok |
| 受信 introspector（真实 IAM token） | `test_the_trusted_introspector_confirms_…` | ok（wave14 词表已对齐） |
| raw IAM service 被拒（**保留的负测**） | `test_the_raw_iam_service_is_not_a_registry_authority` | `TokenAuthorityUnavailable`，`contract_code=registry.token_authority_unavailable`（→ 503） |
| token 通道调用 | `test_b_invokes_with_their_own_scoped_token` | ok（wave14 写前置已收敛） |

## 5. 复现

Tokyo（`device:tokyo-shared-dev-01`），CPU-only，无 GPU、无出站网络、不安装任何东西：

```text
# 当前：公开集成候选 5ea33c9（wave14）
python3 -m unittest tests.v1_vertical.test_v1_68_offline_vertical \
                      tests.v1_vertical.test_v1_68_wave5_invoke
  → Ran 45 tests ... OK                                    exit 0

python3 -m unittest tests.lanes_candidate.test_candidate_e2e
  → Ran 16 tests ... OK                                    exit 0
```

**历史（保留为证据链，勿当作当前结果）**

```text
# 单独 #114 分支：              Ran 45 ... failures=12, errors=4      exit 1
# #114+#115+#116 TEMP union：   Ran 45 ... failures=3, errors=1       exit 1
#   四项依赖见 §3.1，现已全部闭合。
```

**Integrator 的最小 diff 交接**：本 PR 只含 `tests/v1_vertical/**`（45 项）、
`tests/lanes_candidate/test_candidate_e2e.py` 的夹具修正、与本文件。叠加到
`5ea33c9` 即可复现 **45 + 16 全绿**；**不需要任何 backend 改动**，也不删除任何
既有负例或安全断言。

**断言可证伪检验**（只翻转夹具条件，不改任何 owner 源码）：

| 翻转 | 观测 |
| --- | --- |
| token 通道 `enabled=False` | `/api/v2/ops` 由 200 → **401** |
| 去掉 CSRF 头 | share 由 200 → **403** |
| 不补 `engine_secret_refs`（快照为空） | `acked_endpoints` 由 `[ep_…]` → **`[]`** |

三条都翻转成功，说明这些断言不是恒真。

## 6. `HOST_PENDING`（本门禁**不**证明的东西）

- **没有 Dell 现场事实**：没有真实 PAM、真实 Gateway、真实 Strata、真实 key 目录、
  真实 GPU 推理。所有替身都在 `127.0.0.1` 上。
- **V1-07 的「经真实 Gateway 调用」**：本文件用的是 `SyntheticGateway`，
  `result.synthetic is True`。真实 Gateway 的 ACL/计量/request_id 归属**未验证**。
- **同源路径**：会话+CSRF 与 scoped-token 两条通道都已在 `5ea33c9` 上离线跑通；
  但它们指向的是回环 synthetic Gateway，**不是**真实 Gateway 的入口。
- **router-applied 硬门**：在 `5ea33c9` 上已落地（`NotVerified` + 零 Gateway 调用）。
  这证明的是**离线替身**的回读（`ConfirmingRouterAdapter` / `_FixedReadbackRouter`），
  **不是**真实 Gateway 发布的 generation/revision 与 snapshot SHA 回执。
- **跨进程/跨主机**：全部在同一进程内（外加一次回环 HTTP）。
- 未声称 `LIVE` / `REAL` / `RUNTIME_VERIFIED`，未做任何 live E2E。

## 7. 边界

本 PR 是 **test-only**，只含：

* `tests/v1_vertical/**`（从 #121 `b964545` 带过来的 45 项）；
* `tests/lanes_candidate/test_candidate_e2e.py` 的**夹具修正**（正向路径先装配
  `acknowledge_route()`；负例与零 Gateway 调用断言保留）；
* 本文件。

**未修改** `backend/auth`、`backend/bff`、`backend/registry`、`backend/atlas`、
`backend/observability/strata_readonly.py`（PR #127 的路径）、前端、真实 Gateway、
`integration/*` 分支、源 PR 或任何其他 Owner 的工作树。未复制任何 **private 仓库**
的代码/数据/文档/历史到公开仓库。

未安装 PAM、未用 root、未碰 GPU/Strata/Caddy/systemd/8443/生产 Gateway、未动 main
或 Dell #83。只做定向 CPU unittest；本机 SwapFree ≈2.2/32 GiB 且已有其他用户的重活，
因此**未跑 full Python / Chrome**。
