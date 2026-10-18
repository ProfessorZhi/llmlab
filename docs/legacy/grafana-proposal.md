# Grafana 接入方案（第三阶段 · **方案，尚未实施**）

> **状态：提案。** 本文不描述已完成的工作。
>
> 按 brief 的明确约束：**未经批准，不在 {{LAB_NODE}} 上安装任何 Exporter、不修改驱动、
> 不重启服务器。** 截至本文写作，节点上**没有安装任何监控组件**（见下方实测）。
> 本文只给出端口、数据源、保留期、资源预算与部署/回滚动作，供批准后再执行。

---

## 0. 先说结论

| 问题 | 结论 |
|---|---|
| 值得装吗？ | **GPU / 引擎 / 主机三块数据都已经在 `Strata /metrics` 里，且控制台已经在读。** Grafana 的**增量价值只有两样：长期历史 + 告警**。如果这两样不需要，本项目可以完全不装。 |
| 最大的拦路虎 | 不是安装难度，是**本机只剩 1.1 GiB 空闲内存**（实测，见 §5）。 |
| GPU 怎么采 | **不能**用任何基于 NVML 的 Exporter——NVML 在本机是坏的（见 §2）。GPU 只能取 `Strata /metrics`。 |
| 需不需要 Prometheus | 取决于取哪条路。两条路见 §3，A 路要装 Exporter（需批准），B 路零安装但能力打折。 |

---

## 1. 端口分配

沿用现有约定：**本机服务一律绑 `localhost`**。

| 端口 | 用途 | 归属 | 现状 |
|---|---|---|---|
| 8765 | LLM Lab 控制台 | 本项目 | 已占用（本项目） |
| 9000 | EvalScope Dashboard | 本项目 | 已占用（本项目） |
| 18081 | Strata 原生 UI 隧道 | 本项目 | 已占用（本项目） |
| **3000** | **Grafana** | 拟新增 | **实测空白**，与 `config/llm-lab.example.toml` 的 `[services.grafana]` 一致 |
| **9090** | **Prometheus**（仅 A 路需要） | 拟新增 | **实测空白** |
| **9100** | **node_exporter**（仅 A 路需要，**装在节点侧**） | **节点**，不是本机 | 实测未安装 |

启动前必须实测这 3 个端口为空。本机已有一条教训：18080 被一个**对任意路径都回 200**
的服务占着，所以「端口能连上」不等于「端口是空的」，判定要用**裸 TCP connect 失败**为准。

---

## 2. 节点侧实测现状（只读探测所得，2026-10-10）

```
host      {{LAB_HOSTNAME}}        os        Ubuntu 26.04 LTS
kernel    7.0.0-14-generic      python    3.14.4
cpu       Intel i9-10900X  10c / 20 threads
ram       32,038,320 kB ≈ 30.6 GiB     swap 8 GiB
disk      /dev/nvme0n1p2  501.8 GB, 已用 29% (135.4 GB), 余 340.8 GB
loadavg   0.08 / 0.02 / 0.01   ← 节点基本空闲
node_exporter / prometheus / grafana-server / promtool:  全部 MISSING
```

### 2.1 必须订正的一条旧记录：GPU 指标**是拿得到的**

既有文档（`README.md` / `docs/security.md`）写的是「NVML 不可用 ⇒ GPU 指标不可用」。
**这句话只对了一半**，实测把两件事分开：

| 通道 | 实测结果 |
|---|---|
| `nvidia-smi` / 一切 NVML 工具 | ❌ `Failed to initialize NVML: Driver/library version mismatch`（NVML library version: 595.99） |
| **`Strata /metrics` → `hardware`** | ✅ **有完整实时 GPU 数据** |

`Strata /metrics` 当前给出的 GPU 实测值：

```json
{"gpu_util": 0, "gpu_mem_used": 41996058624, "gpu_mem_total": 51527024640,
 "gpu_temp": 27, "gpu_power": 9.455, "gpu_power_limit": 300.0,
 "gpu_pcie_gen": 1, "gpu_pcie_gen_max": 3, "gpu_pcie_width": 16,
 "gpu_pcie_rx_mb": 0.571, "gpu_pcie_tx_mb": 0.667}
static: {"gpu_name": "NVIDIA RTX A6000", "gpu_count": 1}
```

