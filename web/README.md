# web/ —— Portal 前端（Atelier Observatory 06）

## 1. 这是什么

最初，`web/` 是 `prototypes/v5/index.html`（单文件原型）的**工程化等价物**：
同一个视觉、同一套交互、同一批图表，但拆成按职责分层的原生 ESM 模块。视觉一致性
的判定标准只有一条：把两份产物放进同一个浏览器、同一个视口，逐项比对**解析后的
computed style** 与**每个视图的 DOM 结构**是否一致（`tests/browser/portal.browser.test.js`）。

**从 06 起，这里多了第二件事**：原型里的七个视图之外，还长出了两类**要真的取数**
的界面 —— 「我的调用日志」视图与五个模块面板（模型/引擎/部署、Playground 与
Endpoint 可见性、只读数据源、EvalScope 入口、视图过滤与权限边界）。它们的数据来自
已认证的 BFF，未授权时**失败关闭**：不显示任何数字，并说明原因。

因此现在的规则是两条而不是一条：

- `prototypes/v5/index.html` **仍然逐字节不变**，它是视觉与文案的唯一权威；
- 06 的增量（导航项、新视图、模块面板、第四层样式）写在
  `prototypes/v6/additions.{html,css}` 里，由 `derive-from-v5.mjs` **计算**出
  `prototypes/v6/index.html`。本目录的 HTML/CSS 仍由生成器产出，不手工维护。

## 2. 血缘与再生成

```
prototypes/v5/index.html   (sha256: 04003f5c… 冻结的视觉权威，逐字节不变)
        │  prototypes/v6/derive-from-v5.mjs —— 读 v5 前先校验它的哈希
        ▼
prototypes/v6/index.html   (sha256: b05e7544… = v5 + additions/)
        │  web/tools/extract-from-prototype.mjs —— 再校验 v6 的哈希
        ▼
web/index.html · web/css/tokens.css · web/css/base.css · web/css/atlas.css · web/css/portal.css
```

```bash
node prototypes/v6/derive-from-v5.mjs --check      # v6 == v5 + additions？
npm run verify:extract                             # web/ == 从 v6 切出来的？
node prototypes/v6/derive-from-v5.mjs              # 直接写入 v6（改完 additions 才跑）
node web/tools/extract-from-prototype.mjs          # 直接写入 web/（仅在确实要改生成物时）
```

**每一环都会自己检查，没有一环靠"我记得改过了"。** 改 v5 会打破派生；改 v6 会打破
`EXPECTED_SOURCE_SHA256`；改 `web/` 会打破 `--check`。链条本身也有回归
（`tests/atlas-data.test.js` 里「v6 恰好是 v5 加增补」与「生成器钉的正是它读的那个
文件」两条），所以"06 = 05 + 增补"是一句可复现的话，而不是一句注释。CSS 是从源文件中**按字节切片**取出的，
不是手工誊抄——873 个大括号、621 个规则块、148 个自定义属性都做过多重集校验。

`web/index.html` 里的 body 标记是原型第 516–631 行的原文；原来的两段 `<script>` 被替换为
一条 `<script type="module" src="./js/app.js">`。

## 3. 目录

```
index.html            页面骨架（生成物）
css/tokens.css        :root 设计令牌 + 深色主题覆盖（生成物）
css/base.css          基础排版与组件样式（生成物）
css/atlas.css         Atlas 可视化图层（生成物）
css/portal.css        06 的 Portal 模块图层（生成物；只补 05 没有的组件）
js/app.js             启动入口：先起外壳，再解析数据源，最后建 Atlas
js/dom.js  format.js  极小的 DOM / 数字格式化工具
js/events.js          跨模块事件名（THEME_EVENT）
js/router.js          仅 hash 路由，八个视图（原七个 + 我的调用日志）
js/theme.js           明/暗切换
js/timeline.js        首页折线图（指标切换、指针探针、方向键探针）
js/dialog.js          说明弹窗与复制示例（全部是占位文案，无真实密钥）
js/chat.js            演示对话框（纯本地回应，没有任何传输层）
js/data/contract.js   数据源判定 + 远端 DTO 契约校验
js/data/demo-fixture.js  9 条合成负载（唯一被允许的假数据）
js/data/remote-client.js 唯一允许发起网络的模块
js/atlas/selectors.js 纯数值：过滤、缩放、排序、帕累托、对比篮
js/atlas/squarify.js  矩形树图布局
js/atlas/heat.js      7×24 热力矩阵
js/atlas/geometry.js  极坐标弧段
js/atlas/structure.js 三个图形图层（旭日/矩形/点阵）的互斥可见性
js/atlas/theme-colors.js  从 :root 读取颜色（CSS 变量不能用在 SVG 属性上）
js/atlas/render-*.js  各可视化渲染器
js/atlas/exporters.js CSV / JSON 导出
js/atlas/tooltip.js   跟随指针的提示框
js/atlas/binding.js   把数据绑到 DOM 节点
js/atlas/controller.js  状态机、事件接线、整体重绘
js/calls/contract.js  /api/atlas/calls 载荷校验（纯函数，无 DOM/网络）
js/calls/render-log.js  调用日志的表格与面板（纯字符串渲染）
js/calls/controller.js  调用日志的筛选、一次 fetch、页面状态机
js/modules/contract.js  /api/atlas/modules 载荷校验（浏览器侧再设一次防）
js/modules/render.js    模块面板（纯字符串渲染）
js/modules/controller.js 模块面板的一次 fetch 与可见性切换
js/calls/demo-fixture.js  ?demo=1 时代替网络的进程内合成调用日志（atlas-calls/1，transport=demo）
js/calls/insights.js    已加载行的结果构成 / 百分位 / 按模型分解、详情抽屉、CSV/JSON 导出（纯函数）
js/workspace/readiness.js  概览「工作区就绪度」：9 个视图 × 同源路由 × 当前状态（只读各控制器，不发请求）
js/workspace/cli-access.js API 管理「我的 Scoped Token」：GET /auth/tokens 只读列表，写入全部禁用并说明原因
js/workspace/lifecycle.js  API 管理「Endpoint 生命周期与接入」：llmlab CLI 步骤 + 占位符 SDK 示例
js/workspace/routes.js     管理「集成与路由矩阵」：镜像 backend/bff/route_map.py（测试防漂移）
tools/extract-from-prototype.mjs  生成器（v6 -> web/）
../prototypes/v6/derive-from-v5.mjs  v5 -> v6 的派生
tools/serve.mjs       仅回环的静态服务器（浏览器测试与本地预览用）
```

