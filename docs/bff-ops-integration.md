# 同源 BFF：`portal-ops/1` 与 Linux 会话移交（`integration/v6-linux-users-20261015`）

> 这份文件描述**集成分支自身**新增的那一层，以及它与三块已合入模块的接缝。
> 它不重复各模块自己的文档：`docs/registry-cli-contract.md`、
> `docs/linux-pam-login.md`、`docs/v6-ui-flows.md` 仍然是各自的权威。

## 0. 一句话

把 **V6 前端已经写好的 `portal-ops/1` 契约**接到 **Registry 的登记与 ACL** 上，
身份只来自 **Portal 自己的会话 + Linux 账号目录**，会话由 **既有的 `SessionManager`**
签发。新增代码只有 `backend/bff/`，没有第二套会话、第二套 ACL、第二套 DTO。

## 1. 来源与可审计性

| 内容 | 来源 |
| --- | --- |
| 基线（V5 外壳 + 我的调用 + 模块目录） | `feature/portal-self-usage-logs-20261015` @ `372a63f0`，已是本分支祖先 |
| V6 管理后台前端 | `feature/v6-ops-ui-20261015` @ `36f22bc9`（PR #19），本分支的起点 |
| Linux 账号 + PAM broker 契约 | `feature/linux-pam-auth-20261015` @ `aae4e8a1`，`--no-ff` 合并，见 `cf1b5b0` |
| Engine/Model/Deployment/Endpoint 登记与共享 + CLI | `feature/registry-cli-20261015` @ `c6bb50a6`，`--no-ff` 合并，见 `2911d88` |
| EvalScope 评测中心平台层 | `feature/evalscope-center-20261015` @ `77a15ac6`，`--no-ff` 合并，见 `ccb6b91` |
| 监控与日志后端 | `feature/observability-20261015` @ `7170cd5b`，`--no-ff` 合并，见 `6ebed54` |

两个模块分支都以 `6796b62` 为基点、且是纯新增目录，因此合并本身是加法的；
唯一的 add/add 冲突是 `tests/__init__.py`（UI 侧为空文件），取 registry 侧内容。

四个模块分支都以 `6796b62` 为基点、且都是纯新增目录，因此合并本身是加法的。
两次评测/观测合并的 working tree 都是干净的（无冲突）；唯一需要处理的
``tests/__init__.py`` 是「空文件 vs 有内容」，保留本分支已有的非空版本。

**未合并**：无。四个独立模块分支现已全部合入。

