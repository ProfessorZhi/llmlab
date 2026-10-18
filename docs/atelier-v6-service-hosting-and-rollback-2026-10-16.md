# Atelier V6 受控托管、版本 pin 与回滚清单（Runbook）

> Lane Release/Legacy #50 交付物 · 2026-10-16 · 总控 [#43](https://github.com/ProfessorZhi/llm-lab-portal/issues/43) · 审计 [PR #42](https://github.com/ProfessorZhi/llm-lab-portal/pull/42)
>
> **本文档是计划与清单，不是执行记录。** 本 Lane 未获得修改 root/UID、systemd、反向代理、PAM 或任何生产服务的授权，因此这里的所有服务文件都是**文档内嵌示例**，不以可安装文件形式提交；演练脚本 `tests/release/drill-preview-rollback.sh` 只打印计划，`--execute` 会被拒绝。
>
> **代码合并 ≠ 部署。** 本文档描述的是候选发布被批准之后才由人工执行的步骤；合并本 Lane 的 PR 不构成对任何步骤的批准。

---

## 0. 一句话结论

Atelier V6 目前是一个**受控预览**（非生产）。把它变成受托管服务，需要先解决的是**信任边界**（见 `atelier-v6-legacy-trust-boundary-2026-10-16.md`）和**服务身份**两件事；本 Lane 只提供可复现的门禁、版本 pin 与回滚清单，**不宣称生产可用**。

---

## 1. 版本 pin（Version pin）

### 1.1 已冻结的设计来源

设计的权威来源是 `prototypes/v5/index.html`，它**永不编辑**；`prototypes/v6/index.html` 由 `prototypes/v6/derive-from-v5.mjs` 从 v5 计算得到；`web/` 由 `web/tools/extract-from-prototype.mjs` 从 v6 计算得到。三者的 sha256（LF 规范化后）如下，本 Lane 的 CI 与单元测试各自独立地钉住同一个值：

| 产物 | 方法 | sha256 |
| --- | --- | --- |
| `prototypes/v5/index.html` | 冻结的设计权威，只读 | `04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf` |
| `prototypes/v6/index.html` | `v5 + additions`（derive 脚本计算） | `b05e754453edfb5238249957dd1e622d452bbff57cbdf951eb7d4ee5fa7f4573` |
| `web/**` | extract 脚本计算 | 由 `npm run verify:extract` 校验，不单独 pin 摘要 |

**为什么摘要要写三遍**（工作流里一遍、测试里一遍、工具源码里一遍）：只在工具里钉住的话，改工具里的常量就能悄悄移动标尺。独立副本让这种修改必然失败。

复核命令（CPU-only，只读，可在任何克隆里跑）：

```bash
git cat-file blob HEAD:prototypes/v5/index.html | sha256sum
git cat-file blob HEAD:prototypes/v6/index.html | sha256sum
node prototypes/v6/derive-from-v5.mjs --check
node web/tools/extract-from-prototype.mjs --check
```

四个命令的期望输出与上表一致，且**最后一条之后 `git status` 必须为空**——生成链路是只读的，任何写入都是缺陷。

### 1.2 待 pin 的运行时项（尚未 pin，属于发布阻断）

下列项目在旧 LAN 站上存在，但在 V6 侧**还没有**可 pin 的明确取值，因此不能声称"已 pin"：

| 项 | 现状 | 需要谁决定 |
| --- | --- | --- |
| Portal 候选发布 commit | 由 Draft PR 的 head SHA 决定，**每次发布重新记录** | 集成 Lane #49 |
| `llmlab` CLI 版本 | `pyproject.toml` 记为 `0.1.0`，尚无发布版本号策略 | 发布负责人 |
| 模型/引擎（Strata 侧） | 由模型运行时自己的 registry 决定，**本 Lane 不碰** | 用户 + 模型 owner |
| Python 运行时 | 开发机实测 3.14，CI 用 3.12，`requires-python = ">=3.11"` | 发布负责人确认唯一目标版本 |
| Node 运行时 | `package.json` 要求 `>=20`，CI 用 22 | 发布负责人确认唯一目标版本 |

**上报缺口**：CI 里跑的 Python 是 3.12，而交付审计时用的开发机是 3.14。两者都在 `requires-python` 范围内，但**发布时必须选定一个**并写进发布记录，否则"在我机器上通过"没有意义。

---

## 2. 受控托管方案（示例，不执行）

### 2.1 服务身份

