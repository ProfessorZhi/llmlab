# 资源自动接入、启动与服务共享 — 实现规格

> 状态：**实现规格 v2（2026-10-23）**。取代本文件的 v1 讨论稿。  
> 需求：[私有资源、共享服务 — 产品需求](product-shared-model-pool-and-on-demand-serving-2026-10-23.md)（决策 P1–P14）  
> 读者：实现者（本地 Claude Code / 其他编程 agent）与审核者。  
> 写法约定：**必须** = 不做就不通过审核；**禁止** = 做了就不通过审核；其余为建议。

---

## 0. 一页看懂

**用户体验目标：用户下载一个模型，几秒后它就出现在平台「我的模型」里；在网页或命令行里选一个引擎、点启动，平台给出推荐参数并显示预计显存；服务起来后自动接入监控、日志和网关；想共享就开一个开关；想调参就改完点「应用并重启」。用户不需要登记、不需要配密钥、不需要记端口。**

系统由三个部分组成，职责互不重叠：

```
┌──────────── ① 平台守护进程 llmlabd（全局，系统服务，User=llmlab，非 root）────────────┐
│ 账本(SQLite) · 服务自动接入 · 网关 /v1 · 共享 · API Key · 日志 · 监控 · GPU 看板 · 推荐参数 │
└──────▲─────────────────────────────────▲──────────────────────────────▲──────────────────┘
       │ /run/llmlab/agent.sock           │ 127.0.0.1:<port>              │ HTTPS（会话 / 令牌）
       │ （SO_PEERCRED：内核给出对端 uid） │ （健康检查、转发调用）         │
 ② llmlab-agent（每用户一份，          各用户的推理服务                ③ llmlab CLI + skill
    systemd --user，以该用户身份）                                      （人、Claude Code、其他 agent）
    发现文件 · 读引擎参数 · 起/停/重启本人服务
```

| 部分 | 必须做 | 禁止做 |
| --- | --- | --- |
| ① 守护进程 | 存账本；发现**运行中的服务**（端口 + uid + 探测）；网关转发；共享；Key；日志；监控；推荐参数；给代理派发任务 | 读用户私人目录；以用户身份执行任何东西 |
| ② 每用户代理 | 发现**文件**（模型、引擎 build）；读模型元数据与引擎参数；把进程对应到模型；执行本人确认过的起 / 停 / 重启 | 执行任意命令；操作别人的文件或进程；调用 LLM |
| ③ CLI + skill | 提供人和 agent 的全部操作入口；本机上通过 socket 自动取得身份 | 绕过守护进程直接改账本 |

**AI agent（Claude Code 等）是用户的手**：通过 CLI 以用户身份办事，不属于平台，平台不信任它的判断，只执行 CLI 的确定性命令。

---

## 1. 已定决策（实现必须遵守）

| # | 决策 |
| --- | --- |
| D1 | 只共享**服务调用**；模型文件、引擎 build 不共享 |
| D2 | 用户的服务**只能以用户本人身份运行**：用户自己起，或经网页 / CLI 由**本人代理**起。管理员也不能以别人身份起服务 |
| D3 | 管理员可起**平台托管服务**（User=llmlab，模型在平台区） |
| D4 | 服务默认私有；主人开关共享；管理员可改任何服务并设实验室默认策略 |
| D5 | API Key 归调用者；每人首次需要时自动生成一把**默认 Key**；日志按 Key |
| D6 | 存储位置由用户决定；平台只发现、识别移动、给建议；**禁止**平台移动或删除用户文件 |
| D7 | 发现是**确定性程序**；**禁止**用 LLM 扫描或写账本 |
| D8 | root 只用于**一次性安装**（§2）；管理员已确认全局启用代理 |
| D9 | 本机身份走 `SO_PEERCRED`，**不发密码、不存长期令牌** |
| D10 | `llmlab serve` / 平台启动的服务只监听 127.0.0.1、**不设引擎密钥**；鉴权全在网关 |

---

## 2. 一次性安装（管理员 sudo 执行一次）

`deploy/install/llmlab-install.sh` **只打印计划，不执行**（`--apply` 被拒绝，退出码 3）。
理由见 §10.2：`docs/access-boundaries.md` §6 明确本仓库不创建 systemd 单元、不 enable、
不注册定时任务。管理员照打印出来的清单逐条手工执行，并留存记录。

清单覆盖：

1. `/etc/tmpfiles.d/llmlab.conf`：`/run/llmlab`（daemon 用户所有，0755），
   让 BFF 有地方 bind socket。
2. 全局用户级服务 `/etc/systemd/user/llmlab-agent.service`（**没有** `User=`
   —— 用户级单元本来就以该用户自己身份运行，这正是 `SO_PEERCRED` 要的事实）。
3. **逐用户**`loginctl enable-linger <user>` 与 `systemctl --user enable llmlab-agent`：
   一人一次，由该用户本人或管理员执行；**不循环遍历名单、不代任何人 enable**。
   「谁被看见」是那个人的选择。
4. 验收命令（`llmlab agent --dry-run` / `--once` / `llmlab discover`）。

