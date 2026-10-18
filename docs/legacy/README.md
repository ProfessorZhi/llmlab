# 旧 `llm-lab` 技术文档（已脱敏）

本目录是已退役私有仓库 `llm-lab` 的 `docs/` 的脱敏副本，仅作设计参考，
**不是** `llm-lab-portal` 的现行文档。现行文档在仓库根 `docs/` 下。

| 文件 | 内容 |
|---|---|
| [`project-overview.md`](project-overview.md) | 旧仓库根 README：项目定位、职责划分、快速开始 |
| [`architecture.md`](architecture.md) | 组件图、两条只读通道、适配器契约、端口、一次页面加载的数据流 |
| [`security.md`](security.md) | 五条安全规则、可验证的安全闸门、对节点做过的那一处变更的披露 |
| [`design-system.md`](design-system.md) | 控制台的视觉与交互规范 |
| [`grafana-proposal.md`](grafana-proposal.md) | Grafana 看板提案 |
| [`feishu-publish-interface.md`](feishu-publish-interface.md) | 飞书「专项汇报」发布接口的设计（**本阶段不写入**） |
| [`dell-migration/`](dell-migration/) | 节点迁移的六个阶段记录（phase1–phase6） |
| [`pr-history.md`](pr-history.md) | 旧仓库 PR #2 / #12 / #16 / #17 / #20 的去向 |
| `screenshots/` | 已核验可公开的页面截图 02–07 |

## 脱敏

主机名、地址、端口、绝对路径、账户名与组名均已替换为 `{{PLACEHOLDER}}`
占位符，占位符表见 [`../../deploy/templates/README.md`](../../deploy/templates/README.md)。

未迁入的内容：

* `config/ssh/*` —— 主机指纹与公钥属于主机身份信息，**不放入**。
* `docs/screenshots/01-portal-home.png` —— 画面含节点地址与账户名，**不放入**。
* `docs/screenshots/08-evalscope-dashboard.png` —— 画面含工作站本地路径，**不放入**。
* 旧 PR #17 的请求正文记录 —— **不并入**，仅在 `pr-history.md` 记录其设计约束。

## 出处分界

| 本目录文件 | 原仓库路径 |
|---|---|
| `project-overview.md` | `README.md`（根） |
| `README.md` | `docs/README.md` |
| `architecture.md` / `security.md` / `design-system.md` / `grafana-proposal.md` / `feishu-publish-interface.md` | `docs/` 下同名文件 |
| `dell-migration/phase1–3` | `docs/dell-migration/` |
| `dell-migration/phase4–6` | 旧 PR #20 的 `docs/dell-migration/` |
| `screenshots/02–07` | `docs/screenshots/` |
