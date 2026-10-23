# Atelier V6 发布检查单与 GO/NO-GO（Release checklist）

> Lane Release/Legacy #50 交付物 · 2026-10-16 · 总控 [#43](https://github.com/ProfessorZhi/llm-lab-portal/issues/43) · 审计 [PR #42](https://github.com/ProfessorZhi/llm-lab-portal/pull/42) · 本 Lane 工单 [#50](https://github.com/ProfessorZhi/llm-lab-portal/issues/50)
>
> **本 Lane 的当前结论是 NO-GO。** 下文的 GO/NO-GO 是**判据**，不是结果。判据全部满足之前，任何一方都不应把 Atelier V6 描述为"可发布"。

---

## 0. 最重要的两条

1. **代码合并 ≠ 部署。** 合并本 Lane 的 PR 只意味着"文档与 CI 门禁进入了集成分支"，不意味着任何主机上发生了部署，也不意味着发布了新版本。
2. **门禁绿灯 ≠ 生产 READY。** 本 Lane 的 CI 是 CPU-only 的静态与单元门禁，**没有**验证真实主机、真实身份、真实模型调用、真实并发用户或真实回滚。绿色只说明"没有已知的静态缺陷"。

---

## 1. 发布判据（GO / NO-GO）

只有下列**全部**为真才可判 GO。任一项为假即 NO-GO，并记录在 §5 的阻断表里。

### 1.1 必须由 CI 证明（可自动化）

| # | 判据 | 证据 | 现状 |
| --- | --- | --- | --- |
| G1 | V5 冻结设计源未被改动 | `sha256(prototypes/v5/index.html)` = `04003f5c…5c7bf` | 本地与 CI 均通过 |
| G2 | V6 确由 V5 + additions 导出，无手工漂移 | `node prototypes/v6/derive-from-v5.mjs --check` | 本地与 CI 均通过 |
| G3 | `web/` 确为生成物，无手工漂移 | `node web/tools/extract-from-prototype.mjs --check` | 本地与 CI 均通过 |
| G4 | 生成链路只读，检查后工作树干净 | 门禁步骤失败即工作树被写入 | 通过（本地实测 `git status` 为空） |
| G5 | Python 回归全绿 | `python -m unittest discover -s tests -t .` | **未执行**：CI 当前无法运行（§3.1） |
| G6 | Node 回归全绿 | `npm test` | **部分执行**：本机 436/435/1skip（§3），全仓库口径未在 CI 验证 |
| G7 | 浏览器验收**真实执行**且通过 | `npm run test:browser` + 检出浏览器；由 `Release acceptance` 作业 fail-closed 裁决 | **未执行**：本机无浏览器；CI 无法运行（§3.1）。裁决逻辑本身已由测试固化（§2.1） |
| G8 | 仓库内工作流不含 secret、不具写权限 | `tests/release/test_release_gates.py` | 通过 |

### 1.2 必须由人工确认（不可自动化）

| # | 判据 | 谁确认 | 现状 |
| --- | --- | --- | --- |
| H1 | 其他用户的 GPU 作业与数据不受影响 | 用户 / 主机 owner | **未确认** |
| H2 | 维护窗口已通知并同意 | 用户 | **未确认** |
| H3 | 备份已做且**验证可读** | 操作者 | **未确认** |
| H4 | 回滚计划已由操作者阅读（不执行） | 操作者 | **未确认** |
| H5 | 版本 pin 表中"待 pin"项已定值 | 各 owner | **未确认**（见托管文档 §1.2） |
| H6 | 旧站退役已按替代验收完成 | 集成 Lane #49 | **未完成**：旧站必须保留 |

### 1.3 结论

> **NO-GO。** §1.2 的 H1–H6 全部未确认；§5 的发布阻断项仍然开放。本 Lane **不**请求豁免，也**不**代替用户回答 H1。

---

## 2. 本 Lane 交付了什么

| 交付 | 位置 | 性质 |
| --- | --- | --- |
| 旧站 → V6 功能等价矩阵 | `docs/atelier-v6-legacy-parity-matrix-2026-10-16.md` | 只读审计，逐行证据 |
| 新旧信任边界差异 | `docs/atelier-v6-legacy-trust-boundary-2026-10-16.md` | 只读审计 |
| 受控托管 / 版本 pin / 回滚 | `docs/atelier-v6-service-hosting-and-rollback-2026-10-16.md` | 计划，不执行 |
| 发布判据（本文） | 本文件 | 计划，不执行 |
| CPU-only CI 门禁 | ~~`.github/workflows/atelier-v6-release-gates.yml`~~ → `.github/workflows/ci.yml` | 2026-10-23 并入唯一入口工作流（历史记录保留） |
| 发布验收裁决（fail-closed） | ~~`.github/workflows/release-gate-verdict.sh`~~ → `ci.yml` 的 `browser` job | 2026-10-23 删除；fail-closed 现由该 job 的退出码表达 |
| 门禁不变量测试 | `tests/release/test_release_gates.py` | 新增测试（已随上述合并改写） |
| 裁决逻辑回归测试 | ~~`tests/release/test_release_gate_verdict.py`~~ | 2026-10-23 随脚本一同删除 |
| 回滚演练（打印计划） | `tests/release/drill-preview-rollback.sh` + `preview-rollback-plan.txt` | 只打印，`--execute` 被拒 |

