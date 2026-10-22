# 控制台接口快照 · 2026-10-22

这是一份**真实响应**的存档，给看不到那台机器的人改 `web/console/` 前端用。

抓取时间：2026-10-22 16:29（东八区）重抓，初版 15:2x；主机 Dell 5820（LAN `192.168.32.179`，
tailnet `fj-nj-ser-1009`），以平台账户 `vince` 登录后逐条 GET。全部经过脱敏
（`key` / `token` / `password` 字段整棵替换成 `<REDACTED>`，正文里残留的 `sk-*` 也抹掉）。

> 快照是**某一天的样子**，不是契约。任何数值都不许写进代码。

---

## 1. 拓扑：你在改的东西跑在哪

```
浏览器
  │  https://fj-nj-ser-1009.tail3d8b76.ts.net:8444/       （tailscale serve）
  ▼
127.0.0.1:8123   node web/tools/serve.mjs        ← 静态文件 + 把 /api/* /auth/* 反代给 BFF
  ▼
127.0.0.1:8790   backend.bff（systemd --user: llmlab-bff.service，以 fjd 运行）
  │
  ├─ Registry 库        ~/.llmlab-state/registry.db        （引擎/模型/部署/端点登记）
  ├─ 认证库            ~/.llmlab-state/auth.sqlite3       （用户、会话、平台账户）
  ├─ Gateway 用量日志   /srv/llm-lab/state/api-gateway/usage.jsonl
  └─ 本机探测          Strata :8080 / api-gateway :8200 / 端口表 / /proc

推理侧另有两层，与 BFF 无关：
  Strata  127.0.0.1:8080   引擎本体（OpenAI 兼容 /v1/*，自带无鉴权控制台）
  网关    127.0.0.1:8200   老的 LiteLLM 式网关（写 usage.jsonl，**没有 Web UI**）
```

**唯一的例外**：`GET /api/v2/engine/live` 是 BFF **用服务端凭据代取** Strata
`:8080/metrics`（key 取自 `~/.config/strata/api-key.env` 的第一条，那是引擎本体那把，
不是发给使用者的 `sk-qwen-*`），投影成 `engine-live/1` 后下发。浏览器全程不接触那把
key，响应里也没有它。所以「引擎实时负载」不需要人去引擎控制台填 API key。

**前端没有构建步骤。** `web/console/js/*.js` 是原生 ES 模块，改文件即生效，
8444 直接读仓库工作树。所以：不要在浏览器里改、不要引入 bundler。

跑测试：`npm test`（node:test，现为 603 项）。契约相关用例在
`tests/web-features/*.test.js`，它们逐字钉住很多文案与形状。

---

## 2. 响应信封：两种错误形状（这是个坑）

成功统一是：

```json
{ "ok": true, "data": { ... } }
```

失败**不统一**，取决于哪个应用处理：

| 来源 | 形状 | 例子 |
|---|---|---|
| auth 应用（`/auth/*`） | `{"ok": false, "error": {"code","message"}}` | `auth-admin-users.json`（403） |
| BFF 应用（`/api/*`） | `{"error": {"code","message"}}` —— **没有 `ok` 字段** | `api-model-service-key.json`（403） |

前端 `web/console/js/data.js` 主要按 **HTTP 状态码**分派状态
（`404/501/503 → not_configured`，`401 → signed_out`，`403 → denied`），
所以新增代码不要只看 `body.ok`。

---

## 3. 页面 → 接口

| 页面 | 路由 | 读的接口 | 备注 |
|---|---|---|---|
| 概览 | `#/overview` | `/api/v2/ops`、`/api/infra/summary`、`/api/atlas/calls` | calls 当前 503 |
| 资源监控 | `#/usage` | `/api/v2/ops`、`/api/infra/summary`、`/api/infra/system` | 「按用户」表每行带 `account: human|system`，前端默认折叠系统服务账号（见 §5.4） |
| 模型库 | `#/models` | `/api/v2/ops`（其中的 `registry.models`）、`/api/infra/summary` | `GET /api/v2/registry/models` 是 **405**，它只支持 POST 登记 |
| 推理服务 | `#/services` | `/api/v2/ops`、`/api/infra/summary`、`/api/v2/registry/discovery`、`/api/v2/engine/live` | 同上，`/api/v2/registry/engines` 也只有 POST；引擎实时负载由 BFF 代取 |
| 模型评测 | `#/evaluation` | `/api/v2/ops`（`data.ops.evaluation.runs`） | `/api/evaluation/reports*` 另有一套只读列表 |
| 插件 | `#/plugins` | `/api/infra/summary`、`/api/portal/integrations` | |
| API 密钥 | `#/keys` | `GET /api/model-service/key`（**单数**）、`POST /api/model-service/keys` 签发 | 仅平台管理员；成员 403 |
| 请求日志 | `#/requests` | `/api/atlas/calls` | 当前 503，见 `GAPS.md` |
| 用户与权限 | `#/admin/users` | `GET /auth/admin/users` | 仅平台管理员；成员 403 |

**单复数是个反复踩的坑**：读一份清单是 `/api/model-service/key`，
签发一把新的才是 `POST /api/model-service/keys`。写反了会得到 405 而不是 404。

---

## 4. 这份快照里有什么

