# 私有资源发现与服务共享 — 设计

> 状态：**DRAFT / 待评审**　日期：2026-10-23  
> 需求：[私有资源、共享服务 — 产品需求](product-shared-model-pool-and-on-demand-serving-2026-10-23.md)（下称 PRD，决策编号 P1–P11）  
> 本文描述 M1 的实现方式，并给出 M2 / M3 的接口轮廓。不构成部署或宿主权限变更的批准。

## 0. 目标与非目标

**目标（M1）**：用户起的服务自动接入并按策略挂上网关；用户的模型与引擎 build 自动出现在「我的」，挪动后仍认得出；CLI 能一键上线和查看 GPU；平台日常不持有 root。

**非目标**：共享模型文件、发布库、不可变版本；平台以别人的身份执行任何东西；替用户移动 / 删除文件；checkpoint 版本树；外部 HTTPS 入口。

## 1. 总体结构

```
┌──────────────── 平台服务（服务账户 llmlab，非 root）────────────────┐
│ BFF / 控制台   注册表（SQLite）   集成网关 /v1   共享策略   GPU 看板 │
│        ▲ HTTPS（会话 / CLI 令牌）          ▲ Unix socket（SO_PEERCRED）│
└────────┼───────────────────────────────────┼─────────────────────────┘
         │                                   │  /run/llmlab/agent.sock
   浏览器 / llmlab CLI / Claude Code     llmlab-agent（每个用户一份，
   （以用户身份运行，带用户令牌）         systemd --user，以该用户身份）
```

三种参与者：

| 参与者 | 身份 | 能做什么 | 不能做什么 |
| --- | --- | --- | --- |
| 平台服务 | `llmlab` 服务账户 | 存登记、策略、Key、日志；读 `/proc/net/tcp`、`nvidia-smi`、本机 HTTP 探测；网关转发到本机端口 | 读用户私人目录；以用户身份执行 |
| 每用户代理 `llmlab-agent` | 该用户 | 只读扫描本人授权目录；读本人进程的 `/proc/<pid>/{cmdline,fd}`；上报元数据；（M2）执行本人确认过的固定动作 | 任意命令；跨用户 |
| `llmlab` CLI | 该用户（+ 平台令牌） | 调平台 API；在本人 shell 里起 / 停本人服务、挪本人文件 | 绕过平台权限 |

**代理怎么证明「我是 kaidong」**：代理连接平台的 Unix socket，平台用 `SO_PEERCRED` 从内核取对端 uid，再按管理员设定的「平台账户 ↔ Linux 用户」映射（已有）找到账户。没有令牌可偷，也伪造不了。socket 权限 0666，鉴别全靠内核 uid；未映射的 uid 一律拒绝。

## 2. 一次性安装（唯一需要 root 的步骤）

由管理员 `sudo` 执行一个可审阅、幂等、可回滚的安装脚本（M1 提供脚本，**是否执行由运维决定**）：

1. 建目录：`/run/llmlab`（tmpfiles，属 llmlab）、`/data/models/platform`、`/srv/llm-lab/models`、`/opt/llm-engines`；平台区设为「llmlab 写，lab 组只读」。
2. 安装全局用户级服务：`/etc/systemd/user/llmlab-agent.service`，`systemctl --global enable llmlab-agent`。
3. 对每个实验室用户 `loginctl enable-linger <user>`，使代理在未登录时也运行。
4. 不改任何用户 HOME 的权限；不加 sudoers 规则。

卸载：`systemctl --global disable`、删 unit、`disable-linger`。

## 3. 服务自动接入（FR-01）

两层，平台侧够用，代理侧补全：

| 层 | 来源 | 得到 | 说明 |
| --- | --- | --- | --- |
| 平台侧（每 30 s） | `/proc/net/tcp{,6}`（全员可读） | 监听端口 + 所属 uid | 现有 `ProcNetOwnershipProver` 已在用；只看 127.0.0.1 / 允许端口段 |
| 平台侧 | 本机 HTTP 探测（现有：`/props`、`/get_server_info`、`/v1/models` …） | 框架、模型 id、是否健康 | 只读，带超时 |
| 代理侧 | 本人进程的 `/proc/<pid>/fd` → socket inode → 端口；`cmdline` | pid、引擎二进制路径、模型文件路径、启动参数 | 跨用户读 fd 不被内核允许，所以必须由代理做 |

