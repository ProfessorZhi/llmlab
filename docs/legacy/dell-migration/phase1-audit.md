# {{LAB_NODE}} 迁移 · 第一阶段：远程只读审计

| 项 | 值 |
|---|---|
| 目标机 | `{{LAB_HOSTNAME}}` / `{{LAB_HOST}}` / Ubuntu 26.04 LTS / kernel 7.0.0-14-generic |
| 登录账号 | `{{LAB_USER}}`（uid 1002，组 `{{LAB_USER}}` `users` **`{{LAB_GROUP}}`**） |
| 通道 | 项目内固定密钥 `{{LAB_SSH_KEY_PATH}}`，`StrictHostKeyChecking=yes` + 项目 `known_hosts` |
| 审计时间 | 2026-10-10 17:07–17:12 (+08:00) |
| 性质 | 只读。未安装、未启动/停止任何服务，未触碰 `{{STRATA_ROOT}}`、Strata 进程、驱动、CUDA、用户权限 |

**写操作披露（两次，均为探测且已复核清空）**

1. `mkdir -p ~/.local/bin` —— 审计「免 sudo 的用户级安装目录是否存在」时创建。该目录是后续 `uv` 安装路径，保留无害。
2. `{{LAB_ROOT}}/services/.probe_<pid>` —— 探测「第二阶段能否在评测目录下新建隔离子树」时创建，随即 `rmdir` 删除。事后全盘扫描 `{{LAB_ROOT}}` 与 `{{LAB_HOME}}`，`.probe_*` / `.{{LAB_USER}}_*` 残留计数为 **0**，`{{LAB_ROOT}}/services` 已不存在。

未改动 `{{LAB_USER}}` 的 crontab（当前为空）。未读取任何密钥内容。

---

## 一、结论摘要

| 审计项 | 结论 |
|---|---|
| CPU | i9-10900X，20 线程，load1 **0.17–0.28** —— 余量充足 |
| 内存 | 总 **30.55 GiB**，可用 **22.75–23.85 GiB**（Strata 占 6.06 GiB / 19.8%）—— 余量充足 |
| 磁盘 | `/` **318 GiB 可用**（468 GiB 卷用了 29%）—— 余量充足 |
| GPU 遥测 | ⚠️ `nvidia-smi` **在本机失效**（NVML 用户态 595.99.02 vs 已加载内核模块 595.58.03），且存在待生效重启。**GPU 指标只能取 Strata `/metrics`** |
| 端口 | 3000 / 9000 / 9090 / 9100 **空闲**；8080 被 Strata 占用（既有） |
| 出网 | 正常。GitHub 200/0.32s，清华 PyPI 200，Prometheus 制品 302，`dl.grafana.com` 200 |
| Python 环境 | ⚠️ 仅 `python3.14.4`，**无 pip、无 ensurepip**，`python3 -m venv` 直接失败 |
| EvalScope 1.12.0 | ✅ 镜像站与 pypi.org 均有（wheel + sdist） |
| 免 sudo 安装通道 | ✅ 有（`uv` + 独立 CPython；`python3 -m venv --without-pip` 可用） |
| 容器 | ❌ 无 docker / podman —— 与既有配置「preferred Docker-free」一致，走二进制 tarball |
| 用户级服务持久化 | ⚠️ `Linger=no`，且 `{{LAB_USER}}` **无 sudo** ⇒ `systemctl --user` 单元登出即停 |
| 免 sudo 的替代持久化 | ✅ 有：`KillUserProcesses=false`（实测）+ cron 可用 ⇒ 分离进程 + `@reboot` |
| Strata 影响面 | 可做到**零改动**：不碰 `{{STRATA_ROOT}}`、其 venv、配置、进程；只共享 CPU/RAM/磁盘与只读 HTTP 端点 |
| **Strata API key** | ❌ **`{{LAB_USER}}` 读不到**（`configs/api-keys.env` 为 `600 {{LAB_ADMIN_USER}}`）⇒ **核心阻断，见第二节** |

---

## 二、需要你决策的三个阻断点（在此停下）

### B1 · Strata API key 不可读 —— 阻断全部有意义的部署

这是**最硬的一条**，因为它同时卡住两台服务：

- **EvalScope** 被要求「必须使用已有 Strata OpenAI-compatible API，不额外加载模型」；
- **Prometheus** 被要求「只利用现有 Strata `/metrics`」。

两者都要鉴权。实测：

```
GET  /health                -> 200   （免鉴权，唯一）
GET  /metrics               -> 401   {"error":{"type":"authentication_error",...}}
GET  /v1/models             -> 401
POST /v1/chat/completions   -> 401   （路由存在，只是没钥匙）
```

