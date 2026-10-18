# Dell LLM Lab · Atelier Observatory 05 / Traffic Atlas

**交付性质：离线、单 HTML 交互原型。未合入 Dell 的 Portal Git 工作树；未调用真实 Gateway / Strata / Prometheus / Grafana / EvalScope；未创建真实用户、Key 或评测任务。**

## 一、从用户提供的《2027 国考招录观测台》提炼了什么

参考页面以一份汇总数据支撑多种表示：三级旭日图、分层矩形图、逐名额点阵、全国占比环形图、可排序排名、帕累托曲线、规模与密度散点图、按类别筛选、悬停预览/点击锁定、四项比较，以及 CSV / JSON 导出。它的强项不只是图多，而是**不同图共享选择状态、统计口径和可复查数据**。

在 LLM Lab 第五版中，这些交互被重新设计为模型服务平台的语义：

| 参考网页机制 | LLM Lab 第五版的迁移 |
| --- | --- |
| 方向 → 系统两级旭日图 | 业务方向 → 工作负载两级调用量旭日图 |
| 面积比例矩形图 | 工作负载的请求量 treemap |
| 一点对应一个名额 | 一点对应一次**模拟** API 请求，实际按 1,248 个调用点绘制 |
| 全国占比环形图与侧栏详情 | 全局业务占比 + 侧栏锁定工作负载的调用量/p95/Tokens |
| 系统规模排名、搜索与排序 | 工作负载请求量 / p95 / 输出 Tokens 排名、搜索、添加对比 |
| 帕累托累计占比 + 规模散点 | 请求集中度 Pareto + 请求规模 × p95 延迟散点，点大小表示 Tokens |
| 多项对比 | 最多四项工作负载平行比较 |
| 导出可核查数据 | 带 `DEMO` 标记的 CSV / JSON 下载 |
| （原页没有） | 新增 7 天 × 24 小时合成活动热力矩阵，支持体感/延迟两种指标、方向键单元格导航 |

注：参考网页的数据单位是**招录人数与职位数**，并不是 API 数据。本版没有把其招录数字复制进模型平台；只移植可视化思想和交互机制。

## 二、界面风格

保持 Atelier 03/04 已收敛的读写友好字体体系：常规中文无衬线、正文约 15px、标题 30–37px，数字等宽对齐；视图用松绿、氧化铜橙、灰蓝三组低饱和语义色。浅色、深色都支持。图形绘制只在交互时变化，不使用持续运转的装饰性动画。具备 `prefers-reduced-motion` 降级。

信息结构：关键汇总 → 结构图 → 排名 → 分布诊断 → 小时热力矩阵 → 人工添加的对比 → 来源与方法。大区块以细线和留白分层，表格本体密度比大幅留白的营销页更高。

## 三、完整交互与键盘功能

- 七个导航路由：原六页保留，新加 `#atlas` 数据图谱；首页调用性能图下也有入口。
- 24 小时 / 7 天 / 30 天演示窗口（通过静态倍率转换，不暗示历史真实数据）。
- 全部 / 交互与内容 / 开发自动化 / 实验评测，筛选后重算总量、Pareto、散点、工作负载排序。
- `1` 旭日图、`2` 矩形图、`3` 点阵图，`/` 聚焦工作负载搜索，`Esc` 取消锁定，且均忽略输入框中的按键。
- 鼠标悬停预览、点击固定，焦点与 Enter/Space 操作主要图形；点击排名 `+`/`−` 添加移除至四项对比。
- 热力矩阵用方向键在 7×24 格子中移动、Enter/Space 选中。移动端矩阵单独横向滚动，页面主体不横向溢出。
- CSV / JSON 仅包含合成的、当前筛选可见的样本，文件名明确带 `DEMO`。
- 保留旧版对话、API、日志、评测与团队权限页面及深色切换。

## 四、数据准确性和安全边界

