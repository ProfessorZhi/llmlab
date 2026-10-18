# 本地登录预览（`dev/preview_auth.py`）

在**本机**把真实的认证后端（`backend/auth/**`）与真实的登录页（`web/auth/**`）
接到一起，用真实 socket、真实 Cookie 跑一遍完整登录流程，**不需要**部署、
不需要 TLS 证书、不需要网关，也不接触任何真实数据。

> 这是**开发入口，不是部署**。它不会、也不能被当作生产服务：只绑环回、
> 只提供合成 DEMO、不读任何真实飞书凭据、没有任何默认口令。

## 1. 快速开始（Windows，三步）

在**仓库根目录**下执行。`--state-dir` 是**必填**的：它必须是一个**绝对路径**，
并且必须落在本仓库**之外**——凭据文件与会话库是本地开发数据，绝不能出现在
可以提交的目录树里。

> `--state-dir` **没有**「省略就自动建临时目录」这个默认值。全新目录里不可能
> 已经有管理员，凭空造一个出来只会以必然失败收场，还会留下一个没人要的空目录。
> 所以不给就直接拒绝启动（见 §4）。

下面按 **PowerShell** 写，`<state>` 用你自己的绝对路径。**Git Bash 的路径写法
不同**，见本节末尾。

```powershell
# 1) 交互式创建管理员口令。没有任何默认口令，也没有 --password 参数，
#    命令行里不会出现口令；请自己输入一串 >= 16 位的口令并在提示下再输一次。
python -m backend.auth.bootstrap init --state-dir "$env:TEMP\llm-lab-preview"

# 2) 启动预览。--local-http-dev 是必填的显式声明（见 §3）；
#    --mock-feishu 用离线的飞书替身，不配置它则页面如实显示「飞书不可用」。
python -m dev.preview_auth --local-http-dev --state-dir "$env:TEMP\llm-lab-preview" --mock-feishu
```

**Git Bash** 里同一个目录要写成 MSYS 风格，且要防止 MSYS 把 `/c/...` 改写成
Windows 路径之外的东西（用 `MSYS_NO_PATHCONV=1` 最省事）：

```bash
python -m backend.auth.bootstrap init --state-dir "$TEMP/llm-lab-preview"
python -m dev.preview_auth --local-http-dev --state-dir "$TEMP/llm-lab-preview" --mock-feishu
```

`$TEMP` 在 Git Bash 里已经是 `C:\Users\...\AppData\Local\Temp` 这种 Windows 绝对
路径，两边都能直接用。若你写的是 `/c/Users/<你>/AppData/Local/Temp/llm-lab-preview`，
请在前面加 `MSYS_NO_PATHCONV=1`。

启动后终端会打印实际监听的环回端口与三个入口（**不会**打印任何口令）：

```
state dir:  <state>
sign in:    http://127.0.0.1:<port>/login
demo page:  http://127.0.0.1:<port>/atlas   (requires a session; the reader is redirected otherwise)
feishu:     offline mock enabled
press Ctrl+C to stop
```

```bash
# 3) 在浏览器里打开 sign in 那一行。管理员用户名固定为 admin，
#    口令就是你第 1 步自己设的那一串（本仓库里没有任何地方记录它）。
#    成员入口默认显示；管理员入口在 URL 后加 ?mode=admin。
```

`--state-dir` 是必填的，**没有**「省略就自动建临时目录」这回事：新目录里不可能
已经有管理员，那样启动必然失败，还会留下一个没人要的空目录。请始终显式指定它。

`Ctrl+C` 停止。端口是内核分配的临时端口（`--port 0`），进程退出即释放，
不留后台监听。重复启动时复用同一个 `--state-dir` 即可，管理员口令仍然有效。

## 2. 用它能验证什么

