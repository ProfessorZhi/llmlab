# LLM Lab V2 设计系统

本文件是 LLM Lab V2 前端的**唯一设计规范**。所有颜色、间距、时长、缓动都在
`portal/web/styles/tokens.css` 里命名；任何出现在别处的字面量都是缺陷，因为它是
主题切换唯一够不到的值。

---

## 1. 为什么是这套技术选型

| 约束 | 后果 |
|---|---|
| 这台机器是 2019 年的 Windows 工作站，内存常年在 1 GiB 空闲附近 | **零构建步骤**。没有 npm、没有打包器、没有 sourcemap 流水线。浏览器直接跑 ES module。 |
| 控制台必须在节点离线时也能显示「节点离线」 | **零 CDN**。任何从公网拉的字体/图标/图表库，都会在最需要它的那一刻变成空白页。 |
| 运维控制台的可信度来自「数字有出处」 | **手写 SVG 图表**，每张图旁边都有表格孪生体和 `来源 ·` 标注。 |
| 不允许为了改 UI 而迁移框架 | 沿用 V1 的 stdlib `ThreadingHTTPServer` + 原生前端，只重写前端层。 |

页面结构：

```
portal/web/
  index.html            外壳：侧栏 + 顶栏 + <main class="view">，无内联脚本
  app.js                外壳引导：主题、抽屉、刷新按钮、启动三个共享轮询源
  core/                 11 个模块：dom api format icons ui theme chart router store bus sources
  views/                8 个模块：_parts 与七个路由视图
  styles/               5 张表，按层叠顺序加载：
                          tokens → base → layout → components → charts
```

样式表**先于脚本解析**。这是首屏不闪白的原因，也是为什么没有任何 JS 负责"设置初始样式"。

---

## 2. 令牌

### 2.1 字体

```css
--font-sans: "Segoe UI Variable Display", "Segoe UI Variable Text", "Segoe UI",
             "Microsoft YaHei UI", "Microsoft YaHei", system-ui, -apple-system, sans-serif;
--font-mono: "Cascadia Code", "Cascadia Mono", Consolas, "JetBrains Mono",
             ui-monospace, "SF Mono", monospace;
```

**只有 Windows 字体栈。** 明确不使用 Apple 私有字体（SF Pro / SF Mono / New York）：
本应用只在 Windows 上运行，而一个需要先安装的字体，就是一台在它被开发的机器上
会渲染成 Times New Roman 的字体。`-apple-system` 与 `SF Mono` 出现在列表尾部
只作为语义兜底，排在 Windows 面之后，永远不会在本机命中。

中文面用 `Microsoft YaHei UI` → `Microsoft YaHei`，是 Windows 10 中文系统的实际
默认，保证汉字不落到合成兜底字体上。

### 2.2 字号与行高

模块化比例（≈1.2），不是随手挑的 px：

| 令牌 | 值 | 用途 |
|---|---|---|
| `--fs-2xs` | 10.5px | 侧栏分组的全大写小标签、页脚元信息 |
| `--fs-xs` | 11.5px | 徽章、`来源 ·` 行、健康 pill |
| `--fs-sm` | 12.5px | 表头、次要说明 |
| `--fs-base` | 13.5px | 正文默认 |
| `--fs-md` | 14.5px | 导航项、表格单元格 |
| `--fs-lg` | 16px | 卡片标题 |
| `--fs-xl` | 19px | 移动端页面标题 |
| `--fs-2xl` | 23px | 页面标题 `h1` |
| `--fs-3xl` | 28px | 区块标题 |
| `--fs-4xl` | 34px | hero 主读数 |

`--lh-tight: 1.18`（显示级）、`--lh-snug: 1.38`（标题）、`--lh-base: 1.6`（正文）。

`--track-tight: -0.022em` 用于 23px 以上：默认字距在这个尺寸上读作松散。

### 2.3 间距

4px 基数，命名的是**真正复现的那些**档位，不是声称十六档都在用：

`--sp-1: 4` `--sp-2: 6` `--sp-3: 8` `--sp-4: 12` `--sp-5: 16` `--sp-6: 20`
`--sp-7: 24` `--sp-8: 32` `--sp-9: 40` `--sp-10: 56`（px）

