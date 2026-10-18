# Atelier V6 · Phase B 七 Lane 合成候选（Issue #59）· 集成记录

> **STATUS: INTEGRATION CANDIDATE / NOT DEPLOYED / NO-GO.**
> 本条是一份**合成记录**：把七条已推送的 Lane Draft PR 按依赖顺序合成到一个候选
> 分支上，并报告合成后实测到了什么。它**不**关闭 Master [#43](https://github.com/ProfessorZhi/llm-lab-portal/issues/43)，
> 也**不**代表九页业务工作台 [#56](https://github.com/ProfessorZhi/llm-lab-portal/issues/56) 完成。
>
> 验收契约：[Issue #59](https://github.com/ProfessorZhi/llm-lab-portal/issues/59)
> 候选分支：`integration/v6-lanes-candidate-20261016`
> 基线：`integration/v6-operations-linux-auth-20261016` @ `7fcc7d4fbbbf395b87e0e7251a4916856ec1e891`

---

## 1. 合成对象与顺序

七条 Lane 的远端 head SHA 在合成前**逐条重新 fetch 核对**，全部与工单记录一致；
每一条都作为 `--no-ff` merge commit 进入候选，因此各自的提交轨迹（含在途修复）都
保留在历史里，没有被压平。

| 顺序 | Lane | PR | 分支 | 合入的 head SHA |
| --- | --- | --- | --- | --- |
| 1 | IAM | #52 | `feature/v6-closure-iam-20261016` | `17abce7cbd6ac5d63a131011c94b86881d82c495` |
| 2 | Registry/CLI | #58 | `feature/v6-closure-registry-20261016` | `eaf7728b87dc11f45e5576d82e5290003151b320` |
| 3 | Data | #53 | `lane/data-observability-atlas-47-20261016` | `1a49570413eb6e96d156769d653a873857a2a735` |
| 4 | Evaluation | #55 | `lane48/evaluation-source-acl-20261016` | `141e92ae15067eb2d1f869a172c73224ccbb3dfe` |
| 5 | BFF | #57 | `integration/lane49-bff-source-guard-20261016` | `db229eee1b58ad566ebba34df61ccff800302ac7` |
| 6 | UI | #54 | `feature/v6-ui-lane44-nine-page-content-20261016` | `e0d4ff80ab56c7984aa965f106337a39ac376ef7` |
| 7 | Release | #51 | `release/v6-legacy-parity-ci-20261016` | `83824ce149d8207d3b347a017f69fdf87b5dd643` |

**未合入任何 Lane 分支之外的东西**；未 force push、未改 main、未动 8443 预览 checkout
或任何 Lane 的本地工作树。

## 2. 文件所有权冲突矩阵：**零文本冲突**

对每条 Lane 相对基线做 `git diff --name-only`，七集合两两求交：

- 七条 Lane 共触及 **106 个互不相同的文件**；
- **交集为空**——没有任何一个文件被两条 Lane 同时修改。

因此七次 merge **全部干净**，没有出现任何需要人工裁决的文本冲突。这不等于"没有
冲突"：真正的冲突是**语义**的（见下节），以及被写死在两条 Lane 里的**同一个事实**。

按目录看，所有权也是分开的：`backend/auth/**`（IAM）、`backend/registry/**` 与
`cli/**`（Registry）、`backend/observability/**` 与 `backend/atlas/**`（Data）、
`backend/evaluation/**`（Evaluation）、`backend/bff/**` 与 `tests/bff/**`（BFF）、
`web/**` 与 `prototypes/v6/**`（UI）、`docs/`+`.github/workflows/`+`tests/release/`
（Release）。

## 3. 唯一一个真实冲突：V6 摘要 pin（已按门禁要求重钉）

**现象。** 合成后可复现的失败只有一处，且只出现在 Release 的派生链门禁里：

```
FAIL tests.release.test_release_gates.FrozenDesignChainTests.test_v6_prototype_matches_its_pinned_digest
  AssertionError: '883a16c8…' != 'a7ba7a6c…'
FAIL tests.release.test_release_gates.FrozenDesignChainTests.test_extract_tool_pins_the_v6_digest_that_derive_produces
```

**原因（不是 merge 造成的，是两边各写死了一份"当前 V6 是哪一份"）。**
Release #51 在它**自己的基线** `7fcc7d4f` 上量到 V6 = `a7ba7a6c…`；UI #54 改动了
`prototypes/v6/index.html`，于是合成树派生出的 V6 = `883a16c8…`。Issue #59 预告的
正是这一条。

**处理（重钉，不是放宽）。**

- 三处 pin 全部更新为从**合成候选实测**得到的 `883a16c8d3e587c7e3f82682716ce60f2a828553bfeb7755f3af34a87a48561d`：
  `.github/workflows/atelier-v6-release-gates.yml` 的 literal、
  `tests/release/test_release_gates.py` 的 `FROZEN_V6_SHA256`、以及文档。
- **三处仍然是各自独立的字面量**，没有任何一处改成"运行时计算"。摘要是被**重钉**，
  不是被**放宽**——一个自己算自己的门禁，永远只会同意它自己。
- **冻结的 V5 权威 `04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf` 全程未变。**
  这正是"派生件动了"与"设计源被改了"的区别。

命令与实测：

```
$ git cat-file blob HEAD:prototypes/v5/index.html | sha256sum
04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf
$ git cat-file blob HEAD:prototypes/v6/index.html | sha256sum
883a16c8d3e587c7e3f82682716ce60f2a828553bfeb7755f3af34a87a48561d
$ node prototypes/v6/derive-from-v5.mjs --check        # ok
$ npm run verify:extract                               # ok: web/ matches generator output
$ git status --short                                   # 空 —— 派生链只读
```

**顺带修掉的既存文档漂移。** Release 在它的审计里记为发现 **F1**：`web/README.md`
与 `docs/v6-ui-flows.md` 把 V6 摘要写成 `fd3536e2…`。该值在本次合成**之前**就已经
是错的（比 `a7ba7a6c…` 还早）。既然重钉的范围包含"文档"，这两处已更新到
`883a16c8…`；历史运行日志与原始发现行**保留不动**，另加一段 Phase B 说明记录变化。

## 4. 合成后才成立的跨 Lane 验证

`tests/lanes_candidate/test_candidate_e2e.py`（**16 tests**）。单条 Lane 的单元测试
无法回答的问题，只有合成之后才能测：

1. **IAM #52 的 `ScopedTokenService` 直接插进 BFF #57 的 token 通道。** Phase A 当时
   只能对着一个"按 `TokenIntrospection` 字段形状写的 fixture"写契约，因为 IAM 的
   服务不在那条分支上；现在真实现就在树里，`introspect()` **无需任何适配层**即满足
   契约。实测：**轮换**后旧 token 下一次调用即 401、新 token 接上同一主体；
   **撤销**立即生效；主体被**暂停**后下一次调用即拒绝（issuer 的**当前**状态说了算，
   不是签发时的存档）；带有效 token 再伪造 `X-UID` 仍为 400。
2. **跨 UID 全链**：A 注册 → 共享给 B → B **invoke 成功** → A 撤权 → B **立即被拒**，
   且授权行是真的消失（不是打了个标记）。转发层是 **mock Gateway**，每一步都标
   `synthetic` —— 见 §6。
3. **合成后 fail-closed 三件套同进程成立**：匿名 401、陈旧 Host 421（在任何凭据被读
   之前）、客户端自报来源 400、无 CSRF 写 403 且状态未变。

### 一个新记录下来的集成事实（不是缺陷）

一条 CLI token 即使带着 `observability:self:read`，在 `/api/v2/ops` 上的
`scope.scopes` 也是**空的**——因为 `portal:*` 是**前端能力词表**，模块 scope 不在
其中（两个词表各管各的面，见 `backend/bff/scopes.py`）。对一条 CLI token 这是**正确**
的；但它意味着"给 token 加个模块 scope，Web 上那页就能亮"这个直觉是错的。已写进
测试钉住，以免将来有人往 `portal_scopes` 里塞模块 scope 来"让它能过"。

## 5. 合成后实测（CPU-only，全部在本机 Dell 上真跑）

平台：Dell-LLM-Lab · Python 3.14.4 · Node v22.23.3 · 全部 `CUDA_VISIBLE_DEVICES=''`、
`nice -n 19`。开工前实测 load **1.99**、可用内存 **16 GiB**。

| 命令 | 结果 |
| --- | --- |
| `python3 -m unittest discover -s tests -t .` | **Ran 3320, OK (skipped=6)**, 204.4s |
| `npm test` | **tests 471 · pass 470 · fail 0 · skipped 1** |
| `node prototypes/v6/derive-from-v5.mjs --check` | ok（V5 `04003f5c…` / V6 `883a16c8…`） |
| `npm run verify:extract` | ok: `web/` matches generator output |

分域复核：

| 域 | 结果 | 对应验收条款 |
| --- | --- | --- |
| `tests/auth` | Ran 425, OK (skipped=1) | 受信 Principal、token 签发/轮换/撤销 |
| `tests/registry` | Ran 408, OK | Gateway 每次调用的 scope/owner ACL、router ack |
| `tests/observability` | Ran 608, OK | Host/Atlas 严格新鲜度、合成源不得为 live |
| `tests/evaluation` | Ran 476, OK | EvalScope vs legacy 分源、owner/UID 归属、无主旧 run 拒绝 |
| `tests/bff` | Ran 298, OK | source / Host / CSRF fail-closed、伪造 UID |
| `tests/lanes_candidate` | Ran 16, OK | 跨 Lane 接缝（本文件 §4） |
| `tests/release` | Ran 53, OK | 派生链 pin、发布门禁 |

## 6. 明确未执行 / 不可声称

| 项 | 状态 | 依据 |
| --- | --- | --- |
| 真 Chrome 九页 × 宽度 × 主题 | **SKIPPED（≠ PASS）** | 本机无 Chrome/Chromium，`google-chrome`/`chromium`/snap 均不存在，`CHROME_PATH` 未设，只有 harness 不接受的 firefox。浏览器套件本次**未执行**。 |
| GitHub Actions | **EXTERNAL BLOCKED（≠ green）** | 实测 `gh api .../actions/runs/<id>/jobs`：**每个 job 都 `steps=0`、`runner_name` 为空**，即从未拿到 runner。因此 run 的 `conclusion=failure` 是**基础设施**结果，**不是**任何测试失败的证据；反过来也不构成任何通过。 |
| 真实 PAM | **未接线** | 登录路径以 503 `pam_unavailable` 诚实收场；本候选未安装、未修改 `/etc/pam.d`。 |
| 真实 Gateway | **未接线** | 合成链里的转发层是 `MockGatewayAdapter`，输出标 `synthetic`；`transport.mode` 只能是 `demo`。本候选未改动运行中的 Gateway(`:8200`)。 |
| 部署 | **NOT DEPLOYED** | 未重启、未改 systemd/Caddy/端口、未动 Strata(`:8080`)、8443 预览或任何人的 GPU 作业。 |
| 九页业务内容 | **未完成** | 归 [#56](https://github.com/ProfessorZhi/llm-lab-portal/issues/56)，**OPEN**。UI #54 关闭的是默认 live 门控与死按钮，不等于业务工作台交付。 |

## 7. 仍然成立的 blocker（不因本次合成而关闭）

1. **无 runner ⇒ 所有 Lane 的 CI 门禁目前都不生效。** 本地绿 ≠ CI 绿；本候选只声明
   本地实测。
2. **无浏览器 ⇒ 浏览器验收无法在本机取证。** 任何"Dell 上浏览器通过"的说法都不成立。
3. **真实 PAM / Gateway / Router 未接线** ⇒ 合成链是**可信 synthetic loopback**，
   每一步都 `NOT PRODUCTION VERIFIED`。
4. **`portal:*` 与模块 scope 的词表映射**（§4 末）需要产品决策：CLI token 是否应当
   在 Web 上具备任何能力。
5. **BFF 尚未承接的四条同源入口**（Data/Registry 侧数据面）：`/api/atlas/calls`、
   `/api/atlas/modules`、`/api/infra/summary`、`/api/v2/registry/*`——Phase A 记录在
   案；合成候选**仍未**接线，界面必须显式 disabled/unconfigured，不得假装 LIVE。
6. **跨 Lane 的分支不会被本候选自动同步**：本候选是**快照**。任何 Lane 之后再推 fix，
   都需要重新合成与复测。

## 8. 结论

**合成是干净的**（零文本冲突），**唯一真实冲突**（V6 摘要 pin）已按门禁要求**重钉**
而非放宽，冻结的 V5 权威未变。合成后 CPU-only 实测：Python **3320 / OK**、
Node **471 (470 pass, 0 fail)**、派生链两项 `--check` 均 ok。

但按 [#59](https://github.com/ProfessorZhi/llm-lab-portal/issues/59) 的验收口径，
**没有真实 CI runner、浏览器、PAM 与 Gateway E2E 门禁之前，结论是 NO-GO**。
本候选**仅供评审与复现**，不构成部署批准。
