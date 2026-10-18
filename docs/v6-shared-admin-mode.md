# 共享管理员模式（Atelier V6）

> 本文说明 `backend/bff/admin_mode.py`、`backend/bff/{scopes,identity,app}.py` 的相关改动、
> 三个路由、界面入口与运维步骤。**只适用于非生产环境。**

## 0. 一句话

**登录决定你是谁；管理员模式决定你此刻能看什么。**

任何获准的成员用自己的 Ubuntu 账号通过 PAM 登录后，**默认都是 `member`**。只有在自己
**那一条会话**里输入全平台共享的独立管理口令，那条会话才会在 **≤ 15 分钟**内变成
`platform_admin`。退出、过期、登出、口令轮换、功能停用，任何一种发生都会立刻回到 `member`。

`alice` 在 Dell 上是基础设施管理员，`root-owned` 的授权文件也确实把他的用户行标成
`platform_admin` —— 但他在 Portal 的**普通会话里仍然是 `member`**。Strata / Qwen 那侧的
归属不受本功能影响，仍归 `alice`。

---

## 1. 本次修掉的缺陷（先读这一节）

在这之前，`backend/bff/scopes.py` 的 `ROLE_SCOPES` 是**按用户表里的 `role` 列查表**的：

```
用户表 role = platform_admin  →  portal:admin:read + registry:admin:read + ... 全局只读
```

于是 `alice` 只要用 PAM **普通登录**，就自动拿到全局视图 —— 登录决定了可见范围，而这正是
V6 要拆开的两件事。

现在的判定链是：

```
会话 cookie ──► 服务端会话表 ──► 受信主体（只接受 linux_pam 会话）
                                      │
                    admin_mode.is_elevated(session)  ──►  elevated: bool
                                      │
                                 session_role(elevated)  ──►  member | platform_admin
                                      │
                                 session_scopes(...)      ──►  四套 scope 词表
```

**没有任何判权路径读用户表的 `role` 列。** 有测试钉住这一点：

* `tests/bff/test_scopes.py::ElevationIsTheOnlyPromotionTests`
  —— `session_scopes` 的签名里**只有** `elevated` 一个参数（用 `inspect` 断言），
  因此"又把用户表角色传进来"这件事不可能悄悄复发；
* `tests/bff/test_identity.py::test_a_plain_session_is_a_member_even_for_a_granted_administrator`
  —— 断言 `principal.user.role == platform_admin` 与 `principal.is_platform_admin is False`
  **同时成立**；
* `tests/admin_mode/test_http_routes.py::EveryMemberTests` —— 四个 Linux UID 逐一走一遍。

`role_scopes(role)` 那张词表**保留**，因为两套 scope 词表的一致性断言还需要它；但它是词表，
不是判定入口。

---

## 2. 身份与提权的边界

| 会话来源 | 能否进入管理员模式 | 原因 |
| --- | --- | --- |
| Linux PAM 登录（`auth_source = linux_pam`） | **可以** | 唯一有 `(host_id, uid)` 绑定、能落到真实账号的通道 |
| 飞书 SSO（`auth_source = feishu`） | 不可以 → 403 `linux_auth_not_enabled` | 没有 uid，拿不到 Registry 主体，因此没有可提权的会话 |
| 本地演示管理员（`auth_source = local`） | 不可以 → 403 `linux_auth_not_enabled` | 演示用的本地账号；**即使它的 `role` 就是 `platform_admin`** |
| 没有会话 / 会话已过期、已撤销 | 不可以 → 401 | 没有主体 |

这不是"额外加了一道检查"，而是**没有可提权的对象**：提权路由第一步就把 cookie 解成受信主体，
`resolve_principal` 只接受 `linux_pam`。第三个身份来源因此在结构上走不进这个门。

---

## 3. 路由

| 方法 | 路径 | 作用 | 前置 |
| --- | --- | --- | --- |
| `GET` | `/auth/admin/status` | 这条会话此刻是否处于管理员模式 | 无（没有会话也是一个合法答案） |
| `POST` | `/auth/admin/enter` | 用共享口令提权当前会话 | 受信 Linux 会话 + CSRF + 同源 |
| `POST` | `/auth/admin/exit` | 退出管理员模式（幂等） | 受信 Linux 会话 + CSRF + 同源 |
| `POST` | `/auth/logout` | 既有路由；**成功后**顺带清掉该会话的提权行 | 既有规则 |

