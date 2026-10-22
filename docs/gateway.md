# 集成网关与推理服务参数

控制台里的「网关」一栏是平台自带的 OpenAI 兼容网关（类似 LiteLLM），和控制台**同源**：
`https://<控制台地址>/v1`。代码在 `backend/gateway/`，由 BFF 按 `/v1/` 前缀挂载在同一进程里。

## 产品规则

| 问题 | 做法 |
| --- | --- |
| 网关放哪 | 嵌在主站，同源 `/v1`；管理界面是控制台的「网关」一栏（概览 / 模型路由 / API 密钥 / 调用日志 / 在线体验）。 |
| 运行中的服务共享吗 | 共享。服务所有者把自己登记的推理服务**发布**成一个模型名，选「全实验室」或「仅自己」。大家的请求进同一组并发槽位，不重复占显存。 |
| 想要不同配置 | 那是另一个部署（另起服务、另行登记），而不是改别人正在跑的那个。 |
| API 密钥 | 不共享。每人建自己的 `sk-lab-…` 密钥：可限定模型、每分钟请求数、并发、有效期，随时撤销。平台只存 SHA-256 摘要，明文只在创建时显示一次。 |
| 引擎自己的密钥 | 只留在服务端：用 `--engine-secret-ref ENGINE=FILE:VAR` 指向引擎的 env 文件，网关转发时注入；调用者的密钥和身份头永远不会到达引擎。 |
| 谁看得到什么 | 自己：自己的密钥、自己的调用。服务所有者：谁调用了自己发布的模型（只有次数与 Token）。管理员：切到「全实验室」看全部。 |

## 日志

- **以密钥为单位**：「调用日志」顶部是每把密钥的卡片（24 小时请求、7 天 Token、失败数），点一把只看它的调用；在线体验单独一张卡。
- **简要日志**：时间、密钥、模型、Token（输入 → 输出）、耗时、状态码。点任一行打开**单次请求详情**（调用者、接口、上游模型、推理服务、首字/总耗时、生成速度、错误原因）。
- **全量日志**：`#/gateway/logs/full`，最近 30 天最多 500 条的全部字段，可导出 JSONL。
- **正文默认不记录**。密钥所有者可以为某一把密钥打开「记录请求内容」：之后这把密钥的请求 / 响应正文各截断到 64 KiB、保留 7 天，只有本人和管理员能在详情里看到；只有本人能开关（管理员不能替别人打开）。

接口（全部要求会话；写操作要求 CSRF）：

| 路由 | 用途 |
| --- | --- |
| `GET /api/gateway/overview?window=24h\|7d\|30d[&view=all]` | 汇总：请求、Token、成功率、P50/P95、首字、时间序列、按模型 / 调用者 / 密钥、结果分布、星期×小时热力图、我的服务被谁调用 |
| `GET /api/gateway/keys[?view=all]` · `POST /api/gateway/keys/create` · `POST /api/gateway/keys/{id}/revoke` · `POST /api/gateway/keys/{id}/update` | 密钥（含每把密钥的 24h / 7d 用量） |
| `GET /api/gateway/models` · `POST /api/gateway/models/publish` · `POST /api/gateway/models/{id}/update` | 模型路由 |
| `GET /api/gateway/logs?[view=all][&key_ref=][&model=][&outcome=][&limit=]` · `GET /api/gateway/logs/{request_id}` | 简要日志与单次详情（按密钥筛选的参数叫 `key_ref`：`key_id` 在 BFF 的身份守卫里是保留字） |
| `POST /api/gateway/playground` | 在线体验（会话身份、非流式、最多 1024 输出 Token），照常记账 |

## 模型服务密钥（平台签发给调用方）

网关密钥（`sk-lab-…`）用来**经过网关**调用模型；模型服务密钥（`sk-qwen-…`）是另一件事：
由平台管理员签发，**直接**填进客户端去调本机推理引擎（Strata），不经过网关，因此也没有
网关那套按密钥的限流、日志与撤权。代码在 `backend/bff/model_service_keys.py`。

| 事实 | 做法 |
| --- | --- |
| 谁能看、谁能发 | 只对平台管理员开放（`GET /api/model-service/key`、`POST /api/model-service/keys`，写操作要 CSRF + 同源）。成员得到 403，不是一份掩码清单。 |
| 为什么是明文 | 运维要把整串填进客户端，掩码帮不上忙。这是 BFF 唯一会把凭据原样下发给前端的路由，因此它比别的路由窄。 |
| 存在哪 | `<state-dir>/model-service-keys.json`，0600；旧版单数文件 `model-service-key.json` 仍会读取，升级后已发出去的密钥不会消失。 |
| 写引擎环境文件 | **只追加，不覆盖**。`~/.config/strata/api-key.env` 里第一条是网关自己在用的 key，覆盖它等于把网关上游打断，而报错会出现在另一个组件里。 |
| 什么时候生效 | 引擎只在启动时读一次环境文件，所以签发会触发 `systemctl --user restart strata.service`。响应里的 `restart` 如实反映重载有没有被触发：`triggered:false` + 原因（例如 unit 不存在），此时密钥已写入但还不可用。 |
| 地址 | 对外交付地址是**部署事实**，不进源码（内网与 tailnet 地址属于具体机器）。部署时用 `LLMLAB_MODEL_SERVICE_{PUBLIC,EXTERNAL,LAN}_ENDPOINT` 声明，没声明的就不记录；页面据此不显示调用地址，而不是显示一个猜出来的地址。 |

引擎自身的凭据仍然只在服务端（`docs/gateway.md` 上一节与 `--engine-secret-ref`）；这条路由下发的是
**给调用方用的**那一把，两者的权限范围不同。

## 推理服务参数

推理服务详情页（`#/services/<engine_id>`）的「运行参数」由后端参数表生成，每个框架不同
（`backend/runtime/params.py`）：

- llama.cpp / Strata：上下文长度 `--ctx-size`、并发路数 `--parallel`、GPU 层数、批大小、Flash Attention、KV 缓存精度；
- vLLM：`--max-model-len`、`--max-num-seqs`、显存占用比例、张量并行、dtype、前缀缓存、KV 缓存精度；
- SGLang、Ollama 各有自己的表。

能只读测到的值会实测（llama.cpp 系 `GET /props`：每槽上下文 × 槽位数；vLLM `GET /v1/models` 的 `max_model_len`），
页面同时显示实测值与目标值。保存的是**目标值**；平台不替别人重启进程，而是生成对应的启动命令，所有者在运行用户下重启后，
实测与目标一致即显示「已生效」。

## 本地运行

```bash
python3 -m backend.bff --state-dir /tmp/llmlab-dev --database /tmp/llmlab-dev/registry.db \
  --observe-local-engines --engine-secret-ref strata-qwen=$HOME/.config/strata/env:STRATA_API_KEY
```

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8123/v1", api_key="sk-lab-…")
client.chat.completions.create(model="qwen3.8-flash-next", messages=[{"role": "user", "content": "你好"}])
```