系统服务 `llmlabd.service`（`User=llmlab`，替代当前以其他身份运行的 BFF）与平台区目录
（`/data/models/platform` 等）属于发布动作，不在本脚本的计划里——它们要停机、要搬状态，
必须走发布与回滚审批。

**禁止**：改任何用户 HOME 的权限；加 sudoers 规则；改 Strata / Caddy / Tailscale。
卸载：`llmlab-install.sh --uninstall` 打印逆序清单（先停用户单元，再撤 linger，
再删单元与 tmpfiles 条目）。

---

## 3. 数据模型（在现有 registry SQLite 上迁移，不另建库）

```sql
-- 现有 models 表新增列
ALTER TABLE models ADD COLUMN fingerprint TEXT;          -- 见 §5.3
ALTER TABLE models ADD COLUMN kind TEXT;                  -- gguf | hf | diffusers
ALTER TABLE models ADD COLUMN task TEXT;                  -- chat | vision | decision | embedding | rerank | image | video | unknown
ALTER TABLE models ADD COLUMN facts_json TEXT;            -- 见 §5.4（上下文上限、层数、KV 头数…）
ALTER TABLE models ADD COLUMN size_bytes INTEGER;
ALTER TABLE models ADD COLUMN source TEXT DEFAULT 'manual';  -- manual | agent | platform

CREATE TABLE model_locations (
  model_id TEXT NOT NULL REFERENCES models(id),
  path TEXT NOT NULL, uid INTEGER NOT NULL, device INTEGER, inode INTEGER, mtime REAL,
  tier TEXT,                 -- ssd | hdd（按挂载点判定）
  state TEXT NOT NULL,       -- present | missing
  last_seen_at TEXT NOT NULL,
  PRIMARY KEY (model_id, path)
);

CREATE TABLE runtime_builds (
  id TEXT PRIMARY KEY, owner_principal TEXT NOT NULL, owner_uid INTEGER NOT NULL,
  framework TEXT NOT NULL,   -- llama.cpp | strata | vllm | sglang | ollama | …
  binary_path TEXT NOT NULL, version TEXT, git_commit TEXT, fingerprint TEXT NOT NULL,
  flags_json TEXT,           -- 见 §5.5：该 build 实际支持的参数
  formats_json TEXT,         -- 能加载的模型格式：["gguf"] / ["hf"] …
  state TEXT NOT NULL, last_seen_at TEXT NOT NULL
);

-- 现有 engines 表（=运行中的服务实例）新增列
ALTER TABLE engines ADD COLUMN source TEXT DEFAULT 'manual';   -- manual | auto | agent | platform
ALTER TABLE engines ADD COLUMN state TEXT;                     -- seen | healthy | attached | offline
ALTER TABLE engines ADD COLUMN last_seen_at TEXT;
ALTER TABLE engines ADD COLUMN pid INTEGER;
ALTER TABLE engines ADD COLUMN unit TEXT;                      -- systemd 单元名（代理起的）
ALTER TABLE engines ADD COLUMN model_id TEXT;
ALTER TABLE engines ADD COLUMN runtime_build_id TEXT;
ALTER TABLE engines ADD COLUMN launch_json TEXT;               -- 启动时的参数快照

CREATE TABLE agent_tasks (
  id TEXT PRIMARY KEY, uid INTEGER NOT NULL, kind TEXT NOT NULL,   -- start | stop | restart | sync
  payload_json TEXT NOT NULL, requested_by TEXT NOT NULL, created_at TEXT NOT NULL,
  state TEXT NOT NULL,       -- queued | running | done | failed
  result_json TEXT, updated_at TEXT
);

CREATE TABLE lab_settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_by TEXT, updated_at TEXT);
-- default_visibility = private | all（初始 private）
```

---

## 4. ① 守护进程

### 4.1 服务自动接入

每 30 s：

1. 读 `/proc/net/tcp{,6}`，取 127.0.0.1 / ::1 上 LISTEN 的端口及 uid（复用 `ProcNetOwnershipProver`）。
2. uid 不在「平台账户 ↔ Linux 用户」映射里 ⇒ 忽略。
3. 对新端口做只读探测（复用 `local_discovery` 的 `/props`、`/get_server_info`、`/v1/models`），超时 1.5 s。
4. 状态机：

| 当前 | 条件 | 下一状态 | 动作 |
| --- | --- | --- | --- |
| — | 端口出现 | `seen` | 记录 |
| `seen` | 探测成功、识别为 OpenAI 兼容 | `attached` | 归到 uid 对应账户；按 §4.2 决定是否挂路由；开始监控 |
| `seen` | 探测连续 3 次失败 | 丢弃 | 不入账本（不是推理服务） |
| `attached` | 连续 2 轮不见 | `offline` | 路由保留，调用返回 503 `service_offline` |
| `offline` | 同端口同 uid 再出现且模型 id 相同 | `attached` | 路由恢复 |

代理若上报了该端口的 pid / 模型路径 / build（§5.6），补全 `pid`、`model_id`、`runtime_build_id`。

