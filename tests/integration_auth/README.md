# 认证联合集成测试（真实 WSGI 服务）

这里的两个套件对面**不是替身**，而是 `dev/preview_auth.py` 启动的**真服务**：
真 `backend.auth.routes.Application`、真 SQLite、真 Cookie、真 CSRF 校验、
真浏览器绑定 Cookie（飞书为离线 mock）。因此它们验的是「前后端真的接上了」，
而不是「两边各自照着一份契约自说自话」。

离线替身那一套（`tests/auth-ui/`）仍然有价值：它跑得快、能构造真实后端很难
构造的故障（限速、锁定、离线）。两者互补，**不互相替代**。

## 1. 怎么跑

```bash
# 1) HTTP 层：真实 socket，154 个用例（约 75s）
python -m unittest discover -s tests/integration_auth -t .

# 2) 浏览器层：无头 Chromium（CDP，零依赖），16 个用例（约 28s）
node --test --test-concurrency=1 tests/integration_auth/browser.preview.test.mjs
```

浏览器层需要本机有 Chrome/Edge（与 `tests/auth-ui/` 共用
`tests/auth-ui/helpers/cdp.mjs`），可用 `CHROME_PATH` 指定可执行文件。
`--test-concurrency=1` 是刻意的：三个预览服务 + 一个浏览器并发跑会让
断言里的等待时间失真。

截图证据落在 `tests/integration_auth/.artifacts/`（`*.png` 已在 `.gitignore`）。

## 2. 浏览器层怎么把口令安全地交给子进程

`browser.preview.test.mjs` 用 `spawn` 拉起
`python -m tests.integration_auth.preview_e2e_server`，口令在 **Node 进程内随机生成**，
经**标准输入的一行 JSON** 交给子进程：

- 不出现在命令行里，因此不出现在进程列表（`ps` / 任务管理器）里；
- 不写进任何文件；
- 不打印：子进程 stdout 只有一行就绪信息 `{"origin", "port", "session_cookie"}`。
  `wsgiref` 的访问日志走 stderr，只用来在失败时定位，不含口令。

子进程用**与 CLI 同一条缝**创建管理员：
`bootstrap.initialize_admin_password(cfg, read_password=…)`。产品里没有任何
口令预设，这个夹具也不加一个——它只是把提示喂进去。

进程的生命周期就是那根管道：测试结束时关掉 stdin，子进程 `shutdown` 退出。
另有 `SIGTERM` 兜底。

**夹具即运维动作**：成员身份、项目授权都直接走 `store`（`upsert_feishu_user` /
`grant_project`）。产品里本来就没有「授个权」的 HTTP 路由——授权是带外的管理动作。

## 3. 覆盖矩阵

### 3.1 HTTP 层（`test_preview_http.py`，48）

| 组 | 覆盖 |
|---|---|
| 启动守卫 | 缺 `--local-http-dev` 拒绝；`state_dir` 必须绝对路径；只有声明的本地 HTTP 模式才去掉 `Secure`；配置**从空环境构造**（运算符的 shell 变量进不来）；`make_server` 拒绝非环回地址、拒绝没有管理员凭据的状态目录 |
| 静态面 | 登录页 + 三个资源（MIME / nosniff）；登录页不含任何绝对 URL；遍历与白名单探测全部 404；`POST /login` 405 |
| 请求加固 | 非环回 `Host` → 400；`127.0.0.1` / `[::1]` / `localhost` 放行；重复 Cookie 名 → 400；重复的 `X-CSRF-Token`（被 WSGI 逗号黏成 `"one,two"`）→ 403 `csrf_rejected` |
| 匿名访问 | `/atlas`、`/` 全部 302 且带 `next=`，页面无 DEMO；伪造 Cookie 不行；真实登出后会话立刻失效 |
| 授权 | 0 项目管理员 → 200 DEMO；0 项目成员 → 403（但 `/auth/me` 仍 200）；有授权的成员 → 200；停用账号 → 302 |
| DEMO 页 | 横幅 id 与文案、横幅在正文之前、CSP `connect-src 'none'` / `frame-ancestors 'none'`、`no-store` / `nosniff`；确为原型内容且不含任何真实主机名 |
| 登录往返 | 完整 CSRF → 登录 → Cookie 属性 → `/auth/me` → 登出 → 401；缺 CSRF 头 → 403；跨源 → 403；把 token 放 body 里 → 403；口令错误 → 401 且不回显口令；信封始终是 `ok:false` |
| 飞书可用性 | 未配置时 `/auth/feishu/start` → 503 `not_configured`；`/auth/csrf` 如实报 `false` 且不下发任何 Cookie |
| 不挂起 | 手写 HTTP/1.1：无体的 GET / POST 都必须立刻有回包（真 WSGI 层曾经在无 `Content-Length` 的请求上等一个永远不来的体） |
| mock 飞书 | 302 到**环回** URL；绑定 Cookie `HttpOnly; SameSite=Lax`；**逐次授权各发一枚一次性 code**；跳过绑定 Cookie 不行；完整回调登录成功且 `/atlas` 仍 403 |

### 3.2 浏览器层（`browser.preview.test.mjs`，16）

