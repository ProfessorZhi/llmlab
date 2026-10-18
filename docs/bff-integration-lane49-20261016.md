# 集成 Lane（Issue #49）· 同源 BFF 的路由表、守卫与接线契约

> 本文是 **Integration Lane** 的交付说明：Phase A 落了什么、每条跨 Lane 的接缝长
> 什么样、以及**还没做**的部分与它们各自归谁。
>
> 基线：`integration/v6-operations-linux-auth-20261016` @
> `7fcc7d4fbbbf395b87e0e7251a4916856ec1e891`（PR #41，即当前 8443 预览的发布 SHA）。
> 总控 Issue #43 · 审计 PR #42 · 本 Lane 的工单 Issue #49。
>
> **本文不声称任何东西已部署。** 全部改动是源码与测试；没有重启、没有 systemctl、
> 没有碰 PAM / Caddy / Strata / Gateway / 任何人的 GPU 任务。

## 0. 一句话

Phase A 把「同源 BFF 有哪些路由、每条谁能调、数据从哪来」从一串 `if` 变成一张**可
断言的表**，并补上两条此前**完全不存在**的服务端守卫（`Host` 权威、来源声明），
再把「浏览器会话」与「scoped CLI token」拆成两条**各自可声明、可关闭**的凭据通道。
跨 Lane 的功能实现一行没动——需要别的 Lane 提供的东西，这里只写契约与 fixture。

## 1. 路由表（唯一事实）

`backend/bff/route_map.py` 的 `ROUTES` 是这张表。`app.py` 的 `_dispatch` **由表
驱动**：查不到声明就走不到任何实现，「路径存在但方法不对」得到 405，其余 404。

| 路由名 | 方法 | 路径 | 身份 | 读写 | CSRF | 数据来源 | 归属 |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `ops.read` | GET | `/api/v2/ops` | 会话 | 读 | — | Registry | Integration |
| `ops.usage` | GET | `/api/v2/ops/usage` | 会话 | 读 | — | 观测 | Data #47 |
| `ops.engines` | GET | `/api/v2/ops/engines` | 会话 | 读 | — | 观测 | Data #47 |
| `ops.share` | PUT | `/api/v2/ops/deployments/{id}/share` | 会话 | **写** | **要** | Registry | Integration |
| `auth.linux.login` | POST | `/auth/linux/login` | **匿名** | **写** | **要** | 会话 | IAM #45 |
| `auth.linux.status` | GET | `/auth/linux/status` | **匿名** | 读 | — | 会话 | IAM #45 |
| `auth.admin.status` | GET | `/auth/admin/status` | **匿名** | 读 | — | 会话 | IAM #45 |
| `auth.admin.enter` | POST | `/auth/admin/enter` | 会话 | **写** | **要** | 会话 | IAM #45 |
| `auth.admin.exit` | POST | `/auth/admin/exit` | 会话 | **写** | **要** | 会话 | IAM #45 |
| `auth.logout` | POST | `/auth/logout` | 会话 | **写** | **要** | 会话 | IAM #45 |

「匿名」是**一个封闭集合**，只有三条：登录入口本身，与两条「这条路径此刻什么状态」
的只读说明。`tests/bff/test_route_map.py` 逐条断言：任何一条会话路由被匿名调用都是
**401**；任何一条路由用未声明的方法都是 **405**；任何一条受保护路由带上 `X-UID` /
`X-Role` / `X-Admin` / `X-Owner` / `X-Linux-User` 都是 **400**。

`/auth/` 下的**其余**路径继续原样交给既有的 `backend.auth.routes.Application` —— 它
是那条路径的唯一实现者，本 Lane 不复制它的路由表。

## 2. 两条新守卫

### 2.1 `Host` 权威守卫（`backend/bff/origin.py`）

**此前 BFF 没有任何 Host 校验**（`dev/preview_auth.py` 有一份，但那只是预览进程）。
现在每个请求在**看 cookie / CSRF 之前**先过这一关，不通过就是 **421
`misdirected_request`**，理由是 `missing_host` / `malformed_host` / `foreign_host` /
`port_mismatch` 之一。

判定输入是**整个字段值**（`fullmatch`，没有「取冒号之前」的步骤），端口必须是
1–65535 的 ASCII 十进制数字，且必须等于请求**实际到达**的端口（`SERVER_PORT`，由
`wsgiref` 从绑定 socket 填）。因此 `127.0.0.1:59008.evil.com`、
`127.0.0.1:59008,evil.com` 这类走私写法都不是「大致对」，而是畸形。

**默认只认环回**。反代后面用真实主机名是部署决策，必须由运维显式传
`--trusted-authority portal.example.com:8443`（或 `AuthorityPolicy.build([...])`）；
解析不出来的条目会让进程**启动即失败**，而不是被静默丢掉。一个没被配置过的部署
不会因为「反正没人配」而接受任何外部主机名。

与预览进程的关系：语义同一份，由 `tests/bff/test_host_guard.py::AuthorityParityTests`
用**同一张用例表**同时喂两边，任何一边漂移都会失败。