而钥匙在 `{{LAB_ROOT}}/configs/api-keys.env`：

```
-rw-------+ 1 {{LAB_ADMIN_USER}} {{LAB_GROUP}} 230  api-keys.env
```

`600` 且属主 `{{LAB_ADMIN_USER}}`；`{{LAB_USER}}` 虽在 `{{LAB_GROUP}}` 组，但文件组位为 0，`access(2)` 实测 **DENIED**（`start_api.sh` 同为 `700`，亦 DENIED）。`{{LAB_USER}}` 无 sudo，无法绕过。

**注意**：Strata 是以 `--api-key key1,key2` 多钥匙启动的（既有决策）。要新增一把钥匙必须重启 Strata —— **这被明令禁止**。所以可用的路只有「把已在服务端生效的其中一把钥匙，以 `{{LAB_USER}}` 可读的形式放到服务器上」，而不是新造一把。

**需要你选一条：**

- **(a) 由你/{{LAB_ADMIN_USER}} 在服务器上落一个 `{{LAB_USER}}` 可读的钥匙文件**（如 `install -m 640 -o {{LAB_ADMIN_USER}} -g {{LAB_GROUP}}` 到 `{{LAB_ROOT}}/services/secrets/strata-key.env`）。推荐：不动 Strata、不重启、最小暴露面。
- **(b) 你把钥匙交给我，由我写成一个 `600 {{LAB_USER}}` 文件。** 如实说明：钥匙会经过本次会话上下文；我会只写文件、绝不复述内容，但**它确实会出现在这段对话里**。
- **(c) 先跳过依赖钥匙的部分**，只部署 Grafana（自身无外部鉴权依赖）+ Prometheus 抓不到指标的骨架，等钥匙到位再打开。这条会让第一/五阶段实质空转，不推荐。

### B2 · 节点没有 EvalScope 需要的 Python（有免 sudo 绕行，需你点头）

节点上只有 `python3.14.4`，且**重到无法直接建 venv**：

```
python3 -m venv <dir>   -> FAILED: ensurepip is not available
                           建议 apt install python3.14-venv
python3 -m pip          -> No module named pip
python3 -m venv --without-pip <dir>  -> OK
```

`apt` 需要 sudo，而 `{{LAB_USER}}` 无 sudo。同时 Windows 侧那份基准是 **evalscope 1.12.0 / Python 3.12.10**（numpy 2.5.3、openai 2.54.0、transformers 5.19.0，**不含 torch**，即纯 API 用法）。用 3.14 去装 1.12.0 属于把「已知能跑的版本」换成「刚发布的解释器」，是自找的风险。

节点上已确认**没有** `uv` / `uvx` / `pipx` / `pyenv` / `conda` / `virtualenv`。但免 sudo 的通道是通的：

- `https://astral.sh/uv/install.sh` → 301（可用）
- `github.com/astral-sh/uv/releases/latest` → 302
- `python-build-standalone` release API 可达 ⇒ `uv python install 3.12` 能拿到独立 CPython

**建议（需你确认）**：在 `{{LAB_USER}}` 名下装 `uv`（落到 `~/.local/bin`），用它拉一个独立 CPython 3.12 + 建 `evalscope==1.12.0` 的独立 venv。**不碰系统 Python、不碰 `{{STRATA_ROOT}}/.venv`、不需要 sudo。**

### B3 · 登出后能否持续运行（有免 sudo 绕行，需你确认「够用」的标准）

实测三条事实：

1. `KillUserProcesses = false`（`busctl get-property` 读到 `b false`）⇒ **分离进程可以活过 SSH 断开**；
2. `{{LAB_USER}}` 与 `{{LAB_ADMIN_USER}}` 的 `Linger` **均为 `no`** ⇒ `systemctl --user` 单元登出即停；
3. `cron` **active + enabled**，`{{LAB_USER}}` 可自建 crontab（当前为空）⇒ `@reboot` 是免 sudo 的开机自启通道。

**已有的对照事实**：Strata 今天之所以活着，是因为它落在 `{{LAB_ADMIN_USER}}` 的**会话 scope** 里（`user.slice/user-1000.slice/session-241.scope`），**不是** systemd 单元，也没有 linger。也就是说：Strata 自己都不具备「{{LAB_ADMIN_USER}} 登出后继续」的能力。（附带结论：Portal 的 `REMOTE_PROBE` 在探 `systemctl is-active strata strata-eval`，在这台机器上**恒为 inactive**，那个读数不代表健康。）

**建议（需你确认）**：Prometheus/Grafana/EvalScope 用 `nohup`/`setsid` 分离启动 + `@reboot` cron 兜底。这能达到「SSH 断开后继续采样」，但要说清代价：

