# Dell LLM Lab Portal · Atelier V6

> **V1 production execution addendum (2026-10-17):** [target architecture, backend integrations, security, multi-agent work breakdown, Linux Fleet placement, hourly orchestration and acceptance gates](docs/v1/README.md). These are target documents, **not** assertions of Dell live wiring or production deployment. [Dispatch #80](https://github.com/ProfessorZhi/llm-lab-portal/issues/80).

这是面向共享 Dell GPU 工作站的**单节点、多 Linux 用户 LLM 实验室管理后台**。目标是让 Web 与 `llmlab` CLI 使用同一受信身份、Registry / Endpoint 授权和 Gateway 调用事实，完成模型登记、验证、共享、调用、计量、可观测性、评测与审计。

> **架构总入口：[`docs/architecture.md`](docs/architecture.md)** — 完整的产品需求、九页信息架构、组件/信任边界、数据模型、API、发布阶段和验收标准。  
> **状态（2026-10-16）：** V6 有可访问的静态预览与主机只读遥测；[Phase B #60](https://github.com/ProfessorZhi/llm-lab-portal/pull/60) 已提供七 Lane **CPU-only 集成候选**，但它不等于正式部署。真实 PAM、Gateway、Chrome、CI 与业务端到端验收仍为 NO-GO。不得把演示状态或已存在的代码模块写成“生产已上线”。

本仓库与 [`ProfessorZhi/llm-lab`](https://github.com/ProfessorZhi/llm-lab) **并列**，并非对旧仓库的历史重写。现有 Strata/Qwen、Gateway、旧 LAN Portal、Caddy、GPU 作业和其他用户的数据目录均维持各自权限和运行边界。

## 1. 产品范围

| 页面 | 核心用途 |
| --- | --- |
| Overview | 实验室资源与服务总览、当前主体可见模型、可信指标 |
| Compute（Overview 子页） | GPU/显存/温度/功耗、主机、服务与资源历史 |
| Models & Chat | Engine/Model/Deployment/Endpoint、共享和受权真实对话 |
| API Management | Endpoint、Key 生命周期、SDK、Playground |
| My Calls | 个人调用 metadata、筛选、请求 ID、延迟和 Token |
| Observability | 主机/Gateway/用户级指标及其来源、新鲜度与历史 |
| Traffic Atlas | 可授权的模型/项目/工作负载构成、热图和导出 |
| Evaluation | EvalScope 与旧 Benchmark 分源报告、评测与权限 |
| Administration | 会话、scope、临时管理员模式、审计和配置状态 |

导航为 **8 个一级页面 + Compute 子页**。页面壳已有，但**内容齐全、真实操作与跨 UID 验收另由 [Issue #56](https://github.com/ProfessorZhi/llm-lab-portal/issues/56) 跟踪**。

## 2. 技术分层

```text
Web / CLI / SDK
    ↓ Tailnet HTTPS / Caddy（受控入口）
同源 Portal BFF ── Auth / Linux PAM Broker / Scoped Token
    ├── Registry / Owner / Endpoint ACL / Router ACK
    │       └── 受信 Gateway → 既有 Strata / Engine
    ├── Calls / Atlas / Observability ← 受信 Gateway metadata
    ├── Prometheus / Grafana（只读主机事实）
    └── Evaluation / EvalScope / Legacy Benchmark（UID 授权）
```

**浏览器不直连**模型原始 `:8080`、Gateway `:8200` 等内部端口；不从请求自报字段判定 UID、Owner 或指标来源。真实 API 的身份、授权、数据来源、采样时间和错误状态由服务端证明。

Registry 的**注册、验证、共享、路由已生效**属于不同状态。默认 Private、撤权下一次调用生效；重指向的 `revision` 需要 Gateway 的 ACK。个人调用日志不默认保存 prompt/response 正文；UNKNOWN 不得展示为 0，Synthetic 不得展示为 LIVE。

## 3. GitHub 交付与当前证据

| 交付面 | 关联 |
| --- | --- |
| 主跟踪 / 产品 DoD | [#43](https://github.com/ProfessorZhi/llm-lab-portal/issues/43) |
| 七个独立业务 Lane | Draft PR [#51](https://github.com/ProfessorZhi/llm-lab-portal/pull/51)、[#52](https://github.com/ProfessorZhi/llm-lab-portal/pull/52)、[#53](https://github.com/ProfessorZhi/llm-lab-portal/pull/53)、[#54](https://github.com/ProfessorZhi/llm-lab-portal/pull/54)、[#55](https://github.com/ProfessorZhi/llm-lab-portal/pull/55)、[#57](https://github.com/ProfessorZhi/llm-lab-portal/pull/57)、[#58](https://github.com/ProfessorZhi/llm-lab-portal/pull/58) |
| 集成候选、合成证据 | [#59](https://github.com/ProfessorZhi/llm-lab-portal/issues/59) · [Draft PR #60](https://github.com/ProfessorZhi/llm-lab-portal/pull/60) |
| 九页完整业务工作台 | [#56](https://github.com/ProfessorZhi/llm-lab-portal/issues/56) |
| 旧站功能等价及发布 | [功能矩阵](docs/atelier-v6-legacy-parity-matrix-2026-10-16.md) · [发布检查单](docs/atelier-v6-release-checklist-2026-10-16.md) |

Phase B 评审候选 `c941cd3` 的本地 CPU 测试记录：Python `3320` 项（`6` skipped）、Node `471` 项（`470` passed、`1` skipped），V5/V6 derive 与 extract 校验通过。**这不是 GitHub CI 绿灯、Chrome E2E 或正式 Gateway/PAM 验收。** UI PR #54 在候选之后有新提交，后续需重新合成并复测。

## 4. 主要文档

- **[产品与系统架构](docs/architecture.md)** — 权威总览、目标拓扑、九页产品、角色、资源生命周期及各阶段验收。
- **[V6.2 生产接口与数据契约总目录](docs/integrations/README.md)** — Dell/Strata/Prometheus/Grafana/Gateway/EvalScope 能力清单、九页真实数据需求、Linux 管理员 ADR、机器可读接口登记，以及 Claude 不连接 A6000 的无虚构数据开发工作流。
- **[Claude Code 前端改版交接包](docs/frontend/README.md)** — 当前 V6.2 Apple 外观；冻结 V5 保留页面与 Atlas 结构，国考交互与 V3 可读性供参考；前端完整开发须遵守上述生产数据契约。
- [访问边界](docs/access-boundaries.md) · [Linux PAM 契约](docs/linux-pam-login.md) · [临时管理员模式](docs/v6-shared-admin-mode.md)。
- [Registry 与 CLI](docs/registry-cli-contract.md) · [BFF 集成](docs/bff-integration-lane49-20261016.md)。
- [Calls](docs/portal-call-log-contract.md) · [Atlas](docs/atlas-data-contract.md) · [Observability](docs/observability-contract.md)。
- [Evaluation](docs/evalscope-center.md) · [EvalScope/Legacy 分源报告](docs/evaluation-report-sources-contract.md)。
- [Phase B 集成证据](docs/atelier-v6-phase-b-candidate-2026-10-16.md) · [发布与回滚检查](docs/atelier-v6-release-checklist-2026-10-16.md)。

## 5. 开发与测试（非部署）

在已配置的隔离开发分支上执行，不访问真实密码/Key，不启停生产服务：

```bash
python3 -m unittest discover -s tests -t .
npm test
node prototypes/v6/derive-from-v5.mjs --check
npm run verify:extract
```

`prototypes/v5/index.html` 是冻结设计权威；V6 由 `prototypes/v6/additions.*` 派生，`web/index.html` 和 `web/css/*.css` 必须由生成器写入。不能为绕过设计/CI 校验而手工修改生成产物。任何 Chrome/实机测试未执行项应明确写 **SKIPPED / NOT VERIFIED**。

## 6. 数据与生产边界

- **严禁提交：** Linux 密码、真实 Token/API Key、密钥文件、模型权重、真实 `usage.jsonl`、用户 prompt/completion、私人报告原文或敏感附件。
- **运行归属独立：** 本仓库源码和 PR 不授权修改其他用户 HOME、root/PAM、Caddy、systemd、Strata/Gateway 进程、GPU 作业或旧 LAN Portal。
- **发布与回滚单独审批：** 不从开发工作树直接覆写正式服务；固定 SHA、最小权限、健康检查、可读备份、版本回滚及真实多用户 E2E 全部通过后，才可考虑替换旧站。

**当前 Release GO/NO-GO = NO-GO。** 该状态只能由真实验收证据改变，不能由 README、Mock 测试或 PR 数量改变。
