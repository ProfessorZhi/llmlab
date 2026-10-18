# Atelier V6 · 旧 LAN 站功能等价矩阵（Legacy parity matrix）

> Lane Release/Legacy #50 交付物 · 2026-10-16 · 工单 [#50](https://github.com/ProfessorZhi/llm-lab-portal/issues/50) · 总控 [#43](https://github.com/ProfessorZhi/llm-lab-portal/issues/43) · 审计 [PR #42](https://github.com/ProfessorZhi/llm-lab-portal/pull/42)
>
> **方法**：对旧仓库 `ProfessorZhi/llm-lab` 做**只读**审计（不修改、不合并、不继承其历史，见 `access-boundaries.md` §5），逐条列出能力、旧版实现位置、V6 对应物、可复现证据与判定。旧版行号取自审计代理的实际读取，并已抽检复核（`server.py` 路由行、`models.js` 卡片函数行、`labctl.py` 子命令行）。
>
> **状态语义**（每行必居其一，不得留空）：
> - **READY** —— V6 有等价能力，数据源可信，且有可复现测试证据。
> - **PARTIAL** —— V6 有对应实现，但存在已知缺口（数据源未受信、仅只读、缺导出、或待接线）。
> - **BLOCKED** —— 无等价实现，或被 P0 缺口（#34 匿名数据 / #45 可信身份 / #49 同源 BFF）挡住。
> - **UNKNOWN** —— 仅凭现有源码无法判定；**不等于通过**。
>
> **一句话结论**：**没有任何一行是 READY。** 旧站今天能用的每一项，在 V6 侧要么仍是 PARTIAL（界面在、数据源未受信），要么直接 BLOCKED（如服务控制）。这正是发布判据保持 NO-GO 的原因（见 `atelier-v6-release-checklist-2026-10-16.md`）。

---

## 1. 能力对照表