### 2.2 来源声明守卫（`backend/bff/identity.py`）

`?source=` 与 `X-Source` / `X-Data-Source` / `X-Synthetic` / `X-Live-Data` 出现在
**数据路由**上即 **400 `source_assertion_rejected`**。

理由与「拒绝自报身份」同一条：静默忽略意味着下游某一层可以再把它捡起来，而一份
可以把合成数据标成 live 的接口，和一份会说谎的接口没有区别。前端页面 URL 上的
`?source=live` 是**页面**的取数模式（`web/js/features/source.js`），不是发给 API 的
参数；今天前端也从不把它发给 API，所以这条守卫不改变任何既有调用。

服务端**自己**写进 payload 的来源因此必须诚实——见下一节，那里修掉了一个真的谎。

## 3. Phase A 修掉的两个真实缺陷

两个都在本 Lane 拥有的文件里，都是「同一份事实两处判定、只有一处写对」。

**3.1 测试替身被标成 live。** `backend/bff/ops.py::OpsConfig.transport_mode` 只检查
`synthetic` 与 `gateway_attached`，于是 `gateway_attached=True` 的
`MockGatewayAdapter` 会让页面顶栏写「**LIVE · 同源 BFF**」；而同一个 `OpsConfig`
在 `build_ops` 里是把试跑台关掉的（那里一直写着 `!= "mock"`）。两处判定同一件事、
一处漏了。现在两处读同一个常量 `GATEWAY_ADAPTER_MOCK`，测试替身永远只能是 `demo`。

**3.2 portal 管理面按 Registry 词表开门。** `build_ops` 原本用
`requester.is_admin_read`（即 Registry 的 `registry:admin:read`）决定
`admin.principals` —— **全平台账号名单**。对会话而言两个词表等价
（`session_scopes` 让两条 admin read 恒同进同出，`tests/bff/test_scopes.py` 断言了
这件事），所以这个缺陷在只有会话通道时**看不出来**。Phase A 引入 token 通道后它
立刻变成一条真实路径：一条只被授权读登记库的 scoped token 能看见全员名单。现在
管理面按 `portal:admin:read` 判定——两个词表各管各的面。

## 4. 凭据通道：浏览器会话 vs scoped CLI token

`backend/bff/identity.py` 新增 `PrincipalSource` 协议与两个实现。**默认只启用会话
通道。**

* `SessionPrincipalSource` —— 既有路径的薄包装（cookie → 服务端会话行 → 账号目录），
  没有第二套身份实现。
* `ScopedTokenPrincipalSource` —— `Authorization: Bearer <token>` → 主体。**默认
  关闭**，关闭时它仍然**负责**带该头的请求，只是对它们一律 **401
  `token_auth_not_enabled`**。让给会话通道去解释，等于把一次凭据混淆静默解决掉。

打开时，token 的解释**完全**交给一个 `introspector`。本层不解析自签结构、不查
自己的表——它只把「这条凭据是谁」转给那条通道的权威实现，再把答案翻成 `Principal`：

```
Authorization: Bearer <raw>
  → introspector.introspect(raw)        ← 通道的权威实现，本层不复制
    → 只认 active；其余理由全部塌成同一个 401
      → uid 过账号目录（与会话通道同一条规则）
        → Principal(user=None, elevated=False, scopes=binding ∩ portal 词表)
```

token 不是会话，所以 `Principal.user is None`、`elevated` 恒 `False`、portal scope
是「签发的那些 ∩ portal 词表」而不是角色那一组。

### 接线来源：IAM #45 已经实现了这条通道

`feature/v6-closure-iam-20261016` 上的 `backend/auth/tokens.py` 提供了
`ScopedTokenService`（签发 / 内省 / 撤销 / 轮换 / 限速）与
`TokenIntrospection`——后者的字段（`active` / `principal` / `uid` / `linux_user` /
`scopes` / `roles` / `label`）正是本层读的那几个，而且它自己的
`to_registry_dict()` 已经声明「这就是 `backend.registry.identity` 解析所依据的
形状」。

因此本层**不 import** 那个模块（它会先落在别的分支上），只要求一个提供
`introspect(token)` 的对象。

### 给 IAM #45 的契约与接线步骤

| 项 | 约定 |
| --- | --- |
| 接缝形状 | `introspect(raw) -> TokenIntrospectionLike`：读 `active` / `principal` / `uid` / `linux_user` / `scopes` / `roles` / `label`。其它字段不读、不要求。 |
| 接线 | 装配处一行：`ScopedTokenPrincipalSource(introspector=token_service, accounts=accounts, enabled=True)`。本文件与 `app.py` 都不需要改。 |
| 打开条件 | 签发 / 到期 / 撤销 / **审计**都有真实实现之后，才由运维把 `enabled=True`。在那之前默认关闭是**刻意的**：一条没有审计的旁路比没有通道更糟。 |
| 失效理由 | 本层把 `active=False` 的一切理由塌成同一个 401（`no_authenticated_session`）。请**不要**要求把 `revoked` / `expired` 回给调用方——那会把一个可用性试探变成预言机。理由留在服务端日志里。 |
| 提权 | token **不能**提权。管理员模式是**每条会话**的状态；需要管理员级 token 能力时，走「给绑定加 scope」而不是「给 token 一个提权位」。 |
| uid 一致性 | 若装配时给了 `accounts`，introspect 说的 `uid` 必须在目录里、且登录名一致，否则 401。两条通道对「这个 uid 是谁」不能给出两个答案。 |
| 展示 | `ops["keys"]` 只出现掩码前缀，永远没有凭据本身（既有断言）。 |

