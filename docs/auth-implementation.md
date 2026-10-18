# 双通道认证后端实现说明

本文件说明 `backend/auth/**` 这一层做了什么、怎么配置、怎么验收，以及**上线前还有哪些事不归本次实现负责**。

范围与所有权：本次改动只落在 `backend/auth/**`、`tests/auth/**`、`docs/auth-implementation.md` 和 `backend/auth_entry.py`（WSGI 入口）。没有改动 `prototypes/`、`web/`、BFF / Traffic Atlas 的任何文件，也没有新增根目录公共文件。

---

## 1. 角色模型（唯一不可协商的规则）

| 身份 | 来源 | 角色 | 项目授权 |
| --- | --- | --- | --- |
| 本地账号 `admin` | `AdminCredentialStore` 中的口令哈希（唯一来源） | `platform_admin` | 无（超管不需要项目 grant） |
| **其他所有人** | 飞书 Web OAuth 授权码 | `member` | **零**，需他人事后单独授予 |

三条硬约束，写在代码里而不是流程里：

1. `store.upsert_feishu_user()` 的 `ON CONFLICT` 更新分支**故意不写 `role` 和 `status` 两列**。因此一个人的飞书**姓名、邮箱、Linux 登录名、`open_id`、`union_id` 长得再像管理员，重新登录也无法把自己变成管理员，也无法把自己从禁用状态里救回来**——这两个字段只能由运维侧直接改库（并留审计）来变更。
2. 没有任何 HTTP 端点可以授予角色或项目。`Application.authorize(session, require_admin=True)` 是唯一的权限守卫，角色只能来自数据库。
3. 飞书登录成功只产生一条 `member` 记录 + 一个会话。项目 grant 一律为空列表。

### 明确声明：Linux 用户登录 / Caddy BasicAuth **不能**替代业务管理员

Dell 上的 Linux 账号登录、Caddy 的 BasicAuth、SSH 密钥、GPU 机器上的任何系统级凭据，都只是**基础设施访问控制**，与"平台业务管理员"是两件完全不相干的事：

- 业务管理员判定只认一件事——**本地 `admin` 的 `/auth/local/login` 通过后拿到的会话里 `user.is_admin == True`**；
- 反向也成立：能登录 Linux 或能过 Caddy 的人，在本系统里依然是 `member`，`/auth/me` 不会给出任何管理员能力；
- 因此**不要**为了"方便"把 Linux 用户名映射成业务角色，也**不要**用 BasicAuth 后面的 `/auth/me` 结果去推断业务权限。这类映射一旦引入，就等于绕过了上面的规则 1 和 2。

---

## 2. 配置

全部来自进程环境变量，前缀 `LLM_LAB_`。**缺配置一律 fail closed**：不完整或互相矛盾的配置会让进程直接启动失败（`ConfigError`），而不是降级成一个"看起来能用"的登录。

### 2.1 必填

| 变量 | 说明 |
| --- | --- |
| `LLM_LAB_AUTH_STATE_DIR` | **绝对路径**。存放 `auth.sqlite3` 与管理员凭据文件，目录权限 `0700`。 |