| # | 断言 |
|---|---|
| 1–2 | 匿名用浏览器打开 `/atlas`、`/`（含 `fetch` 跟随跳转）落到登录页，DOM 里不存在 DEMO 痕迹 |
| 3 | 管理员用**表单**登录：`POST /auth/local/login` 200 —— 后端只在同源 Origin + 有效 `X-CSRF-Token` 时给 200，所以这个 200 就是「浏览器真的把请求头送到了」的证据 |
| 4 | `document.cookie` 读不到会话名；localStorage / sessionStorage 无凭据 |
| 5 | 登录后 `/atlas` 与 `/` 都是同一份 DEMO，横幅必须含 `DEMO` / `合成演示数据` / `127.0.0.1` 且在正文之前 |
| 6–7 | 离线 mock 飞书全流程：0 项目成员落到拒绝页、已授权成员进 DEMO —— 同一套流程，不同结果，由服务端裁决 |
| 8 | 退出：等到 `POST /auth/logout` 的真实响应才进入「已退出」，随后 `/atlas` 重新不可读 |
| 9 | 未配置飞书：从浏览器里 `fetch` `/auth/feishu/start` 得 503 `not_configured`，入口 `aria-disabled="true"` 且文案如实 |
| 10 | 页面初始化只打 `/auth/csrf` 与 `/auth/me`，**绝不**用有副作用的 `/auth/feishu/start` 当探针 |
| 11–12 | `/dev/preview_auth.py`、`/package.json`、`/auth.sqlite3`、`/backend/**`、`/__debug__`、`/../etc/passwd` 等一律 404；`/auth/*` 只暴露约定的路由 |
| 13–15 | 320 / 390 / 1440 登录页无横向溢出；DEMO 横幅与拒绝页在 320px 下不溢出 |
| 16 | `prefers-reduced-motion` 下页面可用，口令错误如实报 401，全程无页面错误 |

每个用例还声明 `log:` 里不得出现 CSP 违规——策略与页面实际行为必须一致。

### 3.3 只读 Atlas 端点（`test_preview_atlas.py`，58）

对面同样是真服务、真 Session、真 SQLite；授权由服务端注入
（`support.PreviewServer(atlas_grants=…)`，与 CLI 的 `--atlas-demo-grant` 是同一条缝）。

| 组 | 覆盖 |
|---|---|
| 访问控制 | 匿名 / 伪造 Cookie / 已登出 / 会话已吊销 → 401；**管理员无 grant 也 403**（且 `/auth/me` 仍 200）；成员无项目 403；他人项目 403；自己项目 200；无关项目授权 → 200 但 groups 为空 |
| 伪造字段 | 请求里的 `X-Project` / `X-Principal` / `X-Workload` / `X-Key-Id` / `X-Forwarded-User` / `X-Atlas-Role` / `X-Atlas-Scopes` 与查询里的 `role` / `key_id` / `label` / `scopes` 一律无效；`X-Project` 不能扩权；为他人注入的 grant 不生效 |
| 方法与解析 | `POST`/`PUT`/`PATCH`/`DELETE`（含带合法 CSRF 的）→ 405；重复查询参数 → 400；未知 bucket → 400；`bucket=1d` 被遵守；无关参数被忽略 |
| 载荷 | 信封 `{ok,data}`；`synthetic is True` / `source == "synthetic-preview"`；窗口、质量、`observed_at`；形状是 **AtlasDTO 而非 AtlasBundle**（`scope` 是对象、无 `series`）；与 `build_atlas_dto` 逐字节相等 |
| 不伪造 | 无 scope 时 unknown 是 `null`；空组 p50/p95 是 `null` 不是 `0`；窗口半开 + fixture 精确值（`totals.calls == 7`、`out_of_window == 1`、`revoked_key == 1`） |
| 不读真实数据 | `Path.read_text` 间谍证明一次请求恰好读**一个**文件；文件名里没有 `usage.jsonl`；fixture 每行含 `SYNTHETIC`；对模块源码静态扫描；被读后 fixture 未被改写 |
| 响应头与回归 | JSON 头带 `no-store` / `nosniff` / `no-referrer` / JSON CSP；错误响应同样带安全头且不回显 `<script>`；匿名 `/atlas` 仍 302；`/login` 的 `LOGIN_CSP`、DEMO 页的 `DEMO_CSP` 均未变 |
| 生产面 | `ROUTES` 中没有 atlas 路径；生产 `Application` 对同一路径 404；`/api/atlas/usage` 不出现在任何 `backend/**/*.py` |
| 授权配置 | 管理员用户名解析成会话携带的账号 id；完整账号 id（`local:` / `feishu:`）原样采用；grant 永不越过演示项目集；`SUBJECT:PROJECT` 被**拒绝**而不是猜；未知/空名称报错；静态列表重复 subject 是笔误不是覆盖；无项目的 grant 在配置期报错；CLI flag 确实打开端点、可重复、默认为空；grant 也可在构造时注入 |

## 4. 已知边界（不假装覆盖的东西）

- **CDP 读不到重定向那一跳的状态码**：Chrome 不单独为 302 发
  `Network.responseReceived`（跳转信息挂在下一个请求上）。浏览器层因此断言
  **最终落在哪个文档**，裸 302 由 HTTP 层把守。
- **离线 mock 不是真飞书**：它不签名、不校验 tenant 的真实性、不访问网络。
  真企业 App、真回调域名、真 HTTPS 仍然是缺的（见根 `README.md` 与 `docs/`）。
- **不是生产配置**：这里全程明文 HTTP + 环回，`Secure` 属性被显式关闭。
- 不覆盖真实浏览器矩阵，只覆盖本机 Chromium（无头）。

## 5. 如果它红了

先跑一次 HTTP 层（快、无浏览器）。它绿而浏览器层红，问题通常在浏览器可见的
那一侧：CSP、页面脚本的初始化顺序、或某一跳的落点。`waitForUrl` 在超时时会打印
**当前 URL + 页面错误 + 最近 8 条请求**，先看那三行再动手。
