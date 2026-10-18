# Atelier 06 · 管理后台 UI 流程与集成清单（V5/V6 派生，`feature/v6-ops-ui-20261015`）

> 本文件描述五套管理面板（模型与引擎、API 与调试、监控与日志、评测中心、系统管理）
> 的**交互流程、数据契约、网络边界、分角色行为与测试口径**，并给出**明确的集成清单**。
>
> **状态声明（先读这一段）。** 本分支是**纯前端 + 离线合成 fixture**，没有连接任何
> 生产服务，也不能操作任何模型。`backend/registry`、`backend/observability`、
> `backend/evaluation`、`backend/auth_linux` 由**其他独立分支**开发，均**未合并**，
> 契约**尚未稳定**。因此本文件里出现的所有 `/api/v2/…` 路径都是**前端提议的形状**，
> **今天没有任何服务端实现它们**。请勿把本页任何数字、任何入口当作 live。

---

## 0. 一句话

在**不修改任何生成产物**的前提下，把 V6 外壳的五个管理视图做成**可交互**的：
默认跑**进程内合成 fixture**并全程打 SYNTHETIC 徽章；每一个按钮要么真的做事、要么
`disabled` 并写明缺什么；真实身份 / 资源 / 凭据一旦缺失就 **fail closed**。

---

## 1. 视觉基准与"不改生成产物"的做法

派生链没有变，本分支**一个字节都没有动**它：

```
prototypes/v5/index.html   sha256 04003f5c…  冻结的设计权威，永不编辑
  └─ prototypes/v6/index.html   sha256 b05e7544…   05 + additions.{html,css}
       └─ web/{index.html,css/*.css}               由 web/tools/extract-from-prototype.mjs 生成
```

回归命令（本分支上均通过，见 §8）：

```bash
npm run verify:extract                            # web/ 仍等于生成器输出
node prototypes/v6/derive-from-v5.mjs --check      # 06 仍等于 05 + additions
```

**新增面板怎么进去的。** `web/index.html` 是生成物，手工编辑会破坏 `verify:extract`；
重新生成则要动 05 权威 —— 两者都禁止。所以 `web/js/features/controller.js` 的
`mountPanels()` 在**运行时**把 6 个 `<section class="page-block module-panel">`
挂进外壳**已有**的五个视图（`#view-models`、`#view-api`、`#view-observability`、
`#view-evaluation`、`#view-admin`），DOM 里用外壳自己的组件类：

| 用途 | 复用 class（来自 05 外壳或 06 的 `additions.css`） |
| --- | --- |
| 面板框 / 编号条 | `.page-block.module-panel`、`.editorial-index`、`.index-rule` |
| 卡片网格 | `.module-grid`、`.module-card`、`.module-card-sub` |
| 状态药丸 | `.state-pill` + `.state-available / -unavailable / -disabled / -unknown` |
| 键值行 | `.module-kv`（`dd.is-unknown`）、`.module-details` |
| 入口行 | `.module-links`、`.secondary`、`.unavailable`、`.requires` |
| 视图 / 范围开关 | `.seg` + `.seg button.active`（当前项**可见**） |
| 服务端判定的只读口径 | `.seg-readonly` |
| 表格 | `.tablewrap`、`.rowtitle`、`.rowmuted`、`.key` |
| 提示 / 警告 | `.guard`、`.guard.warn`、`.surface`、`.tags`、`.tag` |
| 其他 | `.headrow`、`.group-title`、`.section-h`、`.smallmeta`、`.caps`、`.mono`、`.tiny`、`.field`、`.selector`、`.columns`、`.flatpanel`、`.statusrows`、`.statusitem`、`.codearea`、`.actions` |

**本层没有引入任何新 CSS 规则、没有新增样式表、没有改 token。** 唯一的例外是趋势图
`<svg>`：它的颜色写成内联 `stroke="var(--rust)"` / `fill="var(--dim)"`，引用的仍是 05
已有的 token，因此 dark/light 两套主题自动成立，且不需要第二条调色板。