### 2.2 会话 / Cookie / 限速

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `LLM_LAB_SESSION_IDLE_SECONDS` | `1800` | 空闲超时。每次通过认证会续期。 |
| `LLM_LAB_SESSION_ABSOLUTE_SECONDS` | `43200` | 绝对超时，续期无法越过。必须 ≥ 空闲超时。 |
| `LLM_LAB_COOKIE_NAME` | `llm_lab_session` | 会话 Cookie 名。 |
| `LLM_LAB_CSRF_COOKIE_NAME` | `llm_lab_csrf` | 保留名（CSRF 当前走响应体，见 §4.3）。 |
| `LLM_LAB_OAUTH_BINDING_COOKIE_NAME` | `llm_lab_oauth_binding` | 飞书 OAuth **浏览器绑定** Cookie 名（见 §4.4）。 |
| `LLM_LAB_COOKIE_SECURE` | `true` | 本地纯 HTTP 调试才设为 `0`。 |
| `LLM_LAB_MAX_REQUEST_BYTES` | `8192` | 请求体上限，超出返回 `413`。 |
| `LLM_LAB_LOGIN_MAX_FAILURES` | `5` | 窗口内允许的失败次数。 |
| `LLM_LAB_LOGIN_WINDOW_SECONDS` | `300` | 失败计数窗口。 |
| `LLM_LAB_LOCKOUT_SECONDS` | `900` | 触发后的锁定时长。 |
| `LLM_LAB_POST_LOGIN_PATH` | `/` | 登录成功后的默认落点，必须是同源绝对路径。 |
| `LLM_LAB_LOGIN_ERROR_PATH` | `/login` | 登录失败回跳落点，同上。 |
| `LLM_LAB_TRUSTED_ORIGINS` | 空 | 逗号分隔的额外同源白名单（仅反代改写 `Host` 时才需要）。 |

`HttpOnly` 与 `SameSite=Lax` 是**写死的**，不提供开关：前者保证会话 Cookie 永不被 JS 读到，后者是飞书顶层重定向回跳所必需。

**唯一本地管理员固定叫 `admin`。** `LLM_LAB_ADMIN_USERNAME` 只在取值就是 `admin` 时被接受，其它任何值（含 `bootstrap --admin-username <别的名字>`）都会以 `ConfigError` 拒绝启动。理由：产品只有唯一一个本地超管，允许改名等于凭环境变量凭空造出第二个不受文档约束的管理身份。该校验放在 `AuthConfig.validate()` 里，因此 `replace()` 和直接构造同样绕不过去。

### 2.3 飞书（三者必须同时设置，否则视为配置损坏）

| 变量 | 说明 |
| --- | --- |
| `LLM_LAB_FEISHU_APP_ID` | 飞书应用 App ID。 |
| `LLM_LAB_FEISHU_APP_SECRET` | 飞书应用 App Secret。**只从环境注入，绝不入库/入日志**（`repr=False`）。 |
| `LLM_LAB_FEISHU_REDIRECT_URI` | 必须是绝对 `https://` URL 且不带 fragment。 |
| `LLM_LAB_FEISHU_TENANT_ALLOWLIST` | **必填**，逗号分隔的 `tenant_key` 白名单。留空 = 拒绝启动，绝不等于"接受所有租户"。 |
| `LLM_LAB_ALLOW_INSECURE_REDIRECT_URI` | 仅当回跳地址是 `localhost`/`127.0.0.1`/`::1` 时才允许 `http`。仅限本地调试。 |
| `LLM_LAB_FEISHU_PKCE` | 默认 `false`，**当前必须保持 `false`**（原因见 §5.3）。 |

只设置了三个飞书变量中的一部分 → `ConfigError`，**不会**被当作"飞书未启用"而静默降级。同样地，三个变量都没设时，飞书通道干净地不存在：`/auth/feishu/start` 返回 `503 not_configured`，全程零网络请求。

### 2.4 凭据文件格式

`LLM_LAB_ADMIN_HASH_FILE`（默认 `<state_dir>/admin_password.hash`）权限必须是 `0600` 且属主为服务进程用户，否则该文件被判定为不可用。

单行内容，二选一：

```
scrypt$n=16384,r=8,p=1$<base64 salt>$<base64 digest>
$argon2id$v=19$m=65536,t=3,p=4$<salt>$<digest>
```

- `scrypt`：`hashlib.scrypt`，`n=16384, r=8, p=1`，16 字节随机 salt，32 字节摘要。纯 stdlib，离线可测。
- `argon2id`：若环境里装了 `argon2-cffi` 则可写，校验时自动识别前缀。
- 空文件、多行有效内容、格式非法 → 判定为 `broken`，登录报 `ConfigError`；**没有"默认口令"、没有"空哈希视为不需要密码"这类捷径**。
- 文件每次认证都重新读取，因此轮换**立即生效**，不需要重启进程。