**验收**：起一个假引擎（测试里的 `FakeEngine`）⇒ 30 s 内出现在对应账户「我的服务」；停掉 ⇒ 60 s 内 `offline`，调用得 503。

### 4.2 共享与路由

- `lab_settings.default_visibility` 决定新接入服务的默认可见性；主人显式设置优先。
- `all` ⇒ 自动建网关路由；路由名 = 探测到的模型 id 规范化（`[A-Za-z0-9._:/-]`），冲突时追加 `@<用户名>`。
- 管理员修改默认值**只影响之后接入的服务**；控制台提供「同时应用到现有服务」的显式按钮。

### 4.3 默认 Key

- 用户第一次调用 `GET /api/gateway/keys/default`（或 CLI `llmlab keys default`）时，若无名为 `default` 的有效 Key，则创建一把（无模型限制、默认限速、无过期），返回明文**一次**，并在本机通过 §6.2 写入 `~/.config/llmlab/default_key`（0600）。之后 CLI 读这个文件。
- 用户可轮换：`llmlab keys rotate default`。

### 4.4 推荐参数（§5.4 + §5.5 的输出）

`POST /api/serve/plan`，入参 `{model_id, runtime_build_id, overrides?}`，返回：

```json
{"engine": "llama.cpp@a1b2c3",
 "params": {"ctx_size": 32768, "parallel": 4, "n_gpu_layers": 999, "flash_attn": true, "cache_type_k": "q8_0"},
 "why": {"ctx_size": "模型上限 131072；按可用显存 9.8 GB 与 4 路并发取 32768",
         "cache_type_k": "f16 时预计超出可用显存 1.2 GB"},
 "estimate": {"weights_gib": 4.2, "kv_cache_gib": 2.1, "overhead_gib": 0.8, "total_gib": 7.1,
              "gpu_free_gib": 9.8, "fits": true, "confidence": "estimate"},
 "command": "llama-server -m … --ctx-size 32768 --parallel 4 … --host 127.0.0.1 --port 8013",
 "warnings": ["模型在 HDD 上：冷启动预计慢约 5 倍，建议 --stage"]}
```

显存估算（llama.cpp / Strata）：

```
weights   = 模型文件大小（GGUF）
head_dim  = embedding_length / head_count
kv_bytes  = 2 × block_count × head_count_kv × head_dim × ctx_size × bytes_per_elem(cache_type)
            # f16 = 2，q8_0 ≈ 1.0625，q4_0 ≈ 0.5625
overhead  = 0.6 GiB + 0.05 × weights      # 计算缓冲，经验值，需用实测校准
```

vLLM / SGLang 预占显存比例，估算改为 `gpu_memory_utilization × 总显存`，并据此推出最大 `max_model_len`。
**必须**把结果标为估算（`confidence: "estimate"`），服务起来后用实测值（现有 `observe()` + NVML 进程显存）覆盖显示。Strata 的专家流式加载等特殊情况由引擎适配器覆写估算函数。

推荐规则（按顺序）：`ctx_size ≤ 模型上下文上限`；在 `fits` 前提下优先保 `parallel = 4`，再取最大 2 的幂 `ctx_size`；不够时依次尝试 `cache_type_k=q8_0` → `q4_0` → `parallel` 减半；仍不够 ⇒ `fits:false` 并在 `warnings` 里给出占用者与「申请借用」。

### 4.5 启动 / 重启（派发给本人代理）

- `POST /api/serve/start` `{model_id, runtime_build_id, params, port?, share?}`：
  - **必须**校验：调用者 = 模型与 build 的主人（或 build 在平台区）；参数通过 `runtime_params.validate`。
  - 写 `agent_tasks(kind=start, uid=主人 uid)`，返回 `task_id`。
- `POST /api/serve/{engine_id}/restart` `{params}`、`/stop`：同上，只允许主人；管理员仅允许 `stop`（GPU 仲裁，写审计，§4.7）。
- `GET /api/agent/tasks/{id}`：状态与结果。任务 5 分钟内无代理领取 ⇒ `failed: agent_offline`，界面提示「你的代理未运行」。
- 现有「运行参数」页的「保存目标参数」改为「**应用并重启**」（主人）——原来只生成命令，现在真的能执行。

### 4.6 新增路由（BFF route_map 同步更新镜像与测试）

| 方法 | 路径 | 权限 |
| --- | --- | --- |
| GET | `/api/models/{id}/facts` | 主人 / 管理员 |
| GET | `/api/runtimes` · `/api/runtimes/{id}` | 主人 / 管理员；平台区 build 全员 |
| POST | `/api/serve/plan` | 登录用户（只能用自己的模型和 build） |
| POST | `/api/serve/start` · `/api/serve/{id}/restart` · `/api/serve/{id}/stop` | 主人（stop 另允许管理员） |
| GET | `/api/agent/tasks/{id}` · `/api/agent/status` | 本人 / 管理员 |
| GET / POST | `/api/gateway/keys/default` | 本人 |
| GET / POST | `/api/lab/settings` | GET 全员，POST 管理员 |

### 4.7 GPU 看板与仲裁