演示数据共有九个合成工作负载，24 小时合计 **1,248 次**、输出 **482k Tokens**。这里只定义**每个工作负载自己的 p95**，不对多个 p95 求平均得到一个假的总体分位数，因此总体 p95 显示 `—` 并解释原因。未来真实数据为空或来源不可用时也应以 `null` / `UNKNOWN` 显示，不以零代替。

正式 Portal 对接必须在后端先完成鉴权授权和可信的用户/项目归属。Gateway 既有 `usage.jsonl` 可提供 `ts,key_id,label,method,path,model,status,outcome,stream,usage,bytes_out,queue_ms,ttft_ms,upstream_ms,total_ms` 等元数据；**没有保证可用的 request_id，也没有可信的“业务工作负载”字段**。实际产品要实现“简历解析 / 编码协作”等分类，必须新增经过审核的服务端项目/路由映射，而不是根据客户端可伪造的 label 或消息正文猜测。

建议独立的后端聚合 DTO（示例结构，不代表现有接口已具备所有字段）：

```json
{
  "source": "gateway",
  "synthetic": false,
  "scope": "authorized-projects",
  "window": {"from": "...", "to": "...", "bucket": "1h"},
  "quality": {"calls": "measured", "latency": "partial", "ttft": "unknown"},
  "groups": [{"id": "approved-project-a", "calls": 0, "workloads": []}],
  "series": [],
  "notes": ["No data values fabricated"]
}
```

真实 p50/p95 必须由可见请求的有效延迟样本计算，不能平均各工作负载的 p95。TTFT 需区分有效流式响应，429 / failed / missing 需正确排除或单独统计。对未映射的调用只能显示 `unattributed`，不能把 `label` 当用户归属。若 Gateway 尚无可用小时桶，则热力矩阵保持空状态，不能使用演示值冒充生产监控。

**本版不请求真实业务端点；全部数据在内联 JS 里显式标记 Synthetic。** 这不等同于已经接入 Prometheus / Grafana / EvalScope；原有各服务的 UI 仍应通过 Portal 的 BFF/同源安全代理、真实 RBAC 和审计服务实施。

## 五、测试证据（Chromium Headless）

- 七个页面 × 320 / 390 / 768 / 1024 / 1440px = **35 组**：页面级横向溢出 0，JS `pageerror` 0，正确活动路由 35/35。
- `#atlas` 的 5 种视口：旭日图 12 条路径、9 个排序行、7×24=168 个热力格、无错误。
- 三种图形切换、按方向筛选（开发自动化 24h → 376 次）、7 天转换（→1,993 次）、搜索命中、比较 2 项、热力矩阵模式与键盘焦点均通过。
- SVG 隐藏属性问题在复测中被发现并修复：点阵切换后原 SVG `display:none`，Canvas 为 `display:block`，图框高度不增加；320–1440px 各视口均验证。
- 模式快捷键 `1/2/3`、`/` 搜索、p95 降序（压力测试居首）、CSV/JSON 下载、返回首页原有时间曲线都通过。
- `prefers-reduced-motion:reduce` 下无脚本错误，避免图形动画。
- 测试是**离线 HTML 组件行为验证**，没有进行跨后端集成、浏览器安全审查或生产负载测试。

## 六、交付文件

- `dell_llm_lab_atelier_v5_data_atlas.html` — 独立可交互 HTML（CSS/JS 内联，无网络依赖）
- `llmlab_atelier_v5_preview/atlas-1440-light.png` — 首页下的 Atlas 桌面完整版截图
- `llmlab_atelier_v5_preview/atlas-390-light.png` — 手机版截图
- `llmlab_atelier_v5_preview/atlas-v5-chart-collection.png` — 四种可视化组合预览
- `llmlab_atelier_v5_preview/qa_results.json`、`qa_all_routes.json` — 离线检查记录

后续建议：单独选择已验收 Portal 集成工作树，从中新增 `portal/web/views/atlas.js`、`atlas.css`、对应静态测试，并让 `portal/v2_api.py` 以真实 Gateway usage 适配器输出受权限过滤的聚合 DTO；不要直接把这份单文件覆盖生产 Portal。正式切流仍需单独授权。
