# Atelier V6 · 新旧信任边界对照（Trust boundary）

> Lane Release/Legacy #50 交付物 · 2026-10-16 · 工单 [#50](https://github.com/ProfessorZhi/llm-lab-portal/issues/50) · 相关：[#34](https://github.com/ProfessorZhi/llm-lab-portal/issues/34)、[#45](https://github.com/ProfessorZhi/llm-lab-portal/issues/45)、[#49](https://github.com/ProfessorZhi/llm-lab-portal/issues/49)
>
> **本文只描述边界，不改变边界。** 本 Lane 未获授权改动 PAM、systemd、反向代理、root/UID 或任何生产服务。文中不含任何凭据值、口令哈希或令牌。

---

## 1. 结论（先读这一段）

1. **旧 LAN 站与当前 V6 预览的信任边界在结构上是同一种东西**：一个**共享口令**挡在门口，口令之后所有远程用户**互相等价**。区别只在实现细节，不在安全模型。
2. 因此当前 V6 预览**不是**"更安全的新站"，它是一个**后台视觉/交互已换代、授权模型还没换代**的预览。
3. **在没有受信同源 BFF 之前，旧站必须保留**（见 §5）。
4. **旧站的匿名数据 API 不得直接嵌入新站**（[#34](https://github.com/ProfessorZhi/llm-lab-portal/issues/34)）：那会把"共享口令之后的匿名面"变成"新站里的匿名面"，边界只会更宽，不会更窄。

---

## 2. 旧 LAN 站的边界

### 2.1 进程侧

- 监听 `127.0.0.1:8765`（配置默认），`main()` **拒绝**任何不在 `{127.0.0.1, localhost, ::1}` 的绑定（`portal/server.py:3127`）。
- 也就是说，**进程本身没有任何身份概念**：谁能连上环回端口，谁就能读一切。

### 2.2 路由侧

- `do_GET`（`portal/server.py:2728`）**没有**任何 auth / session / cookie / API key 校验。旧站的全部 `GET` 面在环回上是**完全匿名**的。
- 唯一的一道请求闸门在两处 `POST`（`server.py:2829`、`server.py:2851`）：要求头部 `X-Requested-With`。这是**同源防护**（挡浏览器跨站表单），**不是认证**——任何能构造 HTTP 请求的人都能带上这个头。

### 2.3 真正的边界：代理层

远程用户走反向代理进入。边界由 `deploy/dell/caddy/Caddyfile.tmpl` 承担：

| 代理行为 | 作用 |
| --- | --- |
| 所有路由 **basic_auth** | 唯一入口闸门；口径是**一个共享口令**，不是用户身份 |
| 剥离客户端传入的身份头（`X-Forwarded-User` 等） | 防止"上游直接自称是谁" |
| `/api/service/*` 仅管理员凭据可用 | 对**服务控制**再收一层 |
| `tls internal`（私有 CA） | 传输加密，非身份 |

**这是旧站安全模型的关键**：边界**不在应用里**，在代理里。应用被设计成"只敢绑环回"，把身份问题整个外包给代理。

### 2.4 数据面

- 旧站**不回显密钥**：接口只给 `key_present` 与 `sha256[:12]` 指纹（`server.py:2231`）。
- 路径参数（`/report?p=`、`/file?p=`、`/download?p=`）经 allowlist + `commonpath` 校验（`server.py:1348`），拒绝 `..` 与绝对路径。
- SSH 探测是**固定只读命令**（`server.py:718` 的 `REMOTE_PROBE`），主机密钥 pin（`server.py:759`）。

这些是**好性质，应当继承**——但不是身份边界。

---

## 3. 当前 V6 预览的边界

### 3.1 访问链

```
Tailscale HTTPS :8443
        ↓
sidecar 反向代理  127.0.0.1:18443
        · Basic Auth（一个共享口令，用户名为固定预览账号）
        · Host 白名单：非指定 tailnet FQDN 直接 421
        · 反向代理到预览，且剥离上游身份头
        ↓
静态预览  127.0.0.1:18765   （node web/tools/serve.mjs，只绑环回）
```

`web/tools/serve.mjs` **自身没有**认证、没有 Host 校验、没有 CSRF/Origin 校验；它的边界只有"绑定写死为 127.0.0.1"。上面那条链里**唯一的闸门是 sidecar 的 Basic Auth**。因此：**若代理被摘掉或规则被放宽，预览即刻变成完全匿名**，且应用层不会拦任何东西。

### 3.2 与旧站的同与异

| 维度 | 旧 LAN 站 | V6 预览 | 是否构成真正的安全提升 |
| --- | --- | --- | --- |
| 应用进程绑定 | 仅环回 | 仅环回 | 否（同） |
| 入口闸门 | 代理 basic_auth | 代理 basic_auth | 否（同） |
| 身份粒度 | 共享口令 → 所有人等价 | 共享口令 → 所有人等价 | **否** |
| Host 校验 | 应用层无（代理负责） | sidecar 层有白名单（非白名单 421） | 略好，但仍是**共享口令**之后的加固 |
| 应用层请求校验 | 无（除 `X-Requested-With`） | 静态预览 `serve.mjs`：**无**；环回登录预览 `preview_auth.py`：有全串 Host 守卫 | 部分，且仅对**另一个入口**有效，见 §4 的适用范围 |
| 授权/RBAC | 无 | 无 | 否 |
| 凭据回显 | 只给指纹 | 只给指纹 | 否（同，都是好性质） |
| 数据来源 | 真实本机数据 | **受控只读真实遥测**（`/api/infra/summary`，`readonly=true`）或 `?demo=1` 合成样本 | 数据真实性提升，**不是**授权提升 |

**结论**：V6 预览在**数据真实性**（不再默认合成）与**Host 加固**上有实质改进；在**身份与授权**上没有改进。把前者当成后者，是这次发布最需要避免的误读。

### 3.3 V6 明确是 fail-closed 的地方（好性质）

- Linux 登录表单在未建立会话时**禁用**（字段与提交按钮 disabled），相关 POST 路径**未接线**。
- 预览的 `--state-dir` 有逐级链接检查（`docs/preview-state-dir-security.md`），拒绝把凭据写入被改向的目录。
- 环回登录预览有全串 Host 权威校验（`docs/preview-host-security.md`，Issue #8 修复）。

---

## 4. 一个必须说清楚的适用范围陷阱

`docs/preview-host-security.md` 的 Host 守卫位于 `dev/preview_auth.py`，服务的是**环回登录预览**。该文档自己写明：**预览不带反向代理，也不应被放到反代后面**，因为反代会重写 `Host`，而那条守卫的前提正是"我看到的 `Host` 就是客户端说的 `Host`"。

而当前对外预览走的是 `web/tools/serve.mjs`（静态服务）+ sidecar 代理。

> **因此：不要把 `preview_auth.py` 的 Host 守卫当成"V6 预览已有 Host 防护"的证据。** 那是另一个入口。
> 这份对照里 sidecar 的 Host 白名单是**独立**的一层，两者不可互相代替，也不能合并计数。

---

## 5. 在受信 BFF 落地之前，旧站必须保留

判据（发布检查单 H6）：

1. 旧站承担的每一项能力，在 V6 侧必须先达到**逐项等价**（见 `atelier-v6-legacy-parity-matrix-2026-10-16.md`）。当前矩阵里 L08/L09/L10/L14 为 **BLOCKED**，即**远未达到**。
2. 受信同源 BFF（[#49](https://github.com/ProfessorZhi/llm-lab-portal/issues/49)）与可信身份（[#45](https://github.com/ProfessorZhi/llm-lab-portal/issues/45)）尚未落地。
3. 因此：**旧站不退役、不改路由、不迁移用户**。V6 与旧站并存。

**明确做法**：V6 的代理规则**只新增**预览入口，**不修改**旧站既有入口；改动前后记录 443 监听状态作为回滚依据（见托管文档 §2.2）。

---

## 6. 旧站匿名数据 API 不得嵌入新站（[#34](https://github.com/ProfessorZhi/llm-lab-portal/issues/34)）

这一条要单独强调，因为它看起来像"省事的集成"：

| 想法 | 后果 | 判定 |
| --- | --- | --- |
| 在 V6 页面里 `fetch('/api/reports')` 直接取旧站数据 | 把旧站的**匿名面**原样搬进新站；旧站的边界本来靠"只绑环回 + 代理 basic_auth"，一旦被新站的服务端代理转发，**新站的所有访问者**都能拿到，与"谁是谁"完全脱钩 | **禁止** |
| 让 V6 静态页直连旧站环回端口 | 边界由"谁能到达该端口"决定，比代理层更弱 | **禁止** |
| 让 V6 的 BFF 以**固定服务身份**去读旧站，并把结果按调用者的授权过滤后返回 | 边界仍在 BFF 内，调用者身份参与过滤 | 允许方向，属 [#49](https://github.com/ProfessorZhi/llm-lab-portal/issues/49) 的契约 |
| 把旧站数据导出成静态文件放进 V6 | 数据在 git 里、跨 UID 泄露、无法按人收权 | **禁止**（且违反 `access-boundaries.md` §3） |

判据：**跨 UID 的匿名报告风险必须先封住**（[#34](https://github.com/ProfessorZhi/llm-lab-portal/issues/34) 是 P0），**然后**才谈对外提供任何多用户资料。

---

## 7. 本 Lane 的边界（未做也不打算做）

- 未修改任何代理配置、未新增对外监听、未申请证书。
- 未创建/修改 systemd 单元、未注册定时任务。
- 未读取、未复制、未转存任何凭据（包括旧站端点密钥与预览口令）。
- 未把旧站任何数据、报告原文、prompt/response、`usage.jsonl` 带入本仓库。
- 未对 `ProfessorZhi/llm-lab` 做任何写操作（见 `access-boundaries.md` §5）。