**一处值得记下的细节。** 三组开关（可见视图 / 共享范围 / 趋势范围）都用外壳自己的
`.seg` + `.seg button.active` 表达"当前项"，**而不是**只写 `aria-pressed`：
`web/css/` 里**没有任何** `[aria-pressed]` 规则，只靠它的话三种状态长得一模一样，
"我现在是 Private 还是 All" 在屏幕上无法回答。`aria-pressed` 仍然保留（无障碍需要），
但**可见状态由 `.active` 承担**。禁用项同时带 `disabled`（命中全局 `:disabled{opacity:.7}`）
与 `.unavailable`，因此"不能操作"与"没被选中"是两种不同的样子。
（`tests/web-features/ops-render.test.js` 对这两点各有一条回归断言。）

> 如果将来**确实**需要新视觉（例如抽屉的新布局），正确做法是新建
> `prototypes/v7/`：`derive-from-v6.mjs`（哈希 06 后再读）+ `additions.{html,css}`，
> 再让生成器指向 v7。**不要**在本分支直接改 06 或 `web/`。本分支不需要，故未建。

---

## 2. 五个面板放在哪、长什么样

| 面板 `data-ops` | 所在视图 | 编号 | 内容 |
| --- | --- | --- | --- |
| `registry` | `#view-models` | 11 | 我拥有的部署 / 别人共享给我的 Endpoint、模型详情与登记抽屉、共享范围控制 |
| `keys` | `#view-api` | 12 | 个人 API Key（只出掩码前缀） |
| `playground` | `#view-api` | 13 | 受限试跑台 + SDK 请求示例 |
| `observability` | `#view-observability` | 14 | 指标卡（来源 + 更新时间）+ 我的/全局趋势 |
| `evaluation` | `#view-evaluation` | 15 | 评测队列与结果 + 提交入口 |
| `access` | `#view-admin` | 16 | 本次会话的可见范围、持有 scope、能力判定、部署范围视图 |

**DOM 对照（合成 Mock 的基线）**

```text
#view-models
├── [data-module="engines"]          ← 已合并的 V6 模块面板（不动）
└── [data-ops="registry"]            ← 本分支运行时挂载
    ├── .editorial-index  [data-ops-state="registry"]     "就绪 · 可见 PRIVATE|SHARED"
    ├── .demo-strip        "SYNTHETIC · …"                （live 时改成 "LIVE · …"）
    ├── .headrow > .seg-readonly + [data-visibility="private|shared"]
    ├── .group-title 我拥有的部署  (OWNED · n)
    ├── .module-grid
    │   └── article.module-card [data-deployment-card="dep-demo-1"]
    │       ├── h3 = model_label          span.state-pill = HEALTH
    │       ├── .module-kv 所有者 UID / Engine type / 运行状态 / GPU 卡数 / 显存总量 / 已登记
    │       ├── [data-drawer="deployment"] data-deployment="…"   模型详情 / 登记管理
    │       ├── .module-links 入口（被挡的入口无 href，只有 title=缺少的 scope）
    │       └── [data-share-mode="private|selected|all"] data-deployment="…"
    │           + [data-share-principals="…"]（Selected 时）
    ├── .group-title 别人共享给我的 Endpoint (SHARED TO ME · n)
    │   └── .module-details > .module-kv（只读：所有者、授权方、模式）
    └── [仅 portal:admin:read] .group-title 部署范围（admin 视角）
        └── .tablewrap：每个部署的 owner UID / 健康 / 共享范围（"自己"标 good badge）
```

**共享控制（`[data-share-mode]`）出现在"我拥有的部署"的每张卡上**：`private`（默认）/
`selected`（列出对象）/ `all`。三种都是**动作**，需要 `portal:endpoints:share`，由服务端
重新校验所有权，且**都可撤销**（切回 private 会清空对象列表）。而
`[data-visibility]` 的 private/shared 是**视图开关**，只在服务端已经算好的两个子集之间
切换 —— 两者语义不同，因此前者是真按钮、后者是 `.seg-readonly` 风格。