**状态机**：`seen`（端口出现）→ `healthy`（探测通过）→ `attached`（登记到主人名下）；连续 2 轮不见 → `offline`（保留记录，网关返回 503「服务未运行」）；重新出现且指纹一致 → 回到 `attached`，模型名不变。

**落库**：沿用现有 `engines` 表表示「运行中的服务实例」，新增列：`source`（manual / auto / platform）、`state`、`last_seen_at`、`pid`、`runtime_build_id`、`artifact_id`、`launch_args_json`。手动登记的老记录 `source=manual`，行为不变。

## 4. 按策略自动挂网关（FR-02）

- 新表 `lab_settings`：`default_visibility`（`private` | `all`，初始 `private`）、`owner_override_locked`（布尔，初始否）。只有管理员可改；变更写审计。
- 服务 `attached` 时：若主人有显式设置用主人的，否则用实验室默认值；`all` 时自动创建网关路由。
- 路由名：取探测到的模型 id，规范化为 `[A-Za-z0-9._:/-]`；冲突时追加 `@<用户名>`；主人可改名。
- 路由的 `enabled` 跟随服务状态：`offline` 时路由保留但调用返回 503，恢复后自动可用。
- 管理员把默认值从 `private` 改成 `all` 时：**只影响之后新接入的服务**，已有服务保持原设置（避免一次性把所有人的服务公开）；控制台给出「同时应用到现有服务」的显式选项。

## 5. 模型与引擎发现（FR-03）

由代理以用户身份执行。

**扫描范围**：`~`、`/data/home/<user>`、`~/.cache/huggingface/hub`；`~/.llmlab-ignore`（每行一个 glob）可排除。

**跳过**：`.git`、`node_modules`、`.venv`/`venv`/`site-packages`、`__pycache__`、`build/CMakeFiles`、`third_party/*/models`、`~/.cache`（HF hub 除外）、pip / uv 缓存。

**识别规则**：

| 类型 | 判定 | 读取的内容 |
| --- | --- | --- |
| GGUF | `*.gguf` 且 ≥ 100 MB | 文件头元数据（架构、名称、量化类型、张量数）；同目录 `mmproj*.gguf` ⇒ vision |
| HF 模型 | 目录含 `config.json` 且有 ≥ 100 MB 权重分片 | `config.json`（≤ 256 KB）、`*.index.json` 分片清单 |
| HF 缓存 | `models--<org>--<name>/snapshots/<rev>` | 路径本身给出仓库与版本 |
| diffusers | 目录含 `model_index.json` | `_class_name` ⇒ image / video |
| 引擎 build | 可执行文件名 ∈ {`llama-server`, `strata`, …} 或 venv 的 `bin/{vllm,sglang}` | `--version`（按 inode+mtime 缓存）；上溯找 `.git/HEAD` 读 commit |

**task 推断**：`*ForCausalLM` ⇒ chat；`*VLForConditionalGeneration` / 有 mmproj ⇒ vision；`*ForSequenceClassification` ⇒ decision；sentence-transformers ⇒ embedding；diffusers ⇒ image / video。推断不出时 `unknown`，主人可改。

**节奏与成本**：

- inotify 只监听上述根目录下的模型目录（不监听整个 HOME），事件合并后 5 s 内增量处理；
- 每小时一次对账：只进入 mtime 变化过的目录；`nice 19` + `ionice -c3`；
- `llmlab scan` / 控制台「刷新」立即触发；
- 代理常驻约 20 MB；新文件读 2–3 MB；不加载模型、不全量哈希。

## 6. 指纹与位置（FR-04）

**快速指纹**：`size` + `sha256(前 1 MiB ‖ 后 1 MiB)`；GGUF 另附文件头元数据摘要；HF 目录用 `config.json` 哈希 + 分片清单 + 各分片大小组成目录指纹。