---

## 3. 管理员口令的生命周期

全部通过交互式子命令完成，**没有 `--password` 参数、没有环境变量、没有 stdin 管道**——任何可能落进 shell history、`ps` 输出或 CI 日志的通道都被排除。

```bash
python -m backend.auth.bootstrap init     --state-dir /srv/llm-lab/portal-auth   # 首次设置
python -m backend.auth.bootstrap status   --state-dir /srv/llm-lab/portal-auth   # 只读查看
python -m backend.auth.bootstrap rotate   --state-dir /srv/llm-lab/portal-auth   # 更换口令
python -m backend.auth.bootstrap disable  --state-dir /srv/llm-lab/portal-auth   # 禁用本地登录
```

| 子命令 | 行为 |
| --- | --- |
| `init` | 交互式输入两次，最短 16 字符，两次不一致直接拒绝。凭据已存在时**拒绝覆盖**（`--force` 才覆盖）。写入方式：同目录 `mkstemp` → `fchmod(0600)` → 写 → `fsync` → `os.replace`，崩溃不会留下半个文件。 |
| `status` | 只打印 `state` / `needs_rehash` / `path`，不含哈希的任何片段。 |
| `rotate` | 默认要求**先输入当前口令**；`--recover` 是文档化的 break-glass（假设已具备文件系统权限，等价于本来就能改写该文件），应配合审计使用。 |
| `disable` | 写入哨兵值 `disabled`，此后本地登录报 `account_disabled`（403）；文件仍在，可再 `init --force` 恢复。 |

`status` 的 `state` 取值：`missing`（从未设置）/ `disabled`（被显式禁用）/ `active`（可用）/ `broken`（存在但不可用）。

**首次上线由真实管理员亲自执行 `init`。** 本仓库、本任务、任何测试里都不存在也不允许出现真实口令；测试用的全部是虚构的临时口令。

---

## 4. API 契约

统一响应信封：

```json
{"ok": true,  "data": { ... }}
{"ok": false, "error": {"code": "<snake_case>", "message": "..."}}
```

所有响应都带 `Cache-Control: no-store`、`X-Content-Type-Options: nosniff`、`Referrer-Policy: no-referrer`。

| 方法 | 路径 | 成功 | 说明 |
| --- | --- | --- | --- |
| `GET` | `/auth/csrf` | `200` | `data: {csrf_token, authenticated, providers:{local_admin,feishu}}` |
| `POST` | `/auth/local/login` | `200` | `data: {user, csrf_token, session_expires_at}` + 下发会话 Cookie |
| `GET` | `/auth/feishu/start` | `302` | 跳到飞书授权端点 + 下发**浏览器绑定 Cookie** |
| `GET` | `/auth/feishu/callback` | `302` | 跳回**固定内部路径** + 下发会话 Cookie；**成功与失败都会清除绑定 Cookie** |
| `GET` | `/auth/me` | `200` | `data: {authenticated, user, projects, session_expires_at}`；未登录 `401` |
| `POST` | `/auth/logout` | `200` | `data: {logged_out:true}`，撤销会话并清 Cookie；会话已过期或账号已禁用时同样撤销并清 Cookie（完全没有 Cookie 才 `401`） |

其他路径 → `404`；路径存在但方法不对 → `405`；请求体超限 → `413`。

### 4.1 成功侧导航约定

登录成功后落点取自 `POST_LOGIN_PATH`（默认 `/`），飞书通道可用 `?next=` 指定，但**该值会被重新校验**：不是同源绝对路径就丢弃并回落到默认值。`//evil.com`、`https://evil.com`、`/\evil.com`、含空白/控制字符的值一律不通过——**开放重定向在这里被彻底堵死**。

### 4.2 失败侧导航约定