- NVML 每进程显存 → pid → `/proc/<pid>/status` 取 uid → 账户；pid → 服务由代理映射给出。
- 管理员 `stop` 别人的服务：写 `agent_tasks(kind=stop)` 给**占用者的代理**执行，审计记录操作者与理由；用户界面事先告知规则。

---

## 5. ② 每用户代理 `llmlab-agent`

Python 标准库；常驻内存目标 < 30 MB；`nice 19`、`ionice -c3`。

### 5.1 与守护进程通信

- 连接 `/run/llmlab/agent.sock`，每行一个 JSON。守护进程用 `SO_PEERCRED` 取 uid，**未映射 uid 立即断开**。
- 消息：`hello`（版本、能力）→ `report`（§5.6）→ 轮询 `tasks`（每 5 s，或守护进程推送）→ `task_result`。
- 守护进程不可达时：指数退避重连；本地保留最近一次扫描结果，恢复后一次性上报。

### 5.2 扫描范围与规则

- 根：`~`、`/data/home/<user>`、`~/.cache/huggingface/hub`，以及 `~/.config/llmlab/roots` 里用户自己加的目录。
- 排除：`.git`、`node_modules`、`.venv`、`venv`、`site-packages`、`__pycache__`、`CMakeFiles`、`third_party/*/models`、`~/.cache`（HF hub 除外）、`~/.local/share/uv`、`~/.cache/pip`，以及 `~/.llmlab-ignore` 中的 glob。
- 模型判定：`*.gguf` ≥ 100 MB（名字匹配 `ggml-vocab-*` 一律跳过）；含 `config.json` 且有 ≥ 100 MB 权重分片的目录；HF hub 的 `snapshots/<rev>`；含 `model_index.json` 的目录（diffusers）。
- 节奏：inotify 监听模型目录（事件合并 5 s）；每小时对账一次（只进入 mtime 变化过的目录）；收到 `sync` 任务立即对账。

### 5.3 指纹与移动

- 文件指纹：`size` + `sha256(前 1 MiB ‖ 后 1 MiB)`；HF 目录：`config.json` 哈希 + 分片名与大小清单。
- 匹配：同 device+inode ⇒ 同盘移动；同 uid 下同指纹、旧位置消失 ⇒ 跨盘移动（模型 id 不变）；同指纹两处都在 ⇒ 复制（两个 location）；不唯一 ⇒ `needs_confirmation`，不自动改指向；旧位置消失且未找到 ⇒ `missing`。
- **禁止**全量哈希；**禁止**跨 uid 合并。

### 5.4 模型元数据（facts）

| 格式 | 读取 | 得到 |
| --- | --- | --- |
| GGUF | 文件头 KV（只读到张量数据前，上限 16 MB） | `general.architecture`、`general.name`、`general.file_type`（量化）、`<arch>.context_length`、`.block_count`、`.embedding_length`、`.attention.head_count`、`.attention.head_count_kv`、`.expert_count`（若有）；同目录 `mmproj*.gguf` ⇒ `vision` |
| HF | `config.json`（≤ 256 KB） | `architectures`、`max_position_embeddings`、`num_hidden_layers`、`hidden_size`、`num_attention_heads`、`num_key_value_heads`、`torch_dtype`、`quantization_config` |
| diffusers | `model_index.json` | `_class_name` ⇒ image / video |

task 推断：`*ForCausalLM` ⇒ chat；`*VLForConditionalGeneration` / 有 mmproj ⇒ vision；`*ForSequenceClassification` ⇒ decision；sentence-transformers ⇒ embedding；diffusers ⇒ image / video；其他 ⇒ `unknown`（主人可改）。**禁止**反序列化 `.pt` / pickle。

### 5.5 引擎 build 与参数发现

- 候选：可执行文件名 ∈ {`llama-server`, `strata`}，或 venv / conda 环境里的 `bin/vllm`、`bin/sglang`（`python -m sglang.launch_server` 以包存在判定），`ollama`。
- 版本：`<binary> --version`，超时 5 s，结果按（inode, mtime）缓存。
- commit：向上找 `.git/HEAD` 并解析 ref（不调用 git）；CMake 构建读 `CMakeCache.txt` 的关键开关（如 `GGML_CUDA`）。
- **参数发现**：`<binary> --help`（超时 5 s，缓存），按框架的解析器提取 `--flag`、取值类型与默认值，写入 `flags_json`。与 `backend/runtime/params.py` 的参数表求交：
  - 参数表里有、build 也支持 ⇒ 正常显示；
  - 参数表里有、build 不支持 ⇒ 表单里隐藏并注明「此 build 不支持」；
  - build 支持、参数表里没有 ⇒ 出现在「高级参数」里，以原始 flag 形式可填（类型来自解析结果）。
- `formats_json`：llama.cpp / Strata ⇒ `["gguf"]`；vLLM / SGLang ⇒ `["hf"]`；ollama ⇒ `["ollama"]`。

### 5.6 上报 payload