**admin 视角**不是"另一种共享控制"，而是同一件事的**只读汇总**：持有
`portal:admin:read` 时，"我拥有的部署"下方多出一张**部署范围表**，列出每个部署的
所有者与共享范围（含别人的）。它没有 `[data-share-mode]`，因为改共享只能由所有者对
自己名下的部署做。

---

## 3. `portal-ops/1` 契约（前端定义，服务端尚未实现）

浏览器侧校验在 `web/js/features/contract.js`（纯函数，无 DOM 无网络）。**出口拒绝的七类
形态**，每一类都是"这块屏会理直气壮地撒谎"的一种具体方式：

1. **没有 NVML 就没有 GPU 数字。** `resources.{gpu_count,vram_*}` 与任何 `gpu_*` 指标，
   只有在 `source === "nvml-verified"` 时才可以带值；带值时**必须**同时给出
   `updated_at`。其余一律 `null` + `state:"unknown"` + `reason`。
2. **链接不得离开源、不得指向上游端口。** 复用 `../modules/contract.js` 的
   `isInternalHref`（同一份实现，不复制）：非 `/` 开头、`//` 开头、含 `://`、含 `:8080`
   一律整份载荷拒绝——不是"抹掉地址再渲染"。
3. **"我拥有的部署"里不能有别人的部署。** `registry.owned[*]` 必须
   `owner_is_self === true` **且** `owner_uid === scope.subject`；`registry.shared[*]` 必须
   相反。同调用日志对 `key_id` 的那道防线。
4. **没有 `portal:admin:read` 就不能出现 `scope:"global"` 的评测行、全局趋势序列，
   或 `registry.all[]`（部署范围视图）。** 同"shared endpoint 泄漏进 private 视图"那一类。
5. **Key 只能是掩码。** `secret`/`key`/`value`/`plaintext`/`token` 是禁用字段，
   `prefix` 必须真的含掩码字符（`isMaskedPrefix`）。
6. **共享必须可撤销且自洽。** 缺 `share` 块按 `private` 处理；`selected` 必须列出对象；
   任何非 `private` 模式必须声明 `revocable: true`。
7. **合成数据不得自称 live**，live 必须声明 gateway 已接线；**合成趋势序列只能挂在
   已经承认自己是合成的载荷上**。

另外：数值缺失一路是 `null` → 渲染成 `—`，**永不**变成 `0`；`healthy` 与
`running` 不能同时为假；未测的部署（`health:"unknown"`）必须写原因。

**样例（本分支实际产出的形状，取自 `web/js/features/mock-fixture.js`，已脱敏为合成）**

```jsonc
{
  "schema_version": "portal-ops/1",
  "synthetic": true,
  "transport": { "mode": "demo", "gateway_attached": false, "synthetic": true, "label": "SYNTHETIC / 合成 fixture" },
  "scope": { "subject": "demo-user-a", "roles": ["user"],
             "scopes": ["portal:modules:read", "atlas:calls:self:read", "portal:endpoints:share"],
             "visibility": "private" },
  "registry": {
    "adapter_attached": false, "reason": "registry_adapter_not_attached",
    "owned": [{
      "deployment_id": "dep-demo-1", "model_id": "demo-llm-a", "model_label": "Demo LLM A",
      "engine_type": "strata", "owner_uid": "demo-user-a", "owner_is_self": true, "registered": true,
      "health": "unknown", "health_reason": "no_verified_metric_source",
      "runtime_status": "unknown", "runtime_reason": "registry_adapter_not_attached",
      "resources": { "gpu_count": null, "vram_total_mib": null, "vram_used_mib": null,
                     "source": null, "updated_at": null },
      "share": { "mode": "private", "principals": [], "revocable": true },
      "links": [], "updated_at": "2026-10-15T00:00:00Z"
    }],
    "shared": [{ "endpoint_id": "ep-demo-shared-1", "name": "colleague-chat",
                 "owner_uid": "demo-user-b", "owner_is_self": false, "granted_by": "demo-user-b",
                 "share": { "mode": "selected", "principals": ["demo-user-a"], "revocable": true } }]
  },
  "metrics": { "items": [{ "name": "gpu_vram_total_mib", "value": null, "state": "unknown",
                           "source": null, "updated_at": null, "reason": "no_verified_metric_source" }] },
  "trends": { "window": "24h", "synthetic": true, "source": null, "updated_at": null,
              "reason": "本构建没有接线 Gateway：这条曲线是确定性合成序列…",
              "series": [{ "name": "我的请求", "kind": "requests", "scope": "mine",
                           "unit": "次", "points": [/* 24 点，其中 1 点为 null（缺口）*/] }] },
  "playground": { "authorized": false, "backend": null, "audited": false,
                  "endpoint": null, "reason": "no_authorized_backend" },
  "evaluation": { "runs": [/* … */], "submit": { "ready": false, "reason": "evaluation_backend_not_ready" } },
  "admin": { "available": false, "reason": "missing_required_scope", "principals": [] },
  "notes": ["…"]
}
```