`/auth/feishu/callback` 上的任何失败都**不返回 JSON**，而是 302 到 `LOGIN_ERROR_PATH`，并带上 `error=<oauth_error>&reason=<code>`：

| `error` | 触发场景 |
| --- | --- |
| `invalid_state` | `state` 未知 / 过期 / 已被用过；或缺少/不匹配**浏览器绑定 Cookie**（见 §5.3） |
| `access_denied` | 用户取消、应用被拒、租户不在白名单 |
| `temporarily_unavailable` | 飞书接口超时/不可用、未配置、触发限速 |
| `invalid_request` | 缺 `state` 或 `code` 等请求本身不合法 |

### 4.3 CSRF 与同源校验

写请求（`POST`/`PUT`/`PATCH`/`DELETE`）必须同时满足两条，**校验顺序固定为：体积 → 同源 → CSRF**：

1. **同源校验**：`Origin`（缺失时用 `Referer`）必须与请求自身构成**完整同源元组** `scheme://host[:port]` 相等——**只比 authority 是不够的**：HTTPS 请求携带 `Origin: http://同主机` 是**另一个源**，攻击者的纯 HTTP 页面可以设置它并让浏览器带上，因此必须拒绝。端口按 scheme 归一（`https://h` 与 `https://h:443` 同源）。
   比较所用的 scheme **只取服务端自己的视图**（`wsgi.url_scheme`，由 WSGI 容器设置），**绝不读 `X-Forwarded-Proto` / `Forwarded`**——那是客户端可控的请求头，信任它等于让纯 HTTP 请求自称 HTTPS。也可命中 `TRUSTED_ORIGINS` 白名单（默认空，逐条按同样的完整元组归一后精确比较）。`Origin: null`、协议相对（`//host`）、无 scheme、非法 scheme 一律拒绝（`403 csrf_rejected`）。
2. **CSRF token**，通过 `X-CSRF-Token` 头提交：
   - **已登录**：会话绑定、长期稳定的 token，恒定时间比较；
   - **未登录**（登录表单本身也要防跨站伪造）：`GET /auth/csrf` 签发一次性、10 分钟有效的 token，用掉即失效。

CSRF token **只能**通过服务端接口同源获取，前端**不得**把它（或任何会话凭据）写进 `localStorage`。

### 4.3.1 身份不由请求字段声明

在 IAM 拥有的 auth 路由（`/auth/local/login`、`/auth/tokens*` 等）以及 route map 归属 IAM 的 `/auth/linux/login` 上，query 或 JSON body 中出现 `uid`、`user_id`、`principal`、`owner`、`role`、`roles`、`linux_user`、`admin` 等身份字段时，整个请求以 `400 identity_assertion_rejected` 拒绝，而不是“读到但不用”。字段清单与 `backend.registry.identity.FORBIDDEN_IDENTITY_FIELDS` 由测试钉住，避免两处守卫漂移。

`/auth/local/login` 的 `username` 是被验证的凭据标识，不是主体声明，因此该路由的 **body** 允许它；出现在 query 中仍然拒绝。BFF 自有的 `/auth/admin/*` 与 `/api/v2/ops/**` 保持自己的字段策略（例如 `/auth/admin/enter` 只读取 `password`），本层不改变它们；如需同样的字段级拒绝，由 BFF Owner 在各自路由接线，负向用例见 `tests/auth_linux/test_http_routes.py`。

### 4.4 Cookie

```
Set-Cookie: llm_lab_session=<opaque>; Path=/; HttpOnly; SameSite=Lax[; Secure]
Set-Cookie: llm_lab_oauth_binding=<nonce>; Path=/; Max-Age=600; HttpOnly; SameSite=Lax[; Secure]   # 仅飞书流程
```

会话 Cookie **不设 `Max-Age`**（浏览器会话级），过期完全由服务端的空闲+绝对双重超时决定。值是不透明随机串，数据库里**只存 `sha256` 摘要**——库被读走也无法直接拿来冒充会话。飞书的 `access_token` / `user_access_token` **绝不进入 Cookie**。

