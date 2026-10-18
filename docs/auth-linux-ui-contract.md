# 登录页 · Dell Linux 账号契约（Lane B）

本文件是 `web/auth/` 与**尚未存在的**服务端 Linux PAM 登录之间的契约正本。
它同时是两个方向的规格：UI 侧据此决定「表单能不能用」，服务端侧据此决定
「要不要真的开闸」。

一句话：**在服务端明确声明之前，登录页只是一个 BasicAuth 保护的预览，不接收任何
Dell 系统口令。**

## 1. 为什么退役飞书与本地管理员双入口

- 登录页此前有两条入口：成员走飞书 OAuth，管理员走 `/auth/local/login`。用户决定
  **舍弃飞书**、统一到 Dell Linux 账号，因此两条入口一起退役——留下「仅管理员本地
  账户」的第二条路，本身就是误导：它看起来像另一套可用的身份，实际上谁都用不上。
- 现在的登录页只有**一个**入口，也只承认**一种**身份：Dell 主机上的 Linux 账号。
- 飞书相关的服务端实现（`backend/**`、tokens、provider keys）**刻意保留**供历史兼容，
  本 Lane 不删任何生产用户或凭据。退役的范围是**浏览器前端永不展现、永不触发**。

## 2. UI 侧：唯一开闸依据是 `GET /auth/csrf`

### 2.1 端点契约

只读、同源、**不下发任何 Cookie**、无副作用。

```http
GET /auth/csrf
```

```jsonc
{
  "ok": true,
  "data": {
    "csrf_token": "<非空字符串，≤512>",
    "authenticated": false,              // 布尔，必须存在（契约声明）
    "providers": {
      "feishu": false,                   // 历史字段，保留但 UI 不再据此开闸
      "local_admin": false,              // 历史字段，同上
      "linux_pam": {
        "enabled": true,                 // 严格布尔；非布尔一律按未声明处理
        "uid_scope": "dell-linux-account",
        "root": false,
        "service_account": false
      }
    }
  }
}
```

### 2.2 开闸条件（全部满足才 `ready: true`）

| # | 条件 | 不满足时的原因码 |
|---|---|---|
| 1 | 不是 `file://` 静态预览 | `static-preview` |
| 2 | 未超时（UI 侧上限 5s） | `timeout` |
| 3 | HTTP 2xx | `http-401` / `http-403` / `http-error` |
| 4 | `Content-Type` 不是 HTML | `html-response` |
| 5 | 响应体是 `{ok:true,data:{…}}` 且 `data.authenticated` 是布尔 | `malformed-envelope` |
| 6 | `providers.linux_pam` 是对象 | `provider-absent` |
| 7 | `enabled === true`（**严格**布尔） | `provider-disabled` |
| 8 | `uid_scope === "dell-linux-account"` 且 `root === false` 且 `service_account === false` | `unsafe-principal-scope` |
| 9 | `csrf_token` 非空 | `csrf-missing` |

判定实现：`web/auth/auth-core.js` 的 `linuxLoginReadiness()`；原因码常量
`LINUX_PAM_REASON`。两者都有 `tests/auth-ui/auth-linux-readiness.test.mjs` 逐条覆盖。

**第 7、8 条是重点**：`providers.feishu === true` 或 `providers.local_admin === true`
**不能**让 Linux 表单可用——它们与 Linux 身份无关。一个裸布尔 `linux_pam: true`
也不够：范围三字段缺一即按 `unsafe-principal-scope` 处理，**缺字段不等于默认安全**。

### 2.3 关闸时的 UI 行为（fail-closed）

- `#linux-fields`（整组字段）、`#username`、`#password`、`#pw-toggle`、`#linux-submit`
  全部 `disabled`；
- `#linux-gate` 以 `data-gate="hold"` 与一段直白文案说明「当前仅是 BasicAuth 保护的
  预览；Linux PAM 服务端登录未开通」；
- **不发出任何请求**：不 POST、不 GET 任何登录端点；
- 即使被脚本程序化触发 `form.requestSubmit()`，`onLinuxSubmit` 在 `fetch` **之前**
  就返回（由 `lastGate.ready` 判定）；
- 不访问 `/auth/feishu/start`（有副作用）、不访问 `/auth/local/login`（已退役）。
  两者都不在 `web/auth/auth.js` 的 `AUTH_PATHS` 白名单里，`assertAllowedPath` 结构上拦死。

### 2.4 开闸时的 UI 行为

- 字段与按钮启用，`#linux-gate` 变为 `data-gate="ready"`；
- 提交前重新取一次 CSRF 值（匿名值单次性），写请求 `POST /auth/linux/login`：
  - `X-CSRF-Token: <值>` 走请求头，**不放进请求体**；
  - 请求体只含 `{"username": …, "password": …}`，无任何其它字段；
