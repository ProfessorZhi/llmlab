# LLM Lab 平台 V1 简化架构方案

**日期**：2026-10-17 · **状态**：提案（Draft），待评审 · **适用**：单台 Dell Precision 5820（RTX A6000 48 GB）、多 Linux 用户

本文是在 [`architecture.md`](architecture.md) 基础上的**收敛版**：保留身份、数据真实性和安全边界，砍掉 V1 不需要的复杂度。两份文档冲突时，以本文的「已定决策」为准；本文没提到的细节沿用 `architecture.md`。

---

## 0. 已定决策（产品负责人，2026-10-17）

| # | 问题 | 决定 | 对架构的影响 |
|---|---|---|---|
| D1 | 推理引擎由谁启动 | **每个用户在自己的 Linux 账户下自行启动**，到平台登记 | 平台不启停 GPU 进程，只做登记、健康检查、监控和路由 |
| D2 | API Key 权限粒度 | **Key 绑定可调用的模型列表**；能过期、能撤销 | 不做「把我的 Endpoint 共享给某人」的逐人 ACL；用「模型可见性 + Key 的模型白名单」代替 |
| D3 | 限额 / 限流 | **V1 不做，只记录** | Gateway 无配额状态；用量只用于展示和审计 |
| D4 | Gateway 职责 | **只做三件事：校验 Key → 转发 → 记日志** | Gateway 是一个很薄的反向代理，不承担登记、编排或计费 |

---

## 1. 产品一句话

一台 GPU 机器上的**多用户模型服务平台**：用户自己跑推理引擎、登记成模型、生成 API Key；平台统一转发调用、记录每次调用、监控 GPU 和引擎、跑评测。

**不做**：GPU 调度器、集群管理、远程 shell、权重文件管理、按 Token 计费。

---

## 2. 总体架构

```mermaid
flowchart TB
    subgraph Clients["使用方"]
      WEB["网页 Portal"]
      CLI["llmlab CLI"]
      SDK["SDK / 应用<br/>(OpenAI 兼容)"]
    end

    CADDY["Caddy :443<br/>Tailnet HTTPS"]
    WEB --> CADDY
    CLI --> CADDY
    SDK --> CADDY

    subgraph Self["自建（本仓库）"]
      BFF["Portal 后端 BFF<br/>登录 / 页面数据 / 管理操作"]
      GW["Gateway（薄）<br/>校验 Key → 转发 → 记日志"]
      DB[("SQLite<br/>users · models · api_keys<br/>call_logs · audit")]
    end

    subgraph OSS["开源组件"]
      PROM["Prometheus :9090"]
      GRAF["Grafana :3000<br/>仅管理员"]
      EVAL["EvalScope"]
      DCGM["DCGM / node exporter"]
    end

    subgraph Engines["推理引擎（各用户自己的账户）"]
      E1["Strata :8080<br/>(管理员维护的共享模型)"]
      E2["用户 A 的引擎"]
      E3["用户 B 的引擎"]
    end

    CADDY -->|"/  /api/*  /auth/*"| BFF
    CADDY -->|"/v1/*"| GW
    BFF --> DB
    GW --> DB
    GW --> E1 & E2 & E3
    BFF -->|"只读查询，强制加用户标签"| PROM
    PROM --> DCGM
    PROM -->|"/metrics"| E1 & E2 & E3
    GRAF --> PROM
    EVAL -->|"用发起人的 Key"| GW
    BFF -->|"提交任务 / 读报告"| EVAL
    BFF -->|"PAM"| PAM["Linux PAM Broker"]
```

**一条原则**：开源组件只做它擅长的事。**「谁是谁、能调用什么、调用了多少」只由自建部分（BFF + Gateway + SQLite）说了算。**

### 2.1 两个入口，路径分开

| 路径 | 谁用 | 认证方式 | 处理者 |
|---|---|---|---|
| `https://lab/…` 页面、`/api/*`、`/auth/*` | 浏览器、CLI 管理操作 | Linux 登录后的会话 Cookie（浏览器）/ 平台 Token（CLI） | BFF |
| `https://lab/v1/*`（OpenAI 兼容） | SDK、应用、CLI 的 `invoke`、EvalScope | **API Key**（`Authorization: Bearer sk-lab-…`） | Gateway |