`--gutter: clamp(16px, 2.2vw, 28px)` 是页面左右内边距，随窗口伸缩。

### 2.4 圆角

`--r-xs: 6` `--r-sm: 9` `--r-md: 13` `--r-lg: 18` `--r-xl: 24` `--r-pill: 999px`。

### 2.5 动效

```css
--dur-instant: 90ms;   /* 按下反馈，必须读作瞬时 */
--dur-fast:    140ms;  /* hover / 颜色变化 */
--dur-base:    220ms;
--dur-slow:    380ms;  /* 视图进入、指示器移动 */
--dur-slower:  620ms;  /* hero 入场，只此一处 */

--ease-out:    cubic-bezier(.16, 1, .3, 1);    /* 进入 */
--ease-in-out: cubic-bezier(.65, 0, .35, 1);
--ease-spring: cubic-bezier(.34, 1.56, .64, 1);/* 指示器，带轻微过冲 */
--ease-exit:   cubic-bezier(.4, 0, .8, .4);    /* 退出，故意不带回弹 */
```

**退出用线性偏缓：** 一个正在离开的东西带回弹，读作故障。

### 2.6 断点

`--bp-mobile: 640px` `--bp-tablet: 900px` `--bp-narrow: 1180px`。

CSS 的 media query 读不到自定义属性，所以同样的字面量也出现在 `layout.css`。
**两处必须同步**，`tokens.css` 里有注释提醒。

---

## 3. 主题

两套主题，`<html data-theme="dark|light">`。默认深色（应用是针对深色设计的），
`localStorage['llmlab.theme']` 有值就覆盖，否则跟随 `prefers-color-scheme`，
并且**在用户做出明确选择之前持续跟随**。

浅色**不是深色的朴素反转**：

* 浅色玻璃要**更高**不透明度才站得住（`rgba(255,255,255,.66)` vs 深色
  `rgba(20,23,32,.62)`），因为浅色底上没有暗部可以借；
* 白面没有高光可给，所以高度必须由**阴影**承载，浅色的 `--shadow-*` 是单独调的；
* 分类色是**重新推导**的，不是复用（见 §4）。

### 3.1 三类颜色，职责互不重叠

| 类别 | 令牌 | 规则 |
|---|---|---|
| **表面** | `--bg` `--surface-0/1/2` `--surface-solid` `--surface-sunken` `--glass-*` | 高度靠**不透明度**做，不是一个手挑的灰。 |
| **墨** | `--ink` `--ink-2/3` `--ink-muted` `--ink-faint` | 五档，对着 `--surface-solid` 与玻璃栈实测过。 |
| **状态** | `--good` `--warning` `--serious` `--critical` `--info` `--neutral`（各带 `-soft`） | **保留色。** 永远不做分类色，永远与文字同现。 |

玻璃只给四类容器用：导航栏、工具条、浮层、少数焦点容器。**不是所有卡片都上玻璃**——
一页里到处都是模糊，等于没有层次，而且在这台机器上真的会掉帧。

---

## 4. 图表：方法与理由

`portal/web/core/chart.js`。所有图表手写 SVG，没有 D3、没有 Chart.js。

### 4.1 分类色

```
深色  --series-1 #4f95ee  --series-2 #e0713c  --series-3 #23a87b  --series-4 #a98bf0
浅色  --series-1 #1f6fdc  --series-2 #c2521f  --series-3 #0f8a5f  --series-4 #6c53d8
```

**固定槽位顺序，永不循环。** 第 2 条序列永远是槽 2 的颜色。这些色相在写第一行
图表代码之前，就已经对着图表画布做过对比度与色觉区分的校验（深色画布 `#14161c`，
浅色画布 `#ffffff`）。V2 把画布从 V1 的 `#1a1a19` 换成更暗更蓝的 `#14161c`，
所以四个槽位全部**重新校验**过。

### 4.2 硬规则

1. **一个纵轴。** 需要两个刻度，就该是两张图。
2. **状态色不是序列色。** 绿/黄/红只表示状态，且永远配一个词。
3. **≥2 条序列必须有图例。**
4. **每张图都有表格孪生体。** `barChart`/`lineChart` 自己产出 `details.table-view`。
   这既是无障碍要求，也是运维要求：图被误读时，5 秒内能核对。