`status` 的响应体（也是界面唯一需要的形状）：

```json
{"data": {"admin_mode": {"configured": true, "elevated": false,
                          "ttl_seconds": 900, "expires_at": null}}}
```

* 不含散列、路径、UID、登录名；有测试断言这一点；
* `configured=false` 表示**本次部署没有配置共享管理口令**，此时任何 `enter` 都是
  `503 admin_mode_not_configured`，界面把它画成 disabled 的按钮并写明原因。

**刻意没有把 `admin_mode` 塞进 `/api/v2/ops`**：那个 payload 有逐字段的浏览器契约校验
（`web/js/features/contract.js` 会拒绝意外字段），改形状要动一个已经发布、且被逐字节
fixture 钉住的契约。状态因此走自己的路由。

`/auth/admin/*` 的写操作**复用既有 auth 应用的 `_preflight`**（同源整串比较 + 一次性
CSRF token），不在这里重写一份。

---

## 4. 提权状态存在哪里

`AdminModeStore`，**自己的**一个 SQLite 文件（默认 `<state_dir>/admin_mode.sqlite3`，
`0600`；目录 `0700`）。刻意不写进 `backend/auth/store.py` 的会话表：管理员模式是一个可以
整体删掉的叠加层，让"关掉这个功能"变成一次会话表迁移是不划算的。

```sql
admin_elevations(session_hash PK, principal, uid, linux_user,
                 entered_at, expires_at, credential_version)
admin_attempts(scope, key, window_started_at, failures, locked_until)
admin_audit(at, event, outcome, actor, principal, uid, linux_user, admin_mode, detail)
```

* **主键是会话 cookie 的 SHA-256**，不是 cookie 本身 —— 库被读走也拿不到可用的会话
  （与 `backend/auth/sessions.py` 同一纪律）。有测试断言库里搜不到原始 cookie。
* **一条会话一行。** 同一个人的两条会话彼此隔离：一条提权，另一条仍是 `member`。
* **`credential_version`** 是当前散列的摘要。轮换或停用口令后，所有已发出的提权行立刻不
  再 `is_live`，并且下一次读到它时会被顺手删掉。**过期与轮换都靠状态本身失效，不靠缓存
  失效逻辑**。
* **登出**由 BFF 在既有登出**成功之后**调用 `forget_session` 删除该行。反过来的话，一次
  因 CSRF 失败的登出会顺手把人降成 member —— 降权方向是安全的，但"登出失败却掉了管理员
  模式"是个会让人困惑的副作用。重放旧 cookie 也不会恢复提权（有测试）。
* 失败计数复用 `backend/auth/ratelimit.py` 的 `RateLimiter`：本 store 实现了它需要的
  `read_attempt` / `write_attempt` / `clear_attempt` 三个方法，因此**不需要适配层**。
  scope 是 `admin_mode_enter`，与 `local_login` / `feishu_*` 分开计数。

---

## 5. 口令

### 5.1 存储

* 加盐 `scrypt`（`n=2^14, r=8, p=1`，16 字节盐、32 字节摘要）或 `argon2id`，由
  `backend.auth.hashing` 生成与校验 —— 复用既有实现，不新写一套密码学。
* 文件 `0600`、属主为服务账号、**不可是符号链接**（严格于 `local_admin`：它跟随软链，
  这里不跟随，因为"路径是运维给的"不能作为信任链接目标的理由）。
* **安全下限**：`n ≥ 16384`、`r ≥ 8`、`p ≥ 1`（`argon2id` 为 `m ≥ 65536, t ≥ 3, p ≥ 4`）。
  低于下限的散列被判定为 **broken（拒绝加载）**，而不是"能验就行"。
* 空文件、多行有效内容、格式非法 → `broken`；单词 `disabled` → 显式停用。三者可分辨。

### 5.2 默认状态

**默认 `NOT_CONFIGURED`：没有人能进入管理员模式。** 仓库里没有、也没有任何代码路径会
产出一个默认口令、演示口令或空口令。有测试逐个试 `admin` / `changeme` / `demo` 之类的值，
全部必须被拒。

### 5.3 运维侧设置（交互式，口令只从提示读入）

