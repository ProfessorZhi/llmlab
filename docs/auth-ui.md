# 双入口登录界面（`web/auth/**`）

本文描述 Portal 登录页的**前端实现与其依赖的认证契约**。

读者对象：实现 Portal 后端认证端点的人、后续维护这页 UI 的人、以及做安全复核的人。

> 本文是**前端这一侧**的约定与自我约束。后端尚未实现时，第 6 节的每一项都只是
> 前端提出的**待确认项**，不是既成事实。

---

## 1. 这一页做什么

Portal 有两类使用者，登录页必须让两类人都能一次看懂自己该走哪条路：

| 入口 | 面向谁 | 主次 | 行为 |
|---|---|---|---|
| **飞书登录** | 团队成员 | 主按钮 | 整页跳转到 `/auth/feishu/start`，由服务端 302 到飞书 OAuth 授权页 |
| **管理员登录** | 运维 / 管理员 | 次入口（`quiet` 样式） | 在同一页内展开用户名 + 口令表单，走本地账号登录 |

页面是**一个** HTML 文件，两个入口是它的两个视图，不是两个页面。

### 必须成立的硬约束

1. **不画假二维码。** 成员入口只有在服务端确实会 302 到飞书时才可点击。
   服务端回 `not_configured` 时，页面**如实显示**「飞书登录暂不可用 / 尚未配置」，
   绝不渲染一个扫码框来假装可用。真实二维码只能由飞书授权页自己产生。
2. **不预填任何口令。** 管理员用户名只允许是 `admin` 这个**名字**；
   口令输入框永远为空，仓库里不存在任何默认口令、测试口令、示例口令。
3. **飞书身份不得自动升级为管理员。** 邮箱、昵称、`role`、`is_admin` 等字段
   在本页**不参与**任何授权判定；授权与否只看服务端给出的项目列表。
4. **会话只存在于服务端下发的 HttpOnly Cookie 里。** 页面不读取、不写入、
   不把会话或 OAuth `access_token` 放进 `localStorage` / `sessionStorage` / JS 全局变量。
5. **`file://` 下不发送任何登录请求。** 见第 5 节。

---

## 2. 文件构成

```
web/auth/
  index.html      页面结构与全部静态文案（中文）
  auth.css        Atelier Observatory V5 样式，自带明暗两套 token，不依赖 web/css/**
  auth-core.js    纯逻辑层：路径净化、错误归类、结果判定（浏览器 / Node 共用）
  auth.js         视图编排层：视图切换、主题、同源请求
```

设计取舍：**零构建、零依赖、零 `npm install`**。

### 为什么不用 ES module

`web/auth/index.html` 用的是两个**经典 `<script defer>`**，不是 `<script type="module">`：

```html
<script defer src="./auth-core.js"></script>
<script defer src="./auth.js"></script>
```

`file://` 环境下浏览器给文档的 origin 是不透明的，`type="module"` 的脚本会被当作跨源请求
拦掉，页面直接白屏。而「双击 HTML 就能看的静态预览」是这页的既定要求，不能被牺牲。

为同时满足「浏览器里能跑」与「Node 里能测」，`auth-core.js` 写成 **UMD**：

- 有 `module.exports` ⇒ 导出给 Node（测试直接 `import`，无需打包）；
- 否则挂到 `window.LLMAuthCore`。

`web/auth/package.json` 因此**刻意不声明 `"type": "module"`**，否则 `auth-core.js`
在 Node 里会被当成 ESM 解析而 `module.exports` 不存在。

`auth.js` 是经典 IIFE，只做 DOM 与网络编排，逻辑全部取自 `auth-core.js`；
`auth-core.js` 没加载成功时它直接 `return`，页面停在静态默认视图。

---

## 3. 认证契约（前端依赖的部分）

所有请求均为**同源**、`credentials: 'same-origin'`、`cache: 'no-store'`。
`auth.js` 里有端点白名单 `AUTH_PATHS`，白名单之外的地址一律拒绝，不发出请求：

| 方法 | 路径 | 用途 | 前端期望 |
|---|---|---|---|
| `GET` | `/auth/csrf` | 取页面校验值 | 2xx + JSON 含 `csrf_token`（兼容 `csrf` / `token` 键名），并下发 CSRF Cookie |
| `GET` | `/auth/feishu/start` | 飞书入口 | **已配置**：3xx 跳转到飞书授权页；**未配置**：`503` + `{"error":"not_configured"}` |
| `GET` | `/auth/feishu/callback` | 飞书回调 | 服务端负责换取身份并下发会话 Cookie，然后跳回站内 |
| `POST` | `/auth/local/login` | 管理员登录 | JSON，见下 |
| `GET` | `/auth/me` | 当前会话 | 2xx = 已登录并携带项目列表；`401/403` = 未登录 |
| `POST` | `/auth/logout` | 退出 | 任意 2xx，并清除会话 Cookie |