- 口令提交后立即从 DOM 与内存清空；任何时刻都不进入 `localStorage` / `sessionStorage`；
- 429 / 锁定后的冷却到期**不等于**无条件恢复：仍按最近一次门槛结论决定是否可用。

## 3. 服务端门槛（Linux PAM 登录投产前必须逐条满足）

UI 侧的 fail-closed 只保证「没开通时不误开」。真正的安全边界在服务端，下列每一条
都是投产前的**硬门槛**，缺一条即不得开通。

### 3.1 身份与账号范围

| 门槛 | 要求 |
|---|---|
| **Linux UID** | 登录主体必须是 Dell 主机上真实存在的 Linux 账号；服务端在响应里以 `uid_scope: "dell-linux-account"` 如实声明，UI 据此开闸 |
| **禁 root** | `uid == 0` 一律拒绝，且 `providers.linux_pam.root` 必须报 `false` |
| **禁服务账号** | `nologin`/`false` shell、系统账号、共享服务账号一律拒绝，且 `service_account` 必须报 `false` |
| **禁空口令/锁定账号** | 空口令、`!`/`*` 锁定、过期口令不得通过 |
| **口径一致** | `uid_scope` 不是 `"dell-linux-account"` 时 UI 不得开闸；服务端不得为了让 UI 开闸而谎报该值 |

### 3.2 会话

| 门槛 | 要求 |
|---|---|
| `HttpOnly` | 会话 Cookie 必须 `HttpOnly`，脚本读不到（浏览器测试断言 `document.cookie` 不含它） |
| `Secure` | 必须 `Secure`，只在 HTTPS 下传输 |
| `SameSite` | 至少 `Lax`（写请求为同源 POST，`Lax` 足够且不牺牲可用性） |
| 服务端可撤销 | 退出登录必须服务端失效会话；UI 只有拿到 2xx 才宣告「已安全退出」 |
| 不落地 | 会话标识不得出现在 URL、HTML、`localStorage`、`sessionStorage` |

### 3.3 授权（RBAC）

- 身份确认（你是谁）与授权（你能看什么）**分离**：登录成功不等于获得任何项目。
- 权限按 principal 逐条判定，服务端为准；已被授权但无项目的账号落到「待分配」视图，
  而不是被当成未授权。
- 平台管理员的判定只能来自服务端声明的管理员信号，**绝不**由页面按角色名或项目数量推断。

### 3.4 写保护

| 门槛 | 要求 |
|---|---|
| CSRF | 所有写请求（`POST` 登录、`POST` 退出）必须校验 `X-CSRF-Token`，且值**单次性**，重放必拒 |
| 同源 | 写请求必须校验 `Origin` / `Referer` 同源，在业务逻辑**之前**拒绝 |
| CSRF 端点只读 | `GET /auth/csrf` 不得下发任何 Cookie、不得有副作用 |

### 3.5 限速与审计

| 门槛 | 要求 |
|---|---|
| 限速 | 按 principal 与来源限速，超限返回 429 + `Retry-After`；失败次数达阈值进入锁定（更长 `Retry-After`） |
| 不泄露账号存在性 | 未知账号与错误口令返回**同一个** 401，不得区分 |
| 日志脱敏 | 口令、会话标识、CSRF 值一律不入日志；用户名如需记录按最小必要原则 |
| 审计 | 登录成功/失败、锁定、退出均可审计，且审计记录不含凭据 |

## 4. 非目标

- 本次**不实现**服务端 PAM 校验；`backend/auth_linux` 目前只有 broker/handoff 组件。
- 本次**不改** `web/index.html`、`prototypes/v6/*`、`web/js/**`、`web/css/*` 与后端。
- 本次**不删**任何飞书生产实现、用户或 token——只让前端不再展现与触发。

## 5. 相关文件

| 文件 | 作用 |
|---|---|
| `web/auth/index.html` | 唯一入口的页面结构，默认 `disabled` |
| `web/auth/auth-core.js` | `linuxLoginReadiness()` 等纯逻辑判定 |
| `web/auth/auth.js` | 门槛读取与请求编排；`AUTH_PATHS` 白名单 |
| `tests/auth-ui/auth-linux-readiness.test.mjs` | 门槛逐条单测 |
| `tests/auth-ui/browser.auth-ui.test.mjs` | 真实浏览器验收（含「未开闸不发请求」） |
| `tests/auth-ui/mock-server.mjs` | 后端契约替身，含 `pam-*` 与 `csrf-*` 场景 |