```bash
python -m backend.bff.admin_mode init    --state-dir /srv/llm-lab/portal-auth
python -m backend.bff.admin_mode status  --state-dir /srv/llm-lab/portal-auth
python -m backend.bff.admin_mode rotate  --state-dir /srv/llm-lab/portal-auth
python -m backend.bff.admin_mode disable --state-dir /srv/llm-lab/portal-auth
```

* **没有 `--password` 参数、没有环境变量、没有 stdin 管道**：任何可能落进 shell history、
  `ps` 输出或 CI 日志的通道都被排除。有测试断言 `--password` 会被 argparse 拒绝、
  且环境变量不被读取。
* 最短 16 字符、两次输入必须一致、空白口令拒绝。
* 写入走同目录 `mkstemp` → `fchmod(0600)` → `fsync` → `os.replace`，崩溃不留半个文件。
* `init` 已存在时拒绝覆盖（`--force` 才覆盖）；`rotate` 默认要求先输入当前口令，
  `--recover` 是文档化的 break-glass。
* **`rotate` 与 `disable` 都会立刻作废所有已发出的提权**。

### 5.4 口令在内存里的样子

* 只在 `POST /auth/admin/enter` 的那一个请求体里存在：不写日志、不进审计（审计表**没有**
  凭据列）、不进异常消息、不回显、不进 URL。
* 场景函数里没有任何 `logging`（有测试断言模块不 import 它），也没有把 `password`
  插进 f-string 的地方（AST 级断言）。
* 界面层：读走输入框的值之后**立刻清空**，且**不碰 `localStorage` / `sessionStorage`**
  （有文本级断言）。
* 诚实的限制：Python 的 `str` 不可变，无法被可靠擦除。能做的只是把副本数量与存活时间
  压到最低，而不是假装擦干净。

---

## 6. 审计

一条提权/退出记录长这样：

```
event=admin_mode_enter  outcome=success  actor=bob
principal=linux:dell-test-01:1002  uid=1002  linux_user=bob  admin_mode=true
```

**管理员模式不是一个独立主体。** actor 永远是原来的 Linux uid / 登录名，`admin_mode`
只是一个布尔位，说明"这件事发生在提权期间"。把临时开关写成"另一个账号"，正是让事后
审计说不清"这件事到底是谁做的"的原因。

---

## 7. 管理员模式**不**授予什么

* **不是**读别人的 raw prompt / 图片 / 附件的许可。提权打开的是四套词表里的**全局只读**，
  `admin read` 与 `admin manage` 仍然分离（`registry:admin:manage` 不在任何角色里）。
* **仍然没有** `evaluation:dispatch:real`（真实评测派发在本部署永远关闭）与
  `observability:host:read` 之外的额外能力；host 读数本来就只在管理员侧。
* **不出管理员模式后仍然生效**：没有缓存、没有"重复 cookie 继续访问"。每次请求都重新
  解析会话与提权状态；退出后的下一次请求就是 403。

---

## 8. 界面入口

`web/js/features/admin-mode.js`，导出三个东西：

| 导出 | 作用 |
| --- | --- |
| `describeAdminMode(status)` | 服务端状态 ⇒ 界面状态。**纯函数**，每一种畸形输入都被单测钉住 |
| `renderAdminModeControl(view)` | 界面状态 ⇒ HTML 字符串。纯函数，无 DOM、无网络 |
| `mountAdminModeControl({doc, root, transport, getCsrfToken, onChange})` | 挂进页面并接上调用 |

两条同源调用（`adminModeStatus` / `adminModeAction`）放在**既有的** `source.js` 里，因为
那里已经有审查过的 `endpointUrl` 同源守卫；再抄一份只会得到两份会漂移的校验。写请求除了
既有的 `X-CSRF-Token` **不设任何头** —— 尤其没有 `X-Admin`（有测试断言请求头集合）。

行为：

* `configured=false` → 按钮 `disabled`，并写明"本次部署未配置共享管理口令"；
* 状态未知（预览模式没有服务端会话）→ 画成 unknown 并说明，**不画一个按不出东西的按钮**；
* 提权状态只从服务端读：重画一次就是再问一次服务端，本地不缓存 `elevated`；
* 失败后**重新问一次服务端**，而不是本地推断；
* 一处 CSS 都不加：`web/index.html` 与 `web/css/*` 是生成物，`npm run verify:extract`
  要求逐字节等于生成器输出，因此控件复用既有 class（`.seg` / `.guard` / `.state-pill` /
  `.field`）。