```json
{"type": "report", "scanned_at": "2026-10-23T08:00:00Z", "full": true,
 "models": [{"path": "/data/home/<user>/decider/decider-4b-v2.1-Q8_0.gguf", "kind": "gguf",
             "fingerprint": "4507123712:9f2c…", "size_bytes": 4507123712, "device": 64768, "inode": 1234,
             "mtime": 1761200000.0, "task": "decision",
             "facts": {"architecture": "qwen3", "context_length": 40960, "block_count": 36,
                       "embedding_length": 2560, "head_count": 32, "head_count_kv": 8, "file_type": "Q8_0"}}],
 "builds": [{"binary_path": "~/gpt-oss-probe/third_party/llama.cpp/build/bin/llama-server",
             "framework": "llama.cpp", "version": "b6512", "git_commit": "a1b2c3d",
             "fingerprint": "…", "flags": {"--ctx-size": {"type": "int", "default": 4096}, "…": {}}}],
 "processes": [{"pid": 4242, "port": 8013, "unit": "llmlab-decider-4b.service",
                "binary_fingerprint": "…", "model_fingerprint": "4507123712:9f2c…"}],
 "skipped": {"vocab_file": 42, "below_min_size": 1310, "excluded_dir": 7}}
```

守护进程**必须**校验：路径中的 uid 与对端 uid 一致；`processes[].port` 在 `/proc/net/tcp` 中确实属于该 uid。`full: true` 时，未出现在本次上报中的 location 标 `missing`。

### 5.7 执行任务

- `start`：按 §4.4 的 `command`（守护进程生成、代理只填路径）执行
  `systemd-run --user --unit llmlab-<slug> --property=Restart=no --property=KillMode=mixed -- <argv>`；
  `--stage` 时先复制到 `~/.cache/llmlab/staged/`（复制 → 校验指纹 → 使用），完成后上报 `pid`、`unit`。
- `stop`：`systemctl --user stop <unit>`；只允许本代理起的单元，或该 uid 的进程（管理员仲裁）。
- `restart`：stop → start（新参数）。
- **禁止**：执行守护进程未生成的命令行；`argv` 只允许已登记 build 的二进制路径；**禁止** shell 拼接（`subprocess` 列表参数）。

---

## 6. ③ CLI 与 skill

### 6.1 输出约定（所有命令）

- `--json`：`{"ok": bool, "data": …, "warnings": [], "next": ["<可直接执行的下一条命令>"], "error": {"code", "message"}?}`
- 退出码：`0` 成功；`1` 其他错误；`2` 参数错误；`3` 未登录 / 无权限；`4` 资源不足（GPU / 磁盘）；`5` 未就绪 / 超时；`6` 代理离线。
- 写操作支持 `--dry-run`；`sync` 支持 `--explain`（逐条给出跳过原因）。
- `llmlab help --json`：全部命令、参数、退出码的机器可读描述（给 agent）。

### 6.2 身份

本机：CLI 连 `/run/llmlab/agent.sock` 取短期令牌（15 分钟，`SO_PEERCRED` 认证），无需登录。异机：`llmlab login` 一次，令牌存 `~/.config/llmlab/token`（0600）。

> **状态（2026-10-26）**：socket **签发令牌**这件事**还没做**。现在已经落地的只有
> `llmlab agent`：它不带令牌，用内核给的 uid 只做一件事——上报本机事实。其它命令
> 仍然只认 Portal 签发的令牌（`docs/registry-cli-contract.md` §9.3）。

### 6.3 命令

| 命令 | 作用 |
| --- | --- |
| `llmlab status` | 我的模型、build、服务、GPU、代理状态、上次同步，以及「待办」（带可执行命令） |
| ~~`llmlab sync [--explain] [--dry-run]`~~ → `llmlab agent [--once \| --watch] [--dry-run]` | 立即扫描并上报（幂等）。**已按此名实现**，理由见 §10.1；`--explain` 未做 |
| `llmlab models [list \| show <m>]` | 列表 / 元数据（facts、位置、SSD/HDD） |
| `llmlab engines [list \| show <b> [--flags]]` | build 列表 / 支持的参数 |
| `llmlab serve <m> [--engine <b>] [--ctx N] [--parallel N] [--set flag=value…] [--port P] [--share lab\|private] [--stage] [--plan]` | `--plan` 只打印推荐参数与显存估算；否则启动并等待就绪，输出模型名与调用示例 |
| `llmlab services [list \| logs <s> \| stop <s> \| restart <s> [--ctx …]]` | 本人服务 |
| `llmlab tune <s> [--ctx N --parallel N …] [--apply]` | 显示实测 vs 目标；`--apply` = 重启生效 |
| `llmlab share <s> --scope lab\|private` | 共享开关 |
| `llmlab gpu` | GPU 看板 |
| `llmlab keys [default \| list \| create \| rotate <k> \| revoke <k>]` | API Key |
| `llmlab calls [--key <k>] [--model <m>]` | 按 Key 的调用日志 |

`engines` / `models` / `endpoints` / `invoke` 等现有命令保持兼容。

### 6.4 skill

`docs/agent/llmlab-cli.md`（可直接作为 Claude Code skill / CLAUDE.md 片段）必须包含：命令表、`--json` 示例、常见任务配方（「把刚下载的模型上线给实验室」「调大上下文」「为什么我的模型没出现」）、硬规则（只操作本人资源；先 `--plan` 再启动；不要手动编辑 `~/.config/llmlab`）。

