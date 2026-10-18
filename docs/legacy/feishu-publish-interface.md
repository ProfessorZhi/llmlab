# 飞书知识库「专项汇报」发布接口

**本阶段只定义接口，不写入飞书。** 实现见 `integrations/feishu.py`。

## 为什么先做接口而不是直接写

发布到公司知识库是**对外、且难以撤销**的动作：别人会读到它，它可能被索引，而一个
放错位置的节点不是作者能悄悄修好的。一个「能写」的函数，最终一定会被意外调用——
从重试循环里、从一次「顺手修下列表」的重构里、从某个忘了打桩的测试里。让写路径
**除非显式解锁否则拒绝**，就是把这种意外变成一声响亮的报错。

## 工作流

```
reports/special-reports/<report>.md
        │
        │  PublishRequest(title, markdown_path, space_id, parent_node_token,
        │                 tags, provenance)
        ▼
   plan_publish(request, body_markdown)      ← 纯函数：不读、不写
        │
        │  返回 PublishPlan
        │    content_sha256   内容指纹，供人工比对「要发出去的就是这一份」
        │    content_bytes
        │    warnings         缺字段时的提醒（见下）
        ▼
   publish(plan, acknowledged=False)         ← 默认拒绝
        │
        ├── acknowledged=False  ⇒ 抛 FeishuWriteRefused
        └── acknowledged=True   ⇒ 抛 NotImplementedError
                                    （本项目没有接凭据路径，且刻意不接）
```

`publish()` 即使传入 `acknowledged=True` 也仍然抛异常。签名先立起来，是为了让调用方
和它的测试**按真实形状**编写；在「只设计接口」的阶段凭空造一条凭据路径，正是 brief
所警告的那种错误。

## 未满足的前置条件（记录在此，避免以后重新发现）

| 前置条件 | 现状 |
|---|---|
| 具备 `docx` + `wiki` scope 的应用凭据 | **无**。且它应当来自 vault，**不是配置文件里的密码** |
| 目标 `space_id` | **未确定** |
| 父节点 `parent_node_token` | **未确定** |
| `redirect_uri` 白名单核对 | **未做**。这一项失败时的症状**看起来像凭据问题**，其实不是——所以必须在任何 OAuth 步骤之前先查 |

### 两个「不可信输入」

`space_id` 和 `parent_node_token` 都**不可信**：

* 配置里抄来的值会过期；
* **API 返回的 parent 也不权威**（本项目在飞书侧的既有教训：返回的 parent 不可信，
  `--space` 应为 `--space-id`）。

因此真实实现必须在**发布时刻**从 API 解析目标，而不是信任任何被记住或抄写的值。
`plan_publish()` 在两者为空时会写进 `warnings`，就是为了让这个缺口在评审时可见，
而不是等发布时才发现。

## provenance 是必填的

已发布的报告必须说明**它由哪些产物、哪一次运行**生成。`plan_publish()` 在
`provenance` 为空时给出警告。理由：报告一旦离开本机，读者就无法再从文件系统判断
它是原始版本还是重新生成的版本。

## 启用前必须完成的检查清单

1. 凭据存放在 vault，且**不进 Git、不进日志、不进报告**。
2. `redirect_uri` 白名单已验证（用不着凭据即可探测）。
3. `space_id` / `parent_node_token` 在发布时刻由 API 解析。
4. **先 dry-run**：把 `PublishPlan.as_dict()` 落盘供人工审阅，确认标题、目标、
   `content_sha256` 与 `content_bytes` 都对得上。
5. 只有在上述都成立后，才讨论把 `publish()` 的写路径接上——那是另一次变更，需要
   单独确认。