5. **柱状图**：2px 间隙，数据端圆角，网格线置灰。
6. **不伪造序列。** 样本不够就画 `empty()` 并说明还需要几个点，绝不补零、
   绝不从历史数据倒推一条曲线。

### 4.3 组件

| 函数 | 说明 |
|---|---|
| `barChart(target, spec)` | 分组柱。悬停走指针事件，`wireBars` 统一接管。 |
| `lineChart(target, spec)` | 多序列折线，可选填充、十字准线。 |
| `sparkline(target, values, opts)` | 行内走势。`height` 由 JS 写到 `host.style.height`。 |
| `gauge(target, {value,max,label,unit,digits})` | 270° 弧。`value` 为 `null` 时不画读数。 |
| `donut(target, {parts,unit,center})` | 部分-整体。 |
| `barList(target, {items,unit,digits,color})` | 横向排名条。 |

颜色在**绘制期**从样式表读出来（`getComputedStyle` → `token()`），因为 SVG 需要
真实颜色字符串，不能是 `var(--x)`。代价是：**主题切换必须重画**，所以每个视图都
订阅 `onThemeRepaint`。

---

## 5. 布局

```
.app  ─┬─ aside.sidebar   （浮空玻璃导航栏，从窗口边缘内缩）
       └─ div.content ─┬─ header.topbar
                       └─ main.view      （全文档唯一的滚动容器）
```

`body { overflow: hidden }`，`.view` 是唯一的 scroller。这样窗口滚动条永不出现，
侧栏与顶栏天然固定，不需要 `position: fixed` 那一套（它会带来自己的堆叠与
尺寸测量问题）。

侧栏是**内缩的浮空面板**而不是通栏一列：让环境光从缝隙里透出来，玻璃才读作玻璃，
而不是一个深色矩形。

| 断点 | 行为 |
|---|---|
| `> 1180px` | `--sidebar-w: 252px`，标签常显。 |
| `≤ 1180px` | `--sidebar-w: 214px`，品牌副标题隐藏。 |
| `≤ 900px` | 侧栏脱离文档流，变成 `transform: translateX(...)` 的抽屉；`.rail-toggle` 出现；`.rail-scrim` 从 `display:none` 变为遮罩。桌面布局**一点没变**——是同一个侧栏，只是移出去了。 |
| `≤ 640px` | 标题降到 `--fs-xl`，副标题与健康 pill 隐藏（页脚承载同一状态），所有网格收成一列。 |

`--row-h` 在 `max-height: 780px` 时从 46px 收到 41px，让 768px 高的笔记本少滚一点。

---

## 6. 组件词汇表

| 类 | 出自 | 说明 |
|---|---|---|
| `.card` / `.card-head` / `.card-body` | `_parts.card()` | 标题 + 说明 + 动作 + 正文。 |
| `.band` / `.band-head` | `_parts.band()` | 一页里的一段；通常内含网格。 |
| `.grid-2/3/4/wide` | `_parts.grid()` | `≤640px` 全部收成一列。 |
| `.stat` / `.stat-value` / `.stat-label` / `.stat-sub` / `.u` | `_parts.stat()` | 一个数 + 单位 + 注释 + **`来源 ·`**。来源不是可选项。 |
| `.src` | `_parts.src()` | provenance 行，安静、小、永不省略。 |
| `.flag` + `.dot` | `_parts.flag()` | **点 + 词**。颜色不单独承载状态——大约每十二个读者里有一个分不开"好"和"危急"两个色相。 |
| `.badge` `.badge.on/.off/.warn` | `_parts.badge()` | 中性的短标记。 |
| `.kv` | `_parts.kv()` | 无需图表的定义列表；空值是 `—`。 |
| `.meter` | `_parts.meter()` | 有阈值的进度条（≥75% warn，≥90% crit，可覆盖）。 |
| `.rows` / `.row` | `_parts.row()` | 列表行。 |
| `table.data` | `_parts.table()` | 数据表。数值列右对齐（`{num:[...]}`）。单元格 `{__raw:true}` 表示调用者自己已转义。 |
| `.btn` `.btn-ghost` `.btn-sm` `.btn-primary` | `_parts.button()` | 只有一个真正会改状态的按钮：启动 EvalScope。 |
| `.segmented` | `_parts.segmented()` | 分段控件；滑块的位移由 JS 从实际按钮矩形读出来。 |
| `.notice` `.notice.warning/.info/.error` | `core/ui.js` | 说明**为什么**没有数据。它比一张空表有用。 |
| `.state` | `core/ui.js` `empty()` | "这是一个经过测量的空"，不是渲染失误。 |
| `.skeleton` | `core/ui.js` `skeleton()` | `aria-busy`，只在首次载入用。 |
| `.modal` / `.scrim` / `.popover` / `.toaster` / `.toast` | `core/ui.js` | 浮层与通知。 |