**今天就能端到端测。** `identity.RegistryTokenIntrospector` 把本仓库既有的
Registry 凭据目录适配成同一个契约，`tests/bff/test_principal_sources.py::
RegistryBridgeTests` 用它把这条通道跑到 HTTP 层。它只是契约的 fixture：内存目录、
无到期、无审计，**不是**可以在生产打开的签发者。

## 5. 尚未承接的同源入口（**Blockers**）

Issue #49 要求的同源入口是六组。今天 `backend/bff` 里只有 `auth/*` 与
`api/v2/ops*`；**下面四条前端已经在调，但同源 BFF 还没有实现**，因此现在一律
**404**（`tests/bff/test_route_map.py::test_the_frontend_contracts_the_bff_does_not_serve_yet`
把这条实况钉住）。它们的数据面分别归别的 Lane / 别的模块，本 Lane 只拥有接线，
所以 Phase A **没有**代替实现：

| 入口 | 前端调用点 | 数据面归属 | 缺什么 |
| --- | --- | --- | --- |
| `/api/atlas/calls` | `web/js/calls/controller.js` | Data #47（`backend/atlas`、`backend/observability`） | 服务端 adapter + 本表新增一行 + 按主体过滤的 ACL 证明 |
| `/api/atlas/modules` | `web/js/modules/controller.js` | Data #47 | 同上 |
| `/api/infra/summary` | `web/js/infra/host-status.js` | Data #47（host freshness 契约） | 契约要求 `source=prometheus-readonly` / `synthetic=false` / `readonly=true` + 新鲜度；目前只有 `web/tools/serve.mjs` 的预览投影（默认关闭，`ATELIER_INFRA_READONLY=1` 才开） |
| `/api/v2/registry/*` | CLI 与 Registry 自身 | Registry/CLI #46（`backend/registry/api.py` 已有 `PREFIX`） | 与 BFF 身份/ACL 的**同源装配**；在完成前不许把 :8080 或未授权的 Registry 端口暴露给浏览器 |

**其它已知边界：**

1. **401 用词分歧（待 IAM #45 决定）。** BFF 自有路由说
   `no_authenticated_session`，`/auth/*`（既有 `backend.auth`）说
   `not_authenticated`。两者都是 401、前端都读成未登录，所以不是功能缺陷，但同一
   个源上有两个词是真实的用词分歧。统一它要改 `backend/auth/**`，归 IAM Lane。
   `tests/bff/test_route_map.py::test_authenticated_401_vocabulary_is_pinned_on_both_sides`
   同时断言两个 token，任一边改词都会失败。
2. **真实 PAM 后端不存在。** 登录路由存在、受 CSRF 与同源保护、并以 503
   `pam_unavailable` 诚实收场；`/auth/linux/status` 明说
   `password_verification: "unavailable_in_this_build"`。没有任何测试声称密码可用。
3. **真实 Gateway 未接。** 转发层是 mock 或 detached，`transport.mode` 因此只能是
   `demo`；试跑台关闭。Dell 上跑着的 Gateway（`:8200`）与 Strata（`:8080`）**未被
   触碰**。
4. **管理口令提权**仍是「每条会话 + 共享口令 + TTL ≤ 15 分钟」；本构建没有让任何
   账号成为常驻管理员。

## 6. 跨 PR 合并的次序（**不由本 Lane 单方面执行**）

依赖方向不变（见 PR #42 的 DAG）：先安全/DTO 契约，再 UI 可信状态，再
Registry/Gateway，最后 per-user 使用与报告。本文与 `ROUTES` 是那份契约在**路由层**
的落地。其他 Lane 的 PR 合并进集成分支**需要协调与复测**；本 Lane **没有**合并任何
其他 Agent 的分支，也不改它们的未提交工作。

## 7. 实测与未实测

见 PR 描述里的数字（真实命令、真实计数、真实平台）。**未实测**的部分明确列出：
真 Chrome 浏览器验收（Dell 上无 Chrome/Edge）、真实 PAM、真实 Gateway/Registry
路由、`/api/atlas/*` 与 `/api/infra/summary` 的同源实现、以及任何形式的部署。

`NOT DEPLOYED / NO RUNTIME VERIFICATION` —— 本批改动只在 CPU-only 的进程内 WSGI
测试里跑过。