| 动作 | 期望 |
|---|---|
| 未登录直接打开 `/atlas` 或 `/` | 302 回 `/login?next=…`，**没有任何 DEMO 内容** |
| 管理员入口登录 | 落到 `/`，页面顶部有醒目的 `DEMO · 本地预览环境 · 合成演示数据` 横幅 |
| 已登录访问 `/atlas` | 同一份合成 DEMO（演示原型 `prototypes/v5/index.html`） |
| 成员入口 + `--mock-feishu` | 走本机 mock 授权页 → 回调 → 服务端裁决；0 项目成员看到「尚未获得项目授权」而不是 DEMO |
| 退出登录 | 等到服务端真实 200 才显示「已安全退出」；随后 `/atlas` 重新不可读 |
| 不带 `--mock-feishu` | `/auth/feishu/start` 返回 503 `not_configured`，页面如实显示入口不可用 |
| 浏览器存储 | 会话只存在于 `HttpOnly` Cookie 里；localStorage / sessionStorage 里没有任何凭据 |
| 未授权访问 `GET /api/atlas/usage` | **403**（含管理员）；未登录 401。见 §6 |

DEMO 页是纯静态的：CSP 为 `connect-src 'none'`，`default-src 'none'`，
页面内不发起任何网络请求，也不出现任何真实主机名或数据源。

## 3. 它刻意不做什么

| 项 | 说明 |
|---|---|
| 不重新实现鉴权 | 「谁登录了」「能看什么」全部来自产品自己的 `AuthService` / `Application.authorize`；容器只决定「裁决完之后发什么文档」 |
| 不对外开放 | 只绑 `127.0.0.1`；`make_server` 拒绝任何非环回地址，`Host` 不是环回名字的请求一律 400 |
| 不放松生产 Cookie | 只有在调用方**显式声明**「这是本地明文 HTTP 开发」时才去掉 `Secure`；CLI 上就是 `--local-http-dev`。不写它就直接拒绝启动 |
| 没有默认口令 | 没有默认值、没有 `--password`、环境变量也塞不进来。凭据不存在时启动失败并打印创建命令 |
| 不读环境变量 | 配置从**空环境**构造。你在 shell 里设的 `LLM_LAB_FEISHU_APP_ID` / `LLM_LAB_TRUSTED_ORIGINS` 都到不了预览 |
| 不出网 | 飞书要么「未配置」，要么是环回 mock；mock 不交换任何凭据，授权码按每次授权生成、一次性 |
| 不能读任意文件 | 静态面是**两份精确路径白名单**：`web/auth/`（`/login` + 三个资源）与 `web/`（V5 外壳与调用日志页的资源）。其它路径一律 404 |
| 不留后台端口 | 临时端口 + 前台进程；`Ctrl+C` 即结束 |

`--local-http-dev` 关掉的是 `Secure` 这一个属性，因为在 `http://127.0.0.1`
上带 `Secure` 的 Cookie 根本不会被浏览器存下来。生产环境仍然要求 HTTPS + `Secure`。

## 4. 常见拒绝与处理

| 现象 | 原因 / 处理 |
|---|---|
| `the following arguments are required: --state-dir`（退出码 2） | 没给 `--state-dir`。它是必填的，且**不会**替你建临时目录；补上绝对路径 |
| `refusing to start without --local-http-dev`（退出码 2） | 补上 `--local-http-dev` |
| `no administrator credential in <state>` | 先跑第 1 步的 `bootstrap init`（提示里也会打印这条命令） |
| `state_dir must be an absolute path` | `--state-dir` 要用绝对路径 |
| `state_dir must live outside the repository` | 别把 state 目录放进仓库树；换一个仓库外的绝对路径 |
| `state_dir must not be, and must not sit behind, a link` | 路径本身**或它的任意一段父目录**是链接。Windows 上尤其当心 **junction**：`mklink /J` 免提权，而 `Path.is_symlink()` 认不出它。请指向真实目录，别让凭据路径事后被改向。详见 [`../docs/preview-state-dir-security.md`](../docs/preview-state-dir-security.md) |
| `the preview only binds to loopback` | `--host` 只能给 `127.0.0.1` / `::1` / `localhost` |
| 浏览器里 400 `invalid_request` | 请求的 `Host` 不是环回名字（例如走了某个反代/隧道）。这是拒绝，不是故障 |
| 飞书入口显示「暂不可用」 | 没加 `--mock-feishu`。这是**如实显示**，不是 bug |

## 5. 数据落在哪里

`--state-dir` 目录里只有两样东西：管理员口令的哈希文件 `admin_password.hash` 与
认证 SQLite 库 `auth.sqlite3`。两者都是本机开发数据。

它们**绝不进仓库**，保障有两层：`--state-dir` 必须落在仓库之外（仓库内的路径被
CLI 直接拒绝），`.gitignore` 另外兜住库文件（`*.sqlite3` 等）。