---

## 7. 无障碍

* **键盘**：`.skip-link` 是第一个 Tab 停靠点，跳到 `#view`。抽屉 `Escape` 关闭并
  把焦点还给触发按钮（`app.js:wireRail`）。`main.view` 带 `tabindex="-1"`，
  路由切换后可直接落焦。
* **焦点**：`:focus-visible` 有独立样式，不用 `outline: none`。
* **状态**不是纯颜色：`.flag` 永远是点 + 词；`.dot[data-state]` 同时改**形状语义**
  （`crit` 走脉冲动画）和颜色。
* **图表**：每张图有 `aria` 描述，并且都有一个 `details.table-view` 表格孪生体。
* **`prefers-reduced-motion`**：见 §8。

---

## 8. 动效与 `prefers-reduced-motion`

V2 的动效只有四类，每类都有明确职责：

1. **视图进入**（`.view-enter` → `@keyframes view-in`）：`translateY(9px)` + 淡入，
   `animationend` 时移除类，所以视图内部重绘不会重放它。
2. **导航指示器**（`.nav-indicator`）：一个元素在项目之间**移动**（JS 从激活项的
   矩形定位），不是一个 per-item 伪元素淡入淡出。淡入淡出无法表示方向，而方向正是重点。
3. **hero 入场**（`.rise` → `.rise-in`）：一次位移，只作用在页面的"答案"上，
   不是每一张卡片。
4. **加载导轨**（`body.is-loading-view .view::before`）：路由模块 `import()` 期间
   顶部 2px 进度光带。**不是盖在内容上的转圈**——上一个视图保持可读。

### reduced-motion 契约

`base.css` 用的是 `animation-duration: 1ms`，**不是** `animation: none`：

```css
@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: 1ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: 1ms !important;
    scroll-behavior: auto !important;
  }
  .rise { opacity: 1; transform: none; }
}
```

两个理由：

1. **本应用有两处靠 `animationend` 排序**——`router.js:178` 在它触发时移除
   `.view-enter`，`ui.js:62` 在它触发时移除一个 toast。`animation: none` 会让这两处
   卡住：被"敲击"过的视图会一直带着入场 transform，toast 永远不消失。1ms 仍然会
   触发该事件，而那一帧没人看得见。
2. 入场位移由 `.rise { opacity: 1; transform: none; }` **直接落位**，不交给那 1ms
   动画——万一事件被错过，一个只会"先藏后显"的元素不会卡在 `opacity: 0`。

也就是说：**动效可以被取消，状态不可以丢。**

---

## 9. 数据诚实性（这是设计约束，不是文案约束）

这份规范里最重要的一条，也是 UI 评审最容易放过的一条：

> **任何"看起来像实时数据"的东西，都必须是实时数据，而且必须说得出出处。**

具体落实：

* 每个测量值旁边有 `来源 ·`（`_parts.src()`），写明它出自哪一次探测。
* 配置事实与健康事实**分开渲染**。`models.js` 明确把未被探测的端点画成"配置卡"，
  标注「未探测实时健康」，而不是给它一个绿色的"运行中"。
* GPU 遥测不可用时，**不画归零的表**（那会声称一次从未发生的测量），而是
  `notice('warning', ...)` 说明原因，并解释 `nvidia-smi` 失败**不等于** GPU 不可读。
* 列表为空时说明原因，不印空表头。
* 图表样本不足时画 `empty()`，不补零。

---

## 10. 相关文档

* `docs/architecture.md` —— 组件图与一次页面加载的数据流
* `docs/security.md` —— 安全模型（设置页的"安全边界"卡与它一致）
* `docs/LLM_Lab_V2_Release_Report.md` —— 本次重构的交付与验收记录