---

## 4. 网络边界（第三处有据例外）

`web/README.md` §7.1 记录了三处 `fetch` 例外。本层的唯一一次在
`web/js/features/source.js`，且：

* **默认不开网络。** 默认 `mode = mock`，由 `mock-fixture.js` 在**进程内**用一个
  `fetch(url, init)` 形状的处理器作答，一个 socket 都不开；测试用
  `fetchImpl: forbiddenFetch` 断言这一点（任何请求都会抛）。
* **`?source=live` 才会问网络**，且必须**注入同源 base**（`globalThis.__LLM_LAB_OPS__.opsBase`）。
  没有 base → `unavailable`；base 是绝对 URL / 协议相对 / 含 `:8080` → `unavailable`
  且原因不同（`endpoint-is-not-same-origin`）。**没有静默回退到 fixture。**
* **没有环回以外的地址。** 浏览器测试用
  `--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1` 让任何外部请求**失败**，
  并断言全量请求列表里没有非环回项（`tests/browser/ops-ui.browser.test.js` 最后一条）。
* **没有 8080 直连。** 受限试跑台在**没有经授权的交互式后端**时 `disabled`；本层代码里
  不存在任何指向上游推理端口的地址或代码路径，契约层也会拒绝 `playground.endpoint`
  指向非同一来源的路径。

---

## 5. 分角色行为（权限**只**由服务端断言的 scope 决定）

`web/js/features/capability.js` 是唯一的判定点，输入只有 `scopes`。**角色（role）不产生
任何权限**——`deriveCapabilities` 接受 `roles` 只是为了能被测试证明"加 role 不改变任何
结果"；仓库里**没有任何账号名**参与判定（`tests/web-features/capability.test.js` 逐文件
扫描 `web/js/features/**`，出现 `alice`/`bob`/`carol`/`dave` 即失败）。

| 能力 | 需要的 scope | 不满足时的表现 |
| --- | --- | --- |
| `readCatalogue` | `portal:modules:read` | 目录面板显示原因；不显示任何部署 |
| `shareEndpoints` | `portal:endpoints:share` | 三个共享按钮 `disabled` + `title` 写明缺哪个 scope |
| `viewAllPrincipals` | `portal:admin:read` | 全局趋势开关与主体清单关闭 |
| `openEnginesNative` | `portal:engines:native` | 原生控制台入口无地址，只说明缺什么 |
| `openObservabilityNative` | `portal:observability:native` | Grafana/Prometheus 入口同上 |
| `openEvaluationNative` | `portal:evaluation:native` | EvalScope 原生入口同上 |

**演示 persona**（`mock-fixture.js`，全是合成账号，**不是**任何人的 Linux 账号）：

