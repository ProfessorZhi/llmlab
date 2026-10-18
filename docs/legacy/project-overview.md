# LLM Lab — 本地 AI 运维与评测工作台

> **历史文档。** 这是已退役私有仓库 `llm-lab` 根 README 的脱敏副本，仅作设计参考。
> 它描述的不是 `llm-lab-portal` 的现状；现行说明见仓库根 `README.md`。
> 主地址、账户名、绝对路径已替换为 `{{PLACEHOLDER}}` 占位符。

在一台 Windows 工作站上运行的本地控制台，用来管理、观测和评测 {{LAB_NODE}}
5820 上的 Strata 推理服务。

**职责划分是这套设计的第一原则：**

| | 角色 |
|---|---|
| Windows 工作站 | 全部管理工具：控制台、评测、报告、采集 |
| {{LAB_NODE}} | 只服务 token。不跑前端，不跑大型评测环境，不因本工作台而改变 |

Windows 关机 ⇒ 本地控制台停止；**5820 上的 Strata API 不受任何影响**。这不是
「尽量做到的」，而是结构性的：本项目的写操作面被刻意压到接近零（见「安全模型」）。

---

## 快速开始

```powershell
# 启动（默认自动打开浏览器；加 -NoBrowser 则不打开）
powershell -ExecutionPolicy Bypass -File scripts\start.ps1

# 停止（只停本工作台启动的进程）
powershell -ExecutionPolicy Bypass -File scripts\stop.ps1

# 环境体检（解释器 / 密钥 / SSH 信任锚 / 端口 / EvalScope）
powershell -ExecutionPolicy Bypass -File scripts\check-env.ps1
```

首次使用若 EvalScope 尚未安装：

```powershell
powershell -ExecutionPolicy Bypass -File deploy\evalscope\install-evalscope.ps1
```

### 地址与端口

| 服务 | 地址 | 说明 |
|---|---|---|
| LLM Lab 控制台 | http://localhost:8765 | 全部绑定 loopback |
| Strata 原生 UI（隧道） | http://localhost:18081 | 转发到节点的 `localhost:8080` |
| EvalScope Dashboard | http://localhost:9000 | **按需启动**，不再阻塞首页 |
| Grafana（规划中） | http://localhost:3000 | **未安装**，方案见 `docs/grafana-proposal.md` |

入口页就是控制台本身，**没有为了一个首页去引入 Docker**。

### 统一入口：三个专业系统，一套导航

首页不重复铺四个系统的入口卡。它只回答「**现在什么状态**」，然后把专业页面
放到导航里，一处跳一次：

| 导航组 | 打开 | 形式 |
|---|---|---|
| 本 Portal | Benchmark / 报告 / 环境 | 同页锚点 |
| Strata 原生界面 | Chat / Monitor / About | **新标签页**打开节点自己的 UI |
| EvalScope | Dashboard | **新标签页**，可**按需拉起** |
| Grafana | —— | 路线图，灰显 |

**刻意不做 iframe 嵌入。** 两个理由：原生 UI 会把 API Key 写进**它自己那个源**的
`localStorage`（见 `docs/security.md` §6），嵌进来会模糊「Key 是谁的」；
而且 iframe 会让「点的是哪个源」变得不可见。**新标签页打开，源是显式的。**

Strata 的三条深链是 **hash 路由**（`#chat` / `#monitor` / `#about`），不是路径——
`/monitor` 这种写法在节点上会 404。`selftest.py` 有一条检查专门盯这个。

`start.ps1` 的行为约定：

* **Portal 先起、先可访问**，随后才补隧道；EvalScope **只拉起进程，不等它**——
  它的冷启动实测在 **31 s ~ 459 s** 之间（15 倍差），任何固定超时都会误判