**浏览器绑定 Cookie**（挂起流程期间才存在）：值是与 `state` 相互独立的 256-bit 随机串，**只下发给发起该次登录的那一个浏览器**，不出现在任何 URL、响应体或日志里；服务端对应用同样的**摘要**存档（`sha256("oauth-binding\0"+nonce)`），库被读走也拿不到 nonce。属性固定为 `Path=/`、`HttpOnly`、`SameSite=Lax`、`[Secure]`、`Max-Age` 等于流程 TTL（600s），**不设 `Domain`**（host-only，不外溢到同域其它子域）。`SameSite=Lax` 不是偏好而是必需：回调是来自飞书的**顶层跨站 GET 导航**，`Strict` 会不发送该 Cookie，而比 `Lax` 更松会让 nonce 搭上跨站子资源请求。流程结束——**无论成功还是失败**——都下发 `Max-Age=0` 清除，绝不留下陈旧 nonce。

---

## 5. 安全不变量

### 5.1 本地口令

scrypt（或 argon2id）+ 每账号独立随机 salt；校验用 `hmac.compare_digest` 恒定时间比较；用户名比较同样恒定时间，且**用户名错误时依然执行一次昂贵的口令校验**，避免通过响应耗时区分"用户不存在"和"口令错误"。失败响应文案统一为 `Authentication failed.`，不提 password / hash / 用户是否存在。

限速计数与锁定状态存在 SQLite（`auth_attempts` 表）里，**跨进程、跨 worker 生效**——多 worker 部署下不是每个 worker 各算各的。

### 5.2 会话

单一服务端会话存储、统一角色判定。会话 id 由 `secrets.token_urlsafe(32)` 生成（≥256 bit 熵）；空闲与绝对双重过期；重新登录**先撤销旧会话再签发新会话**（防会话固定）；登出即撤销；`purge` 清理过期行。

### 5.3 飞书通道

- 端点全部取自官方文档，不做任何猜测：授权 `https://accounts.feishu.cn/open-apis/authen/v1/authorize`；`app_access_token` 走 `auth/v3/app_access_token/internal`；换 `user_access_token` 走 `authen/v1/access_token`；取用户信息走 `authen/v1/user_info`。所有出站请求强制 HTTPS（`ssl.create_default_context()`）。
- `state` 是服务端短生命周期的一次性 nonce，**用掉即删**；授权码同理做一次性消费记录（含重放保护）。
- **`state` 本身不足以证明"回调来自发起它的那个浏览器"**。`state` 会经飞书往返，任何拿到该 URL 的人都能把它递给另一个浏览器；同 NAT 下更是唾手可得。若只校验 `state`，第二个浏览器就会成为持有会话的一方——即登录 CSRF / 会话错置：受害者此后操作的是攻击者能看到的账户。因此每次 `start` 额外生成一个**独立的** `browser_nonce`，只下发给发起浏览器（见 §4.4），并在服务端与 `state` 记录存放在一起（只存摘要）。
- **回调校验顺序固定**：先确认本请求带有绑定 Cookie（缺失即拒绝，**不触碰数据库**），再在**同一个事务里**读取 `state` 记录、用 `hmac.compare_digest` 做**恒定时间**摘要比对、**匹配成功才**删除该 `state`——即"先验证、再原子消费"。绑定不符时**不消费**该行：否则任何见过 `state` 的人都能凭一次无 Cookie 的请求把真正用户晾在门外（拿 nonce 猜是不可能的，所以留着这行毫无代价，只把失败从"拒绝"变成"denial"）。只有在这些都通过之后，才会把授权码发往飞书换取身份——**证明不了浏览器身份的回调，绝不会触发任何上游 token exchange**。授权码一次性消费与租户白名单校验保持原样。
- 身份归属是**联合键** `(app_id, tenant_key, open_id)`：本地用户主键与 `users` 的唯一索引都按这三元组取值。`open_id` 由应用**和租户**共同限定，只用 `(app_id, open_id)` 会让白名单里的两个租户塌缩成同一个本地用户，连同其项目授权一起被继承。
- `redirect_uri` 固定取配置，不接受请求参数覆盖。
- 校验 `tenant_key` 是否在白名单内；对 `open_id` + `tenant_key` 做换 token 结果与 `user_info` 结果的**交叉核对**，不一致即视为上游异常。不同应用、不同租户之间都不会混用身份（用户唯一键是 `(auth_source, app_id, tenant_key, external_id)`）。
- **PKCE**：`supports_pkce = False`。飞书官方 `/authen/v1/access_token` 目前**不接受** `code_verifier`，因此这里选择显式拒绝而不是"静默降级到不安全的 URI"——把 `LLM_LAB_FEISHU_PKCE` 打开会让进程直接报错。待官方支持后再实现。
- 空租户白名单 fail closed（见 §2.3）。