| persona | subject | scopes | 屏幕上的差异 |
| --- | --- | --- | --- |
| `user` | `demo-user-a` | modules:read + calls:self:read | 共享按钮全灰；无全局趋势；无主体清单 |
| `sharer` | `demo-user-a` | + endpoints:share | 共享按钮可点，可发布 / 可撤销 |
| `admin` | `demo-admin` | + admin:read | 主体清单、全局评测行、全局趋势序列、**部署范围共享表**（`registry.all`） |
| `ungranted` | `demo-user-c` | （空） | 页面仍渲染，但每个写操作都关着并说明原因 |

URL 开关：`?persona=user|sharer|admin|ungranted`、`?source=mock|live`。

---

## 6. 集成清单（**这是本分支与其他 CC 的交接面**）

> 每一行都**尚未完成**。前端已经按下面的形状写好适配器与 mock；服务端一旦给出稳定
> DTO，只需替换数据来源，UI 不需要改结构。

| # | 依赖 | 需要的服务端产物 | 前端已就位 | 阻塞点 / 待确认 |
| --- | --- | --- | --- | --- |
| 1 | `backend/registry` | `GET /api/v2/ops` 的 `registry` 段：`owned[]`（含 `owner_uid`、`engine_type`、`health`、`runtime_status`、`resources`、`links`）、`shared[]`，以及 **admin 视角的 `all[]`** | 契约校验、卡片、详情抽屉、共享控制、部署范围表、错误回显 | **契约未定**；`share.mode` 三值（private/selected/all）目前是前端提议，需要 registry 确认；`selected` 的对象选择器需要目录服务；`all[]` 的鉴权口径（是否严格 `portal:admin:read`）待确认 |
| 2 | `backend/registry` | `PUT /api/v2/ops/deployments/:id/share`（body `{mode, principals[]}`） | 适配器 + fixture 已实现同一路径与语义（403/404/400 均覆盖） | 需要确认：鉴权口径（`portal:endpoints:share` + 所有权）、审计事件、幂等性 |
| 3 | `backend/observability` | `metrics.items[]`（**必须**带 `source` 与 `updated_at`）与 `trends`（`synthetic:false` 时必须带已验证来源） | 指标卡、趋势图（缺口按缺口画）、我的/全局开关 | **全局范围的 gating 是前端提议**：现按 `portal:admin:read` 判定，需 observability/后端确认；`nvml-verified` 是否已有可信 NVML 观测源 |
| 4 | `backend/observability` | Grafana / Prometheus 的**同源内部路径** | 入口按 `links[]` 渲染，被挡时只说明缺什么 | 需要真实反代路径与 SSO 权限映射 |
| 5 | `backend/evaluation` | `evaluation.runs[]` + `submit.ready` / `reason` | 队列表、结果表、提交按钮（未就绪时 disabled） | 需要"就绪"的判定口径；提交任务需要的 scope 尚未定义（本分支**不猜**，因此按钮只由 `submit.ready` 控制） |
| 6 | `backend/auth_linux` | 会话主体与 scope 列表（`scope.subject/scopes/roles`） | 全部判定只读 `scopes` | 产品登录路径（Linux 账号 + 受限 PAM）**未上线**；本分支不把任何账号名当身份 |
| 7 | Gateway 计量 | `gateway-usage-export` 作为已验证来源 | 指标的来源白名单已含它 | 需要确认导出口径与时间戳语义 |
| 8 | 个人 Token 服务端 | 个人 Key 列表（**只**返回掩码前缀） | Key 表 + 掩码校验 | 见 `docs/` 中 personal API token 相关工作；本分支不签发任何 Key |

**前端在服务端就绪前不会做的事**：不伪造 GPU 数字、不预置"看起来像"的评测得分、
不开启试跑台、不给任何"点了没反应"的按钮。

---

## 7. UI 自动化：分角色矩阵与 Mock E2E

### 7.1 离线（不需要浏览器，本机可跑）

`npm test` 现在同时跑 `tests/*.test.js` 与 `tests/web-features/*.test.js`。