* 端口被占且应答健康 ⇒ 复用它，不重复拉起；被占但不健康 ⇒ 报错退出，**不抢夺**（`-Force` 才换端口）
* 只启动**实际已安装**的组件；EvalScope 未装就明确写「skipped」，不假装启动
* 有完整的启动日志（`logs/`）、错误信息和分阶段耗时
* **不自动启动 Benchmark**，**不自动修改远程服务器**
* 就绪判定失败时会区分「进程还活着只是在慢慢启动」和「进程已经退出」——这两种情况的修复动作完全不同

实测：`start.ps1` 全程 **9 秒**返回（portal +5 s，隧道 +7 s，EvalScope 拉起 +9 s）。

`stop.ps1` 只停本项目启动的进程：它按 `run/*.pid` 取 PID，并且**在杀之前核对那个
PID 的命令行仍含预期标记**——被回收的 PID 不该让一个无关进程送命。

---

## 目录结构

```
llm-lab/
├── portal/            控制台：后端 server.py + 前端 web/（纯 stdlib + 原生 JS，无 CDN）
├── integrations/      适配器：strata / openai-compatible / feishu（接口设计）
├── scripts/           start / stop / check-env / install-evalscope / selftest / 远端接入引导
├── config/            llm-lab.toml（无密钥）+ secrets.env（gitignore）+ ssh/（gitignore）
├── data/
│   ├── benchmarks/    评测产物与 provenance.json
│   ├── metrics/       采集落点（规划中）
│   └── cache/         探针缓存
├── reports/
│   ├── deployment/    部署报告归档（保留原始版本与出处）
│   └── special-reports/  专项报告
├── monitoring/        只读采集设计（计划中用现有方案，不自研）
├── evaluation/        EvalScope 独立 venv
├── logs/              启动日志、服务 stdout/stderr
├── run/               PID 文件
└── docs/              架构、安全说明、飞书发布接口
```

---

## 依赖清单（实测版本）

| 组件 | 版本 / 位置 | 备注 |
|---|---|---|
| Windows | 10 Pro 19045 | |
| PowerShell | 5.1（Windows 自带） | 脚本**纯 ASCII**编写，见下方说明 |
| Python（控制台） | 3.12 | 控制台**只用标准库**，无第三方依赖 |
| Python（EvalScope） | 3.12，独立 venv `evaluation/.venv` | **不修改全局 Python** |
| EvalScope | **1.12.0**（`[service]` extra） | 提供 Dashboard，端口 9000 |
| OpenSSH 客户端 | Windows 自带 | 用 `IdentitiesOnly` / `BatchMode` / 项目内 `known_hosts` |
| 前端 | 无构建步骤 | 手写 SVG 图表，零 CDN、零 npm |

### 为什么 PowerShell 脚本是纯 ASCII

PowerShell 5.1 读取**不带 BOM** 的 `.ps1` 时按系统 ANSI 代码页（本机为 GBK）解码，
文件里的非 ASCII 字节会被读坏，症状是脚本报出与真实原因无关的解析错误。因此这些
脚本一律用 ASCII 编写。修改时请勿在其中加入中文。

---

## 配置与密钥

* `config/llm-lab.example.toml` — 端口、节点、模型端点、本地服务。**不含任何密钥**，且内容不
  参与 gitignore；可以自由复制修改。
* `config/secrets.env` — Strata API Key 等。**已 gitignore**，只由控制台**服务端**读取，
  永不进入浏览器、报告或日志。
* `{{LAB_SSH_KEY_PATH}}` — 独立 SSH 私钥。**已 gitignore**。

密钥的展示方式是「指纹」而非明文：控制台只显示 `sha256[:12]`，足以说明**加载的是哪把
钥匙**，但不泄露钥匙本身。

`[portal].startup_timeout_s`（默认 120）是**慢机器旋钮**，不是健康检查：在 D: 卷饱和的
工作站上，冷启动解释器可能远超一分钟才 import 并 bind 完成。

---

## 安全模型

1. **全部绑定 `localhost`。** `start.ps1` 在解析配置后会**拒绝**非 loopback 的 `host`：
   这个控制台没有鉴权，把它暴露到网络上等于把节点管理面交出去。