### 5.4 信息暴露面

`/auth/me` 与所有响应只返回必要身份信息：`id`、`display_name`、`role`、`auth_source`、`is_platform_admin`、项目列表。**不含**任何凭据、哈希、上游 token、`open_id`/`union_id`/`tenant_key`。审计表只记结果与内部用户 id，不记口令、不记哈希片段（有测试断言这一点）。

未预期的异常一律收敛为通用 `500`，内部堆栈不外泄。

### 5.5 请求体读取契约与代理边界

只有服务端声明了**正整数且格式合法**的 `Content-Length`，才去读请求体；其余情况（缺失、空串、非十进制整数、负数、带 `Transfer-Encoding`）一律**视为空体，并且完全不触碰 `wsgi.input`**。

- 依据：PEP 3333 允许无请求体的请求不设 `CONTENT_LENGTH`（浏览器普通 `GET` 即如此），也从不承诺一次读会读满所请求的字节数。真实服务器交给应用的 `wsgi.input` 是**socket 支撑**的 reader，`read(n)` 会阻塞到读满 `n` 字节或对端关闭——对无体请求"顺手读一下"会让请求一直挂到客户端超时。内存 `BytesIO` 客户端复现不了这一点，因此该缺陷在纯内存测试下不可见。
- fail closed 的两个后果：(a) 未声明长度的写请求不会被当成"读到 EOF"的正常 JSON 正文，而是以空体进入路由，由路由以其普通 `400 invalid_request` 拒绝；(b) 非法 / 负 / 缺失长度既不会阻塞，也不会被强行解读。
- 上限：读取量恒为 `min(max_bytes + 1, declared)`。多读的那 1 字节用于让路由在**不信任 `Content-Length`** 的前提下判定超限（`413`），同时永不整段缓冲请求体。只接受 ASCII 十进制数字——`int()` 会接受 `"+5"`、`"1_0"` 等非法写法，这里一律拒绝而不是重新解释。
- **代理边界（chunked）**：分块传输的解码是服务器的职责（PEP 3333）。本层**不做** chunked 解码，也不把它当作正文。受支持的部署形态是：Portal 前的可信反向代理（Caddy）终结 chunked 并转发 `Content-Length`；若请求到达本层时仍是 chunked，则按空体处理并被路由拒绝（fail closed），而**不是**静默当作正常 JSON 正文。
- 残余 caveat（本层不负责、交由前置设施）：客户端可以谎报 `Content-Length`（声明很大却只发一部分）从而让 `read` 阻塞。这是 WSGI 输入流的固有属性，须由前置服务器 / 网关的**读超时**与**请求体上限**兜底；同理，超限请求在 `413` 后不会读完剩余字节，因此该连接必须关闭（wsgiref 与前置代理均按 HTTP/1.0 语义或 `Connection: close` 关闭）。

---

## 6. 测试与本地验收