**数据模型**（扩展现有 `models` 表，不另建库）：

- `models` 新增：`fingerprint`、`kind`（gguf / hf / diffusers）、`task`、`format_meta_json`、`size_bytes`、`source`（manual / auto）。
- 新表 `model_locations`：`model_id`、`path`、`uid`、`device`、`inode`、`mtime`、`tier`（ssd / hdd，按挂载点判定）、`state`（present / missing）、`last_seen_at`。
- 新表 `runtime_builds`：`id`、`owner`、`kind`、`binary_path`、`version`、`git_commit`、`fingerprint`、`last_seen_at`。

**匹配规则**（每次上报时）：

| 观察 | 判定 | 动作 |
| --- | --- | --- |
| 同 device + inode，路径变了 | 同盘移动 | 更新路径 |
| 新路径指纹 = 某记录，旧位置同时 missing，且同 uid | 跨盘移动 | 更新位置，模型 ID 不变 |
| 同指纹两个位置都在 | 复制 | 两个位置挂同一模型 |
| 同指纹出现在不同 uid 名下 | 各自独立的模型（不跨人合并） | 记一条「重复副本」统计，供 §6 推迟项决策 |
| 匹配不唯一 | 停下 | 标「需确认」，不自动改指向 |
| 旧位置消失、未找到新位置 | 离线 | `missing`，保留记录 |

路径只对主人和管理员可见；别人只看到模型名、类型、大小等级。

## 7. CLI（FR-06）

在现有 `cli/llmlab/entry.py` 上扩展；全部命令支持 `--json`，输出统一信封：

```json
{"ok": true, "data": {...}, "warnings": [], "next": ["llmlab share decider-4b --scope lab"]}
```

退出码：`0` 成功；`2` 参数错误；`3` 未登录 / 无权限；`4` 资源不足（如 GPU 被占）；`5` 未就绪 / 超时；`1` 其他。写操作支持 `--dry-run` 输出计划。

| 命令 | M | 行为 |
| --- | --- | --- |
| `status` | M1（扩展现有） | 我的服务、模型、build、GPU 占用、SSD/HDD 用量 |
| `scan` | M1 | 让本人代理立即对账，返回新增 / 移动 / 消失 |
| `storage report` | M1 | 本人在各盘的用量、大且久未用的文件、重复副本 |
| `serve <model> --engine <build> [--ctx N --parallel N …] [--port P] [--share lab\|private] [--stage]` | M1 | 见下 |
| `services list \| logs <svc> \| stop <svc>` | M1 | 只作用于本人服务（`systemctl --user` / `journalctl --user`） |
| `share <svc> --scope lab\|private` | M1 | 改本人服务的共享设置 |
| `gpu` | M1 | GPU 看板的命令行版 |
| `storage move <model> --to ssd\|hdd [--link]` | M2 | 复制 → 校验指纹 → 删源；可留软链接 |
| `gpu borrow --reason …` | M2 | 申请借用 |

**`serve` 流程**：

1. 解析模型（名字 / 路径 / 指纹前缀）与 build，必须都属于本人（或平台区只读引擎）；
2. 模型在 HDD 且未加 `--stage`：警告冷启动约慢 5 倍；加 `--stage` 则先拷到本人 SSD 目录；
3. 查 GPU 看板：可用显存不足 ⇒ 退出码 4，并给出占用者与 `gpu borrow` 建议；
4. 按引擎参数表（`backend/runtime/params.py`）生成命令，`systemd-run --user --unit llmlab-<name> --property=…` 启动；
5. 等 `/v1/models` 返回 200（默认超时 10 分钟，可调），期间输出进度；
6. 等平台接入（§3）完成，按 `--share` 或策略挂路由；
7. 输出模型名、调用地址、curl / Python 示例。

**给 Claude Code 的说明**：仓库提供 `docs/agent/llmlab-cli.md`（可作为 skill / CLAUDE.md 片段），写明命令、存放约定、「挪文件必须先校验」「只操作本人资源」等规则。它不握任何权限。