依赖方向是单向的：`render-* → selectors/geometry/theme-colors → data/*`。
`render-*` 之间互不引用；编排只在 `controller.js`。

## 4. 数据来源规则（硬约束）

| 场景 | 结果 |
| --- | --- |
| 无参数且未配置受信 Atlas 同源 endpoint | `unknown/not configured`：没有合成工作负载、不发往 Gateway/外网的请求，指标显示 `—` |
| 无参数且已注入受控的 `/api/atlas/…` 同源 endpoint | 默认请求同源 BFF；必须经过当前会话授权和 `validateAtlasBundle` 才可展示真实数据 |
| 显式 `?demo=1` 或 `?demo=true` | 才使用 9 条**合成** QA 工作负载，始终标为 SYNTHETIC / DEMO |
| `?source=remote` 但没有受信 endpoint | `unknown` 受控空态；不回退到合成值 |
| `?source=remote` 且 endpoint 合法 | 与默认 live 相同的已配置同源 BFF 路径；不允许绝对 URL、跨域或客户端注入上游地址 |
| `?source=demo` / 未知 `?source` | 非 `?demo=1` 的 legacy 参数无权打开合成模式：`unconfigured` |
| 远端 payload `synthetic !== false` | 拒绝，落为 `unknown`——不允许把假数据当真实数据展示 |
| 远端某指标缺失 | 保持 `null`，渲染为 `—`，**绝不当成 0** |

endpoint 只从 `globalThis.__LLM_LAB_ATLAS__.endpoint` 读取，且必须是 `/api/atlas/…` 的固定同源路径。**本仓库不存放任何凭据**；
`remote-client.js` 使用浏览器同源 cookie，鉴权由服务端 Portal/RBAC 负责，
前端不做信任假设。

`<html>` 上会写入两个惰性标记供测试与排查使用（没有任何 CSS 选择它们，因此不影响视觉）：
`data-data-source="demo|live|empty|unknown"`、`data-synthetic="true|false"`。

## 5. 与原型的两处**有意**差异

1. `images/` —— 无。原型不引用任何外部字体或图片，本目录同样没有，且**不新增**。
2. `js/router.js` 的视图切换：原型只 `catch` 了 `viewTransition.finished`，
   于是"250ms 内连续切页"会让 `ready` 的 `AbortError` 变成一条 unhandled rejection。
   本目录补了 `t.ready?.catch(() => {})`。这不改变任何像素，只是消掉控制台噪音。

主题不落盘（刷新即回到明亮），与原型一致——这是有意的，不是遗漏。

## 6. 本地运行与测试

```bash
npm run serve              # http://127.0.0.1:8123/  仅回环
npm test                   # 139 项：数据契约、选择器、几何、导出、路由常量、静态服务器，
                           #        以及两个认证视图的载荷契约与渲染（calls-log / modules-view）
npm run test:browser       # 10 项：真实 Chrome/CDP，含与原型逐项比对、8 个视图、5 档宽度、零外部请求
npm run test:all
node prototypes/v6/derive-from-v5.mjs --check   # v6 是否仍等于 v5 + additions
npm run verify:extract                          # web/ 是否仍等于从 v6 切出来的产物
```