| 文件 | 端点 | 状态 | 内容是否真实 |
|---|---|---|---|
| `_routes.json` | —（从 `backend/bff/route_map.py` 导出） | — | 30 条路由：方法、鉴权、CSRF、来源、归属域、scope |
| `_index.json` | — | — | 抓取清单：每条的状态码与字节数 |
| `responses/auth-me.json` | `GET /auth/me` | 200 | 真实 |
| `responses/auth-tokens.json` | `GET /auth/tokens` | 200 | 真实（令牌值已脱敏） |
| `responses/auth-admin-users.json` | `GET /auth/admin/users` | **403** | 真实错误体；成员无权看全员名单 |
| `responses/api-v2-ops.json` | `GET /api/v2/ops` | 200 | **真实**，控制台最重要的一份 |
| `responses/api-infra-summary.json` | `GET /api/infra/summary` | 200 | 真实（本机探测） |
| `responses/api-infra-system.json` | `GET /api/infra/system` | 200 | 真实（/proc、hwmon、statvfs）；`per_user[]` 每行带 `account` |
| `responses/api-v2-engine-live.json` | `GET /api/v2/engine/live` | 200 | **真实**：BFF 代取 Strata `/metrics` 的窄投影（槽位、排队、吞吐、token 计数），不含提示词与密钥 |
| `responses/api-model-service-key.json` | `GET /api/model-service/key` | **403** | 真实错误体 |
| `key-page-projection.json` | 同上，**用服务层投影** | — | 真实数据、真实形状（`ModelServiceKeyStore.list()`，与路由同一条代码路径），密钥值已脱敏 |
| `responses/api-atlas-calls.json` | `GET /api/atlas/calls` | **503** | 真实错误体：适配器没挂上 |
| `responses/api-portal-integrations.json` | `GET /api/portal/integrations` | 200 | 真实 |
| `responses/api-portal-capabilities.json` | `GET /api/portal/capabilities` | 200 | 真实 |
| `responses/api-portal-services.json` | `GET /api/portal/services` | 200 | 真实 |
| `responses/api-v2-registry-discovery.json` | `GET /api/v2/registry/discovery` | 200 | 真实（本机扫描到的端口） |
| `responses/api-v2-ops-usage.json` | `GET /api/v2/ops/usage` | 200 | 真实 |
| `responses/api-evaluation-reports.json` | `GET /api/evaluation/reports` | 200 | 真实 |
| `responses/api-evaluation-report-sources.json` | `GET /api/evaluation/report-sources` | 200 | 真实 |

`key-page-projection.json` 是**唯一**一份不是 HTTP 抓的：`GET /api/model-service/key`
只对平台管理员开放，抓取时用的是成员会话。它走的是路由里同一句
`ModelServiceKeyStore.list()`，所以形状与取值都是真的，只是没有经过 HTTP 那一跳。
凡是这种「投影」的地方都在 `GAPS.md` 里单列，不混在真实响应里冒充。

---

## 5. 三条必须记住的口径

1. **「空」有三种含义，页面上必须分开说**：没接线（`not_configured`）、接上了但
   读不到（`readable: false`）、读到了但窗口内确实没有（`in_window: 0`）。
   第三种可以显示 `0`，前两种必须显示「—」或「未知」。这是这个控制台最核心的设计约束。
2. **GPU 相关读不到就是 UNKNOWN，不是 0**。见 `api-v2-ops.json` 的 `metrics.items`：
   每条都带 `value: null` + `state: "unknown"` + `reason: "no_sample"`。
3. **权限只看服务端会话**。请求头里塞 UID/角色会被直接拒绝；前端能做的只是
   `ctx.isAdmin()` 决定画不画入口，真闸门在 BFF。
4. **17 个账户不等于 17 个人**。`per_user[]` 每行带 `account: "human" | "system"`，
   判据是 **UID**（`< 1000` 或 `65534` = 系统服务账号），不看用户名像不像人名。
   快照里是 4 个人类账户（fjd / kaidong / llmlab / vince）+ 13 个系统服务账号
   （root、polkitd、messagebus、avahi、_chrony…）。前端默认只列人类账户，系统账号
   折成一句摘要按需展开 —— 数据完整，只是不让守护进程冒充使用者。
   已知边界：`llmlab` 是平台自建的服务账号，但 uid ≥ 1000，因此被判为 human。

---

## 6. 不要做的事

- 不要把快照里的数值（16 把 key、173 行日志、1 个引擎）写死进代码或测试。
- 不要按快照里的主机名/端口写死地址；BFF 只绑 `127.0.0.1:8790`。
- 不要把「有数据」当成默认：前端现有的 LIVE-or-UNCONFIGURED 策略
  （拿不到就画 UNKNOWN，绝不回退 demo）要继续保持。`?demo=1` 只允许在 `web/qa.html`。
- 改契约形状会连带打破 `tests/web-features/` 下大量逐字断言，改之前先读那些用例。

---

## 7. 怎么重抓

抓取脚本（不在仓库里，属运维侧）：登录 → 逐条 GET → 递归脱敏 → 落盘。
重抓时把 `responses/` 整体替换即可，`_routes.json` 用
`backend/bff/route_map.py` 的 `ROUTES` 导出。
