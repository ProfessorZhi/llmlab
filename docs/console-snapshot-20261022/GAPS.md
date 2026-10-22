# 快照里的缺口：哪些是真的空，哪些是坏了

改界面前先读这一页。快照里「空」不等于「零」，下面每一条都标了成因。

---

## 1. 请求日志页读不到数据（真缺陷）

```
GET /api/atlas/calls  →  503
{"error": {"code": "atlas_adapter_not_attached",
           "message": "the atlas (calls) adapter is not attached"}}
```

`llmlab-bff.service` 的启动参数里没有给 atlas 传数据源，所以这个适配器根本没挂上。
「请求日志」页（`#/requests`）读的就是它 ⇒ **这一页永远是空的**，与有没有人调用无关。

对照组：概览页「API 调用」段读的是 `<ops>.gateway_feed`，那条**是通的**（见第 2 条）。
如果要把请求日志救活，两条路：给 BFF 补上 atlas 参数，或者把该页改成读 `gateway_feed`。

## 2. 网关用量：有日志、但窗口内为 0、且一把 key 都归不到人

`api-v2-ops.json` → `data.ops.gateway_feed`：

```json
{"feed": "gateway-usage-export", "readable": true, "lines": 173,
 "events": 173, "in_window": 0, "last_event_ts": "2026-10-15T01:37:43Z",
 "keys_total": 16, "keys_bound": 0, "unmapped": 0}
```

三件事同时成立：

- **读得到**（`readable: true`，70323 字节）——不是权限问题；
- **近 24 小时 0 条**（`in_window: 0`），日志最后一次写入是 10-15；
- **16 把 key 里 0 把声明过属主**（`keys_bound: 0`）。`keys.json` 本身没有属主字段，
  归属只能由人写进 `~/.llmlab-state/gateway-key-owners.json`。

页面正确说法应是「近 24 小时 0 次 · 日志最近 10-15 09:37」，而不是「未接入」。

## 3. 指标卡全是 UNKNOWN

`<ops>.metrics.items` 每一条都是：

```json
{"value": null, "state": "unknown", "reason": "no_sample", "source": null}
```

这是**正确状态**：没有白名单来源（`gateway-usage-export` / `nvml-verified` /
`prometheus-readonly`）就一律不给数字。不要为了「好看」把它们填成 0。

## 4. 趋势曲线没有

`<ops>.trends` 是 `null`。原因同上：没有可用的历史采样源。
前端对 `trends: null` 必须画空态，**不许**塞合成曲线（`contract.js` 会直接判契约错误）。

## 5. 两条管理员接口在快照里是 403

抓取用的是平台账户 **vince（member）**，不是管理员：

| 端点 | 状态 | 含义 |
|---|---|---|
| `GET /auth/admin/users` | 403 `forbidden` | 「用户与权限」页对成员不可见 |
| `GET /api/model-service/key` | 403 `bff.access_denied`（"a platform administrator is required"） | 「API 密钥」页对成员不可见 |

`key-page-projection.json` 用服务层补了后者的真实形状（`ModelServiceKeyStore.list()`，
与路由同一句代码），值已脱敏。**`/auth/admin/users` 没有补**——它没有等价的离线投影，
要它的真实形状只能拿管理员会话重抓。

另注：`<ops>.admin` 在成员视角下是 `{"available": false, "reason": "missing_required_scope"}`。

## 6. 引擎/模型的读写不对称

```
GET  /api/v2/registry/engines   → 405   （只有 POST = 登记）
GET  /api/v2/registry/models    → 405   （只有 POST = 登记）
```

列表其实在 `GET /api/v2/ops` 的 `data.ops.registry.engines` / `.models` 里。
当前真实内容：**1 个引擎**（`strata-qwen38-flash-next`，:8080，属主 fjd）、
**1 个模型**（`qwen3.8-flash-next-iq2_xs`，属主 fjd）。
`registry.adapter_attached: true`，`ownership_state: "operator_verified"`。

## 7. playground 不可用

`<ops>.playground` = `{"authorized": false, "backend": null, "reason": "no_authorized_backend"}`。
成员没有可用的试玩后端，前端应显示「无授权后端」而不是禁用按钮了事。

## 8. `notes` 里有几句和实测对不上（文案债）

`api-v2-ops.json` → `data.ops.notes` 目前还写着：

> 「转发层是 mock，不是生产 Gateway。」
> 「观测数据来自合成样本；……只有曲线带 SYNTHETIC 标记。」

但同一份响应的 `synthetic` 是 `false`，`gateway_feed` 读的是真实 `usage.jsonl`，
`trends` 是 `null`（没有曲线，也就谈不上 SYNTHETIC 标记）。**这几句是上一版状态留下的**，
建议随这次改版一并核对，否则会让人以为页面在造假。

## 9. 交付的密钥绕开了网关（产品口径问题）

发给外部使用者的模型服务密钥，走的是：

```
tailscale funnel :8443  →  本机 :8081（白名单反代，只放行 /v1/*）  →  Strata :8080
```

**整条不经过 :8200 网关**。后果：

- `usage.jsonl` 不会增长，「调用量」「请求日志」都看不到这些调用；
- 也就没有按 key 限流、没有吊销按钮；
- 唯一的吊销手段是改 `~/.config/strata/api-key.env` 再重启引擎（约 55–70 秒，无热加载）。

所以「请求日志」页的空态文案已改成如实描述：
*「这里只统计经网关转发的调用；直连推理服务的请求不经过网关，不会出现在这里。」*

要根治，得把交付链改回走网关（同时把网关的 `--global-limit 1` 抬开），
那是一次单独的发布，不在前端改动范围内。

## 10. 插件页

`gateway` 卡片已在 2026-10-22 从插件页**删除**：`:8200` 没有任何 Web UI
（`/`、`/ui/`、`/sso/key/generate` 全部 404 JSON），卡片只能填出一个假的「跳转地址」，
而它的运行状态在概览「实际运行状态」和推理服务页都已经显示。

插件页现在只剩：Strata、vLLM、SGLang、Prometheus、Grafana、EvalScope。
其中已配跳转地址的只有 4 个（Strata / Prometheus / Grafana / EvalScope），
vLLM 与 SGLang 是「按服务登记时检测」的纯适配器，没有界面。