**示例配方**（agent 执行）：

```bash
llmlab --json sync
llmlab --json models show decider-4b                 # 看 facts 与位置
llmlab --json serve decider-4b --plan                 # 看推荐参数与显存
llmlab --json serve decider-4b --share lab            # 启动、等就绪、挂网关
llmlab --json keys default                            # 拿调用用的 Key
```

---

## 7. 控制台变化

- 「模型库 → 我的」：自动出现的模型带「自动发现」标签、facts 摘要（架构 / 量化 / 上下文上限 / 大小 / SSD 或 HDD）。
- 模型详情「启动」按钮 → 启动面板：选 build（只列格式兼容的）→ 推荐参数（可改，含「高级参数」）→ 显存估算条（权重 / KV / 其他 / 可用）→ 共享开关 → 启动；之后显示任务进度并跳到服务详情。
- 服务详情「运行参数」：实测 vs 目标，「应用并重启」（主人）。
- 顶部提示：代理离线（「你的代理未运行，自动发现已暂停」）、上次同步时间。
- 管理员「用户与权限」：每人的代理状态与上次同步；实验室默认共享策略开关。

---

## 8. 实施切片

每片一个 PR，含测试；不改 Strata / Caddy / Tailscale / 生产入口。

| 片 | 内容 | 验收（必须有自动化测试） |
| --- | --- | --- |
| **S1** 发现规则 + facts + 指纹 | 纯函数：§5.2 规则、§5.3 指纹与匹配、§5.4 GGUF/HF 读取、§5.5 `--help` 解析器（llama.cpp、vLLM 各一份样本） | 合成 fixture（仿 kaidong 目录：4 个真模型 + 词表 + 6 棵源码树）⇒ 恰好 4 模型 / 2 build；移动、复制、歧义、缺失各一例；GGUF facts 与样本一致 |
| **S2** 服务自动接入 + 默认策略 + 默认 Key | §4.1、§4.2、§4.3；`lab_settings`；控制台开关 | 假引擎 30 s 内接入；默认私有他人调不到；切 `all` 后新服务可调；`offline` 时 503 |
| **S3** 代理 + socket + 上报 | ✅ **已实现**：`llmlab agent`、`SO_PEERCRED`、§5.6 校验、安装计划打印器。`sync` 更名为 `agent --once` / `--watch`（§10.1） | 未映射 uid 被拒；伪造他人路径 / 端口被拒；`full` 上报标记 missing |
| **S4** 推荐参数 + 启动 / 重启 | §4.4、§4.5、§5.7；CLI `serve` / `services` / `tune`；控制台启动面板与「应用并重启」 | `--plan` 数值与公式一致；假引擎经代理启动、就绪、接入、挂路由；重启后实测值更新；非主人启动被拒 |
| **S5** CLI 完整化 + skill | §6 全部命令、`help --json`、`docs/agent/llmlab-cli.md` | 每个命令的 `--json` 结构有快照测试；配方在测试环境端到端跑通 |
| **S6** GPU 看板与仲裁 | §4.7 | 与 NVML 进程表一致；管理员 stop 写审计 |

## 9. 明确不做

共享模型文件 / 发布库；checkpoint 版本树；LLM 参与发现；平台以别人身份执行；平台移动或删除用户文件；外部 HTTPS 入口（单独审批）。

---

## 10. 状态：S3 的落地方式（2026-10-26）

> 本节记录 S3 **实际交付了什么、和规格哪里不一样、为什么**。§10.1–10.4 写的是
> **2026-10-26 白天、部署之前**的状态（`NOT DEPLOYED`：没装过单元、没 enable 过
> 任何人、没改过 BFF 的运行方式）；当天晚些时候通道按 §10.5 落地，落地记录与
> 验收数据见 §10.5.5。

### 10.1 与规格的差异

| 规格 | 落地 | 为什么 |
|---|---|---|
| `sync` 命令 | `llmlab agent --once` / `--watch` | 这条命令报的是**本机现状**（不是「把账本同步下来」），叫 `sync` 会让人以为它会拉东西回去。它只发不收。 |
| 报告里可以带 `uid` / `owner` 等 | **禁止**，带了整条丢弃（`identity_field_present`） | 归属只能有一个来源：内核 + `/proc/net/tcp`。允许自报字段等于开一条伪造的路。 |
| `loginctl enable-linger` 对名单里每个用户 | 只**打印**命令，一人一次 | 见 §10.2。 |
| 安装脚本先 `--dry-run` 再执行 | 只有打印；`--apply` 拒绝（退出码 3） | 见 §10.2。 |

`host_id` 也改了来源：规格让代理报，落地由服务端从主体名 `linux:<host>:<uid>` 推出来。
否则同一台机器上不同的人上报会被算成两台主机。

### 10.2 安装脚本只打印