原因：Strata 自己就是**持有 CUDA 上下文的那个进程**，它读显卡状态走的是 CUDA 运行时，
不是 NVML。NVML 库版本错配只杀死「外部工具」，杀不死「进程内自省」。

**双源核对已通过**：`/metrics hardware` 报 `gpu_mem_used = 40050.6 MiB`，
`/v1/status machine.gpu` 报 `40051.0 MiB`，偏差 `0.0%`。两个独立端点对上了。

> **这条订正决定了下文的选型**：任何 `dcgm-exporter` / `nvidia_gpu_exporter` 方案
> 在本机**必然失败**，因为它们全部基于 NVML。硬上的结果是「Exporter 起来了但
> GPU 面板永远空」，然后很容易被误判成 Grafana 配置问题。

### 2.2 `/metrics` 的格式是一个硬约束

```
HTTP 200   Content-Type: application/json
```

**是 JSON，不是 Prometheus 文本暴露格式。** Prometheus **不能**直接 scrape 它。
所以「拿 Grafana 直接连 Strata」这条路中间**必须垫一个转换器**（见 §3 B 路）。

`/metrics` 顶层结构：

```
engine, live, requests, conversation_cache, requests_kept,
totals, hardware, hardware_static, history, time
history = {gpu_util, gpu_mem_used, gpu_temp, gpu_power, gpu_pcie_rx_mb,
           cpu, ram_used, disk_read_mb, tok_s, prefill_tok_s_mean}
```

`history` 是**节点侧环形缓冲**，即节点自己就在存历史——这也是「Grafana 的历史价值
有多大」这个问题的关键（见 §3 B 路）。

---

## 3. 两条路线

### A 路：标准栈 `node_exporter + Prometheus + Grafana`

```
{{LAB_NODE}}                         Windows 工作站
┌──────────────────────┐          ┌────────────────────────────┐
│ node_exporter :9100  │◄─────────┤ Prometheus :9090           │
│  (CPU/RAM/disk/net)  │  ①scrape │  ├ scrape node_exporter     │
│                      │          │  └ scrape 转换器            │
│ Strata :8080         │          │        ▲                    │
│  /metrics (JSON)     │◄─────────┤  ②转换器 :9101              │
│    └ GPU / 引擎 / 主机│  ①scrape │     Strata JSON → Prom 文本 │
└──────────────────────┘          │                            │
                                  │ Grafana :3000 ──► 浏览器     │
                                  └────────────────────────────┘
```

* 需要**在节点上装 `node_exporter`**（单个静态 Go 二进制，见 §6 的轻装法）。
* 需要**在本机写一个约 100 行的转换器**，把 Strata 的 JSON 翻成 Prometheus 文本。
  这是本方案的**唯一自研件**，也是它最脆的地方——但它无法回避，因为 §2.2。
* GPU 数据的**唯一来源仍是 `Strata /metrics`**，没有任何 Exporter 能替它。

**优点**：标准栈，Grafana 生态完整，告警（Alertmanager）可用，历史保留期可控。
**缺点**：要动节点（需批准）；多一个自研转换器；吃内存。

### B 路：零安装 `Grafana + Infinity 数据源`

不装 Prometheus、不动节点。Grafana 用 **Infinity**（社区 JSON API 数据源）直接打
`Strata /metrics` 和 `Strata /v1/status`，把 JSON 字段映射成面板。

**优点**：**节点侧零改动**，完全在 brief 的许可范围内就能做；内存占用只有 Grafana 一个进程。
**缺点（必须如实说）**：

* Infinity 是**社区插件**，不是 Grafana 官方数据源，多一个信任面；
* **没有历史、没有告警**——Grafana 只能显示「当前值 + `Strata /metrics history` 环形缓冲里那点」；
  它不能替你存一年。这样一来 Grafana 的**增量价值几乎归零**，因为门户已经在画同样的图；
* 主机 CPU/内存可以取 `/metrics hardware`，但**网络与磁盘 I/O 的粒度不如 node_exporter**。