管理和推理走不同的进程：Gateway 出问题不影响登录和查看，BFF 发版也不会打断正在进行的推理流。

---

## 3. 组件职责

| 组件 | 类型 | 负责 | 不负责 |
|---|---|---|---|
| **Caddy** | 开源 | HTTPS、按路径分流到 BFF / Gateway | 鉴权 |
| **Portal 后端（BFF）** | 自建 | PAM 登录与会话；模型登记与健康检查；Key 的签发、列表、撤销；查询调用日志；代查 Prometheus；提交评测；审计 | 转发推理请求 |
| **Gateway** | 自建，**薄** | 解析 Key → 判断 Key 是否有效、模型是否在白名单 → 转发到引擎 → 写一条调用日志 | 登记、配额、编排、存正文 |
| **SQLite** | 开源 | 唯一业务数据库（WAL 模式），BFF 读写、Gateway 读 Key/模型并写日志 | — |
| **推理引擎** | 用户自己 | 推理；暴露 OpenAI 兼容接口和 `/metrics` | 鉴权（只认 Gateway 持有的引擎密钥） |
| **Prometheus** | 开源 | 采集 GPU（DCGM）、主机（node_exporter）、引擎 `/metrics`、Gateway 指标 | 按用户鉴权 |
| **Grafana** | 开源 | 管理员看全局深度图表 | 普通用户的数据隔离（所以普通用户不进 Grafana） |
| **EvalScope** | 开源 | 跑评测、出报告 | 自己决定占 GPU、绕过 Gateway 直连引擎 |
| **llmlab CLI** | 自建 | 命令行完成登记、Key 管理、查看日志、调用 | — |

---

## 4. 数据模型（SQLite）

只有六张核心表。用户身份 = Linux UID，不用用户名字符串做主键。

```sql
-- 登录过的 Linux 用户（PAM 证明身份后自动建档）
users(uid INTEGER PRIMARY KEY, username TEXT, is_admin INTEGER DEFAULT 0,
      disabled INTEGER DEFAULT 0, created_at)

-- 模型 = 一个可被调用的服务入口（一个引擎可登记多个模型名）
models(id TEXT PRIMARY KEY,            -- 对外的模型名，如 "qwen3-8b-zhangsan"，全局唯一
       owner_uid INTEGER,              -- 谁登记的
       upstream_url TEXT,              -- 只允许 127.0.0.1:<白名单端口> 或 unix socket
       upstream_model TEXT,            -- 引擎内部的模型名
       upstream_secret_ref TEXT,       -- 引擎密钥的引用（明文不入库，见 §7）
       metrics_url TEXT,               -- 可选，供 Prometheus 采集
       visibility TEXT CHECK (visibility IN ('private','public')) DEFAULT 'private',
       status TEXT,                    -- registered / healthy / unhealthy / disabled
       last_check_at, created_at)

-- API Key：只存哈希，明文只在签发时显示一次
api_keys(id TEXT PRIMARY KEY, owner_uid INTEGER, name TEXT,
         prefix TEXT,                  -- 前 8 位，用于界面识别，如 sk-lab-3f9a…
         hash TEXT,                    -- SHA-256(明文)
         expires_at, revoked_at, last_used_at, created_at)

-- Key 能调用哪些模型（D2）
api_key_models(key_id TEXT, model_id TEXT, PRIMARY KEY (key_id, model_id))

-- 每次调用一行，只有元数据，没有 prompt / completion
call_logs(id INTEGER PRIMARY KEY, ts, request_id TEXT,
          key_id TEXT, caller_uid INTEGER, model_id TEXT, model_owner_uid INTEGER,
          path TEXT, stream INTEGER, status INTEGER,
          prompt_tokens INTEGER NULL, completion_tokens INTEGER NULL,   -- 未知为 NULL，不填 0
          ttft_ms INTEGER NULL, total_ms INTEGER, error TEXT NULL)

-- 管理操作审计：登记/删除模型、签发/撤销 Key、管理员操作
audit_log(id INTEGER PRIMARY KEY, ts, actor_uid, action TEXT, target TEXT, detail_json TEXT)
```

**可见性规则（替代逐人共享）**：

- `private` 模型：只有所有者的 Key 能绑定它。
- `public` 模型：任何用户的 Key 都能绑定它（例如管理员维护的 Strata 共享模型）。
- 所有者把模型从 `public` 改回 `private`，或删除/停用模型 → **别人已绑定的 Key 立即调不通**（Gateway 每次请求都实时检查，见 §5.3）。