### 2.1 审查修正：浏览器缺失不再可能产生绿色发布门禁（已修）

`PR #51` 的独立审查（review `5545378017`）指出一个语义缺陷并**已在本 PR 内修复**：

- **缺陷**：原 `gate-verdict` 在 `BROWSER_AVAILABLE != true` 时只发 `::warning::`，随后检查 Design/Python/Node，而 `BROWSER_RESULT=skipped` **既不匹配失败分支也不匹配成功分支**，于是打印成功信息并 `exit 0`。结果是：**零次浏览器 E2E 也能得到一个名为 "Gate verdict" 的绿色检查**。Job summary 里写"不是通过"改变不了 check 的语义——**只有退出码可以**。
- **修复**：把裁决从 YAML 内联脚本提取为 `release-gate-verdict.sh`，并拆成两个**语义不同**的信号：
  | 作业 | 模式 | 浏览器缺失时 |
  | --- | --- | --- |
  | `CPU unit checks (informative)` | `--mode cpu` | 报告 SKIPPED 并注明"不是通过"；**不影响退出码** |
  | `Release acceptance (fail-closed)` | `--mode release` | **失败**（退出码 1）。要求 `browser-available=true` **且** `browser-result=success`；缺失浏览器、作业被跳过、探测作业本身出错，三者**都判失败** |
- **回归测试**：`tests/release/test_release_gate_verdict.py` 直接以子进程断言退出码，覆盖审查点名的四种情形——browser **passed / skipped / failed / detector errored**——外加 CPU 门禁失败、被跳过、参数缺失、模式非法等。`test_release_gates.py` 另加护栏：Lane 自有 shell 脚本必须全部列入特权命令检查清单，且脚本内不得出现特权命令。

**团队因此可以只看 GitHub check 本身判断发布验收是否具备浏览器证据，而不必人工阅读 job summary。**

**本 Lane 未做**：未改生产服务、未改 PAM/root/UID/systemd/反向代理、未碰模型运行时或 Gateway、未碰他人 worktree、未执行回滚、未上传任何密钥或请求原文。

---

## 3. 测试结果如实报告（含未执行项）

必须区分"通过""失败""未执行"。**缺失即报告，不得记为通过。**

| 项 | 状态 | 说明 |
| --- | --- | --- |
| V5 摘要比对 | **通过** | 实测 = `04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf` |
| V6 摘要比对 | **通过** | 实测 = `883a16c8d3e587c7e3f82682716ce60f2a828553bfeb7755f3af34a87a48561d`（Phase B 合成候选重钉；原值 `a7ba7a6c…` 是 Release #51 单独基线 `7fcc7d4f` 上的 V6 摘要，UI #54 改动 `prototypes/v6/index.html` 后失效） |
| `derive-from-v5.mjs --check` | **通过** | `ok: prototypes/v6/index.html matches v5 + additions` |
| `extract-from-prototype.mjs --check` | **通过** | `ok: web/ matches generator output` |
| 检查后工作树干净 | **通过** | `git status` 为空 |
| Node 回归 `npm test` | **通过** | `tests 436 · pass 435 · fail 0 · skipped 1`，退出码 0（Node v22.23.3，CPU-only） |
| Linux 登录契约（无浏览器部分） | **通过** | `auth-core` + `auth-linux-readiness` + `mock-server` = **98 / 98**，退出码 0 |
| `tests/release/test_release_gates.py` | **通过** | 本 Lane 新增，27 例；实际输出见 PR |
| 全量 Python 回归（95 个测试文件） | **由 CI 执行** | 本地未整轮执行：见下方"为什么" |
| 浏览器验收（Chrome/Chromium） | **看运行器** | 有浏览器才执行；无则显式 SKIPPED，**不是通过** |
| Linux 登录浏览器验收 `browser.auth-ui.test.mjs` | **看运行器** | 与上一行同一个条件任务 |
| 真实主机 / 真实身份 / 真实模型调用 | **未执行** | 超出本 Lane 授权，且需要生产环境 |
| 回滚演练 | **未执行** | 按设计只打印计划（`--execute` 被拒） |