### 我的建议

**先不装。** 理由不是「麻烦」，是**投入产出**：

1. 三块核心数据（模型健康、GPU、引擎吞吐）**门户已经在实时渲染**，且都已通过双源核对；
2. A 路要动节点 + 维护一个自研转换器，换来的主要是「History」；
3. B 路不动节点，但恰恰把「History」这个唯一优势丢掉了，等于装了个更漂亮的同一个页面；
4. 本机**只剩 1.1 GiB 空闲内存**，而 Grafana 官方最低就要 512 MB（见 §5）。

**如果确实要装，我推荐 A 路**，并且明确接受「在节点上跑一个用户态 `node_exporter`」
这一个动作——它是可逆的，且不碰驱动、不碰 Strata、不碰模型文件。

---

## 4. 数据源清单（A 路）

| 数据源 | 端点 | 采集者 | 指标 |
|---|---|---|---|
| 节点 CPU/RAM/磁盘/网络 | `localhost:9100/metrics`（经隧道） | node_exporter | `node_cpu_seconds_total`、`node_memory_*`、`node_filesystem_*`、`node_network_*` |
| GPU | `Strata /metrics → hardware` | **转换器** | util / 显存 / 温度 / 功耗 / PCIe |
| 推理引擎 | `Strata /metrics → live, totals` | **转换器** | `tok_s`、`prefill_tok_s_mean`、排队数、累计请求与 token |
| 主机（节点自报） | `Strata /metrics → hardware` | **转换器** | `cpu`、`ram_used`、`disk_read_mb`（**与 node_exporter 交叉验证用**） |

> 保留「主机自报」这一路是**刻意的**：它和 node_exporter 是**两个独立来源**，
> 差异本身就是信号。这正是门户现在做 `双源核对` 的同一个理由。

### 隧道

`node_exporter` 只绑节点 loopback，经 SSH 隧道取回，与现有 `strata-native` 隧道同构：

```
ssh -N -L localhost:19100:localhost:9100 {{LAB_USER}}@{{LAB_HOST}}
```

* 本机端口用 **19100**（避开已被占用的 18080/18081 段）；
* 复用现有 `{{LAB_SSH_KEY_PATH}}` + 项目内 `known_hosts`；
* 隧道是**只读的单向转发**，与 `strata-native` 那条一样由 `start.ps1` 管生命周期。

---

## 5. 资源预算（实测基线）

### 本机实测（约束所在）

```
Windows 10 Pro 19045
cpu      i5-1135G7   4 cores / 8 threads      ← 笔记本级
ram      15.7 GiB 总量，**空闲仅 1.1 GiB**
disk     C: 237.8 GB（余 68.8，NVMe）  D: 931.5 GB（余 797.7，HDD）
已装     node {{NODE_BIN}}
未装     grafana-server / prometheus / promtool
```

**1.1 GiB 空闲内存是这条路上真正的天花板。** Grafana 官方给出的最低是
**512 MB RAM / 1 CPU**，但官方同时说明那只是「安装评估下限」，小型生产档建议
**2–4 GB RAM / 2 核**。也就是说：**光 Grafana 一个进程就可能吃掉本机全部空闲内存。**

### 预估占用

| 组件 | RSS 预估 | 说明 |
|---|---|---|
| Grafana | 150–400 MB | Go 二进制，无 JVM；随仪表盘数量增长 |
| Prometheus | 200–400 MB | Go 二进制；另有 TSDB 的 mmap |
| 转换器（自研） | < 30 MB | 单文件 Python/Go |
| node_exporter（**节点侧**） | 15–25 MB | 节点有 23.8 GiB 可用，可忽略 |
| **本机合计** | **约 0.4 – 0.8 GiB** | 相对 1.1 GiB 空闲：**紧，但可行** |

### 存储预算

```
磁盘 = 保留秒数 × 每秒样本数 × 每样本字节数 × (1 + WAL 余量)
```

* 每样本压缩后实测经验值 **1.5–1.7 B**，取 **1.7 B**；
* node_exporter 默认约 1000–1300 条时间序列，转换器约 30 条 → 取 **1200 条**；
* 采集间隔 **60 s**（本lab是低频观测台，不是生产监控，**没必要 15 s**）。

