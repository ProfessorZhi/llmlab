# V1 静态发布包与安全门禁（Lane Release/Packaging #69）

> 本文是**交接文档**，不是执行记录。本 Lane 未在 Dell、`/8443/`、Caddy 或 systemd 上
> 做任何发布动作；没有部署、没有重启、没有改任何现场服务。
>
> **Dell 侧状态：`HOST_PENDING`。** 打包器与门禁在东京 Linux 本机可复现；真实主机的
> 落地仍待 Owner 按其维护窗口执行，并仍受既有发布检查单的 H1–H6 人工判据约束
> （见 `docs/atelier-v6-release-checklist-2026-10-16.md` §1.2）。

## 1. 为什么需要这条 Lane

只读审计（2026-10-17，基于 PR #86 tip `c59d618e`）确认了三件事：

1. **仓库里没有发布产物定义。** 没有 `Dockerfile`，没有部署脚本，没有静态产物的
   allowlist。`docs/atelier-v6-service-hosting-and-rollback-2026-10-16.md` 明确写着服务
   文件只是"文档内嵌示例"。
2. **`web/` 是源码树，不是发布产物。** 同一目录里同时住着：
   - `web/qa.html` —— QA 构建，`data-build-profile="qa"`，`?demo=1` 可显示合成样例；
   - `web/tools/serve.mjs`、`web/tools/extract-from-prototype.mjs` —— 构建与开发服务器；
   - `web/README.md`、`web/auth/package.json` —— 文档与工程清单。
3. **现有的本地预览服务直接以 `web/` 为根**（`web/tools/serve.mjs`）。因此"把 `web/`
   原样拷到主机"这个最省事的做法，会把 QA 构建与构建工具一起发布出去。

结论：需要一个**只产出可公开发布资产**的打包步骤，且它必须能阻止"未来新增的 QA 类
入口未经批准自动发布"。

## 2. 打包器做了什么

`tools/release/build_static_bundle.mjs`：零依赖、无网络、无子进程、无特权，只写
`--out` 指定的目录，并**拒绝写进仓库工作树**。

它不是"复制目录"，而是**入口闭包**：

```
entry_points（allowlist 唯一授权）
   web/index.html
   web/auth/index.html
        │  跟随 src / href / 静态 import / export-from / 动态 import / url() / @import
        ▼
   传递闭包  →  只发布这些文件
```

三条 fail-closed 规则：

| 规则 | 行为 |
|---|---|
| 闭包需要一个**被拒**的文件（如 `qa.html`、`tools/**`、`**/package.json`） | **构建失败**。跳过会产出运行时坏掉的包。 |
| 闭包需要一个**未获批准的 `.html`** | **构建失败**。这正是"新增 QA 入口被自动发布"的防线。 |
| 任何被发布的 HTML **声明**了非 `production` 的 `data-build-profile` | **构建失败**。 |

关于最后一条的精确口径：`web/js/build-profile.js` 把**任何不等于 `qa` 的值**（含缺失）
解析为 `production`，所以"缺失"在运行时是 fail-closed 的，不构成洞；它会被记录在
manifest 的 `undeclared_profile` 里让复核者看得见。当前唯一一项是
`auth/index.html`（独立的登录文档，不加载应用 bundle）。给它补一个属性是 UI Owner
文件里的一词改动，**本 Lane 不改 `web/**`**，已在 §7 作为契约交回。

## 3. 允许清单

`tools/release/release-allowlist.json`（路径相对 `web/`）：

```json
{
  "schema": "atelier-v1-static-release-allowlist/1",
  "source_root": "web",
  "entry_points": ["index.html", "auth/index.html"],
  "deny_paths": ["qa.html", "tools/", "README.md"],
  "deny_globs": ["**/qa*.html", "**/*.test.*", "**/package.json", "**/*.md",
                 "**/fixtures/**", "**/*.log", "**/*.map"],
  "require_html_profile": "production"
}
```

`entry_points` 是唯一的发布授权。新增任何入口都必须改这个文件，因此是一次可审阅的
发布决定，而不是 `git add` 的副作用。

## 4. 怎么构建与验证

```bash
# 1) 构建（产物只落在你给的目录；拒绝写入仓库工作树）
node tools/release/build_static_bundle.mjs --out /tmp/atelier-v1-bundle

# 2) 静态门禁（17 项，无网络、无浏览器、秒级）
python3 -m unittest tests.release.test_v1_static_bundle -v

# 3) 浏览器 smoke（真 Chrome + CDP，27 项）：CI 里由 browser job 直接执行，
#    无 Chrome 时该 job 失败（fail-closed），不再有 skip 通道
node tools/release/smoke_static_bundle.mjs /tmp/atelier-v1-bundle --json
```

`--source <checkout>` 可对另一份检出构建（测试用它验证 fail-closed 场景）。

## 5. 产物与哈希

本机基于 **PR #86 tip `c59d618e3bef56d9487add367b0c1bad25fff454`** 构建：