`docs/access-boundaries.md` §6 是本仓库的硬约束：「不创建 systemd 单元、不注册定时
任务」。安装要动的正是这些，所以脚本刻意**没有**执行路径。它打印的内容包括
`/etc/tmpfiles.d/llmlab.conf`、`/etc/systemd/user/llmlab-agent.service` 的完整内容、
逐用户的 enable 命令与验收命令；`--apply` 退出 3 并指向批准流程。

`tests/security/test_install_plan_is_print_only.py` 把 `sudo` / `systemctl` /
`loginctl` / `systemd-tmpfiles` / `rm` / `mkdir` 全换成会写标记的命令再跑一遍脚本，
断言标记从未出现——这条用例防的是「某天有人悄悄加了一句 `systemctl enable`」。

### 10.3 交付物

| 层 | 文件 |
|---|---|
| 通道 | `backend/registry/agent_channel.py`（AF_UNIX、`SO_PEERCRED`、上限、状态） |
| 校验/入库 | `backend/registry/agent_reports.py`、`RegistryService.report_agent`、schema v5 的 `agent_reports` 表 |
| 服务接线 | `backend/bff/devserver.py`（`--agent-socket` / `--no-agent-socket`） |
| CLI | `cli/llmlab/agent.py`、`llmlab agent`（不解析令牌） |
| 控制台 | `web/console/js/pages.js` 的 `flattenAgentReports` / `agentReportsPanel` |
| 装机 | `deploy/install/llmlab-install.sh`（打印器） |
| 文档 | `docs/registry-cli-contract.md` §22 |

**自动化证据**（全部 CPU-only、离线）：

| 测试 | 条数 | 钉住的事 |
|---|---|---|
| `tests/registry/test_agent_channel.py` | 18 | 真 socket：握手、上限、错误透传、清理；两条用真 `SO_PEERCRED` |
| `tests/registry/test_agent_reports.py` | 40 | 每条拒绝理由、服务端写归属、覆盖不追加、审计、可见性、v4→v5 迁移 |
| `tests/registry/test_agent_cli.py` | 10 | 真子进程、清空 `LLMLAB_TOKEN*` 后仍能上报 |
| `tests/web-features/agent-reports.test.js` | 14 | 「可登记 / 端口不在允许列表 / 已登记 / 已过期 / 不完整」不互相顶替 |
| `tests/security/test_install_plan_is_print_only.py` | 7 | 装机脚本一次系统命令都不调用 |
| `tests/bff/test_account_directory_by_uid.py` | 4 | `by_uid` 回归：空表会把所有人拒在门外 |

### 10.4 没做、也不声称

* **没有装**（*部署之前*的状态，已由 §10.5 落地）：`/run/llmlab` 当时不存在，
  `/discovery` 是 `agent_socket_dir_missing`，通道没开。
* **没有多用户证据**（*部署之前*）：跨 UID 的拒绝当时只有合成测试，没在真实多用户
  主机上跑过；§10.5.5 是第一次真实多账户运行。
* **S4 之后**（推荐参数、起停任务、`/api/agent/tasks`）都还没做；代理现在**只上报，
  不接受任务**。§5.7 的「代用户起服务」没有实现。

## 10.5 2026-10-26：管理员代跑 + 发布到 `/opt/llmlab`

用户的要求很直接：不要等 `kaidong` 自己 `enable` 一次代理，管理员用 root **自动**
把每个账户的现状送进平台。README 与 §10.1 里的「谁被看见是那个人的选择」因此被
**有意偏离**了一次。记录决定、代价与回头路。

### 10.5.1 为什么不能「让 BFF 自己扫」

BFF 是以一个人的账户跑的进程。它去 `stat` 另一个人的家目录只会拿到 `EACCES`
（`0700`），而提权去读别人的家目录正是 `docs/access-boundaries.md` 禁止的事。
让它报「没有模型」也不对——那不是「空」，是「看不见」，两者在页面上长得一样。

### 10.5.2 决定：root 只决定轮次，扫描仍以本人 uid 运行

`deploy/inventory/host-inventory.sh` 对每个账户执行
`runuser -u <name> -- … -m cli.llmlab agent`。内核在 `connect()` 时把**那个人的
真实 uid** 填进 `SO_PEERCRED`，服务端的 `PrincipalDirectory.by_uid` 与
`agent_reports` 的归属校验**一行都没改**。协议、校验、存储全部沿用 §22；本决定
唯一新增的是「谁按下开始」。

| 选项 | 代价 | 结论 |
|---|---|---|
| 让每个人自己 `enable`（§22.8） | 没登录过的人永远不出现；用户要先读装机清单 | 保留，仍然是首选路径 |
| BFF 提权读别人的家目录 | 破坏进程边界；一个被攻陷的 BFF 就是全机读取 | **否决** |
| root 代跑，扫描以本人 uid 运行 | 默认扫所有人 | **采纳**，加 `~/.config/llmlab/no-inventory` 退出 |

**代价，明说**：

* 默认「被看见」。退出靠用户自己在 `~/.config/llmlab/no-inventory` 放一个文件——
  这是他的文件，不需要谁替他决定；但**默认值**变了，这一点必须写在文档里，而不是
  只在代码注释里。