```bash
python -m unittest discover -s tests/auth -t .
```

- 全部使用**纯 stdlib**（`hashlib`、`hmac`、`secrets`、`sqlite3`、`wsgi`），可在无网络、无第三方依赖的环境（例如 Dell 上的 `alice`）直接跑。
- 测试**不发起任何真实网络请求**：飞书侧用 `FakeTransport` / `FakeFeishuProvider` 打桩，覆盖成功、错码、未知租户、`state` 失效、重放、超时、OAuth 错误等分支。
- HTTP 层用内存 WSGI 客户端走**完整调用链**（environ → 路由 → 服务 → 存储 → 响应 + Cookie jar），不是只拼 URL 字符串。
- 另有一组 **socket 级**测试 `tests/auth/test_wsgi_socket.py`：真实 `wsgiref` 服务器绑定随机 `127.0.0.1` 端口，客户端用 `http.client`（部分用例直接写原始请求字节，以证明请求确实**没有** `Content-Length`），每个用例客户端超时 `3s`——**任何阻塞都会以超时失败**。覆盖：无 `Content-Length` 的 `GET`/`HEAD`、显式 `Content-Length: 0`、携带合法 JSON 正文的 `POST`、超限 `413`、非法 / 负数 / 缺失 `Content-Length`、以及 `Transfer-Encoding: chunked`。协议栈单元层则用记录型流断言"未声明长度时 `read` 从未被调用"。
- 覆盖的反例包含：飞书用户邮箱为 `admin@...`、姓名为 `admin`、`open_id` 形似管理员——**一律不得拿到管理员角色**；以及各种提权尝试（伪造 `role` 字段、复用他人会话、篡改 `next` 参数做开放重定向）必须失败。

凭证卫生检查：`git diff --check`；并对本次全部改动做敏感信息扫描，确认无任何 token / 口令 / 哈希泄漏到代码、测试、文档、HTML。

---

## 7. 尚未完成 —— 上线前必须由**其他人**完成的事项

以下事项**不在本次实现范围内**，且**未获得授权**去执行。本层代码上线前，这些必须逐项闭环：

1. **飞书应用侧配置（由应用管理员完成）**
   - 创建/确认自建应用的 App ID / App Secret，并只通过环境变量注入；
   - 在飞书后台登记重定向 URL，且必须与 `LLM_LAB_FEISHU_REDIRECT_URI` **逐字符一致**；
   - 申请并开通 `authen:user.id` 等所需权限；
   - 确认实际 `tenant_key` 并写入 `LLM_LAB_FEISHU_TENANT_ALLOWLIST`（仍须在真实租户上验证一次白名单拦截行为）。
2. **真实回调与 HTTPS 终结**：真实域名、证书、反代（含 `Host` 转发是否正确，否则 §4.3 的同源校验会误拒）。
3. **管理员安全设置**：由真实管理员亲自执行 `init` 设定口令；确定轮换周期与 `--recover` 的 break-glass 使用规程；确认 `state_dir` 的 `0700` 与凭据文件 `0600` 属主。
4. **多进程/多实例部署核对**：确认限速与一次性 token 的 SQLite 共享路径在真实部署形态下确实被所有 worker 共享；确认备份策略不把凭据文件带进非受控位置。
5. **独立 QA 验收**：由非本次实现者按 §4/§5 复测 HTTP 调用链、错误分类、CSRF/同源、开放重定向与提权反例。
6. **部署审批**：上线动作（服务单元、端口、反代与 Gateway/Caddy 变更、生产环境变量注入）**均需单独审批**。本次实现明确**未**触碰 Gateway、Caddy、Strata、GPU 相关配置，也未变更任何 SSH 用户或密钥。
7. **前端对接**：`/auth/csrf` → 取 token → 写请求带 `X-CSRF-Token`；凭据只走 Cookie，不入 `localStorage`；按 §4.2 的错误码渲染提示。前端改动不属于本仓库这一层。