| 项 | 值 |
|---|---|
| 文件数 | **65**（+ `release-manifest.json`） |
| 总字节 | **688,289** |
| **bundle sha256** | `a2dc0ee92b74fa9d480313b93f51acf7e273016ff5a017b8293f472973cf384f` |

`bundle_sha256` = 对按路径排序的 `path:sha256` 行取 sha256。同输入必得同值（有回归测试）。

<details>
<summary>完整文件清单（字节 / sha256 前缀）</summary>

| 路径 | bytes | sha256 |
|---|---:|---|
| `auth/auth-core.js` | 34041 | `9a3c368c24ce4762…` |
| `auth/auth.css` | 13846 | `37d6c3028468e542…` |
| `auth/auth.js` | 27269 | `4bc73d26fd0c5e0c…` |
| `auth/index.html` | 9358 | `a670a1cf4f4e20ad…` |
| `css/atlas.css` | 14052 | `474402397fe38c51…` |
| `css/base.css` | 45100 | `b19d4da1063869e0…` |
| `css/portal.css` | 29113 | `81ccaa59be86a271…` |
| `css/tokens.css` | 5711 | `4fdca795a1eca198…` |
| `index.html` | 42441 | `79d846a98eaed5eb…` |
| `js/app.js` | 11175 | `e8241d3d6b167570…` |
| `js/atlas/binding.js` | 2100 | `b6b38ac47ba56795…` |
| `js/atlas/controller.js` | 13719 | `4142ad2b346eda3a…` |
| `js/atlas/exporters.js` | 2444 | `f655a86007bc26f9…` |
| `js/atlas/geometry.js` | 1414 | `acc043b65c8ed5e6…` |
| `js/atlas/heat.js` | 2671 | `61ecf44780966722…` |
| `js/atlas/render-analysis.js` | 4764 | `902c0bd452a01a97…` |
| `js/atlas/render-detail.js` | 2899 | `7941661115f0b272…` |
| `js/atlas/render-donut.js` | 1935 | `6f5b3149d1726d25…` |
| `js/atlas/render-dots.js` | 2978 | `b1337dcc9fd228d2…` |
| `js/atlas/render-empty.js` | 984 | `9183700c7061fdd9…` |
| `js/atlas/render-heatmap.js` | 3512 | `c20efa9e4c50c9c9…` |
| `js/atlas/render-kpis.js` | 1959 | `63e6864850784b66…` |
| `js/atlas/render-ranking.js` | 3901 | `3661fa5c36ba45af…` |
| `js/atlas/render-sunburst.js` | 2845 | `e7cf503e7b114206…` |
| `js/atlas/render-treemap.js` | 2221 | `3ac0c9c4a9a00c96…` |
| `js/atlas/selectors.js` | 3398 | `f23a566c0d25543a…` |
| `js/atlas/squarify.js` | 1923 | `d272163c48688efe…` |
| `js/atlas/structure.js` | 1002 | `28dfdb8a5c9174cb…` |
| `js/atlas/theme-colors.js` | 667 | `ed59694d20628343…` |
| `js/atlas/tooltip.js` | 988 | `4664a799f59a16ec…` |
| `js/build-profile.js` | 2323 | `f53dcd024c53c965…` |
| `js/calls/contract.js` | 12046 | `bbe5943745bdabc5…` |
| `js/calls/controller.js` | 16531 | `55552653c48abe08…` |
| `js/calls/demo-fixture.js` | 8946 | `f790fdea67331c75…` |
| `js/calls/insights.js` | 9922 | `5476f9aa03d1f6e3…` |
| `js/calls/render-log.js` | 12728 | `9da4e7d4ea9ea36e…` |
| `js/chat.js` | 4496 | `aadf7e56f6dbb1de…` |
| `js/compute/view.js` | 12488 | `a26229ae69e2837f…` |
| `js/data/contract.js` | 8121 | `61bb8e8edcf8ecea…` |
| `js/data/demo-fixture.js` | 3048 | `b796fe247a47d469…` |
| `js/data/remote-client.js` | 2847 | `db47522c6489cc3e…` |
| `js/demo-mode.js` | 11762 | `7cc19f6e33377eb5…` |
| `js/dialog.js` | 3188 | `ecbe3a032b3ff113…` |
| `js/dom.js` | 1462 | `f1fdfcb6b117da8e…` |
| `js/events.js` | 312 | `eeac82d5020c5125…` |
| `js/features/admin-mode.js` | 11614 | `553e0b756e4d3eb1…` |
| `js/features/capability.js` | 6777 | `d0cdf6097c52c4ea…` |
| `js/features/contract.js` | 36491 | `d4262003d097fa95…` |
| `js/features/controller.js` | 20228 | `fe208aac786b8d96…` |
| `js/features/mock-fixture.js` | 20306 | `70636fa33527b277…` |
| `js/features/render.js` | 45374 | `e5ba5da179fe10d4…` |
| `js/features/source.js` | 19216 | `c29bf32d33f05a23…` |
| `js/features/trend.js` | 3453 | `0d5e8dcf178b8132…` |
| `js/format.js` | 1146 | `210534d212ec3b9a…` |
| `js/infra/host-status.js` | 38038 | `5419030dd05d79d2…` |
| `js/modules/contract.js` | 10083 | `f02632fbb94c674f…` |
| `js/modules/controller.js` | 6924 | `b2cc4244f1592f12…` |
| `js/modules/render.js` | 11701 | `185595e7c36a678e…` |
| `js/router.js` | 5464 | `41640cdebe4e0873…` |
| `js/theme.js` | 952 | `aacc9a70ba07db93…` |
| `js/timeline.js` | 13111 | `e77cddf8245d7505…` |
| `js/workspace/cli-access.js` | 10302 | `db4817ac63aaf5f1…` |
| `js/workspace/lifecycle.js` | 10669 | `ffc4576df9737216…` |
| `js/workspace/readiness.js` | 9049 | `99f921ed7671e63d…` |
| `js/workspace/routes.js` | 6741 | `07f02d358b9ae5cf…` |