- 它**不是** systemd 服务：没有 `Restart=always`、没有开机依赖序、没有 `journalctl` 统一日志、崩溃不会自动拉起；
- 「真正持续运行」只能算**部分成立**：跨登出成立，跨重启依赖 cron 且无健康自愈；
- 若你要的是 systemd 级的可靠托管，就必须有 sudo（`loginctl enable-linger {{LAB_USER}}`，或建系统级单元）——那需要你去申请。

---

## 三、资源实测

```
CPU      Intel(R) Core(TM) i9-10900X @ 3.70GHz   20 线程   load1 0.17 / 0.28
内存     MemTotal 32,038,320 kB (30.55 GiB)   MemAvailable 22.75-23.85 GiB
         Swap 8,388,604 kB，已用 496,376 kB
磁盘     /dev/nvme0n1p2  468 GiB 卷  127 GiB 已用  318 GiB 可用 (29%)
         sda/sdb 各 931.5G 但为 VMFS_volume_member（VMware 数据存储，不可用）
GPU      无 nvidia-smi 读数（见下）
```

GPU 失效读数（**既有问题，非本次引入**）：

```
Failed to initialize NVML: Driver/library version mismatch
NVML library version: 595.99

用户态 libnvidia-ml.so.595.99.02   vs   已加载内核模块 595.58.03
/var/run/reboot-required 存在
```

驱动包与内核模块包都在（`linux-modules-nvidia-595-7.0.0-14-generic`），**重启即可自愈——但重启被明令禁止，且不在本次范围**。因此：**不改动、仅记录；GPU 指标一律从 Strata `/metrics` 取**（这正是第五阶段的既有前提）。

## 四、端口与暴露面

| 端口 | 状态 |
|---|---|
| 8080 | **BUSY** — Strata，`LISTEN {{LAB_BIND_ADDR}}:8080`（既有决策：`--host {{LAB_BIND_ADDR}}` 已写进配置） |
| 3000 / 9000 / 9090 / 9100 / 26500 | **free** |
| 22 / 631 / 53 | 系统占用 |
| 8765 | free（Portal 在 Windows 侧，节点不用） |

新服务全部绑 `localhost`，**不开放任何新的对外端口**。

## 五、兼容性与制品可得性

| 项 | 结果 |
|---|---|
| `evalscope==1.12.0` | ✅ 清华镜像与 pypi.org 均有 `evalscope-1.12.0-py3-none-any.whl` + `.tar.gz` |
| Windows 基准 | evalscope 1.12.0 / Python 3.12.10 / numpy 2.5.3 / openai 2.54.0 / transformers 5.19.0 / **无 torch** |
| Python 3.14 轮子 | numpy 2.5.3 已有 `cp314` manylinux x86_64 轮子 ⇒ 3.14 非绝对不能跑，但整棵依赖树未验证 |
| Grafana OSS | ✅ `12.3.1` / `12.2.1` / `12.1.0` 的 `.linux-amd64.tar.gz` 均 200，且 `.sha256` 可下载 ⇒ **可校验** |
| Prometheus | ✅ `3.7.3` / `3.6.0` / `3.1.0` 均 302（跳转到制品），`sha256sums.txt` 可下载 ⇒ **可校验** |
| node_exporter（可选） | ✅ `1.9.1` 可达 |
| 容器 | ❌ 无 docker / podman，无 `/var/run/docker.sock` ⇒ 二进制 tarball 路线 |
| 编译工具 | `gcc` / `make` / `cc` / `git` / `jq` / `openssl` / `rsync` / `xz` 齐备；**`nvcc` 不在 PATH**（既有事实，不影响纯 Python 部署） |

## 六、权限与 ACL 地图（全部实测 `mkdir` 验证）

| 路径 | `{{LAB_USER}}` 权限 |
|---|---|
| `/`、`/srv` | 只读 |
| `{{LAB_ROOT}}` | **可写**（组 `{{LAB_GROUP}}` = `rwx`，setgid 继承） |
| `{{LAB_ROOT}}/{results,scripts,configs,datasets}` | 目录**可写** |
| `configs/api-keys.env`、`configs/start_api.sh` | **不可读**（`600`/`700` {{LAB_ADMIN_USER}}） |
| `scripts/probe_caps.py`、`eval/run_eval.py` | **不可读**（文件级限制） |
| `{{STRATA_ROOT}}`、`{{STRATA_DATA_ROOT}}` | 只读（且本次不改） |
| `~/.local/bin`、`/tmp` | 可写；`/tmp` 挂载含 `nosuid,nodev` 但**无 `noexec`** ⇒ 可执行 |