> 如果以后确实需要「只共享给张三」，再加一张 `model_grants(model_id, grantee_uid)` 表即可，不影响上面的结构。现有 Registry 的 `endpoint_grants` 可以直接迁过来。

---

## 5. 关键流程

### 5.1 登记引擎和模型（D1）

```
用户在自己账户下启动引擎（例：vllm serve … --host 127.0.0.1 --port 8601 --api-key <引擎密钥>）
  → 网页「模型」页或 llmlab models register 提交：模型名、upstream_url、upstream_model、引擎密钥、可见性
  → BFF 校验：
       · upstream_url 只能是 127.0.0.1 + 端口白名单（拒绝 0.0.0.0、外网、:8080 以外的系统端口）
       · 端口当前的监听进程属于该用户的 UID（读 /proc/net/tcp + /proc/<pid>/status，
         防止把别人的引擎登记成自己的）
       · 用引擎密钥调一次 GET /v1/models，确认模型存在 → status = healthy
  → 写 models + audit_log
  → BFF 重新生成 Prometheus 的 file_sd 目标文件（见 §6.2）
```

后台每 30 秒做一次健康检查，更新 `status` 和 `last_check_at`。引擎挂了，页面显示 `unhealthy`，Gateway 对该模型返回 `503 model_unavailable`。

### 5.2 签发和撤销 API Key（D2）

```
网页「API 管理」→「新建 Key」：名称、有效期（默认 90 天）、勾选可调用的模型（只列出自己能看见的）
  → BFF 生成 sk-lab-<32 字节随机>，只存 SHA-256 哈希 + 前缀
  → 明文在弹窗中显示一次（复制按钮 + 「关闭后无法再次查看」提示）
撤销：写 revoked_at → Gateway 的下一次请求就会拒绝（缓存最多 10 秒，见 §5.3）
```

浏览器发起的写操作都要过 CSRF + Origin 校验（沿用现有 `backend/auth` 的实现）。

### 5.3 调用路径（Gateway，D3 / D4）

```
POST /v1/chat/completions   Authorization: Bearer sk-lab-…   {"model": "qwen3-8b-zhangsan", …}

1. 取 Key → SHA-256 → 查 api_keys（10 秒内存缓存，按哈希）
     不存在 / 已撤销 / 已过期 / 所属用户已停用     → 401 invalid_api_key
2. 查 model：存在、未停用、Key 的白名单包含它、
     且（visibility='public' 或 owner_uid = Key 的 owner_uid） → 否则 404 model_not_found
     （不可见的模型和不存在的模型返回同样的 404，不泄露别人的模型名）
3. 模型 status = unhealthy                         → 503 model_unavailable
4. 转发到 upstream_url：替换 Authorization 为引擎密钥，model 字段改写为 upstream_model；
     流式请求自动加 stream_options.include_usage = true，原样流回客户端
5. 结束（成功、失败或客户端断开）时写一条 call_logs：
     tokens 取响应里的 usage；拿不到就记 NULL；记录 TTFT 和总耗时
6. 暴露 Prometheus 指标：lab_gateway_requests_total{model,status}、
     lab_gateway_latency_seconds{model}（标签只用 model 和 status，不放 key/uid，控制基数）
```

**Gateway 支持的路径（V1）**：`/v1/models`（只列出这个 Key 能用的模型）、`/v1/chat/completions`、`/v1/completions`、`/v1/embeddings`。其他路径一律 404。

**实现建议**：一个约 300–500 行的 Python 异步服务（aiohttp 或 Starlette + httpx），单进程，systemd 托管，监听 `127.0.0.1:8300`。不引入 Redis、消息队列或配额状态。日志写入用一个后台队列批量插入 SQLite，不阻塞转发。

> **为什么不直接用 LiteLLM Proxy**：它能做 Key 和日志，但「Key 属于哪个 Linux UID、模型属于哪个用户、私有模型对别人返回 404」都需要在它外面再包一层，最后不比自己写这 400 行省事。Gateway 保持 OpenAI 兼容，将来真要换也容易。

### 5.4 查看自己的调用

