# 架构

## 一句话

浏览器 → 本地控制台（Windows）→ 两个只读通道（Strata HTTP / 固定 SSH 探针）→
{{LAB_NODE}}。**没有反向的那条线**：节点不知道这个控制台存在，也不会因为它的启停而改变。

## 组件图

```
┌─ Windows 工作站 ────────────────────────────────────────────┐
│                                                             │
│  browser ──HTTP/localhost:8765──┐                           │
│                                 │                           │
│                     ┌───────────▼────────────┐              │
│                     │ portal/server.py       │              │
│                     │ ThreadingHTTPServer    │              │
│                     │ 纯标准库                │              │
│                     └───┬──────────────┬─────┘              │
│                         │              │                    │
│         ┌───────────────▼──┐   ┌───────▼─────────────────┐  │
│         │ integrations/    │   │ 固定 SSH 只读探针        │  │
│         │  strata.py       │   │ (server.py 里的常量)     │  │
│         │  openai_compat…  │   │ subprocess → OpenSSH     │  │
│         │  feishu.py(设计)  │   │ 项目内 known_hosts + pin │  │
│         └───────┬──────────┘   └───────┬─────────────────┘  │
│                 │                      │                    │
│  portal/web/  ◄─┘  index.html / app.js / core/ / views/ /   │
│                    styles/  零 CDN、零构建、手写 SVG 图表      │
└─────────────────┼──────────────────────┼────────────────────┘
                  │ HTTP :8080           │ SSH :22
        ┌─────────▼──────────────────────▼─────────┐
        │ {{LAB_NODE}} ({{LAB_HOST}})     │
        │  Strata v0.1.41 / Qwen3.8-Flash-Next     │
        │  RTX A6000 48GB / IQ2_XS / 32K / par=1   │
        │  ← 只读。不重启、不写文件、不改参数        │
        └──────────────────────────────────────────┘
```

## 为什么控制台只用标准库

`portal/server.py` 不依赖任何第三方包。这不是清高，是为了 **Windows 关机不影响节点**
这条硬约束在运维上成立：

* 没有依赖 ⇒ 没有 `pip install` 会失败的现场，没有锁文件漂移，没有「三个月后跑不起来」；
* 控制台的解释器可以随手换成任何一个 3.11+ 的 Python；
* **评测环境的依赖爆炸被隔离在 `evaluation/.venv` 里**，不会传染到控制台。

前端同理：手写 SVG 图表，没有 npm，没有构建步骤，没有 CDN。离线可用。

## 两条只读通道

### 1. Strata HTTP（`integrations/strata.py`）

四个端点的访问规则**不一样**，把它们当成一样会产生「把健康节点报成故障」的控制台：

| 端点 | 是否需要 key | 提供什么 |
|---|---|---|
| `/health` | 否 | 开放的存活状态，并声明 `api_key: true\|false` |
| `/v1/models` | 视部署 | 所服务的模型、上下文长度、加载状态 |
| `/v1/status` | 视部署 | `machine.gpu` 与 `machine.ram` ——**本机唯一的实时 GPU 遥测通道** |
| `/v1/chat/completions` | 视部署 | 推理。**控制台加载页面时绝不调用它** |

`/v1/status` 为什么特殊：本机 NVML 因驱动/库版本不匹配而不可用（`nvidia-smi`
和一切基于 NVML 的工具都失败），但 CUDA 运行时本身仍能正常初始化。所以
`machine.gpu` 与 `/metrics hardware` 是**唯一**两条实时 GPU 来源，而它们互相独立。
控制台不推断、不猜测，拿不到就显示拿不到。

`api_key` 是否必需由 `/health` 自己声明的标志决定，**不是猜的**。

### `/metrics` 给什么，以及它为什么重要

`/metrics` 返回 **`application/json`**（不是 Prometheus 文本格式，这一点在
`docs/grafana-proposal.md` §2.2 有直接后果）。顶层有十块：

```
engine, live, requests, conversation_cache, requests_kept,
totals, hardware, hardware_static, history, time
```

* `hardware` —— GPU（util / 显存 / 温度 / 功耗 / PCIe）+ 主机 CPU/RAM/磁盘
* `live` / `totals` —— 推理引擎的当前吞吐与累计量
* `history` —— **节点侧环形缓冲**（`gpu_util`、`gpu_temp`、`cpu`、`tok_s` …）

**双源核对**：`/metrics hardware.gpu_mem_used` 与 `/v1/status machine.gpu` 是两个
独立端点对同一件事的答复。实测 40050.6 MiB vs 40051.0 MiB，偏差 `0.0%`。
门户把这两个数并排显示——**差异本身就是信号**，一致才敢说「这条读数可信」。

### 2. 固定 SSH 只读探针（`portal/server.py` 的 `REMOTE_PROBE`）

一条**硬编码常量**，只读 `/proc`、`df`、`ss`、`pgrep`、`systemctl is-active`、
`nvidia-smi`。它是**全项目唯一的远程命令**。

它存在的原因：`systemctl is-active strata` 返回 `inactive`，而 `:8080` 明明在应答。
与其推断，不如把**事实**取回来——探针因此额外采集：

* `ss -ltn` 的监听端口 → 得到 `LISTEN {{LAB_BIND_ADDR}}:8080`
* `pgrep -af strata` → 得到真实的进程命令行
  （`{{STRATA_ROOT}}/.venv/bin/python -u {{STRATA_ROOT}}/serve/server.py --engine strata …`）

控制台据此显示「**端口监听才是事实来源**」，而不是拿一个 systemd 单元名当真相。

## 适配器契约（`integrations/__init__.py`）