2. **key 从文件读，绝不作为参数传递。** 不落日志、不进异常消息、不进响应体。
3. **全项目只有一条远程命令**，是 `portal/server.py` 里的硬编码常量（只读的采集探针）。
   **不提供任意 shell 执行能力。**
4. **SSH 主机密钥固定（pinned）。** 用项目内 `known_hosts` + `StrictHostKeyChecking=yes`；
   自动化脚本**不接受**未经验证的主机密钥。
5. **不做遥测表演。** 遥测拿不到就如实显示故障原因，**不拿历史数据冒充实时指标**。

   **订正（2026-10-10）**：本机 NVML 因驱动/库版本不匹配不可用（`nvidia-smi` 失败），
   但这**不等于** GPU 指标不可用——Strata 自己是持有 CUDA 上下文的进程，它的
   `/metrics hardware` **给得出完整实时 GPU 数据**，控制台显示的就是它，
   并与 `/v1/status` 做过双源核对（偏差 `0.0%`）。详见 `docs/security.md` §5。

第 3、5 两条在本项目里是**可验证的**，不是声明：`scripts/selftest.py` 里有 **13 道安全
闸门**（路径穿越、绝对路径、`config/secrets.env` 原文与 URL 编码、SSH 私钥、`portal/server.py`
经 `/file` 与 `/download`、`/report` 指向源码文件、带 `../` 的 run 名、以及
`POST /api/service/start` 的无头/未知服务名两道），另有 **2 道阳性对照**，
用来证明「闸门返回 403/404」不是「路由整体坏掉」或「白名单什么都不放行」。
**当前实测：50 项检查全通过。**

---

## 验收状态（如实记录）

| # | 交付项 | 状态 |
|---|---|---|
| 1 | 建立项目与目录 | ✅ |
| 2 | README / 环境检查 / 配置模板 | ✅ |
| 3 | `start.ps1` / `stop.ps1` | ✅ 已实际运行；实测全程 **9 秒** |
| 4 | 统一 Web 入口 | ✅ 四组导航 + 首页状态带 |
| 5 | 接入 Strata 实时健康 | ✅ 已实测，模型 API / GPU / 主机 / 引擎**四块分列**且各带来源 |
| 6 | EvalScope Dashboard | ✅ 1.12.0，**按需启动**；实测 `down → starting → up` 迁移（3.1 s / 10.2 s） |
| 7 | 导入部署报告与评测结果 | ✅ 17 个产物 + provenance |
| 8 | 报告在浏览器打开与下载 | ✅ 已用真实浏览器验证 |
| 9 | 页面截图 | ✅ `docs/screenshots/` 8 张，取自真实运行中的页面 |
| 10 | Strata 原生 UI 集成 | ✅ 隧道 18081；三次 sha256 比对与直连**逐字节相同** |
| 11 | Grafana 方案 | ✅ **提案已交**（`docs/grafana-proposal.md`）；**未安装任何东西** |

**已知环境问题（非本项目缺陷）**：本机 D: 卷当前处于饱和（`%Idle` ≈ 0，队列 3–10），
导致冷启动任何位于 D: 上的解释器异常缓慢。实测：客户端预热后，控制台对
`/api/healthz` 的应答为 **51–84 ms**、TCP connect **1–8 ms**；而一次冷启动的测量
客户端自身耗时可达 **75 s**。**这类超时必须被描述为「慢」，不能被描述为「坏」**——
`start.ps1` 的就绪判定已按此修改。

---

## 下一步

1. **Grafana 是否落地**——决策点见 `docs/grafana-proposal.md` §9。
   我的建议是**先不装**：三块核心数据门户已在实时渲染，Grafana 的增量只有
   「长期历史 + 告警」，而本机**只剩 1.1 GiB 空闲内存**是硬约束。
2. **控制台的视觉重设计**——进行中。
3. 只读采集继续复用现有 SSH 探针，不自研。