| ID | 旧 LAN 能力 | 旧版证据（file:line） | V6 对应（API / UI） | 测试 / Issue | 状态 |
| --- | --- | --- | --- | --- | --- |
| L01 | Strata 引擎/模型 serving 卡（模型 id、引擎态、tok/s、队列、加载数、端点、密钥指纹） | `portal/web/views/models.js:60` servingCard；`portal/server.py:1627` strata_console；`/api/v2/live` `server.py:2792` | Overview「模型服务一览」(`web/index.html` view-overview)；`/api/v2/ops/engines` (`backend/bff/app.py:81`) | 待接线：[#46](https://github.com/ProfessorZhi/llm-lab-portal/issues/46) | PARTIAL |
| L02 | 逐模型状态/上下文表 + 引擎配置表 + tok/s 折线 + 近期请求表 | `models.js:160` modelsBlock、`models.js:186` engineConfigBlock、`models.js:243` paintThroughput | Models 页「引擎与部署」(`web/js/modules/`) | 待接线：[#46](https://github.com/ProfessorZhi/llm-lab-portal/issues/46) | PARTIAL |
| L03 | GPU 实时指标（利用率/温度/功耗/VRAM）含双源交叉校验 | `portal/web/views/compute.js:53` gpuBlock；`integrations/strata.py:272` cross_check | Compute 子页 (`web/js/compute/view.js`)：仅在 `nvml-verified` 来源下出数，否则 **UNKNOWN** | `tests/compute-view.test.js`；[#38](https://github.com/ProfessorZhi/llm-lab-portal/issues/38) | PARTIAL |
| L04 | GPU/主机的**历史曲线**（时间序列，节点侧环形缓冲或本地采样） | `compute.js:144` histSeries、`compute.js:201` trendBlock；`server.py:649` SAMPLE_BUFFER（**真实**节点侧环形缓冲） | Overview TELEMETRY 折线 (`web/js/timeline.js`)：`TIMELINE_SERIES` 是**合成 24 点**（`timeline.js:8` 注明 "DEMO ONLY"），图例写 `DEMO · 24 POINTS`（`web/index.html:31`），但该图在 LIVE 模式下**不被隐藏**——它不在 `?demo=1` 闸门后。主机新鲜度走 `/api/infra/summary` | `tests/compute-view.test.js`；[#38](https://github.com/ProfessorZhi/llm-lab-portal/issues/38) | PARTIAL |
| L05 | 受控服务状态表（本地 TCP 连接探测）+ 端口占用 | `server.py:1179` probe_services、`server.py:1204` probe_local_ports；`compute.js:215` servicesBlock | Overview「服务基础设施」；`/api/v2/ops/stacks`（`backend/bff/stacks.py`） | [#47](https://github.com/ProfessorZhi/llm-lab-portal/issues/47) | PARTIAL |
| L06 | 主机事实（内存/负载/磁盘）经**固定只读 SSH 探测**取得，主机密钥 pin | `server.py:718` REMOTE_PROBE、`server.py:803` probe_ssh、`server.py:759` verify_pinned_fingerprint | `/api/infra/summary` (`source=prometheus-readonly`, `readonly=true`) | [#47](https://github.com/ProfessorZhi/llm-lab-portal/issues/47) | PARTIAL |
| L07 | Benchmark 运行列表 + 详情（throughput/concurrency/quality/cold_start/smoke 五维） | `server.py:464` list_runs、`server.py:491` run_detail、`server.py:1758` evaluation_summary；`views/benchmark.js:332` | Evaluation 页「EvalScope 集成」(`web/index.html` view-evaluation) | `docs/evalscope-center.md`；[#48](https://github.com/ProfessorZhi/llm-lab-portal/issues/48) | PARTIAL |
| L08 | Benchmark **导出**：CSV（bench/concurrency/quality/files）+ 整目录 ZIP | `server.py:2772` `/api/export/csv`、`server.py:2775` `/api/export/zip`、`server.py:3028` `_export_csv` | **无等价端点**：V6 侧未见对应导出路由（前端 fetch 面只有 `/api/atlas/*`、`/api/infra/summary`、`/api/v2/ops*`） | [#48](https://github.com/ProfessorZhi/llm-lab-portal/issues/48) | BLOCKED |
| L09 | 报告中心：递归扫描 + 类型/文本筛选 + 统计 | `server.py:599` list_reports；`views/reports.js:61` filtersBand、`reports.js:107` | **无独立报告页**：九页中无 reports | [#34](https://github.com/ProfessorZhi/llm-lab-portal/issues/34)、[#47](https://github.com/ProfessorZhi/llm-lab-portal/issues/47) | BLOCKED |
| L10 | 报告预览（md→HTML）、原文查看、**下载附件** | `server.py:3003` `_report`、`server.py:3017` `_file`、`server.py:332` md_to_html | Atlas 可导出 CSV/JSON（`web/js/atlas/exporters.js`，文件名带 `DEMO` 标记），**不是**报告等价物 | [#34](https://github.com/ProfessorZhi/llm-lab-portal/issues/34) | BLOCKED |
| L11 | 外部集成面板（Strata/Grafana/DeepSeek/Production/Feishu/EvalScope 逐项**真实探测** + 证据原文） | `server.py:2472` v2_integrations、`server.py:2441` _feishu_probe；`views/integrations.js:79` itemCard | `/api/v2/ops/stacks`（`backend/bff/stacks.py`） | [#46](https://github.com/ProfessorZhi/llm-lab-portal/issues/46)、[#47](https://github.com/ProfessorZhi/llm-lab-portal/issues/47) | PARTIAL |
| L12 | 设置：主题、刷新策略（只读展示）、端点凭据**指纹**（绝不回显密钥） | `server.py:2635` v2_settings、`server.py:2231` _endpoint_credential；`views/settings.js:116` endpointsCard | Admin 页「身份与权限 / 权限边界」 | [#45](https://github.com/ProfessorZhi/llm-lab-portal/issues/45) | PARTIAL |
| L13 | 设置：路径与端口占用（occupied/free）、服务与隧道定义 | `server.py:1204` probe_local_ports；`views/settings.js:183` pathsPortsCards | Admin 页（`web/index.html` view-admin） | [#47](https://github.com/ProfessorZhi/llm-lab-portal/issues/47) | PARTIAL |
| L14 | **服务控制**：`labctl status/start/stop/disable/enable/autostart`，经 `os.killpg`/`Popen` 管理本账号进程 | `deploy/dell/labctl.py:188` status、`:197` start/stop/disable/enable、`:201` autostart；`server.py:2008` start_service、`server.py:1862` stop_service | **有意不提供。** V6 侧无任何页面或 API 可启动/停止服务；`backend/bff` 只读 | 见 §3「不得迁移」 | BLOCKED |
| L15 | 首页总览（hero、告警、四块实时磁贴、GPU 曲线、最新 benchmark、最近报告） | `views/overview.js:36` hero、`overview.js:106` liveTiles、`overview.js:192` benchmarkBlock、`overview.js:267` reportsBlock | Overview 页（`web/js/features/`、`web/js/infra/host-status.js`） | `tests/shell.test.js` | PARTIAL |
| L16 | 旧版**导航/路由不一致缺陷**：服务端 nav 发 `#bench`/`#env`/`#home`，而路由表无这些 id，全部回落到 overview | `server.py:2967` `_nav`；`portal/web/core/router.js:20` ROUTES 表、`:49` parseHash 回落 | V6 路由表 `web/js/router.js` PAGES/SUBPAGES 与 `pageFromHash()` 回落一致，且兼容旧 `#/compute` 深链 | `tests/shell.test.js`（八条主路由 + Compute 子页未被改动） | READY |

> 关于 L16：「READY」指的是 **V6 不再重复旧站的这一缺陷**，不是指某个业务能力已上线。它是本表中唯一一行 READY，且它是**缺陷不复现**，不是功能交付。

---

## 2. 逐项要点（为什么多数是 PARTIAL 而不是 READY）

### 2.1 数据源可信度尚未建立

旧站的后端与它展示的数据**在同一台主机、同一个进程所有权内**：`portal/server.py` 直接读本机 `ss`/`/proc`、直接连本机 Strata `/health`+`/metrics`、直接扫本机数据目录。V6 的同源 BFF 还没有把这条链建立起来（[#49](https://github.com/ProfessorZhi/llm-lab-portal/issues/49)），因此 V6 侧即便界面存在，也只能：

- 在**未受信**来源下显示 `UNKNOWN`（例如 `web/js/compute/view.js` 只在 `nvml-verified` 来源下出 GPU 数）；
- 或依赖 `?demo=1` 的**合成**样本（明确标记为演示，不是遥测）。

这两者在矩阵里都记 PARTIAL，**不记 READY**——"界面能画出来"不等于"数据是这台机器的真数据"。

### 2.2 导出与报告几乎整体缺失（L08–L10）

旧站有完整的 CSV/ZIP 导出与报告预览/下载。V6 的三处导出都不是等价物：

| 旧站 | V6 | 差异 |
| --- | --- | --- |
| `/api/export/csv?run=&kind=`（4 种 kind，UTF-8 BOM） | 无 | 端点不存在 |
| `/api/export/zip?run=`（整目录） | 无 | 端点不存在 |
| `/report` 预览 + `/download` 附件 | Atlas CSV/JSON（`exporters.js`） | 内容不同：Atlas 导出的是**图谱切片**，文件名显式带 `DEMO` |

这些是**发布阻断**（L08/L09/L10 = BLOCKED），因为"旧站退役"要求逐项等价（判据 H6）。

### 2.3 服务控制必须**有意不迁移**（L14）

`labctl` 能在本账号内启动/停止服务，并且**也**能停止受控的手动启动项（`server.py:1837` 的 `portal_managed=false` 闸门只约束控制台，不约束 `labctl`）。把这条能力搬到 Web 上，等价于给 V6 增加一个远程服务控制面；在可信身份（[#45](https://github.com/ProfessorZhi/llm-lab-portal/issues/45)）与同源 BFF（[#49](https://github.com/ProfessorZhi/llm-lab-portal/issues/49)）落地之前，这是**必须保持 BLOCKED** 的一项。本 Lane 的判定是"不迁移"，而不是"待实现"。

---

## 3. 不得迁移到 V6 的旧站行为

以下行为在旧站**存在**，但迁移即引入缺陷或越权，故显式列为"不迁移"：

| 旧站行为 | 出处 | 为什么不能进 V6 |
| --- | --- | --- |
| 所有 `GET` 路由**完全匿名**（无 auth、无 session、无 API key） | `server.py:2728` `do_GET` 无任何校验 | 旧站靠"只绑环回 + 前置反代 basic_auth"兜住。V6 若在任何非环回入口复用这个面，等于把匿名数据 API 暴露出去（[#34](https://github.com/ProfessorZhi/llm-lab-portal/issues/34)） |
| 写操作只靠 `X-Requested-With` 头 | `server.py:2829`、`server.py:2851` | 这是同源防护，**不是认证** |
| 服务端导航产出路由表里不存在的 id | `server.py:2967` `_nav` vs `router.js:20` | 旧站缺陷（L16），V6 已不再复现 |
| 报告/文件路径由查询参数直接给出 | `server.py:2763` `/report?p=`、`server.py:2766` `/file?p=` | 旧站有 allowlist+commonpath 防线（`server.py:1348` `_resolve_public`），但形态本身是"参数即路径"。V6 应使用受权对象标识而非路径 |

---

## 4. 新旧信任边界摘要

完整对照见 `atelier-v6-legacy-trust-boundary-2026-10-16.md`。此处只记结论：

| 维度 | 旧 LAN 站 | V6 预览（当前） |
| --- | --- | --- |
| 进程绑定 | `127.0.0.1:8765`，**拒绝**非环回 host | `127.0.0.1:18765` |
| 对外暴露 | Tailscale/LAN → 反向代理，**每路由 basic_auth**，剥离客户端身份头 | Tailscale HTTPS 8443 → sidecar（Basic Auth + Host allowlist，非白名单 Host 返回 421）→ 预览 |
| 身份 | 代理层单一共享口令，**非用户身份** | 同样**非用户身份**；Linux 会话未建立（登录表单 fail-closed） |
| 授权 | 代理后所有远程用户等价 | 同上 |
| 密钥处理 | 只回显 `key_present` + `sha256[:12]` 指纹 | 同构：不回显密钥 |
| Host 校验 | 无（依赖代理） | `dev/preview_auth.py` 全串校验（Issue #8 修复），但**只适用于环回登录预览**，不适用于 `serve.mjs` 静态预览 |

**核心差异**：旧站的信任边界**在代理层**（"能过 basic_auth 就是自己人"），V6 目前**也还是**这样。真正要建立的是"每个 Linux 用户一个受信 Principal"，那属于 [#45](https://github.com/ProfessorZhi/llm-lab-portal/issues/45)/[#49](https://github.com/ProfessorZhi/llm-lab-portal/issues/49)，**本 Lane 不做**。

---

## 5. 本轮无法判定的项（UNKNOWN）

诚实地列出，避免"没写就是通过"：

| 项 | 为什么 UNKNOWN |
| --- | --- |
| 旧站 benchmark **提交/运行发起**能力 | 旧站 UI 只**选择/读取**已有 run；README 明确禁止自动启动 benchmark。是否存在未发现的 CLI 发起入口，未证实 |
| 旧站 `60-lifecycle.sh` 安装的 cron 行原文 | 未逐字核对；只确认其存在"@reboot + 每 5 分钟"两条 |
| V6 报告/集成/设置三项的**最终**端点形态 | 依赖 [#49](https://github.com/ProfessorZhi/llm-lab-portal/issues/49) 的同源 BFF 契约，现在写死会制造与 #49 的契约冲突 |
| V6 在**真实多用户**下的隔离 | 需要真实 Linux 身份与多账号，超出本 Lane 授权 |
| 旧站 `/api/v2/*` 与 V6 `/api/v2/ops*` 的字段级等价性 | 需要逐字段比对；本 Lane 只做到能力级 |

---

## 6. 复核方式（只读，可重放）

旧站（**只读**，不写、不合并不 push）：

```bash
# 列出旧站页面与后端入口
ls  portal/web/views/ portal/web/core/
grep -n 'path == "/api/' portal/server.py | head -30
grep -n 'add_parser' deploy/dell/labctl.py

# 旧站导出面
grep -n 'api/export' portal/server.py
```

V6：

```bash
node -e "import('./web/js/router.js').then(m=>console.log(m.ALL_ROUTES))"
grep -n 'PATH' backend/bff/app.py
grep -rhoE '/api/[a-zA-Z0-9/_.-]+' web/js | sort -u
```

两项复核都不需要网络、不需要浏览器、不启动服务、不触碰任何生产对象。