**接线（一行）**：在 ops 控制器挂完面板之后把控件挂进 `#view-admin` 即可：

```js
import { mountAdminModeControl } from './admin-mode.js';
mountAdminModeControl({ doc, root: doc.querySelector('#view-admin'),
                        transport: { mode, base, fetchImpl },
                        getCsrfToken: () => fetchCsrfToken() });
```

本次**没有**改 `web/js/features/controller.js`：它的 DOM 替身（`emptyDoc` / `recordingDoc`）
是一层被冻结的测试面，在它上面加挂载点属于另一件需要单独评审的改动。控件本身已被 32 个
前端测试直接覆盖。

---

## 9. 测试

```bash
CUDA_VISIBLE_DEVICES='' python3 -m unittest discover -s tests -t .
npm test
npm run verify:extract
```

* `tests/admin_mode/**`（158 个）—— 口令文件的状态与安全下限、提权存储、服务语义、
  **HTTP 全链路**（`tests/admin_mode/test_http_routes.py`）、运维 CLI、源码级断言；
* `tests/bff/**` 已按新模型更新，并**加强**了断言：同一个人"提权 / 未提权"两条会话的
  对比、`X-Admin` 在**普通**会话上被拒（在已提权的会话上试没意义）、退出后立刻 403。

HTTP 链路覆盖：四个 Linux UID 的普通登录、正确口令提权、错误口令、空口令、限速与锁定、
CSRF 缺失、跨源 `Origin`、`X-Admin` 伪造、无会话、飞书会话、本地演示管理员会话、登出、
TTL 过期、口令轮换、切用户、双会话隔离、未配置部署、响应不含口令。

### 9.1 已知的既有失败（不修改假绿）

* `tests/serve.test.js` 的 `accepts a root written with forward slashes` 在**基线提交
  `a403896` 上就已经失败**（断言自己的假根目录"需要换一个形状才有意义"，在 POSIX 路径
  下不成立）。本线未修复、也未绕过：`npm test` 前后都是这同一个失败。
* `tests/bff` 有既有的 `ResourceWarning: unclosed database`（基线与本线同为 105 条），
  来自共享 harness 的清理，不是本次引入。

---

## 10. 未完成 / 依赖他人 / 残余风险

| 项 | 状态 |
| --- | --- |
| 真实 PAM 后端 | **仍是未实现**。测试用的还是 `FakePamBackend`；它证明周围机制，**不证明 PAM**。各 Linux 账号是否有可用口令仍为 **UNKNOWN** |
| 路由 / 控件接线到 ops 控制器 | 见 §8，一行，留待单独评审 |
| 共享口令本身 | 一把**全平台共享**的秘密：任何拿到它的人都能给自己的会话提权。含义是轮换要勤、`state_dir` 与备份要当凭据对待。若将来需要"每人一把"，那是一次设计变更，不是配置项 |
| 无逐用户撤销 | 只有"全部作废"（轮换）与"单条会话作废"（登出/退出），没有"撤销某人的提权" |
| TLS | `SameSite`/`Secure`/`HttpOnly` 与同源校验由既有 auth 层负责；本功能假设前置有 HTTPS 终结（与本仓库其它登录通道相同的前提） |
| 多进程部署 | 提权行与失败计数都在共享的 SQLite 文件里，跨 worker 生效；但 `AdminModeService` 的默认限速器**不是**进程本地的那个（它复用 DB 版） |
| 时间来源 | 服务端时钟。TTL 是 `entered_at + ttl_seconds` 的绝对时间戳，前端不做任何本地倒计时判定 |

---

## 11. 回滚

1. 从服务环境移除 `LLM_LAB_ADMIN_MODE_ENABLED`（或设为 `0`）并重启 —— 所有提权立刻作废，
   `enter` 一律 `503`，界面画成 disabled。
2. 需要彻底移除时：删掉 `admin_mode.sqlite3` 与 `admin_mode.hash`，并从 BFF 装配处摘掉
   `admin_mode=` 参数。`resolve_session` 的 `admin_mode=None` 分支等于"无人提权"。
3. **回滚不影响既有登录**：本地 `admin` 与飞书通道未改动；Linux 登录仍然只是登录，
   只是再也拿不到管理员模式。