原则：**每个服务一个专用非登录账号**，不使用 `root` 运行，不与操作者账号共用，不复用其它项目已有的账号。

```ini
# 示例 · 不要直接安装 · /etc/systemd/system/atelier-v6-preview.service
# 本 Lane 未获授权创建此文件或此账号。请由具备 root 权限的操作者评审后单独执行。
[Unit]
Description=Atelier V6 preview (non-production, loopback only)
Documentation=file:///srv/atelier-v6/docs/atelier-v6-service-hosting-and-rollback-2026-10-16.md

[Service]
Type=simple
User=atelier-v6-preview
Group=atelier-v6-preview
WorkingDirectory=/srv/atelier-v6/current
# 注意：serve.mjs 只读 PORT 环境变量，并且把绑定写死为 127.0.0.1。
# 它**不解析** --host / --port 参数——写上它们不会报错，只会被忽略，
# 于是服务静静听在 8123 上。端口必须通过环境变量给。
ExecStart=/usr/bin/node web/tools/serve.mjs
Environment=PORT=18765
Environment=ATELIER_INFRA_READONLY=1
Environment=LLM_LAB_PREVIEW_STATE_DIR=/var/lib/atelier-v6-preview
Environment=PYTHONDONTWRITEBYTECODE=1

# 只读根、最小可写面
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=/var/lib/atelier-v6-preview

Restart=on-failure
RestartSec=3
TimeoutStopSec=20

[Install]
WantedBy=multi-user.target
```

要点：
- `User=` 是**专用账号**，不是 operator，也不是别的项目的账号。
- `ProtectSystem=strict` + `ReadWritePaths=` 只放开状态目录——预览进程不应能写代码目录。
- **没有** `AmbientCapabilities`、**没有** `CapabilityBoundingSet` 放宽、**没有** 绑定特权端口。预览端口非特权。
- **端口只能经 `PORT` 传入**（见上方注释）。这不是风格问题：把 `--port 8443` 写进 `ExecStart` 会让服务听在默认 8123 上，而单元仍显示 active —— 一个"看起来健康但没人能访问"的失败模式。
- `ATELIER_INFRA_READONLY=1` 才会开启 `/api/infra/summary`；不设则该端点不存在（fail-closed），主机面板显示 `UNKNOWN`。
- 健康检查见 §2.3。

### 2.2 反向代理（示例，不执行）

旧站与新站共用同一台主机的 443。**任何代理改动都可能影响其他用户正在使用的服务**，因此必须走维护窗口，且必须保留旧站路由直到 V6 通过替代验收。

```caddyfile
# 示例 · 不要直接加载 · 仅为说明路由意图
# 本 Lane 未获授权改动正在运行的代理配置。
# 链路口径：外部 TLS :8443 → 代理 :18443 → 预览 127.0.0.1:18765
preview.example.internal {
    # 仅内网；不对外
    @intranet remote_ip 10.0.0.0/8 192.168.0.0/16 127.0.0.1
    handle @intranet {
        # 上游是**预览自己的端口**（18765），不是外部 TLS 端口（8443）
        reverse_proxy 127.0.0.1:18765
    }
    respond 403
}
```

要点：
- V6 预览**不替换**旧站路由；两者并存直到 V6 通过替代验收。
- 代理层不承担身份：预览身份契约见信任边界文档，`reverse_proxy` 不得成为"绕过认证"的旁路。
- **`reverse_proxy` 的目标端口必须是预览监听的端口**；把外部 TLS 端口（8443）写进上游，是本文件里最容易犯、又最难在 `active` 状态下被发现的一类错误。
- 改动前后都必须记录 `443` 监听状态（`ss -ltnp` 只读快照）作为回滚依据。

### 2.3 健康检查

| 检查 | 期望 | 说明 |
| --- | --- | --- |
| 进程存活 | 单元处于 active | 仅作为第一层，不代表业务可用 |
| 环回健康端点 | 200 | **只在 127.0.0.1 上探测**，不经过公网/内网入口 |
| 未认证访问受保护路径 | 401/403（**不是** 200） | 这是负向检查，必须显式断言 |
| 绑定地址 | 仅 127.0.0.1:18765 | 若监听 `0.0.0.0` 即视为配置漂移，直接判定失败 |
| 状态目录 | 仍指向预览专用目录 | 若指针指向共享目录，判定失败 |

### 2.4 日志安全