⇒ 第二阶段落在 **`{{LAB_ROOT}}/services/`**（新建独立子树），或 `$HOME`。`/srv` 属主是 root，无法在它下面另开顶层目录。

## 七、对 Strata 的影响面与不干扰保证

**不做**：不读/写 Strata 配置与模型文件，不碰 `{{STRATA_ROOT}}/.venv`，不启停其进程，不改 `--host`/`--api-key`，不重启、不升级驱动/CUDA/内核。

**共享的只有**：CPU、RAM、磁盘，以及对 `:8080` 的**只读 HTTP 调用**。

- Prometheus 以 60 s 级低频抓 `/metrics`，对 HTTP 服务负载可忽略；
- EvalScope 的评测**会真实占用 Strata 的算力与显存**——这是设计使然（用户显式触发，不自动跑），不是副作用，但必须明确写进使用说明；
- 唯一真正的耦合风险是**磁盘**：Prometheus 无上限会吃满 318 GiB。方案里设 `--storage.tsdb.retention.time` 与 `--storage.tsdb.retention.size` 双保险。

## 八、资源预算

| 组件 | RAM | CPU | 磁盘 | 端口 |
|---|---|---|---|---|
| Grafana | 200–400 MB | 近 0 | < 1 GB | 3000 |
| Prometheus | 0.5–1.5 GB | < 5% | 5–20 GB（受保留期约束） | 9090 |
| EvalScope 服务 | 0.4–1 GB（空闲） | 空闲近 0 | 2–5 GB（venv + wheels） | 9000 |
| **合计** | **≈1.1–2.9 GB** | — | **≈8–26 GB** | — |
| **占用后余量** | **≥ 19 GB RAM** | — | **≥ 292 GB** | — |

结论：**资源不是瓶颈**。真正的瓶颈是权限与凭据，不是机器。

## 九、风险表

| # | 风险 | 级别 | 处置 |
|---|---|---|---|
| R1 | Strata API key 不可读，EvalScope 与 Prometheus 都无法工作 | **高 · 阻断** | 见 B1，需你决策 |
| R2 | 用 Python 3.14 装 evalscope 1.12.0，依赖树未验证 | 中 | 用 `uv` 拉独立 CPython 3.12，与 Windows 基准对齐 |
| R3 | 无 linger + 无 sudo ⇒ 无 systemd 级托管，崩溃不自愈 | 中 | 分离进程 + `@reboot` cron；**如实标注这不是 systemd 级可靠** |
| R4 | `nvidia-smi` 失效 + 待重启；GPU 指标来源受限 | 低 | 不改动；GPU 指标走 `/metrics` |
| R5 | Strata 绑 `{{LAB_BIND_ADDR}}:8080`（既有） | 低 | 不改（属既有决策，超出本次授权） |
| R6 | 与 `{{LAB_ADMIN_USER}}` 共用 `{{LAB_ROOT}}` | 低 | 新建 `services/` 子树，互不覆盖 |
| R7 | Prometheus 无上限吃满磁盘 | 低 | 保留期 + 保留体积双限 |

## 十、建议的第二阶段方案（**待你批准后才执行**）

```
{{LAB_ROOT}}/services/
├── secrets/           640，存从 B1 拿到的 Strata key（唯一含密钥之处）
├── evalscope/         uv 建的 Python 3.12 venv + evalscope==1.12.0
│   ├── config/        指向 http://localhost:8080/v1 的评测配置
│   ├── logs/          独立日志
│   └── outputs/       评测产物
├── prometheus/        3.7.3 二进制 + 独立 data/ + 独立 config/
│   ├── prometheus.yml 抓 Strata /metrics（带 key）+ 自身
│   └── data/          TSDB，双限保留
├── grafana/           12.3.1 二进制 + 独立 data/ logs/
├── metrics-adapter/   Strata JSON → Prometheus 文本（若 /metrics 结构需要）
└── run/               pid + 启动/停止脚本
```

全部：`localhost` 绑定、独立配置、独立日志、独立数据目录、版本锁定、按官方发布的 `.sha256`/`sha256sums.txt` 校验下载产物、先低风险小规模验收再接 Portal。

## 十一、明确不做的事

重启服务器；改驱动/CUDA/内核/Ubuntu；改 Strata 运行参数、配置、模型或进程；改 Linux 用户、组、sudo、linger；开新对外端口；写 systemd 系统级单元；改 `{{LAB_ADMIN_USER}}` 的 crontab；大 Benchmark 或并发压测；调用生产 API 产生费用；向飞书发布报告；读取或转述任何密钥内容。