* root 有了一个「以任意用户身份跑固定命令」的入口。它的 argv 完全由脚本拼出
  （`env -i` 清环境、`PATH` 固定、代码目录只读），但它是 root 权限的执行路径：
  改这个脚本等于改全机。所以它和 BFF 代码一样归入发布流程，不从开发工作树直接跑。

### 10.5.3 前置条件：代码必须离开 `0700` 的家目录

实测发现现有清单漏了一步：仓库通常躺在**某个人的家目录里**（`0700`/`0750`），
**除他以外任何账户 `import cli` 都会 `ModuleNotFoundError`**，按 §22.8 装好的单元会
以启动失败告终。
所以代跑要求先把代码发布到任意账户可读的位置：

```
rsync -a --delete <repo>/ /opt/llmlab/
chown -R root:root /opt/llmlab
chmod -R a+rX,go-w /opt/llmlab
```

脚本在扫第一个人之前自己先验这一条（`runuser -u <首个非 root 账户> -- test -r`），
不通过就打印上面三行并退出 `2`——不让每个账户各撞一次同样的错。

### 10.5.4 风险与回滚

| 风险 | 处理 |
|---|---|
| 定时器反复失败没人看 | oneshot 单元 + `TimeoutStartSec=15min`；脚本对通道不通（退出码 7）整轮中止并打印检查清单 |
| 代码副本与仓库不一致 | `/opt/llmlab` 只是发布副本，随时可重新 `rsync`；回滚就是 `rm -rf /opt/llmlab` |
| 有人不想被看见 | `~/.config/llmlab/no-inventory`（脚本输出里会记一行「跳过」） |
| 加了新账户却不知道它会跑 | 没有硬编码名单：脚本遍历 `getent passwd` 里 `uid ∈ [1000, 65534)` 且家目录存在的账户 |

回滚（任取其一即停）：`systemctl disable --now llmlabd-inventory.timer`；
删 `/etc/systemd/system/llmlabd-inventory.{service,timer}` + `daemon-reload`；
`rm -rf /opt/llmlab`。三条都不影响已经上报的数据——`agent_reports` 是「发现」，
不是「登记」；BFF 重启会自己 `unlink` socket。

### 10.5.5 落地记录（2026-10-26 14:10 +08:00）

按批准的顺序执行，五步都成功：

1. **发布**：`rsync -a --delete`（排除 `.git/` 与 `__pycache__/`）→ `/opt/llmlab`，
   `chown -R root:root`、`chmod -R a+rX,go-w`。验收：另一个非 root 账户
   `import cli.llmlab.agent` 成功。发布副本约 30 MB，不含任何 `.db`/`.env`/`.key`。
2. **通道目录**：`/etc/tmpfiles.d/llmlab.conf` 写 `d /run/llmlab 0755 <BFF 账户> -`，
   `systemd-tmpfiles --create` 生效。
3. **重启 BFF**（用户级单元，带 `--state-dir/--database/--operator-policy` 等原有参数），
   日志出现 `agent socket /run/llmlab/agent.sock (SO_PEERCRED)`。
4. **手工代跑一轮**：`sudo /opt/llmlab/deploy/inventory/host-inventory.sh --once` → 6 个
   账户全部上报，0 跳过 / 0 无身份 / 0 失败。
5. **定时器**：`llmlabd-inventory.service`（oneshot）+ `.timer`（`OnBootSec=2min`、
   `OnUnitActiveSec=5min`）写入 `/etc/systemd/system/`，`enable --now`；第一次由
   timer 触发的运行 4.5 秒完成，退出 0。

**验收数据**（`/discovery` 的 `data` 块与 `agent_reports` 表一致）：

| 项 | 值 |
|---|---|
| `agent_channel.listening` | `true`（`/run/llmlab/agent.sock`，0666） |
| `agent_channel.connections` / `reports` | 第一轮之后 `6` / `6`（自 BFF 启动起累计，每轮 +6；表里始终 6 行） |
| 上报账户 | 6 个（其中 3 个名下确实什么都没有，报的是空清单） |
| 有模型的账户 | 2 个；其中一个人 4 个真实权重（最大 63.39 GB） |
| 扫描器卫生 | 同一个人此前报出 42 条模型，其中 38 条是 `ggml-vocab-*` 词表；过滤后正好 4 条 |
| 引擎去重 | `lib`/`lib64` 双计数修掉后，该账户的 llama.cpp 只出现一次 |

**管理员视角的复验**：本地管理员凭据文件里的口令是过期的（登录 `invalid_credentials`），
先用 `backend.auth.bootstrap rotate --recover` 轮换，再以管理员会话请求
`/api/v2/registry/discovery`：`agent_reports_scope` 从 `self` 变成 `all`，六条上报
逐条可见，`kaidong` 那行是 4 个模型（`decider-4b-v2.1-Q8_0`、
`eagle3-gpt-oss-120b-Q8_0`、`gpt-oss-120b-MXFP4`、`test-qwen0.5b-q4km`）。走的是
控制台同一条读取路径与同一个 scope 判断，但没有在浏览器里点过——最后一眼仍建议由
人自己看。