### `POST /auth/local/login`

请求：

```http
POST /auth/local/login HTTP/1.1
Content-Type: application/json
Cookie: llm_lab_csrf=...

{"username":"…","password":"…","csrf_token":"…"}
```

**请求体只有这三个字段**，不夹带任何其它内容（有专门的用例断言这一点）。

响应（前端据此分支）：

| 状态 | `error` | 页面行为 |
|---|---|---|
| `200` / `204` | — | 跳转到站内 `next`（默认 `/`） |
| `400` | `invalid_request` | 字段级错误提示 |
| `401` | `invalid_credentials` | 清空口令、保留用户名、提示「用户名或口令不正确」 |
| `403` | `csrf_invalid` | 重新取一次校验值，提示「请再试一次」；**不重复提交口令** |
| `403` | `account_disabled` | 表单进入不可用终态 |
| `423` | `account_locked` | 表单进入不可用终态，若带 `Retry-After` 则一并说明 |
| `429` | `rate_limited` | 提示冷却时长并**禁用提交按钮倒计时** |
| `≥500` | — | 「认证服务异常」，可重试 |
| 无响应 | — | 离线视图 |

### 跳转目标

登录成功后只允许跳到**站内**路径。`next` 参数经 `sanitizeNextPath` 白名单式校验收敛：

- 只接受以 `/` 开头、且不是 `//` 的相对路径；
- 含绝对 scheme（`http:`、`javascript:` 等）、反斜杠、空白或控制字符的一律丢弃；
- 控制字符检查在 `trim()` **之前**做，避免 `/atlas\t` 这类被 trim 洗白；
- 任何不合格的输入都回落到默认落地页 `/`。

---

## 4. 必须实现的界面状态

| 状态 | 触发 | 表现 |
|---|---|---|
| 初始检查中 | 首屏 | `boot` 视图，不发内容 |
| 未登录 | `/auth/me` 401 | 成员入口（飞书主按钮） |
| 管理员入口 | 点击「管理员登录」或 `?mode=admin` | 管理员表单 |
| 口令显示 / 隐藏 | 点眼睛按钮 | 切换 `input.type`，同步 `aria-pressed` / `aria-label` |
| 键盘焦点 | Tab / 点击切换入口 | 切换后焦点落到新面板标题；错误时聚焦出错字段 |
| 字段错误 | 用户名或口令为空 | 字段级 `aria-invalid` + 焦点，**不发请求** |
| 凭据错误 | 401 | 错误提示，清空口令 |
| 限速 | 429 | 倒计时禁用提交 |
| 锁定 / 停用 | 423 / 403 | 表单终态不可用 |
| 操作中 | 提交在途 | 按钮禁用 + 转圈 + 「正在登录…」，重复点击不产生第二个请求 |
| 已登录无项目 | `/auth/me` 返回空项目列表 | **待分配**视图 |
| 会话过期 | `/auth/me` 401/403 且有会话痕迹 | 回到登录入口并提示，不显示任何已登录内容 |
| 已退出 | 点退出 / `?logged_out=1` | 「已退出」视图 |
| 飞书未配置 | 探测到 `not_configured` | 按钮禁用 + 如实说明 |
| 离线 | 请求不可达（`status: 0`） | 离线视图 + 重试按钮 |

视觉基调：Atelier Observatory V5（暖白 + 松绿 + 克制的铜橙），**默认明亮**、
可切深色并记忆；正文 15–16px；过渡 250ms；`prefers-reduced-motion: reduce`
下动画与过渡被关闭。主题偏好存在 `localStorage` 的 `llm-lab.auth.theme`
**这一个**键里，与凭据无关。

---

## 5. `file://` 静态预览

直接用浏览器打开 `web/auth/index.html` 时：

- 页面**正常渲染**，明暗主题可切换；
- 横幅如实说明「未连接认证服务，不会发送任何登录请求」；
- 飞书按钮与管理员表单均为禁用态，不会发出任何请求。

判定「是否静态预览」看 `location.protocol === 'file:'`（`isStaticPreview`）。
这是**硬闸门**：即便有人在 `file://` 下硬提交表单，`onAdminSubmit` 也会先返回。