## 2. 路由

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/v2/ops[?visibility=private\|shared]` | 管理后台的全部数据 |
| `PUT` | `/api/v2/ops/deployments/{id}/share` | 改一个部署的共享范围，body `{mode, principals[]}` |
| `GET` | `/api/v2/ops/usage[?scope=self\|global]` | 完整用量 DTO（观测模块自己的形状） |
| `GET` | `/api/v2/ops/engines` | 引擎状态 DTO（观测模块自己的形状） |
| `POST` | `/auth/linux/login` | Linux 账号 + 密码登录（需 CSRF + 同源） |
| `GET` | `/auth/linux/status` | 这条登录路径此刻的真实状态 |

其余 `/auth/*` 原样交给既有的 `backend.auth.routes.Application`——它才是那条路径的
唯一实现者。

响应形状与前端状态机对齐：成功 `{"data": {...}}`，失败
`{"error": {"code", "message"}}`；`401` → 未登录，`403` → 无权限，其余 → UNKNOWN。

## 3. 身份：只从会话来

```
cookie llm_lab_session
  → backend.auth.SessionManager.authenticate      （既有的会话实现，唯此一处）
    → User（auth_source 必须是 linux_pam）
      → external_id "host:uid" 取 uid
        → PwdSource.by_uid(uid)                    （账号目录，权威）
          → RequesterContext(principal, uid, linux_user, scopes, roles)
```

* **主体名**是 `backend.auth_linux.handoff.linux_user_id()` 建的那个
  （`linux:<host>:<uid>`），按 host + uid 而不是登录名——改名不换人。
* **scope** 由**受信角色**推出（`backend/bff/scopes.py`），角色只来自 PAM 策略；
  `platform_admin` 只能由 root 拥有的授权文件（按 host + uid + 名字三项匹配）产生。
  这台机器上**那份文件不存在**，因此 `alice` 此刻默认拿不到管理员——
  开发进程里要显式 `--platform-admin alice`，且只作用于该进程内存。
* **角色不产生权限**：`portal:*` 与 `registry:*` 两套 scope 由同一个角色推出，
  `tests/bff/test_scopes.py` 断言「两个 admin read 恒同进同出」。
* 请求头里的 `X-UID` / `X-Admin` / `X-Role` / `X-Owner` 等（清单直接复用
  Registry 的 `FORBIDDEN_IDENTITY_HEADERS`）**出现即 400**，不是「读到不用」。
* Feishu / local-admin 会话仍然有效，但**不产出 Registry 主体**（403
  `linux_auth_not_enabled`）：前者没有 uid，后者是演示用的本地管理员。

## 4. 数据：形状归前端，可见性归 Registry

`backend/bff/ops.py` 只做两件事，且都借用模块自己的实现：

* **取数**走 `RegistryService.policy(requester)` → `AccessPolicy.visible_*()`，
  行级 ACL 的**唯一**判定处；
* **塑形**走 `registry.dto.render_deployment` / `render_endpoint` /
  `scope_block` 与 `registry.health` 的 health dict。

BFF 里没有一处自己判断「谁能看见什么」。

### 记录映射

| 前端字段 | 来源 |
| --- | --- |
| `owner_uid` / `scope.subject` | 主体名（字符串）；前端契约要求二者相等 |
| `engine_type` | Engine 的 `protocol`（封闭取值）。`engine.name` 是用户自起的字符串，用它既不稳定也不可比 |
| `health` / `runtime_status` | Registry health 的 `state`。`healthy` 必然映射成 `running`——「健康却没在跑」是契约直接拒绝的组合 |
| `health_reason` / `runtime_reason` | **透传** Registry 的 reason，不翻译成更好看的词 |
| `resources.*` | 全为 `null`：没有 NVML 来源，就不给 GPU 数字 |
| `share` | Endpoint 的 visibility（见下） |
| `registry.all[]` | 仅 `portal:admin:read`；非管理员恒为 `[]`（契约会把非空判为违约） |

### 共享范围：一个 Deployment 可能挂着多个 Endpoint

前端每行只有一个共享控件，而 Registry 的可见性挂在 **Endpoint** 上。合成规则是
**取下界**：只要有一个 Endpoint 还是 `private`，这一行就报 `private`；
`selected` 时受托人取**交集**，交集为空则退回 `private`。宁可少报可见性，
也不多报。写路径作用于该 Deployment 名下**属于我的全部** Endpoint，
因此写完之后这一行的读数就是准确的。

受托人解析接受 **UID**（`1004`）、**主体名**（`linux:dell-test-01:1004`）或
**Linux 账号名**（`dave`），三者都必须在受信目录里查得到，且都以
`PrincipalDirectory` 为存在性与审批状态的唯一裁判。最终写进 ACL 的**永远是主体名**。

**没有别名表。** `erin` 不是 `dave`：前者只会「查无此人」而被拒。
`tests/bff/test_sharing.py` 与 `tests/bff/test_two_user_acl.py` 都断言这一点。

## 5. 观测与评测：接线了，但「能不能显示数字」由契约决定

两个模块都通过 adapter 接进了同一个 payload（`backend/bff/observability_adapter.py`、
`backend/bff/evaluation_adapter.py`），装配在 `backend/bff/stacks.py`，默认值全部是
「本部署此刻诚实的取值」。**接线不等于数字会出现**，这一节就是那件事。

### 观测

| 前端槽位 | 接的东西 |
| --- | --- |
| `metrics.items[]` | 观测模块的读数（`principal.*`，管理员再加 `node.*` / `device.*`） |
| `trends` | `build_timeline_dto` 的分桶 → 请求 / tokens / 延迟三条序列 |
| （无槽位） | `build_engine_status_dto` → `GET /api/v2/ops/engines` |

**指标卡一律 UNKNOWN，这是契约决定的，不是本层偷懒。** 前端
`contract.js` 把「带数字的指标」绑死在白名单来源上：

```js
VERIFIED_METRIC_SOURCES = ['gateway-usage-export', 'nvml-verified', 'prometheus-readonly']
```

本部署的观测数据来自 `synthetic-fixture`（开发/演示的合成样本），**不在白名单里**。
所以带数字的读数在 `_metric_card` 里被降级成 `value: null` + `state: unknown`，原因写
`no_verified_metric_source`（前端有对应的中文标签）。把它标成
`gateway-usage-export` 会让 UI 把一个没人量过的数字渲染成「已验证」——那正是这一层
存在的意义所在。**这是一处契约级缺口**：卡片槽位没有合成数的出口，曲线槽位有。

**曲线可以有合成数**，代价是必须自报：`trends.synthetic = true` + 原因，前端把它画成
`SYNTHETIC · …` 的注脚。缺测的桶在序列里是 `null`（观测模块只返回有观测的桶，本层用
`expected_bucket_starts` 重建等距轴并补 `null`）；**全为缺口的序列直接不发**，因为
「一条平线」与「没有这条序列」是两回事。

一处真实的模块级不对称：`usage.self` 与 `usage.admin` 的桶**不带 tokens**
（`_bucket_block(..., with_tokens=False)`），只有 `timeline` 带。所以全局 token 序列在
模块层面就不存在——本层因此不会发出它，而不是发一条全 `null` 的曲线。

**整卡显存结构上不可能按人给**：观测模块的 `device.*` / `node.*` 属于
`PHYSICAL_SCOPES`，读数里没有 `uid` 字段，catalogue 里也没有
`principal.gpu.*`。本层只在会话持有 `observability:host:read` 时请求主机读数，
并且照原样透传。

### 评测

| 前端槽位 | 接的东西 |
| --- | --- |
| `evaluation.runs[]` | `EvaluationService.jobs` → 每行一条任务 |
| `evaluation.submit` | 模块自己的准入决定 |
| （无槽位） | `module_gate` / `admission_view`（`/usage`、`/engines` 之外未另开路由） |

* `scope` 按主体算：管理员看别人的任务是 `global`，自己的仍是 `mine`；契约要求
  `global` 必须伴随 `portal:admin:read`，而那条 scope 与其余三个 admin read 同源。
* `metric` 只在 `status == "done"` **且**确有命中样本时才有数字；被取消的任务
  （模块的 `canceled`）在前端词表里没有对应值，如实报成 `unknown` 并给出原因。
* `model_id` 由 BFF 用 Registry 的连接补上：评测任务的目标是一台 **deployment**，
  而前端那一列叫「模型」。取不到就退回 deployment id，不编。
* **`submit.ready` 恒为 false**，原因是模块自己的准入原因
  （默认 `admin_dispatch_disabled`）。真实派发在本部署有三道锁：准入策略
  `AdmissionPolicy.closed`、没有任何 adapter 能 `run()`（`_EvalScopeAdapterBase.run`
  无条件抛 `RunnerUnavailable`）、以及从不签发 `evaluation:dispatch:real`。

### 引擎状态

观测侧的 `EngineRegistry` **由 Registry 的可见引擎/端点现算**
（`engine_registry_from_bff`），不维护第二份名单。映射以 **endpoint 为键**——
观测模块的可见性口径就是「endpoint 在不在 `EngineRegistry.endpoints()` 里」。
（早先按 engine 建键的版本会让非 owner 的可见集恒为空，结果是「管理员的全局曲线
一条都没有」；那是接错线，不是安全。）

## 6. 离线 Mock E2E 纵切（四角色）

```bash
python3 -m unittest discover -s tests/bff -t .        # 190 个用例
node --test tests/web-features/ops-bff-payload.test.js  # 12 个用例
```

`tests/bff/test_cross_module_e2e.py` 用一个文件跑完整的四角色矩阵：

| 角色 | uid | 看得到什么 | 看不到什么 |
| --- | --- | --- | --- |
| `alice` | 1000 | 三个模块的全局 metadata、全员名单、全局曲线 | 任何原文 / 报告内容 |
| `bob` | 1002 | 自己的登记、自己的用量、自己的评测队列 | 别人的行 |
| `carol` | 1003 | 同上（一个无关的第三人，用来证明共享是**点名**的） | 别人的行 |
| `dave` | 1004 | bob 共享给他的那条 Endpoint，以及**自己的**用量 | bob 的用量与评测任务 |

```
A 登记 engine → model → deployment → endpoint（出生即 private）
  → B 看不见（私有视图 0 行；直接读是 404 而不是 403）
    → A 按 **uid 1004** 共享给 B
      → B 在共享视图看见它，并通过 MockGatewayAdapter **调用成功**
        → A 撤销 → B 立刻失去可见性与调用权，授权行被删除
          → 管理员在三个模块里都看到全局，且看不到任何原文
```

**「B 看不到 A 的用量」是可观察的**：合成语料刻意让每个 key 的行数不同
（`per_key + position % 3`），否则两边恰好相等，隔离做没做都看不出来。

**跨语言那一半**：`tests/bff/make_golden.py` 用真实请求路径生成三份 payload
（成员 / 被共享者 / 管理员），**钉住 Registry 与评测两处的 id 工厂 + 冻结时钟**后
逐字节稳定，提交在 `tests/web-features/fixtures/bff-ops-payloads.json`；Python 侧断言
「重新生成是空操作」，Node 侧把同一份文件喂给浏览器**自己的** `validateOps` 与
`renderPanels`。（评测的 `new_job_id` 是随机的，且 `service` 用
`from .models import new_job_id` 直接绑名——两处都要钉，少一处 fixture 就不可复现。）

## 7. 本分支明确**不**声称的事

1. **没有真实 PAM。** `backend/auth_linux/pam_backend.py` 只提供
   `UnavailablePamBackend`，它拒绝一切；没有 ctypes / C 互操作代码。
   `/auth/linux/status` 因此返回 `password_verification: "unavailable_in_this_build"`。
   一个返回 `authenticated` 的测试替身**不是**「真实密码可用」的证据。
2. **没有任何 Linux 账号的密码被校验过**，也没有读取过 `/etc/shadow`。
3. **没有连接真实 Gateway / Prometheus / Grafana / NVML / EvalScope。**
   观测数据是合成语料（`fixture: "SYNTHETIC-BFF"`），引擎探针是
   `MockEngineProbe`，主机读数是 `MockHostCollector`。因此指标卡全部 UNKNOWN，
   曲线全部标 SYNTHETIC。
4. **没有跑过任何真实评测。** 任务只入队、永不派发；`metrics` 里的分数是
   `MockRunner` 的合成样本，不是测量值。
5. **没有签发、轮换或撤销任何 API key。** 页面上出现的只有掩码前缀。
6. **没有部署、没有 sudo、没有常驻服务。** `python3 -m backend.bff` 是一个前台
   环回进程，拒绝绑定 `0.0.0.0` 与 **8080**（上游 Gateway 端口）。
7. **没有生成任何截图**，也没有跑浏览器 E2E（本机无 Chrome，见 §8）。
8. **没有改任何 V5/V6 视觉**：`prototypes/v5/index.html` 的 sha256 前后一致
   （`04003f5c…`），`web/` 目录**零改动**——观测与评测两段是前端**已经**写好的
   面板，接线之后它们自己就有了内容，不需要动一行 UI 代码。

## 8. 验收记录（Dell，Linux，bob，2026-10-15）

| 命令 | 结果 |
| --- | --- |
| `python3 -m unittest discover -s tests -t .` | **2397 passed**（skipped=5）。两个模块合入前 1485；合入后 2326；本轮 BFF 侧 190 |
| `npm test` | **340/341**，1 个**预先存在**的失败：`tests/serve.test.js:35`（为 Windows 写的 `path.sep` 断言，在 Linux 上自触发）。该文件最后一次修改在 `c621946`，早于本分支 |
| `node --test tests/web-features/ops-bff-payload.test.js` | **12/12 passed** |
| `npm run verify:extract` | passed — `ok: web/ matches generator output` |
| `node prototypes/v6/derive-from-v5.mjs --check` | passed |
| `sha256sum prototypes/v5/index.html` | `04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf`，与合并前一致 |
| 资源 | 全部 `CUDA_VISIBLE_DEVICES=''`、`nice -n 10`、纯 CPU；无 GPU 工作、无模型加载 |
| `npm run test:browser` | **BLOCKED** — 本机无 Chrome/Chromium，需 Windows CC 执行；本分支**未**声称浏览器 E2E 通过 |

## 9. 接口缺口与上生产的门槛

1. **真实 PAM 后端不存在**，broker 未安装，root 拥有的授权文件也未创建。
   在它落地之前，`platform_admin` 在真实部署里**无人持有**，管理员视图恒为不可用。
2. **真实 Gateway 未接线**。多引擎路由在**另一个仓库**的分支上，尚未合入或发布。
   接上之后要同时改 `RegistryConfig.mode` 与 `OpsConfig.transport_mode`，
   并让真 adapter 具备 `attached = True`；观测侧则要把 `Provenance` 换成非合成、
   把来源名换成前端白名单里的那一个，指标卡才会开始出现数字。
3. **前端契约的指标卡槽位没有「合成数」出口**（`trends` 有）。因此在本部署里，
   合成用量**只能**以曲线的形式呈现；要让卡片出现数字，必须真的接上
   `gateway-usage-export` / `nvml-verified` / `prometheus-readonly` 之一。
   这是一个跨模块的契约决定，需要前端与观测两侧一起改。
4. **观测的两套口径在桶上不对称**：`usage.admin` 的桶不带 tokens，只有 `timeline`
   带。全局 token 时间线因此取不到；需要观测侧在 `usage.admin` 的桶里也带上
   tokens（或另给一条时间线）。
5. **评测任务的 dataset 在排队阶段不可见**：`render_job_status` 不带 spec，
   数据集只在有结果时通过 `reproducibility` 出现。要让排队行也显示数据集，
   需要评测侧在任务 DTO 里带上它——本层没有绕到 service 背后去读 store。
6. **个人 API Key 的签发方未合入**；`keys[]` 渲染的是 Registry 已绑定的主体凭据
   （只有 label / scope / 掩码前缀），`created_at` / `last_used_at` 是 `null`。
7. **引擎状态只有接口，没有面板**。数据可得、受 scope 约束、形状就是观测模块自己的
   DTO，但 V6 现有面板没有引擎状态槽位；画出来属于 UI 变更，本轮不做（§7.8）。
8. **登录路由挂在 BFF**，而 `docs/linux-pam-login.md` 把 HTTP 路由划给
   `backend/auth/**` 的所有者。搬过去只需把 `sign_in_with_linux` 移过去。
9. **`docs/v6-ui-flows.md` §6 的集成清单仍写着「每一行尚未完成」。** 本轮把第 1、2、
   3、4、5、6 行都做实了，第 7 行（Gateway 计量来源）与第 8 行（个人 Token）
   仍未完成。该文件属于 UI 分支，本分支**未改**它——留给主控同步。