**本机没有 Chrome**（只有 Firefox，而 `tests/auth-ui` 的 CDP 驱动不接受它）。因此：

- `tests/auth-ui/` 的**纯逻辑**三个套件在本机跑出 98/98（与基线记录一致）；
- `tests/auth-ui/browser.auth-ui.test.mjs` 在本机会因**缺浏览器**而失败，**这与断言无关**。所以 CI 把它放进"检测到浏览器才运行"的任务里，无浏览器时整条记为 SKIPPED，并在摘要中写明"不是通过"。**不得**把它记为 Dell 上的通过，也不得用它替代浏览器的证据。

**为什么全量 Python 回归交给 CI 而不是在本机整轮跑**：本机同时有其他 Lane 的会话与预览进程在运行，完整 Python 回归会占用大量 CPU，并可能因端口冲突产生**假失败**，从而既干扰他人又把环境噪声伪装成结论。把整轮执行放在隔离的 CI 运行器上，是刻意选择，不是遗漏。

**CI 结果以 PR 上的运行记录为准**；但见 §3.1：本仓库当前**无法产生任何 CI 结果**，因此 G5/G6/G7 记为"未执行"，**不是"通过"**，也不是"等待中"。

### 3.1 CI 目前在本仓库**根本无法运行**（已实测）

本 Lane 的 PR 触发后，工作流确实被调度了（`push` 与 `pull_request` 各一次），但**所有作业都在 0 步之内失败**：

| 观测 | 值 |
| --- | --- |
| 典型失败形态 | 作业 `steps=0`、`runner_name` 为空、耗时约 3 秒 |
| 受影响的作业 | 本 Lane 全部作业，**包括只做 `command -v` 的浏览器探测作业** |
| 既有工作流 | `Atelier V6 Beta (isolated CPU regression)` 的 `linux-regression` **同样** `steps=0`、无 runner、约 3 秒 |
| 历史 | 本仓库最近 5 次运行**全部** `failure`，最早可追溯到 2026-10-15；未观察到任何一次成功运行 |

**结论**：这是**仓库级**的 Actions 运行器/额度条件，**不是**本 Lane 工作流的 YAML 缺陷——一个连 `command -v` 都没跑起来的失败，不可能由 YAML 内容造成，而既有工作流以完全相同的方式失败。

**因此**：G5/G6/G7 在当前状态下**既未通过、也未执行**，而不是"等待 CI"。任何一方都**不得**把"CI 会跑"当作门禁存在。解除需要仓库 owner 处理配额/计费/运行器可用性，**超出本 Lane 授权**（本 Lane 不申请、不改设置）。

这也意味着：**本仓库当前没有任何自动化门禁在真正生效**——这对全体 Lane 都成立，不只是本 Lane。

---

## 4. 本轮发现的既有问题（**只报告，未修改**）

这些是在只读审计中发现的、属于**他人文件所有权**的问题。本 Lane 的边界是 `docs/**`（新文件）、`.github/workflows/**`（新文件）、`tests/release/**`，因此**不修改**它们，只上报给对应 owner。