真实交互回归由 `tests/auth-ui/mock-server.mjs` 在 `127.0.0.1` 随机端口上提供，
只有在那样的同源环境下才会真的发送口令，且口令是每次启动随机生成的虚构值。

---

## 6. 待后端确认 / 待接入

以下项目**尚未有后端实现**，前端按其做了约定；后端定稿后需回填本节：

1. **CSRF 的传递方式**：前端按「双提交」实现——值从 `/auth/csrf` 的 JSON 取，
   同一值也在 Cookie 里下发，提交时放进请求体 `csrf_token` 字段。
   若后端改走 `X-CSRF-Token` 请求头或纯 Cookie 比对，前端需同步改 `auth.js`。
2. **`/auth/me` 的响应形状**：前端只认 `projects` / `authorized_projects` 这两个键
   作为授权事实；`authenticated` 与 `username` 用于判定登录态。
   其余字段（`email`、`nickname`、`role`、`is_admin`）**被刻意忽略**。
3. **飞书未配置的表达方式**：前端接受 `503 + {"error":"not_configured"}`。
   若改用一个总返回 302、仅在回调时才失败的方案，页面就无法提前如实告知，
   第 1 节的「不假装可用」会被削弱——需要后端明确取舍。
4. **错误码枚举**：第 3 节表格里的 `error` 字符串需要后端确认一致性。
5. **`Retry-After`**：前端同时支持秒数与 HTTP-date，`retryAfterSec` 向上取整
   （宁可让用户晚一点重试，也不让他过早撞第二次 429）。
6. **会话 Cookie 属性**：期望 `HttpOnly; Secure; SameSite=Lax`（或更严）。
   前端不依赖具体 Cookie 名，只依赖「脚本读不到」。
7. **`/auth/feishu/callback` 之后的落地页**：目前约定跳回站内 `/`。
8. **Portal 主导航接入**：本页目前是独立入口。谁来承载登录后的 Portal 主界面、
   导航如何与本页衔接（重定向？嵌入？），需要 Portal 主界面定型后确定。
9. **部署路径**：页面假设 `/auth/**` 挂在同源根路径下。若将来挂在子路径，
   `AUTH_PATHS` 与 `index.html` 里的 `action` 需要一并调整。

---

## 7. 本地怎么跑

### 静态预览

用浏览器直接打开 `web/auth/index.html`。不需要任何服务、不需要构建。

### 自动化测试

```bash
# 全部（Node 逻辑 + mock 契约 + 真实 Chromium），79 个用例
node --test --test-concurrency=1 "tests/auth-ui/*.test.mjs"
```

必须**用通配符或逐个列文件**。直接给目录（`node --test tests/auth-ui/`）在 Windows 上
会被 Node 当成一个待加载的模块，报 `Cannot find module ...\tests\auth-ui` 并只报 1 条失败，
看起来像整套挂了，实际是调用方式的问题。`--test-concurrency=1` 让三个文件串行跑，
避免多个无头浏览器同时抢资源。

测试**零依赖**：不需要 `npm install`，不需要 Playwright。
浏览器测试用 Node 24 自带的 `WebSocket` / `fetch` 直连 Chrome DevTools Protocol，
可执行文件路径可用环境变量 `CHROME_PATH` 指定。

用例约定（测试数据全部虚构）：

- 只监听 `127.0.0.1` 随机端口，结束后自动关闭；
- 用 `--host-resolver-rules` 把浏览器的域名解析**硬性切断**，页面若尝试外网请求会立刻暴露；
- 断言 `localStorage` 里不含任何凭据类键值、`sessionStorage` 为空；
- 截图落在 `tests/auth-ui/.artifacts/`（已被 `.gitignore` 忽略）。

---

## 8. 安全复核要点

复核这页时，重点看这几处：

- `web/auth/auth.js` 的 `AUTH_PATHS` + `assertAllowedPath`：是否还能发出白名单外的请求；
- `auth-core.js` 的 `sanitizeNextPath`：`//evil.com`、`/\evil.com`、`javascript:`、
  带控制字符的变体是否都被挡下；
- `authorizedProjects`：是否读到了任何「身份字段」并据此授权；
- 全仓库搜索 `localStorage` / `sessionStorage`：是否只有主题键一个写入点；
- `auth.js` 中口令字段的生命周期：提交后是否立刻从 DOM 与内存清除；
- 页面全文是否出现任何口令字面量（应当没有）。