预览**不会删除、也不会 chmod 一个已经存在的 `state_dir`**：目录按原样使用，被
拒绝的路径原封不动留在那里。要彻底清零，请自己删掉那个目录。

## 6. 只读 Atlas 端点 `GET /api/atlas/usage`

预览里唯一一个**返回 JSON** 的入口，也是唯一一个会把「后端聚合结果」交给浏览器的
入口。它**只**存在于预览，`backend/auth/routes.ROUTES` 里没有它、生产 `Application`
对同一路径返回 404（有测试钉住）。

数据来源只有一处：`dev/fixtures/atlas_preview_sample.jsonl`，**离线合成**、每行都带
`SYNTHETIC` 标注、常量级、不含任何真实用量或正文。响应里 `synthetic: true`、
`source: "synthetic-preview"`，前端按 `source` 分流时不会把它当 live。

### 6.1 谁可以读

**默认零授权**：不传 `--atlas-demo-grant` 时，任何人（**包括管理员**）访问都是 403。
授权在**进程内**由运维/CLI 注入，键是**会话给出的账号 id**：

```bash
python -m dev.preview_auth --local-http-dev --state-dir "$TEMP/llm-lab-preview" \
  --mock-feishu --atlas-demo-grant admin
```

| 输入 | 行为 |
|---|---|
| 未登录 / Cookie 伪造 / 已登出 / 会话已吊销 | 401 |
| 已登录但**没有**该账号的 grant（管理员也一样） | 403 `forbidden` |
| 账号已禁用 | 403 `account_disabled` |
| 请求里的 `role`、`key_id`、`label`、`X-Project`、`X-Principal`、`X-Atlas-Role` 等 | **完全忽略**，既不能授权也不能扩权 |
| 请求其他项目的用量 | 403（`backend.atlas` 的 `AccessDenied`） |
| `POST` / `PUT` / `PATCH` / `DELETE`（含带合法 CSRF 的） | 405 |

`--atlas-demo-grant` 可重复，接受**管理员用户名**（`admin`，由存储自己解析成账号 id）
或**完整账号 id**（`local:…` / `feishu:…`，原样采用）。**不接受** `SUBJECT:PROJECT`
写法：账号 id 自带冒号，没有可靠的断点 —— 所以这个 flag 只会**打开**端点，永远落在
演示项目集之内，不会扩大任何人的可见范围。写错就直接拒绝启动，不猜。

需要给单个账号收窄项目，那是**进程内**配置动作
（`app.atlas_grants.set(AtlasPreviewGrant(...))`，测试用的就是这个接口），不是命令行参数。

### 6.2 响应形状

`{"ok": true, "data": {"observed_at": …, "atlas": {…AtlasDTO…}}}`，其中
`data.atlas` 是 `backend.atlas` 的 **`AtlasDTO`**（合同见
[`../docs/atlas-data-contract.md`](../docs/atlas-data-contract.md)），
**不是**前端 `web/js/data` 期待的 `AtlasBundle` —— 两者 `scope` 形状不同
（DTO 是对象、Bundle 是字符串），DTO 也没有 `series`。**前端尚未接这个端点**；
要接就得在客户端做一次显式映射，而不是把 DTO 直接当 Bundle 用。

UNKNOWN 一律是 `null`（不是 `0`）：没有样本时 `p50`/`p95` 为 `null`，
窗口外的请求、未知 key、已吊销 key 各记各的计数器，不会被伪装成一次成功的调用。

### 6.3 它不读什么

不读真实 `usage.jsonl`、不连生产 Gateway、不出网、不读环境变量。整个模块除了那
**一个** fixture 文件不做任何 IO —— `NoRealDataTests` 用 `Path.read_text` 间谍钉住
「一次请求恰好读一个文件」，并对 `dev/atlas_preview.py` 源码做静态扫描。

## 7. 按主体的逐条调用端点 `GET /api/atlas/calls`

与 §6 的**项目汇总**是两条独立口径：§6 回答「我被授权项目的用量」，本端点回答
「**我自己**的每一条调用」。它有自己的 scope（`atlas:calls:self:read`）、自己的
schema（`atlas-calls/1`）和自己的 fixture
（`dev/fixtures/atlas_calls_sample.jsonl`，同样每行带 `SYNTHETIC-PREVIEW` 标注）。
合同见 [`../docs/portal-call-log-contract.md`](../docs/portal-call-log-contract.md)。