| 保留期 | 计算 | 结果 |
|---|---|---|
| 15 天 | 1,296,000 s × 20 样本/s × 1.7 B | **≈ 44 MB** +WAL ≈ **55 MB** |
| 90 天 | 7,776,000 s × 20 样本/s × 1.7 B | **≈ 264 MB** +WAL ≈ **330 MB** |
| 1 年 | 31,536,000 s × 20 样本/s × 1.7 B | **≈ 1.07 GB** +WAL ≈ **1.3 GB** |

**建议保留期 90 天**，落 **C: 盘**（NVMe），预算 **~330 MB**。
*不落 D:* —— D: 是机械盘且长期饱和，本项目已有实测教训：D: 上的冷启动会慢到分钟级。

> **存储从来不是瓶颈**（按 60 s 采集，一年才 1.3 GB）。瓶颈自始至终是**内存**。

---

## 6. 部署方案（批准后才执行）

### 若选 A 路

1. **节点侧**：取 `node_exporter` 静态二进制，**以 `{{LAB_USER}}` 用户态运行**，
   `--web.listen-address=localhost:9100`，**不装 systemd 服务、不用 root**。
   *这样可以完全绕开「修改后台服务」这条禁令，且一条 `kill` + 一次 `rm` 即可回滚。*
   采集项只读 `/proc`、`/sys`。
2. **隧道**：按 §4 新增 `[tunnels.node-exporter]`，本机 `19100`。
3. **转换器**：本机单文件，读 `Strata /metrics` JSON → 输出 Prometheus 文本，绑 `localhost:9101`。
4. **本机**：Prometheus（`--storage.tsdb.retention.time=90d`、`--storage.tsdb.path=C:\...`、
   绑 `localhost:9090`）+ Grafana（绑 `localhost:3000`）。
5. 接入 `config/llm-lab.example.toml` 的 `[services.grafana]`（把 `enabled` 翻成 `true`、`managed` 视情况），
   让 `start.ps1` 统一管生命周期。

### 若选 B 路

1. 只在本机装 Grafana + Infinity 插件。
2. 数据源指向 `http://localhost:18081`（现有 `strata-native` 隧道）。
3. 无需隧道新增、无需节点改动、无需转换器。

---

## 7. 回滚方案

| 动作 | 回滚 |
|---|---|
| 本机装了 Grafana / Prometheus | 删安装目录；`stop.ps1` 不管它们（`managed = false` 时本就不管） |
| 新增隧道 | 删 `[tunnels.node-exporter]` 配置 + `taskkill` 对应 ssh pid（`stop.ps1` 会按端口认领） |
| **节点侧装了 node_exporter** | `pkill -u {{LAB_USER}} node_exporter` + `rm ~/bin/node_exporter`。**不碰驱动、不碰 Strata、不碰模型文件，不留残留** |
| 转换器 | 删文件；无副作用 |
| Prometheus 数据 | 删 `C:\...\prometheus\data` |

**每一层都可单独回滚，且没有任何一层需要重启节点。**

---

## 8. 明确不做的事

| 不做 | 原因 |
|---|---|
| 安装 `dcgm-exporter` / `nvidia_gpu_exporter` | **基于 NVML，本机必然失败**（§2.1） |
| 修改驱动 / 重装 NVML | brief 明令禁止；且 GPU 数据已从另一条路拿得到，**没有必要** |
| 在节点上装 systemd 服务 | 越权且不可逆；用户态进程已足够 |
| 重启服务器 | brief 明令禁止 |
| 用 NVML 失败去推断「GPU 不可观测」 | **实测已证伪**（§2.1） |

---

## 9. 待批准的决策点

1. **要不要装？**（我的建议：先不装，理由见 §3）
2. 若装，走 **A 路还是 B 路**？
3. 若走 A 路，**是否批准在节点上以 `{{LAB_USER}}` 用户态运行一个 `node_exporter`**？
   （这是整个方案里唯一一个触及节点的动作，可逆、非 root、纯只读。）
4. 保留期取 **90 天** 是否合适？