| # | 发现 | 证据 | 影响 | 建议归属 |
| --- | --- | --- | --- | --- |
| F1 | **派生链摘要文档漂移**：两处既有文档把 v6 的 sha256 写成 `fd3536e2…`，而当前值与 `extract-from-prototype.mjs` 的 pin 都是 `a7ba7a6c…` | `web/README.md:31`、`docs/v6-ui-flows.md:28`；实测 `node web/tools/extract-from-prototype.mjs --check` 打印 `source sha256: a7ba7a6c…` | 文档与代码不一致会让人以为派生链被改动过（或反过来放过真正的漂移） | 文档 owner（非本 Lane） |
| F2 | **首页合成时间序列默认可见**：Overview 的 TELEMETRY 折线是合成 24 点，虽标注 `DEMO · 24 POINTS`，但在非 demo 模式下**不被隐藏** | `web/js/timeline.js:8`（"DEMO ONLY"）、`web/index.html:31`、`web/js/demo-mode.js:73-83`（闸门只处理 Atlas 主体） | 与总控 [#43](https://github.com/ProfessorZhi/llm-lab-portal/issues/43) 的 P0「清除默认首页合成时间序列」直接相关 | UI #44 |
| F3 | **示例配置类风险**：`web/tools/serve.mjs` 只读 `PORT`、绑定写死环回，**不解析** `--host`/`--port`；若按参数形式写进服务单元，进程会静默听在 8123 而单元仍显示 active | `web/tools/serve.mjs:149`（绑定）、`:164`（端口来源） | "看起来健康、实际不可访问"的失败模式；已在本 Lane 的托管文档 §2.1 以警示注释规避 | 运维/发布（已在文档侧规避） |
| F4 | **CI 全仓库失效**：本仓库所有 Actions 作业在 0 步内失败、拿不到 runner，既有工作流同样如此；最近 5 次运行全为 failure | 见 §3.1；`gh run view --json jobs` 显示 `steps=0`、`runner_name` 空 | **全体 Lane 的自动化门禁目前都不生效**，"CI 会拦住"这一假设不成立 | 仓库 owner（配额/计费/运行器） |

**F1/F2 未在本 PR 中修复**，因为它们分别是既有共享文档与 V6 UI 生成链的改动，超出本 Lane 所有权（见工单 [#50](https://github.com/ProfessorZhi/llm-lab-portal/issues/50) 的"文件所有权"）。

### Phase B 合成候选（[#59](https://github.com/ProfessorZhi/llm-lab-portal/issues/59)）对本表的影响

上面三行 F1/F2/F4 是 Release #51 在**它自己的基线** `7fcc7d4f` 上的记录。七条 Lane 合成到
`integration/v6-lanes-candidate-20261016` 之后，事实变化如下，**原记录不删**，因为它当时是准的：

- **F1 → 在本候选内已修复。** 合成候选重钉了 V6 摘要：`prototypes/v6/index.html` 现在是
  `883a16c8d3e587c7e3f82682716ce60f2a828553bfeb7755f3af34a87a48561d`（UI #54 改动了该文件，
  Release #51 记录的 `a7ba7a6c…` 因此失效；再早经文档记载的 `fd3536e2…` 更是早已过期）。
  `web/README.md` 与 `docs/v6-ui-flows.md` 的派生链图已更新到该值，本表 §3 的 V6 行同步更新。
  三处 pin（工作流 literal、`tests/release` literal、工具源码常量）都是**独立字面量**，没有
  任何一处改成"运行时计算"——摘要是被重钉，不是被放宽。冻结的 V5 权威 `04003f5c…`
  全程未变。
- **F2 → 由 UI #54 处理，本候选内已不可复现。** 实测 `web/js/timeline.js:5-13`：该模块现在
  自己声明「the series below is authored, so it is demo-only」，且 live URL 上不画曲线、
  只画诚实的不可用状态（闸门从 KPI 行下移到了画曲线的那一处）。
- **F4 → 仍然成立且未被本候选改变。** 本仓库 Actions 依旧拿不到 runner，因此本候选的
  **CI 状态是 EXTERNAL BLOCKED，不是绿色**；所有结论只来自本地 CPU-only 实测。

---

## 5. 发布阻断项（NO-GO 明细）

| # | 阻断 | 依据 | 谁来解 |
| --- | --- | --- | --- |
| B1 | V6 尚无受信同源 BFF 证书链，预览身份与 Linux 身份不是同一件事 | 信任边界文档 §2 | IAM #45 / 集成 #49 |
| B2 | 旧站匿名数据 API 不得直接嵌入新站 | 工单 [#34](https://github.com/ProfessorZhi/llm-lab-portal/issues/34) | 数据 #47 / 集成 #49 |
| B3 | 旧站仍在服务真实用户，替代验收未完成前不得退役 | 判据 H6 | 集成 #49 |
| B4 | 版本 pin 存在"待 pin"项（模型/引擎、运行时版本、发布版本号） | 托管文档 §1.2 | 发布负责人 + 用户 |
| B5 | 浏览器验收可能被跳过；跳过时**不得**记为通过 | 本文 §3；现已由 §2.1 的 fail-closed 裁决在**退出码**层面强制 | 已缓解：仍需真实浏览器运行器；在 B8 解除前无自动证据 |
| B6 | Python 目标版本在开发机（3.14）与 CI（3.12）之间未收敛 | 托管文档 §1.2 | 发布负责人 |
| B7 | 真实主机、真实多用户、真实回滚均未验证 | 本文 §3 | 需要发布环境与授权 |
| B8 | **CI 在本仓库无法产生任何结果**，全体 Lane 的自动化门禁均不生效 | 本文 §3.1、发现 F4 | 仓库 owner（配额/计费/运行器） |

---

## 6. 签署栏（留空，等待有权人）

本 Lane 不代替任何有权人签署。

- [ ] 代码审查：______ 日期：______
- [ ] 安全审查（信任边界、凭据、权限）：______ 日期：______
- [ ] 运维审查（托管、备份、回滚、维护窗口）：______ 日期：______
- [ ] 用户批准（跨设备/生产操作、其他用户影响）：______ 日期：______

**在以上四项完成之前，Atelier V6 的状态是：预览（非生产），NO-GO。**