权威来源是构建产物里的 `release-manifest.json`（含完整 sha256、字节数与入口列表）。
</details>

**明确未发布**：`web/qa.html`、`web/tools/**`、`web/README.md`、`web/auth/package.json`、
`prototypes/**`、`tests/**`、`dev/**`、`backend/**`、任何 fixture / 日志 / sourcemap。

## 6. 合成数据为什么仍在包里（而不是被删掉）

`web/js/data/demo-fixture.js` 由 `web/js/app.js` **正常 import**，`js/calls/demo-fixture.js`
与 `js/features/mock-fixture.js` 由各自 controller import。盲删会让生产启动直接报错。

它们的安全性不靠"文件不在包里"，而靠**产物属性门**：
`web/js/build-profile.js` 只把 `data-build-profile="qa"` 当作 QA；生产包声明的是
`production`，因此 `?demo=1`、`?source=mock`、`?utm_source=…` 全部无效。smoke 逐条实测：

- 5 种 URL（bare / `?demo=1` / `?source=mock` / `?demo=1&source=mock` / `?utm_source=`）下，
  profile 恒为 `production`、data mode 恒为 `live`、`1,248 / 99.4% / 1.42 s / 482k / 8,412`
  与 `Demo LLM A / demo-user-a / run-demo-1` **一个都不出现在渲染文本里**；
- 无 console 错误、无未捕获异常、无跨源请求。

**结论：生产包里合成数据仍不可达。** 因此不构成"仍可从 URL 触达 synthetic"的 BLOCKED 条件。

## 7. 交回 Owner 的契约（本 Lane 不改这些文件）

| 文件 | 事项 | 归属 |
|---|---|---|
| `web/auth/index.html` | 未声明 `data-build-profile`。运行时按 `production` 处理（fail-closed），已记入 manifest 的 `undeclared_profile`；建议补一词使其显式。 | UI Owner（`web/**`） |
| `web/tools/serve.mjs` | 本地预览以 `web/` 为根，会同时服务 `qa.html` 与 `tools/**`。**仅用于本地复核**，不得用作生产文档根。 | UI Owner / 运维 |
| `docs/atelier-v6-service-hosting-and-rollback-2026-10-16.md` | §部署建议把代码目录直接作为静态根；应改为指向本文件的产物目录。 | Release Owner |

## 8. 回滚

本 Lane 不新增回滚机制，沿用既有计划
`docs/atelier-v6-service-hosting-and-rollback-2026-10-16.md`（状态目录指针切换 +
`tests/release/drill-preview-rollback.sh` 演练打印）。

发布标识改用 **`bundle_sha256`**：回滚就是"把静态根指回上一个 `bundle_sha256` 对应的
目录"。产物是纯静态、无运行时报文，因此回滚不需要迁移任何数据。

## 9. Dell 侧状态

**`HOST_PENDING`。** 本 Lane 只完成源码与门禁：

- 未在 Dell 上创建/修改 Caddy、systemd、`/8443/`、PAM、UID 或任何服务；
- 未上传任何密钥；
- 真实主机的目录选择、维护窗口、备份与回滚演练仍由 Owner 按发布检查单 §1.2 的
  H1–H6 逐条确认。

## 10. 本机执行环境（只读观测）

东京共享宿主，2026-10-17 11:06 CST：`load average 14.94 / 18.16 / 19.42`（128 核）；
**swap 28.7 GiB / 31 GiB 已用（91%）**，余 3.4 GiB；PSI `memory some avg10=0.00`、
`io some avg10=0.23`、`cpu some avg10=10.33`。当前无 stall，但 swap 水位偏高——因此本
Lane 的测试全部有界、无并发矩阵、不跑全量 Python/浏览器套件。