网页「我的调用」或 `llmlab logs --mine` → BFF 查 `call_logs WHERE caller_uid = 当前用户`，支持按时间、模型、状态筛选和分页导出。模型所有者可以额外看到「别人调用我的模型」的汇总（只有次数和 Token 数，不含调用者明细之外的内容）。管理员可以看全部。

---

## 6. 监控设计

### 6.1 采集

| 数据 | 来源 | 采集方式 |
|---|---|---|
| GPU 利用率、显存、温度、功耗 | **DCGM exporter**（或 nvidia_gpu_exporter） | Prometheus 抓取 |
| CPU、内存、磁盘、网络 | **node_exporter** | Prometheus 抓取 |
| 每个用户占用的显存 | 自建小采集器：NVML 列出 GPU 进程 PID → `/proc/<pid>/status` 取 UID → 汇总 | 暴露 `lab_gpu_memory_bytes{uid}`，Prometheus 抓取 |
| 引擎指标（排队、吞吐、KV cache） | 各引擎自己的 `/metrics` | Prometheus 用 `file_sd` 抓取，目标由 BFF 根据 `models` 表生成 |
| 调用量、延迟、错误率 | Gateway 的 `/metrics` + `call_logs` 表 | 趋势图查 Prometheus，明细查 SQLite |

### 6.2 按用户归属

BFF 在模型登记、删除时重写 `/etc/prometheus/file_sd/lab-engines.json`，给每个目标打上 `owner_uid`、`model` 标签。Prometheus 每 30 秒重新读取，不需要重启。

### 6.3 谁能看什么

| 用户 | 看到的内容 | 实现方式 |
|---|---|---|
| 普通用户 | 整机 GPU 和主机概况（只读）+ **自己的**引擎、显存、调用 | BFF 代查 Prometheus，**强制追加 `owner_uid="<当前 UID>"` 标签过滤**；前端拿不到 Prometheus 地址 |
| 管理员 | 全部数据 + Grafana | Grafana 走 Caddy 的 auth 转发，只放行 `is_admin=1` 的会话 |

数据没有来源或已过期时显示「未接入 / 已过期」，**不显示 0**（沿用现有前端的诚实状态设计）。

---

## 7. 安全要点（单机多用户特有）

1. **回环地址不等于隔离。** 同一台机器上的任何用户都能访问 `127.0.0.1:端口`，所以：
   - 每个引擎必须设置自己的 API 密钥（vLLM `--api-key`，Strata 已有 `STRATA_API_KEY`），只有 Gateway 和所有者知道；
   - 更强的做法是引擎监听 unix socket，权限 `0660`、属组 `lab-gateway`。
2. **引擎密钥存放。** 登记时提交的引擎密钥由 BFF 写入 `/var/lib/llmlab/secrets/<model_id>`（权限 `0600`，属主是运行 Gateway 的服务账户）；数据库只存引用路径。
3. **防冒领端口。** 登记时校验监听进程的 UID = 登记人（§5.1）。
4. **Key 只存哈希。** 明文只显示一次；日志、审计、截图中都不出现明文。
5. **调用日志不存正文。** 只存元数据；用户自己有需要再在应用层保存。
6. **所有服务只监听 127.0.0.1。** 对外只有 Caddy `:443`（Tailnet 内），浏览器不能直连 `:8080` / `:9000` / `:9090` / `:3000`。
7. **管理员 ≠ root。** Portal 管理员只能看数据、停用 Key 或模型、停用用户，不能在机器上执行命令。

---

## 8. EvalScope 集成

- 用户在「评测」页选择模型和数据集 → BFF 生成一个**仅绑定该模型、有效期 24 小时**的临时 Key → 把评测任务放进队列。
- 一个 Runner 进程**一次只跑一个任务**（单卡，避免和在线推理抢显存），EvalScope 通过 **Gateway** 调用（所以评测调用也会出现在调用日志里）。
- 报告存放在 `/var/lib/llmlab/reports/<uid>/<job_id>/`，只有发起人和管理员能看。
- 旧的 Benchmark 报告以「历史报告」只读列表展示，与新报告分开。

---

## 9. 部署拓扑（单机）