| 文件 | 覆盖 |
| --- | --- |
| `tests/web-features/capability.test.js` | scope→能力映射；**role 不产生权限**；畸形输入 fail closed；**账号名不得出现在 `web/js/features/`**（含反向对照） |
| `tests/web-features/ops-contract.test.js` | §3 的七类拒绝形态，逐条穷举（GPU 无 NVML、越权 owner、明文 key、不可撤销的共享、无 admin scope 的全局行、离开源/8080 的链接、合成自称 live…） |
| `tests/web-features/ops-render.test.js` | 每个面板的 HTML 内容：`—` + 原因、数值必带来源与时间、disabled 必须写原因、趋势缺口几何、转义 |
| `tests/web-features/ops-source.test.js` | 来源解析（默认 mock / 显式 live / 未知值 → unavailable）；同源强制；**mock 模式下一个 socket 都不开**；401/403/非 JSON/契约违规的映射；写路径的错误码透传 |
| `tests/web-features/ops-controller.test.js` | 控制器自身的逻辑（不需要 DOM 的那部分）：装载、状态机、能力派生、**未验证的写路径绝不上网**、抽屉无宿主时不抛异常 |
| `tests/web-features/ops-mock-e2e.test.js` | **Mock E2E**：`fixture → source → contract → capability → render` 全链路，含真实共享写入/撤销/审计；并结构化断言**每个可点按钮都绑定了控制器真的绑的钩子、每个 disabled 按钮都写明原因** |

分角色：上面每个文件都在 `user / sharer / admin / ungranted` 四个 persona 上跑。

### 7.2 浏览器 E2E（需要 Chrome）

`npm run test:browser` → `tests/browser/ops-ui.browser.test.js`：

* 六个面板挂进五个视图；生成文档结构未被改动（8 视图 / 5 模块面板 / 6 个 ops 面板 / 4 样式表）；
* **共享按钮点击 → 屏幕状态变化 → fixture 状态变化 → 撤销清空对象**；
* 无 `endpoints:share` 的会话：按钮全 `disabled` 且 `title` 写明；点击 disabled 控件不发请求；
* 试跑台发送与评测提交**永远** `disabled`；
* 新面板与已发布模块面板的**计算样式逐项相等**（视觉连续性）；
* 趋势图顶点全部落在 viewBox 内；
* **401 / 403 / 契约违规三条路径用 CDP `Fetch` 拦截做真实往返**：401→"请先登录"（每面板一致、`ops === null`）、403→"未获得模块目录授权"且**只请求一次**（不自动重试）、违规载荷→整页"暂时无法确定"且被拒的 GPU 数字**没有**出现在屏幕上；
* `?source=live` 无注入 base → `unavailable`，屏幕上**不出现** `SYNTHETIC` 也不出现"我拥有的部署"；
* 移动端 390px 不横向溢出；
* 全程零非环回请求，零 console error，零未捕获异常。

**本机（Dell, bob）没有 Chrome/Chromium**，因此这一套**全部 SKIP**
（`no Chrome/Chromium found — set CHROME_PATH to run browser checks`），
**不由本分支宣称通过**；需在 Windows QA 机独立执行。

### 7.3 合成 Mock 截图规格 / DOM 对照

截图**本分支未产出、也不宣称产出**（无浏览器）。规格如下，供有浏览器的机器执行：