每个适配器都是**面向网络的、以读为主**的客户端，必须遵守四条：

1. **不用 shell。** 只讲 HTTP 或 SFTP；不拼命令串，不接受调用方给的命令串。
2. **凭据来自文件，绝不来自参数。** 适配器拿到的是密钥文件的*路径*。
3. **不做任何变更。** 不重启服务、不写节点上的文件、不改运行参数。
4. **不可用就如实上报**，不用缓存值或历史值冒充当前值。

`KINDS` 注册表把配置里的 `kind = "..."` 映射到类；**未知 kind 会响亮地失败**，
而不是静默产生一个永不答复的端点。

## 端口

| 端口 | 用途 | 归属 |
|---|---|---|
| 8765 | 控制台 | 本项目 |
| 18081 | Strata 原生 UI 隧道 → 节点 `localhost:8080` | 本项目（ssh） |
| 9000 | EvalScope Dashboard | 本项目（按需拉起） |
| 3000 | Grafana | 规划中，**未安装** |
| 8080 | Strata API | **节点，不是本项目** |

全部本地服务绑定 `localhost`。`start.ps1` 在解析配置后会**拒绝**非 loopback 的
`host`——这个控制台没有鉴权，暴露到网络上等于把节点管理面交出去。

> **端口选择不是随意的。** 18080 在本机已被一个**对任意路径都回 200** 的无关服务占据，
> 对它的「健康检查」会回答正常而其实在服务别的东西。18081 是实测为空的。
> 本项目有一处直接教训：**只按状态码判断 SPA 会得到假阳性。**

### 为什么有隧道，而直连也没坏

节点的 `:8080` 从本机是**直连可达的**。仍然建隧道，是为了：

* 得到一个**不随工作站换网而变**的稳定入口；
* 把节点的原生控制台放在一个**不离开本机**的源上。

直连 URL 作为带标注的 fallback **保留而非删除**——假装隧道是唯一通路是不实的，
今天两条都通。隧道状态被拆成**三个各自独立的事实**上报：

| 字段 | 含义 |
|---|---|
| `listening` | 端口有没有人在应答 |
| `pid_alive` | 我们记的 pid 是否还活着 |
| `owned` | **两个都真**，才算「我们的隧道是通的」 |

分开报是因为它们在实际中会不一致：**端口有应答但 pid 已死 ⇒ 是别人的东西占着这个端口**，
这既不该报成「隧道正常」，也不该报成「服务挂了」。

## 数据流：一次页面加载

1. 浏览器取 `/`，拿到 `index.html`；再取 `/assets/app.js`，然后按路由**懒加载**
   `/assets/views/<id>.js`（七个视图：overview / models / benchmark / compute /
   reports / integrations / settings）。样式是 `styles/` 下的五张表
   （tokens → base → layout → components → charts），**先于脚本解析**，所以首屏不闪白。
2. `app.js` 只做一件事：起 shell。它启动**三个共享轮询源**（`core/sources.js`）——
   `/api/v2/live`（20 s）、`/api/v2/overview`（60 s）、`/api/lab`（一次）——
   视图只 subscribe / unsubscribe，**没有任何视图 start 或 stop 它们**：
   health pill 在每一页都要这个数据，被离开的视图不等于没人要的数据。
3. 服务端把三者拼成一份 JSON。**API key 始终留在服务端**，前端只拿到模型名、
   健康标志和密钥指纹（`sha256[:12]`）；SSH 私钥只以**路径**形式出现。
4. 每个视图用手写 SVG 画图（`core/chart.js`）：KPI 磁贴、sparkline（**只用本次会话
   收集到的采样**，不从历史数据伪造曲线）、柱状对比（2px 间隙、圆角数据端、置灰网格线）、
   gauge / donut，以及在 `details.table-view` 里的**表格孪生体**——每张图都有一条
   可读的等价文本路径，这是无障碍要求，也是「图被误读时能立刻核对」的运维要求。

### v2 信封：HTTP 200 不是健康声明

`/api/v2/*` 一律返回 HTTP 200，判定权交给**信封本身**：
`{ok, data, error, ts, source, partial}`。`ok:false` 也走 200，因为「服务端答了，
但上游答得不对」和「路由不存在」是两件事——只有后者是 404。

之所以这么做，是因为在这台机器上 200 确实不能当健康声明：`:18080` 上另一个无关
服务对**任意路径**都回 200，EvalScope 的 SPA fallback 也一样。所以后端用
`_looks_like(payload, required_keys)` 做**结构**判据：解析得开但形状不对，照样
`ok:false`，并在 `error` 里说清楚是哪个键缺失。裸看状态码的实现会把别人的页面
当成我们的数据渲染出来。

### EvalScope 的三态，而不是两态

`evalscope_state()` 返回 `up` / `starting` / `down`。中间那一档是**必要的**：
EvalScope 在 bind 端口之前要 import 好几分钟，所以「端口没在听」必须再分成
「还活着，在启动」和「已经没了」。旧实现把两者混为一谈，于是把一个正在正常启动的
服务报成故障——两件事的修复动作完全相反（等 vs 救）。

实测的一次真实迁移：`down` → 点「启动」→ `starting`（t=3.1 s，pid 活着、端口未绑）
→ `up`（t=10.2 s）。

## 相关文档

* `docs/security.md` —— 安全模型的细节与验证方式
* `docs/grafana-proposal.md` —— Grafana 接入方案（**提案，未实施**）
* `docs/feishu-publish-interface.md` —— 飞书发布接口的设计
* `README.md` —— 快速开始、依赖清单、验收状态