服务端一侧的契约（三个端点）由 `python -m unittest discover -s tests/backend` 与
`... -s tests/integration_auth` 覆盖，后者跑的是真 ``wsgiref`` + 真 SQLite + 真
Session Cookie。

已知的一处环境性失败：`tests/serve.test.js` 的
`accepts a root written with forward slashes` 断言
`REPO_ROOT.split(sep).join('/') !== REPO_ROOT`，该断言**只在 Windows 成立**
（Linux 的 `sep` 就是 `/`）。它在未改动的 `origin/pr/11` 上同样失败，与本分支无关。

浏览器测试需要本机 Chrome；用 `CHROME_PATH` 指向别的二进制。
它**不安装任何依赖**：Node 24 自带的 WebSocket 直连 DevTools 协议，
并用 `--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1`
把非回环主机解析成 NOTFOUND——所以"零外部请求"是被观测到的事实，而不是一句承诺。

## 7. 不要做的事

- 不要手工编辑 `web/index.html`、`web/css/*.css`——改 `prototypes/v6/additions.*`
  后依次跑派生与生成器。（也**不要**改 `prototypes/v5/index.html`：它是冻结的权威。）
- 不要引入框架、构建步骤、远程字体或任何运行时依赖。
- 不要给 `web/js/data/` 之外的文件加网络调用（**四处有据例外**见下 §7.1）。
- 不要为了让数字"看起来更真实"而放宽 `validateAtlasBundle`。
- 不要提交 `.env`、密钥或任何环境配置。

### 7.1 已记录的例外：`web/js/calls/`、`web/js/modules/` 与 `web/js/features/`

「我的调用日志」与模块面板取的是**另一套数据**（已认证的 BFF，而不是 Atlas bundle），
因此它们各自带一套模块：`web/js/calls/{contract,render-log,controller}.js` 与
`web/js/modules/{contract,render,controller}.js`。它们**不改动上游生成的
`index.html` / `css/*.css`**（上面第 1 条的约束）——新的样式落在 06 自己的
`prototypes/v6/additions.css` 里，再生成成 `css/portal.css`。

`web/js/features/` 是第三处例外：五套管理面板（模型与引擎、API 与调试、监控与日志、
评测中心、系统管理）。它同样**一行都不改**生成的 `index.html` / `css/*.css`——面板由
`features/controller.js` 在运行时挂进外壳已有的五个视图，只用外壳自己的组件类
（`.page-block.module-panel`、`.module-card`、`.module-kv`…），因此
`npm run verify:extract` 与 `prototypes/v6/derive-from-v5.mjs --check` 都按字节不变通过。

前两处唯一的一次 `fetch` 分别在 `web/js/calls/controller.js` 与
`web/js/modules/controller.js`，而不是 `web/js/data/`。原因：放进 `data/` 需要在
`.gitignore` 开第五条例外，而 `tests/tracked-assets.test.js` 把"恰好四条前端例外"
钉死了。`web/js/features/` 沿用同一先例，且**不新增任何 `.gitignore` 例外**（该目录不
匹配 `data/` 或 `*token*`）——所以那条断言仍然是绿的。它的唯一一次 `fetch` 在
`web/js/features/source.js`，仅在宿主注入**受信同源** base 时才会发送 `GET /api/v2/ops`（无参数即默认选择它）；未配置则显示 `auth-bff-not-connected`，**不产生合成模型实例**。仅 `?demo=1` 启用 `mock-fixture.js`；
遗留 `?source=mock` 不再能绕过生产门控。例外局部、有据、共三处 `fetch`。

第四处（V6.1，Issue #56）：`web/js/workspace/cli-access.js` 的唯一一次 `fetch` 是
`GET /auth/tokens`（`backend/auth/routes.py::tokens_list`，经 BFF 的 `/auth/` 委派），
只读、同源、`credentials: 'same-origin'`；响应若携带 `token`/`secret` 等明文字段即整体拒绝。
`?demo=1` 时不发请求。文件名刻意避开 `.gitignore` 的 `*token*` / `*credentials*`，
因此**不新增任何 `.gitignore` 例外**。`web/js/calls/demo-fixture.js` 不开 socket：它是
`fetch` 形状的进程内处理器，仅在 `?demo=1` 时被注入调用日志控制器。

调用日志**曾经**是一个独立页面（`web/calls.html` + `web/css/calls.css`），现已并入
外壳的 `#view-calls`：一个视图有两个文档就等于有两份会各自腐烂的实现。老的 URL
`/calls` 由预览 302 到 `/atlas#calls`，`/calls.html` 本身不可达。

`render-log.js` / `render.js` 的每个函数都是 `(log) -> html 字符串`，所以它们能在
plain Node 下被测（`tests/calls-log.test.js`、`tests/modules-view.test.js`），不依赖
本仓库没有的 Chrome。契约见 `docs/portal-call-log-contract.md` 与
`docs/portal-modules-contract.md`。
