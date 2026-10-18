# 评测报告接缝与受限下载（V1 Lane #78）

> Lane：Evaluation / reports [#78](https://github.com/ProfessorZhi/llm-lab-portal/issues/78)。
> 调度：[#80](https://github.com/ProfessorZhi/llm-lab-portal/issues/80)。
> BASE：`docs/v1-production-architecture-agent-orchestration-20261017` @
> `5ea80535a3ccdde63281314324195cd727a94700`（= V1 架构 Draft PR
> [#81](https://github.com/ProfessorZhi/llm-lab-portal/pull/81) 的 HEAD）。
> 执行状态：**代码与 CPU-only 测试已交付；Dell 侧一律 `HOST_PENDING`。**

本文只说**代码契约**。它不代表任何来源已接线、任何报告已产生、任何部署已验收。

## 0. 一句话

BFF 那一轮（[#79](https://github.com/ProfessorZhi/llm-lab-portal/issues/79) / PR #85）挂了三条
报告只读路由，并在 `BFF_BATCH1_SEAMS.md` 里写明"接缝就是
`backend/bff/capabilities.py:SOURCE_SEAMS`，别的 Lane 接的是表里的一个条目"。
本文交出**评测这一侧的那一半**：同一张表的四条路由、一个**本 Lane 新增**的受限下载判定，
以及没接线时该下发什么状态码。挂载仍然由 BFF Owner 做——`backend/bff/**` 不在本 Lane 的
改动面里。

## 1. 交付物

| 文件 | 归属 | 职责 |
|---|---|---|
| `backend/evaluation/download.py` | 本 Lane 新增 | 受限下载的**判定**（授权、来源、上界、原因）与有界读取器 |
| `backend/evaluation/report_seam.py` | 本 Lane 新增 | V1 BFF 的接缝清单（四条路由）与可注入接缝 |
| `backend/evaluation/errors.py` | 本 Lane 追加 | `ReportDownloadRefused`（`evaluation.report_download_refused`） |
| `backend/evaluation/api.py` | 本 Lane 追加 | 该契约码 → **413** 的映射 |
| `backend/evaluation/service.py` | 本 Lane 追加 | `EvaluationService.report_download`、两个配置字段、`capabilities()["report_download"]` |
| `backend/evaluation/__init__.py` | 本 Lane 追加 | 导出新名字、模块布局表、立场第 8 条 |
| `tests/evaluation/test_v1_report_download.py` | 本 Lane 新增 | 38 条：可见性、部署状态、上界、路径防护、DTO 形状 |
| `tests/evaluation/test_v1_report_seam.py` | 本 Lane 新增 | 22 条：清单、构造期校验、可注入接缝 |

**未触碰**：`backend/bff/**`（含 `route_map.py`）、`tests/bff/**`、`docs/integrations/**`、
`tests/evaluation/test_identity_and_rbac.py`（QA PR #87 已编辑）、`web/**`、任何 workflow。
既有测试文件**一个都没有改**——本 Lane 的全部测试都在 `test_v1_*` 新文件里。

## 2. 四条路由

`report_seam.REPORT_ROUTE_SEAMS` 是声明式真源；`tests/evaluation/test_v1_report_seam.py`
逐条执行它。前三条与本模块自己的路由表（`api.ROUTES`）**逐字比对**——两张表漂移过一次，
前端就会对着一条不存在的路由写代码。

| # | 路由 | scope | TTL | 谁提供 | 谁挂载 |
|---|---|---|---|---|---|
| 1 | `GET /api/evaluation/report-sources` | `evaluation:read` | 无 | 本 Lane | BFF（PR #85 已挂） |
| 2 | `GET /api/evaluation/reports` | `evaluation:read` | 无 | 本 Lane | BFF（PR #85 已挂） |
| 3 | `GET /api/evaluation/reports/{report_ref}` | `evaluation:read` | 无 | 本 Lane | BFF（PR #85 已挂） |
| 4 | `GET /api/evaluation/reports/{report_ref}/download` | `evaluation:read` | 无 | **本 Lane（新增）** | **BFF（待挂）** |

`ttl_seconds` 一律为 `None`（"新鲜度不由 TTL 管"）：逐来源的新鲜度由
`report_catalog.DEFAULT_STALENESS_SECONDS`（3600 秒）在评测模块**内部**判定并写进它自己的
DTO。在 BFF 层再判一次会得到"清单说 `ready`、DTO 说 `stale`"这种自相矛盾的两份答案。

第四条**不**在 `api.ROUTES` 里：本 Lane 只提供判定，挂载是 `backend/bff` 的事。

## 3. 受限下载：两个字段，不是一个

下载判定把两件事分开，DTO 里也分成两个字段：

```json
{
  "report_ref": "evrp-…",
  "source": "evalscope",
  "source_label": "EvalScope 报告",
  "authorized": true,
  "content_read": false,
  "content_type": "application/json",
  "filename": "synth-run-1.json",
  "size_bytes": null,
  "max_bytes": 4194304,
  "reason": "report_content_reading_disabled",
  "note": null
}
```

* `authorized` —— **策略**："允许你发起这次读取"（`authz.may_read_report_reference`，
  与列表里的 `download_authorized` 是同一个判定）。
* `content_read` —— **部署 + 产物**事实："这一份此刻真的能读出来吗"。

把后者折进前者，会让"本部署不提供下载"看起来像"你没有权限"——那正是运维事故最常见的
开端。**本部署 `content_read` 恒为 `False`**，原因如实写着 `report_content_reading_disabled`
（读取器未打开）；即便打开读取器，报告根未接线时的原因是 `report_root_not_configured`。

### 3.1 有界 = 两个方向都关着

* **上界**：`MAX_DOWNLOAD_BYTES` = 4 MiB。读取器**先 `stat` 判、读后复核**，一份**超出**上界
  的产物被**拒绝**，**不是**被截断——一份被截断的报告比一份被拒绝的报告危险得多，前者
  看起来像一份完整的报告。超界时 `size_bytes` 是 `null`（未知），不是"被截断到的那个数"。
* **默认关**：`BoundedReportReader.enabled` 缺省 `False`，`EvaluationConfig.report_download_enabled`
  缺省 `False`，`ReportRoot` 缺省未接线。因此"本部署不从这里流出报告正文"是一条**结构**
  事实——既有保证 `test_static_guarantees.test_the_report_content_reader_is_closed_by_default`
  仍然成立，`ReportRoot.read_bytes` 仍然一律 `NotConfigured`。

### 3.2 两个引用，各归各的

客户端手里的是**不透明引用**（`evrp-` + 摘要），它**不是**路径。服务端内部用 `reports`
判过的**相对引用**。`ReportDownloadPlan` 同时持有两者，但只有前者进 `to_dict()`：
`path_ref` 是内部字段。下发一条相对路径就是下发产物布局，而 `authz` 已经把"拿到指向报告
文件的引用"当作一次**单独的**授权决定。

### 3.3 内容读取器与 `ReportRoot.read_bytes` 是两个决定

刻意共存：`ReportRoot.read_bytes` 说"本部署的报告内容读取是关闭的"，且**保持关闭**；
`BoundedReportReader` 说"若某部署**显式**打开读取，它也只能这样读"。打开它的第一步必须是
有意地把 `enabled` 改成 `True`，而不是发现"好像本来就能读"。

## 4. 安全负测（全部有断言）

| 情形 | 期望 | 测试 |
|---|---|---|
| 不是 owner（同项目成员） | 404，与"不存在"**逐字节同形** | `test_a_project_member_who_is_not_the_owner_is_refused` |
| 跨项目 | 404 | `test_a_report_from_another_project_is_refused` |
| 归属无法核实 + `include_unowned=true`（普通成员） | 404（放行在 `authz`，要求 `admin:manage`） | `test_an_unowned_report_stays_hidden_even_with_the_flag_for_a_member` |
| 只有 `admin:read` 的管理员 + `include_unowned` | 404 | `test_an_admin_without_manage_cannot_reach_the_unowned_report_either` |
| 下载面比详情面看见更多 | 不可能；两条路径同一条 404 | `test_the_download_is_a_superset_of_nothing_seen_in_the_detail_view` |
| 判定函数被**直接**调用（绕过取件路径） | 同样拒绝 | `DownloadPlanGuardTests` |
| 路径穿越 `../` / 绝对路径 / 反斜杠 | 在**形状**层被拒（碰文件系统之前） | `DownloadPathGuardTests` |
| 软链接指向根**之外** | `realpath` 包含性拒绝 | `test_a_symlink_escaping_the_root_is_refused` |
| 软链接指向根**之内** | 按"引用只指一个产物，不指一次跳转"拒绝 | `test_a_symlink_inside_the_root_is_refused_by_its_own_rule` |
| 目录冒充报告文件 | 拒绝（不是普通文件） | `test_a_directory_is_not_a_downloadable_artifact` |
| 词表外后缀（`.bin`） | 无内容类型，不下载 | `test_a_disallowed_extension_never_gets_a_content_type` |
| 缺测维度 | `state="unknown"`, `value=null`，**绝不是 0** | `MissingMetricIsNullTests` |

**fail-when-reverted 已做**：把 `download_plan` 里的授权守卫中性化 → 2 条直调守卫测试失败；
把上界判定中性化 → 上界测试失败。两次探针都先发现了一个**真实缺口**（见 §7），补测后
才通过。

## 5. BFF 侧要做什么（由 BFF Owner / Integrator 落地）

1. **挂载** `GET /api/evaluation/reports/{report_ref}/download`（`route_map.py`，本 Lane 不碰）。
2. 在 `ModuleEvaluationAdapter` 上加一条薄透传，形状与既有三条一致：

   ```python
   def report_download(self, *, identity, report_ref, include_unowned=False) -> dict:
       requester = self.requester_for(identity)
       return self.stack.service.report_download(
           requester=requester, report_ref=report_ref, include_unowned=bool(include_unowned)
       )["data"]
   ```

3. 把 `report_seam.REPORT_ROUTE_SEAMS` 的第四条加进 `SOURCE_SEAMS` 的 evaluation 条目
   （或另立一条），字段逐字对齐。
4. 状态码按 `report_seam.SEAM_ERROR_STATUS` 下发。注意这是**两条不同的映射**：

   | 契约码 | 本仓库评测 API（`api.CONTRACT_STATUS`） | BFF 面（`SEAM_ERROR_STATUS`） |
   |---|---|---|
   | `evaluation.not_found` | 404 | 404 |
   | `evaluation.not_configured` | **501** | **503** |
   | `evaluation.report_download_refused` | 413 | 413 |

   本 Lane **没有**改动 `api.CONTRACT_STATUS` 的既有取值（那是一次仓库级决定）；BFF 面按
   契约要讲成 503，因此那一份写在 seam 表里。

## 6. 测试（CPU-only，未起任何真实 runner）

```bash
# 本 Lane 的 59 条
CUDA_VISIBLE_DEVICES='' python3 -m unittest discover -s tests/evaluation -t . -p 'test_v1_report_*.py'

# 评测模块全量
CUDA_VISIBLE_DEVICES='' python3 -m unittest discover -s tests/evaluation -t . -p 'test_*.py'

# BFF 套件（回归：它 import 本包）
CUDA_VISIBLE_DEVICES='' python3 -m unittest discover -s tests/bff -t . -p 'test_*.py'

# 全量 Python
CUDA_VISIBLE_DEVICES='' python3 -m unittest discover -s tests -t . -p 'test_*.py'
```

**必须带 `-t .`**：少了它 `tests/` 会被当顶层包，`backend.bff` 解不出来，冒出上百个
`ModuleNotFoundError` 假红（见 `docs/v1/LOCAL_TEST_AND_RELEASE.md` §2 的同一处陷阱）。

### 6.1 结果与**归因**（这是本 Lane 唯一需要解释的红色）

| 套件 | 结果 | 说明 |
|---|---|---|
| `tests/evaluation` | 535 tests，2F + 2E | 全是 `test_identity_and_rbac.HostIdentityFactsTests` 的**既有**宿主可移植性失败 |
| `tests/bff` | 298 tests，**OK** | 与本 Lane BASE 一致（PR #85 会把基线带到 368） |
| 全量 Python | 3385 tests，2F + 15E + 6S | 与 BASE 的 3326 tests / 2F + 15E + 6S **逐项同签名**；本 Lane 只**增加** 59 条通过的测试 |

四项既有失败**都不是本 Lane 的改动**，也不由本 Lane 修（本 Lane 的排他面是
`backend/evaluation/**` 与新增的 `tests/evaluation/test_v1_*`）：

1. **`tests/auth_linux` 13 errors** —— `ServiceHarness` 用 `write_text` 造政策文件，权限 =
   `0o666 & ~umask`；本机 **umask 0002** ⇒ `0664` 组可写 ⇒ `securefile` **正当拒绝**
   （`detail='policy file is writable by group/other (mode 0664)'`）。
2. **`tests/evaluation` 2F + 2E** —— `HostIdentityFactsTests` 断言**本机** `/etc/passwd`
   （本机无 `alice` / `dave`，且 `carol` 的 uid 与本表不同）。而 `backend/evaluation`
   全包**从不调用 `pwd`**——身份来自 `tests/evaluation/__init__.py` 的 `HOST_USERS`。
3. 6 个 skip 全是环境性：2× `argon2-cffi` 未安装、4× 未提权的 Windows 文件系统语义。

**这三条正是 QA PR [#87](https://github.com/ProfessorZhi/llm-lab-portal/pull/87) 在现场定死并
修掉的根因**（修后 3327 tests / `OK (skipped=6)` / rc 0）。本 Lane 的 BASE 早于 #87，因此
在本地看到它们；修法属于 #87 的改动面（`tests/auth_linux/test_service.py`、
`tests/evaluation/__init__.py`、`tests/evaluation/test_identity_and_rbac.py`），**本 Lane 不碰**。

## 7. 交付过程中发现并修掉的一个真实缺口

只走 `service.report_download` 的端到端测试**不足以**证明 `download_plan` 自己的授权守卫
在位：那条路径上 `catalog.lookup` 已经先拒绝了一次，于是把 `download_plan` 里的守卫删掉，
端到端测试**仍然全绿**。这个缺口是由一次 fail-when-reverted 探针发现的，不是靠阅读发现的。

而 `download_plan` 是交给 BFF 的**公开**函数——接缝另一侧可能直接调它。因此补了
`DownloadPlanGuardTests`：绕过取件路径，直接以非 owner / 无归属 / 非法主体三种输入调它。
补测后探针如期变红。

## 8. HOST_PENDING（Dell 侧，本 Lane 一条都没有实测）

- [ ] Dell 上是否配置了**任何**报告根目录（`ReportRoot` 当前未接线 → 一律
      `report_root_not_configured`）。**未验证。**
- [ ] EvalScope `/api/v1/reports` 是否真的可达、返回什么形状。**未验证**（本仓库没有
      EvalScope 本体，`report_readers` 是只读适配器桩，从不在测试里连它）。
- [ ] 历史 Benchmark 镜像的 run 是否有可核实的 owner UID 绑定。**未验证**；无法核实的
      一律按"对所有人隐藏"处理（`authz.report_visibility`），要显形必须走 Issue #34 的
      **逐 run 审核过的迁移映射**。
- [ ] 报告产物的真实大小分布是否落在 4 MiB 之内。**未验证**——这个数是一根声明出来的
      上界，不是实测出来的中位数。
- [ ] BFF 面上这条路由的实际状态码（501 vs 503）。**未验证**——见 §5 第 4 条。
- [ ] **真实 GPU EvalRunner Job 未启动，也不会在本 Lane 启动。** 准入仍是
      `AdmissionPolicy.closed`，`runner` 仍是 mock，`evaluation:dispatch:real` 不授予任何主体。

## 9. 刻意不做

- **不改** `api.CONTRACT_STATUS` 里 `NotConfigured` 的 501——那是仓库级契约决定，本 Lane
  只在 seam 表里声明 BFF 面应有的 503。
- **不挂载**任何路由，**不碰** `backend/bff/**`：那是 BFF Owner / Integrator 的最后一步。
- **不打开**报告内容读取：`report_download_enabled` 缺省 `False`，本部署维持"正文不经这个
  面出去"。
- **不发明**第二套 Owner / ACL / Session：可见性一律走 `authz`，来源一律由适配器盖章，
  `source` 与 `include_unowned` 都不在 BFF 层重映射。
- **不造**任何合成真实评测分数：本 Lane 的测试全部使用 `tests/evaluation/support.py` 里
  显式合成的语料，凭据一律 `synthetic-` 前缀。