| 进程 | 运行身份 | 监听 | 托管 |
|---|---|---|---|
| Caddy | caddy | `:443`（Tailnet） | systemd |
| Portal 后端（BFF） | `llmlab` 服务账户 | `127.0.0.1:8400` | systemd |
| Gateway | `llmlab-gw` 服务账户 | `127.0.0.1:8300` | systemd |
| PAM Broker | root（最小化，仅 unix socket） | `/run/llmlab/pam.sock` | systemd |
| Prometheus / Grafana / exporters | 各自服务账户 | `127.0.0.1:9090` / `:3000` / exporter 端口 | systemd |
| EvalScope Runner | `llmlab-eval` | 无对外端口 | systemd |
| Strata（共享模型） | 管理员维护 | `127.0.0.1:8080` | 现有方式，不改 |
| 用户引擎 | 各用户自己 | `127.0.0.1:<白名单端口段，如 8600–8699>` | 用户自己（可用 `systemd --user`） |

Caddy 路由：`/v1/*` → Gateway；`/grafana/*` → Grafana（仅管理员）；其余 → BFF。

数据目录：`/var/lib/llmlab/{lab.db, secrets/, reports/}`。备份时只需备份这一个目录。

---

## 10. 与现有代码的对应关系

| 现有模块 | V1 处理 |
|---|---|
| `backend/auth`、`backend/auth_linux`（PAM、会话、scoped token） | **保留**。scoped token 作为 CLI 的「平台 Token」；新增 `api_keys` 作为调用 Key |
| `backend/registry`（engines/models/deployments/endpoints/grants/routing ACK） | **简化**：合并成一张 `models` 表 + 可见性；`deployments`、`endpoint_routings`（Router ACK）、`repoint/rollback` 推迟到 V2 |
| `backend/registry/gateway.py`（只读路由配置 + adapter） | **由新 Gateway 进程替代**。新 Gateway 直接读 SQLite，不再需要「配置契约 + ACK」两步 |
| `backend/atlas`（调用日志契约 `atlas-calls/1`） | **保留契约**，数据源改为 `call_logs` 表 |
| `backend/observability`（来源白名单、新鲜度） | **保留**，加上 BFF 代查 Prometheus 时的强制用户标签 |
| `backend/evaluation` | **保留**报告目录与 ACL；Runner 按 §8 接入 |
| `backend/bff` 路由表 | **保留**，按 §2.1 补齐 Key 管理和调用日志路由 |
| 前端 9 个页面（PR #62） | **保留**。API 管理页的「签发 / 撤销」在后端路由就绪后启用；图谱、评测排在后面 |

---

## 11. 里程碑（按依赖顺序，每一步都在真机上验收）

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| **M1 只读监控** | Prometheus + DCGM + node_exporter + 每用户显存采集器；BFF 代查；概览和算力页接入真实数据 | 页面显示真实 GPU 数据，过期能正确显示「已过期」 |
| **M2 登录 + 模型登记** | PAM 登录；`models` 表；登记校验（端口属主、健康检查）；file_sd 生成 | 用户 A 登记自己的引擎，用户 B 看不到 A 的私有模型 |
| **M3 Key + Gateway** | `api_keys`；签发 / 撤销；薄 Gateway；`call_logs` | **V1 闭环**：A 签发 Key → 调用成功 → 日志记在 A 名下 → 撤销 → 下一次调用 401 |
| **M4 公共模型** | `public` 可见性；Strata 登记为公共模型 | B 的 Key 能调 Strata；A 把私有模型设为 public 后 B 能调，改回 private 后立即 404 |
| **M5 评测** | 队列 + Runner + 报告 ACL | 评测调用出现在日志中；报告只有发起人能看 |
| **M6 管理员与上线** | Grafana（仅管理员）、审计查询、备份、回滚手册 | 旧 Portal 功能等价后切换 |

**交付方式建议**：每个阶段一个小 PR，**当天合并到 main**；停止在长期草稿分支上层层叠加（目前约 100 个 PR 全部是 Draft，没有一个合并）。

---

## 12. 仍需确认的问题

1. **用户引擎的端口段**：是否就用 `8600–8699`？要不要强制用 unix socket？
2. **Key 默认有效期**：90 天是否合适？是否允许「永不过期」？
3. **模型所有者能否看到「谁调用了我的公共模型」**：只看汇总，还是能看到调用者？
4. **管理员名单**：是 Linux 用户组（如 `llmlab-admin`），还是在 Portal 里单独维护？（建议用 Linux 组，最简单）
5. **调用日志保留多久**：建议 180 天，超过的按月归档成 CSV。