| # | 场景 | URL | 视口 | 必含 | 断言口径 |
| --- | --- | --- | --- | --- | --- |
| S1 | 模型与引擎（我拥有 / 私有视图） | `/web/index.html#models` | 1440×950 | SYNTHETIC 条、两张部署卡、共享控制（灰） | 每张卡 `[data-deployment-card]`；`total .module-card === owned.length` |
| S2 | 模型与引擎（含共享） | `#models` 且切到 Shared | 1440×950 | "别人共享给我的 Endpoint" 区出现 1 行，含 `demo-user-b` | 私有视图下该行**必须不存在** |
| S3 | 模型详情抽屉 | 点 `[data-drawer="deployment"]` | 1440×950 | `<dialog id="explainDialog">` 打开，含 Deployment ID / 资源来源 / "backend/registry 尚未接线" | `dialog.open === true`，且资源行显示 `—` + 原因 |
| S4 | 共享控制（可发布） | `?persona=sharer#models` | 1440×950 | 三个按钮可点；点 Selected 后该按钮 `aria-pressed="true"` 且出现对象输入框 | 见 §7.2 第 2 条 |
| S5 | API 与调试 | `#api` | 1440×950 | Key 表（掩码前缀）、受限试跑台（发送 disabled + 原因）、curl 示例 | `input[aria-label="试跑输入"]` disabled；`select` disabled |
| S6 | 监控与日志 | `#observability` | 1440×950 | 6 张指标卡全 UNKNOWN 且各带原因；趋势曲线带 SYNTHETIC 说明；"全局" disabled | **页面上不出现任何 `⟪数字⟫ MiB`** |
| S7 | 评测中心 | `#evaluation` | 1440×950 | 队列表；"提交评测任务" disabled + 原因 | 未完成的行**没有**分数 |
| S8 | 系统管理（admin） | `?persona=admin#admin` | 1440×950 | 主体清单（3 行）、持有 scope、能力判定 | UID 列显示 `demo-*`；不出现任何真实账号名 |
| S9 | 系统管理（非 admin） | `?persona=user#admin` | 1440×950 | 无主体清单；写明缺 `portal:admin:read` | 页面上**不出现** `demo-user-b` |
| S10 | 移动端 | `?persona=sharer#models` | 390×844 | 单列卡片 | `scrollWidth <= innerWidth` |
| S11 | 深色主题 | `#models`，切主题 | 1440×950 | 同上，用 dark token | 与 S1 结构一致，颜色取自 `:root[data-theme=dark]` |

**DOM 对照**（与已发布的 `[data-module]` 面板比对，用于证明"同一套视觉"）：
对 `#view-models [data-module="engines"]` 与 `#view-models [data-ops="registry"]`
各取 `border-top-color / border-radius / background-color / padding-top / padding-left / font-size`
的 `getComputedStyle`，**逐项相等**（§7.2 已实现该断言）。

---

## 8. 本机验收记录（Dell, Linux, bob）

| 命令 | 结果 |
| --- | --- |
| `npm run verify:extract` | **passed** — `ok: web/ matches generator output`，`source sha256: fd3536e210c5…` |
| `node prototypes/v6/derive-from-v5.mjs --check` | **passed** — `ok: prototypes/v6/index.html matches v5 + additions` |
| `npm test` | **328/329 passed**，1 个**预先存在**、与本分支无关的失败：`tests/serve.test.js:35` "accepts a root written with forward slashes" 在 Linux 上因 `path.sep === '/'` 触发它自己的"必须与 REPO_ROOT 不同"断言（该回归守卫是给 Windows 写的）。基线 `372a63f` 上同样失败（基线为 140/141，同为这一条）。 |
| `npm run test:features` | **passed** — 188/188 |
| `npm run test:browser` | **blocked / skipped** — 本机无 Chrome/Chromium（无 `google-chrome`/`chromium`，`snap` 下也没有）。24 条用例全部以明确原因 SKIP，**未宣称通过**；需 Windows QA 执行。 |
| 测试进程资源 | 全部 `nice -n 10`、`CUDA_VISIBLE_DEVICES=''`、纯 CPU、无 GPU、无模型加载、峰值远低于 2 GiB |

---

## 9. 本分支明确**不**声称的事

1. 没有连接任何生产服务；没有读取 Gateway / Prometheus / Grafana / NVML / EvalScope。
2. 不能注册、部署、启动或停止任何模型；"登记管理抽屉"是**只读**的，且自己就这么写。
3. 没有签发、轮换或撤销任何 Key；页面上出现的只有掩码前缀。
4. 没有真实评测后端；"提交评测任务"永远 `disabled`。
5. 没有把任何账号名、角色当作权限；权限只来自服务端断言的 scope。
6. 没有产出任何截图；§7.3 是**规格**，不是产物。
7. `portal-ops/1` 是**前端提议**的契约，服务端尚未实现；路径 `/api/v2/ops` 目前没有任何
   服务在监听。