### 7.1 谁可以读

**默认零授权**：不传 `--calls-self-grant` 时，任何人（**包括管理员**）访问都是 403。
授权在**进程内**由运维/CLI 注入，且绑定到**一个**账号 id：

```bash
python -m dev.preview_auth --local-http-dev --state-dir "$TEMP/llm-lab-preview" \
  --calls-self-grant admin
```

| 输入 | 行为 |
|---|---|
| 未登录 / Cookie 伪造 / 已登出 / 会话已吊销 | 401 |
| 已登录但不是被授权的那个账号（管理员也一样） | 403 `forbidden` |
| 请求别人的 / 不存在的 / 自己已吊销的 key | 403 —— 与上一条**完全相同的响应**（成因不可区分，避免枚举他人 key 是否存在） |
| 请求里的 `subject`、`principal`、`role`、`X-Forwarded-User` 等 | **完全忽略**，既不能授权也不能扩权 |
| 非 `GET` | 405 |

### 7.2 为什么目录在启动期只构建一次

归属判定是「服务端 key 目录里的 `principal` 是否等于会话主体」。如果这个目录按
**每个请求的调用者**去构建，那么每一个获授账号都会「拥有」同一批演示 key ——
跨用户边界的演示会恰好反过来变成它的反面。所以目录与主体一起由
`dev/atlas_calls.SelfCallsGrant` 在配置期绑定，请求路径上只做**相等**比对。

本端点因此只接受**一个** self-grant：两个账号共享一份目录会让其中一人看到别人的
key，各自生成一份又会让归属判定失效。配置期直接拒绝，而不是在两种错误里选一个。

### 7.3 响应形状与正文

`{"ok": true, "data": {"observed_at": …, "call_log": {…AtlasCallsDTO…}}}`。
逐条字段：时间、状态、outcome、模型、`request_id`、key、工作负载、Token、排队、
TTFT、总耗时 —— 以及一个派生的 `call_id`（明确标注 `call_id_basis`）。

`content.included` **恒为 `false`**：不是「这个用户没同意」，而是元数据管道里
根本没有正文（`ingest` 在身份解析之前就拒绝一切正文类字段）。`content.consent`
只如实反映服务端记录在案的 opt-in **状态**，让前端能区分「未开启」与「已开启但本
管道不采集」，而不是把两者显示成同一句「暂无内容」。

### 7.4 页面

调用日志现在并入 V5 外壳的 `#/calls` 路由（同一个已认证会话内），另有一个
`/calls` 深链会 302 到该路由。`/calls.html` 这个文件名本身**不可达**。

### 7.5 它不读什么

同 §6：不读真实 `usage.jsonl`、不连生产 Gateway、不出网、不读环境变量。
数据只来自那一个合成 fixture。

## 8. 自动化测试

同一套东西由两个套件真跑：`tests/integration_auth/test_preview_http.py`
（真实 socket 直连）与 `tests/integration_auth/browser.preview.test.mjs`
（无头 Chromium）。详见 [`../tests/integration_auth/README.md`](../tests/integration_auth/README.md)。
测试里的口令是**进程内随机生成**的，不打印、不落盘、不进命令行。

Atlas 端点由 `tests/integration_auth/test_preview_atlas.py`（真实 HTTP）与
`tests/backend/`（纯函数合同）两套覆盖；逐条调用端点由
`tests/integration_auth/test_preview_calls.py` 与 `tests/backend/test_self_calls.py` 覆盖。

`test_preview_http.py::CliFirstRunTests` 专门钉住上面这套首次运行的契约：缺
`--state-dir` 必须立刻拒绝且**不留下任何目录**、未初始化的目录必须 fail-closed、
非环回地址必须拒绝、仓库内/符号链接的 state 目录必须拒绝，以及一个已 bootstrap
的目录能真的在环回上监听并干净地关掉。

`test_preview_http.py::StateDirRedirectTests` 补的是同一族里的 Windows 特例：
目录 junction（含**祖先**是 junction）必须被拒，而普通目录——包括本机那种 8.3
短名 `%TEMP%`——**不得**被误拒。