## 8. GPU 看板（FR-07）与借用（M2）

- 数据：`nvidia-smi --query-compute-apps=pid,used_memory`；pid → uid 读 `/proc/<pid>/status`（全员可读）；pid → 服务由代理上报的映射给出；平台托管服务直接已知。
- 「空闲多久」：网关最后一次转发时间与引擎指标（如 Strata 的 requests_total 变化）取较近者；都没有时显示未知。
- 借用（M2）：`requested → notified → approved | timeout → stopping → released → (restore_reminded)`。停止由占用者的代理以占用者身份执行固定动作 `stop <unit>`；管理员仲裁走同一动作并写审计。

## 9. 平台托管服务（M2，FR-11）

- 以 `llmlab` 账户的 systemd 用户服务运行（linger），模型从 `/data/models/platform` 校验后拷到 `/srv/llm-lab/models`；
- 只有管理员可通过 API / 控制台起停；参数用参数表；
- Strata 是否迁为平台服务是**单独的生产变更**，不随本设计自动发生。

## 10. 多类型模型（M3，FR-12）

| task | 网关接口 | 计量 |
| --- | --- | --- |
| chat / vision / decision | `/v1/chat/completions`（vision 带图片） | tokens |
| embedding / rerank | `/v1/embeddings`、`/v1/rerank` | tokens |
| image | `/v1/images/generations` | 张数 × 分辨率 × 步数 |
| video 及长任务 | `/v1/jobs`：提交 → 轮询 → 取结果；结果存 HDD 并设保留期 | 视频秒数 |

网关按路由的 `task` 只放行对应接口；在线体验按类型呈现。

## 11. 安全要点

- 平台账户与 Linux 用户的映射只由管理员设定；未映射的 uid 的代理连接一律拒绝。
- 代理只接受固定动作；每个动作需本人（或管理员仲裁时管理员）在平台上确认，并写审计。
- 扫描只读元数据；不反序列化 `.pt` / pickle；不读超过上限的配置文件。
- 私人路径只对主人和管理员可见。
- 平台从不删除用户文件；`storage move` 只能由用户本人发起。
- 网关只连本机端口；引擎自身的密钥留在服务端。

## 12. 实施切片（M1）

| 顺序 | 切片 | 主要改动 | 验收 |
| --- | --- | --- | --- |
| 1 | 发现规则修正 + 指纹 + 位置表 | `local_discovery.py`、`store.py` 迁移、单测 | kaidong 目录样本只识别 4 模型 / 2 build；移动 / 复制 / 歧义各有用例 |
| 2 | 服务自动接入 + 自动挂路由 + 实验室默认策略 | 平台侧端口 / 探测轮询、`engines` 新列、`lab_settings`、控制台开关 | 起一个假引擎 ⇒ 30 s 内接入；默认私有不可被他人调用；切默认开放后新服务可调 |
| 3 | 代理 | `llmlab-agent`（标准库）、socket + `SO_PEERCRED`、安装脚本与文档 | 未映射 uid 被拒；代理上报的 pid / 模型路径与进程一致 |
| 4 | CLI | `status` / `scan` / `storage report` / `serve` / `services` / `share` / `gpu`、JSON 信封、`docs/agent/llmlab-cli.md` | 「HDD 上的模型 → 上线给实验室」端到端 |
| 5 | GPU 看板 | 控制台资源监控页、`llmlab gpu` | 与 `nvidia-smi` 一致；未知显示未知 |

每个切片单独 PR，各自带测试；不改 Strata、Caddy、Tailscale 与现有生产入口。

## 13. 待定

1. `/data` 是否支持 reflink（影响 M2 `storage move` 的速度）。
2. 自动挂路由的命名冲突：追加 `@用户名` 是否可接受，还是要求主人命名后才挂。
3. 服务 `offline` 多久后自动删除路由（建议不自动删，只停用）。
4. 代理的安装是否需要每个用户知情确认（建议：控制台首次登录时告知扫描范围与 `~/.llmlab-ignore`）。