- 日志中**不得**出现：请求原文、`prompt`/`response` 内容、API Key、Cookie/会话值、密码、令牌。
- 预览账号的日志目录权限为 0750，属主为专用账号。
- 日志轮转由操作系统统一策略负责，本项目不自建轮转。
- 本 Lane 的 CI 不采集任何运行时日志到 GitHub；PR 里只允许脱敏结论。

### 2.5 配置检测（发布前必做）

- [ ] 代码目录不含任何运行时密钥（见 §4.1 扫描）。
- [ ] 服务单元没有 `User=root`、没有放宽 capability。
- [ ] 代理配置改动**仅**新增预览路由，未修改既有路由。
- [ ] 状态目录与代码目录分离，且状态目录不在 git 工作树内。
- [ ] 预览端口不与旧站、Gateway、监控端口冲突（端口占用只读核对）。

---

## 3. 回滚单（Rollback sheet）

> 演练计划正文见 `tests/release/preview-rollback-plan.txt`，打印方式：`tests/release/drill-preview-rollback.sh --check`（只读）。**任何一步都不许由脚本执行。**

### 3.1 触发条件

出现下列任一情况即触发回滚，不再继续调试：

1. 预览服务在已发布 commit 上无法通过 §2.3 的健康检查。
2. 出现未认证即可访问受保护路径（信任边界失守）——立即回滚并单独报告。
3. 代理或绑定范围发生非预期扩大（例如监听 `0.0.0.0`）。
4. 影响到其他用户正在运行的作业或数据。

### 3.2 回滚步骤（人工，按序）

见 `tests/release/preview-rollback-plan.txt` 的九步。摘要：

1. 记录"回滚前"状态（当前 SHA、进程身份）。
2. 用**启动它的同一个 supervisor** 停止预览进程。
3. 预览工作树 detached 检出上一个已知良好 commit。
4. 确认端口与状态目录未变宽。
5. 用同一 supervisor 重新启动。
6. 环回健康检查 + 登录页检查，确认认证挑战仍然存在。
7. 确认预览可达且**生产端口未变**。
8. 记录结果，包括**未能验证**的部分。
9. 任一步失败即停止并升级，不要在半个回滚之上再叠一次回滚。

### 3.3 回滚后

- [ ] 预览服务于上一个良好 commit。
- [ ] 状态目录未改变，且未引入共享路径。
- [ ] 为导致回滚的缺陷建立 issue。
- [ ] 失败的候选分支**保留**用于诊断，不删除。

---

## 4. 发布前置门禁

### 4.1 自动化（本 Lane 提供的 CI）

`.github/workflows/atelier-v6-release-gates.yml`：CPU-only、`permissions: contents: read`、**不引用任何 secret**。作业：

| 作业 | 内容 | 缺失时的行为 |
| --- | --- | --- |
| V5 frozen + V6 derivation | 独立摘要比对 + `derive --check` + `extract --check` + 确认检查未写盘 | 硬失败 |
| Python regression | `python -m unittest discover -s tests -t .` | 硬失败 |
| Node regression | `npm test` | 硬失败 |
| Browser acceptance | 检测到浏览器才运行 | **无浏览器 → 记 SKIPPED 并在摘要里写明"不是通过"** |
| Gate verdict | 汇总各门禁结果，任一必需门禁非成功则失败 | — |

`tests/release/test_release_gates.py` 把这些不变量固化为测试，其中包括"仓库内任何工作流都不得出现 `secrets.`"、"工作流必须声明只读 `contents: read`"、"Lane 自有可执行文件不得包含特权命令"。

### 4.2 人工确认（无法自动化）

- [ ] 其他用户的 GPU 作业与数据不受影响（**需人工确认，不得由本 Lane 代答**）。
- [ ] 维护窗口已通知。
- [ ] 备份已做且**验证过可读**，不只是"已执行备份命令"。
- [ ] 回滚演练计划已由操作者读过一遍（不执行）。
- [ ] 版本 pin 表中"待 pin"的项已由对应 owner 确定。

---

## 5. 明确未授权事项（本 Lane 边界）

- 未修改 root/UID、systemd 单元、PAM 栈、反向代理配置。
- 未启动/停止/重启任何模型运行时、Gateway、监控或他人的作业。
- 未改动任何生产账号、密钥、用户数据。
- 未执行任何回滚演练（`--execute` 被脚本主动拒绝）。
- 未声称生产可用：**合并本 PR 不等于部署，也不等于 GO。**
