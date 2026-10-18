# Host 只读证据采集规格（V1-02 / #74）

**状态：OPERATOR-RUN / READ-ONLY / NOT YET EXECUTED。** 本文不是“已完成的证据”，
而是一份**可以照着执行**的采集单。在 Dell #83（CloudCLI 审批与工作根）被运维确认
修好之前，任何一次执行都会以 `BLOCKED_ACCESS` 收场；届时**不要**重试、不要绕过沙箱、
不要自行扩大目录权限。

**这份文件回答一个问题：** 我们怎样在不碰生产、不碰 GPU 作业、不取任何密钥的前提下，
证明那份 `llmlab_strata_*` 指标真的存在于 Dell 的 Prometheus，并且值/单位/新鲜度
符合契约？它**不**回答“模型能不能调”（那是 #76 / #79 的事），也**不**产生任何
LIVE 徽标。

关联实现：`backend/observability/sources.py` 的 `STRATA_HOST_METRICS` 与
`HostProjectionReader`；契约见 `docs/observability-contract.md` §16.6。

---

## 0. 硬性前置与禁止项

| 项 | 要求 |
|---|---|
| 执行者 | **人**（运维 / Host Owner），在 Dell 本机终端手动执行。不是 Agent 自动轮询。 |
| 权限 | 只需普通账号 + 本地环回 GET；**不需要** root / sudo / PAM / 改文件权限。 |
| 前置 | #83 的 CloudCLI 审批与项目工作根已由运维确认修复。 |
| 方法 | **只读 GET**。不写、不改、不重启任何服务。 |
| 禁止 | 不调模型、不发推理、不跑 GPU 作业、不抓私有标签/日志、不导出 `/health` 响应体。 |
| 禁止 | 不碰 Caddy / systemd / 8443 / Gateway Router / Strata 配置。 |
| 禁止 | 不传 `Authorization` / `Cookie` / `api_key`；不加 `-k`；不加 `-L`（不跟随重定向）。 |
| 落盘 | 证据写到受控 artifact 目录，**不要**提交原始响应到公开 Issue。 |

**为什么这么严：** 这次要证明的是“指标存在且语义正确”，不是“服务在跑”。把一次只读
采样做成一次生产变更，正是 V1 门禁要防的事。

---

## 1. 固定环回端点（唯一允许的集合）

| 组件 | origin / 查询基数 | 健康探测 URL（逐字） | 允许读什么 |
|---|---|---|---|
| Prometheus | `http://127.0.0.1:9090` | `http://127.0.0.1:9090/-/healthy` | 状态码、`status`、`resultType`、`value` 对、`result` 长度 |
| Gateway | — | `http://127.0.0.1:8200/healthz` | **仅** HTTP 状态码 |
| Grafana | — | `http://127.0.0.1:3000/api/health` | **仅** HTTP 状态码 |
| EvalScope | — | `http://127.0.0.1:9000/health` | **仅** HTTP 状态码 |

**明确不在允许集合里：Strata 的 `/health`。** 它的响应体带 `api_key`。Strata 的状态
**只**取自 `llmlab_strata_up` 这条指标（`HostProjectionReader` 也是这么实现的：探针
目标表里根本没有 strata）。任何“顺手 curl 一下 Strata /health 看看”的做法都违反本节。

> 端口/路径若与上表不符，**先停下来记录差异**，由 Host Owner 更新本文件与
> `HOST_HEALTH_TARGETS`，不要临场改参数。

---

## 2. 六个允许的指标（逐字）

| 投影键 | Prometheus 指标名 | 期望单位 | 归约 |
|---|---|---|---|
| `gpu_utilization_ratio` | `llmlab_strata_gpu_utilization_ratio` | `ratio`（0..1） | max |
| `gpu_temperature_celsius` | `llmlab_strata_gpu_temperature_celsius` | `celsius` | max |
| `gpu_power_watts` | `llmlab_strata_gpu_power_watts` | `watts` | max |
| `strata_up` | `llmlab_strata_up` | `bool`（0/1） | max |
| `gpu_memory_used_mib` | `llmlab_strata_gpu_memory_used_bytes` | `mib`（源为 **bytes**） | max |
| `gpu_memory_total_mib` | `llmlab_strata_gpu_memory_total_bytes` | `mib`（源为 **bytes**） | max |

查询就是**裸指标名**：没有 `{}`、没有函数、没有算术。任何“加个 `sum by (instance)`”的
写法都不在 allowlist 内。

---

## 3. 执行命令（复制即用，GET-only）

```bash
set -u
PROM='http://127.0.0.1:9090'
OUT="${OUT:-./host-evidence-$(date -u +%Y%m%dT%H%M%SZ)}"
mkdir -p "$OUT"

# 通用只读 GET：2s 超时、不跟随重定向、不发送任何凭据。
get() { curl --silent --show-error --max-time 2 --no-progress-meter -X GET "$1"; }
code() { curl --silent --show-error --max-time 2 --no-progress-meter -o /dev/null \
              -w '%{http_code}' -X GET "$1" || printf 'ERR'; }

# 3.1 Prometheus 自身健康（只要状态码）
printf 'prometheus /-/healthy -> %s\n' "$(code "$PROM/-/healthy")" | tee "$OUT/health.txt"

# 3.2 指标元数据：存在性 / type / help / unit（这是“名字确实存在”的唯一证据）
get "$PROM/api/v1/metadata" > "$OUT/metadata.json"

# 3.3 六个指标的即时向量：一次请求同时落盘响应体与 HTTP 状态码
for m in \
  llmlab_strata_gpu_utilization_ratio \
  llmlab_strata_gpu_temperature_celsius \
  llmlab_strata_gpu_power_watts \
  llmlab_strata_up \
  llmlab_strata_gpu_memory_used_bytes \
  llmlab_strata_gpu_memory_total_bytes
do
  curl --silent --show-error --max-time 2 --no-progress-meter \
       -o "$OUT/query-$m.json" -w '%{http_code}' -X GET "$PROM/api/v1/query?query=$m" \
       > "$OUT/status-$m.txt"
done

# 3.4 四个服务的可达性（只要状态码；响应体丢弃）
for pair in \
  "prometheus:$PROM/-/healthy" \
  "gateway:http://127.0.0.1:8200/healthz" \
  "grafana:http://127.0.0.1:3000/api/health" \
  "evalscope:http://127.0.0.1:9000/health"
do
  name=${pair%%:*}; url=${pair#*:}
  printf '%s %s -> %s\n' "$name" "$url" "$(code "$url")"
done | tee "$OUT/services.txt"
```

`export OUT=...` 可选；不设时写到当前目录的带时间戳子目录。

---

## 4. 处理原始响应：**只保留白名单字段**

原始 `query-*.json` 里每个序列都带 `metric` 标签（可能含 `instance` / `host` /
设备标识）。这些**不是**本次要证明的东西，而且会把“某台机器/某张卡”的标识带进证据。
用下面的 reducer 把它压成白名单结构，并且**丢弃 `metric` 标签**。

```bash
python3 - "$OUT" <<'PY'
import json, pathlib, sys, time
out = pathlib.Path(sys.argv[1])
metrics = [
    ("gpu_utilization_ratio", "llmlab_strata_gpu_utilization_ratio", "ratio"),
    ("gpu_temperature_celsius", "llmlab_strata_gpu_temperature_celsius", "celsius"),
    ("gpu_power_watts", "llmlab_strata_gpu_power_watts", "watts"),
    ("strata_up", "llmlab_strata_up", "bool"),
    ("gpu_memory_used_mib", "llmlab_strata_gpu_memory_used_bytes", "mib"),
    ("gpu_memory_total_mib", "llmlab_strata_gpu_memory_total_bytes", "mib"),
]
now = time.time()
rows = []
for key, metric, unit in metrics:
    path = out / f"query-{metric}.json"
    row = {"key": key, "metric": metric, "unit_expected": unit,
           "http_status": None, "prometheus_status": None, "result_type": None,
           "series_count": None, "sample_at_unix": None, "age_seconds": None,
           "values_raw": [], "state": "unknown", "reason": None}
    status_path = out / f"status-{metric}.txt"
    try:
        row["http_status"] = int(status_path.read_text().strip())
    except Exception:
        row["http_status"] = None
    try:
        doc = json.loads(path.read_text())
    except Exception:
        row["reason"] = "unreadable-response"
        rows.append(row); continue
    row["prometheus_status"] = doc.get("status")
    data = doc.get("data") or {}
    row["result_type"] = data.get("resultType")
    result = data.get("result") or []
    row["series_count"] = len(result)
    stamps, values = [], []
    for s in result:
        pair = s.get("value") or []
        if len(pair) == 2:
            try:
                stamps.append(float(pair[0])); values.append(float(pair[1]))
            except (TypeError, ValueError):
                pass
    if stamps:
        row["sample_at_unix"] = max(stamps)
        row["age_seconds"] = round(now - max(stamps), 3)
        row["values_raw"] = sorted(values)          # 无序数值列表，**不带标签**
        row["state"] = "known"
    else:
        row["reason"] = "no-sample"
    rows.append(row)
(out / "evidence.json").write_text(json.dumps({"captured_at_unix": now, "metrics": rows},
                                              indent=2, sort_keys=True))
print((out / "evidence.json").read_text())
PY
```

**不要**把 `metric` 标签、原始响应体、或任何 `api_key` / `Authorization` / `Cookie`
写进 `evidence.json`。

---

## 5. 证据记录允许 / 禁止的字段

**允许（白名单）**

`device_ref`, `host_id`, `captured_at_unix`, `executor`（角色而非个人姓名）,
`endpoint`, `http_status`, `key`, `metric`, `unit_expected`, `unit_observed`,
`prometheus_status`, `result_type`, `series_count`, `sample_at_unix`, `age_seconds`,
`values_raw`（无序数值数组）, `value`（max，按投影口径）, `state`, `reason`,
`probe_evidence_time`, `authorized_scope`.

**禁止（任何一条出现即视为证据失败）**

`api_key`、`Authorization`、`Cookie`、`Set-Cookie`、`token`、`password`、`secret`、
`private_key`、任何 `metric` 标签原文（`instance` / `host` / `uid` / `email` / 设备序列号）、
任何 `/health` 响应体、任何原始日志行。

采集完请做一次自查：

```bash
grep -RinE 'api_key|authorization|cookie|token|password|secret|private_key' "$OUT" \
  && echo 'FAIL: secret-shaped field present' || echo 'OK: no secret-shaped field'
```

---

## 6. 判定清单（V1-02 / #74 的实机部分）

逐条勾选；**未勾选的项保持 `HOST_PENDING`，不得用“代码已实现”顶替**。

- [ ] `/-/healthy` 返回 `200`。
- [ ] `/api/v1/metadata` 中六个指标名**全部存在**；记录每个的 `type` 与 `help`。
- [ ] 六次 `query` 均 `status == "success"` 且 `resultType == "vector"`。
- [ ] 每个指标 `series_count >= 1`（多卡时应 `> 1`，且基数被如实记录）。
- [ ] `sample_at_unix` 与 `captured_at_unix` 之差在 `[−15s, +90s]` 内（未来 ≤15s，过期 ≤90s）。
- [ ] `llmlab_strata_gpu_memory_*_bytes` 的量级与“字节”相符（换算成 MiB 后与卡容量同量级）。
- [ ] `llmlab_strata_gpu_utilization_ratio` 的取值落在 `0..1`。**若落在 `0..100`，停止并把
      单位差异报回 #74** —— 那需要一次契约变更，不能在采集侧偷偷除 100。
- [ ] `llmlab_strata_up` 的取值严格属于 `{0, 1}`。
- [ ] 四个服务探针记录了 HTTP 状态码（**只**记状态码）。
- [ ] 自查无 secret-shaped 字段（§5 的 grep 输出为 `OK`）。

**全部勾选后**，把 `evidence.json` 与 `services.txt` 存入受控 artifact，在 #74 记录
`HOST_READONLY_EVIDENCE=<artifact ref>`。在此之前，本 Lane 的实机部分一直是
`BLOCKED_ACCESS` / `HOST_PENDING`。

---

## 7. 这份证据**不能**证明什么

| 容易误读的结论 | 为什么不能得出 |
|---|---|
| “主机 LIVE” | 一次采样只说明“这一刻有数”，不说明它持续新鲜；LIVE 还需要 BFF 侧的 90s 判定与 UI 刷新契约。 |
| “模型 ready” | `strata_up=1` 只说导出器在报数；Gateway `/healthz` 200 只说端点答了。模型可调用还需要 Registry/Deployment/Router ACK 与引擎实际 served-model 状态（#76 / #79）。 |
| “GPU 空闲/忙碌” | 采到的是整卡量规；**不是**任何用户/项目的用量，也不能推出排队或可用性。 |
| “服务健康” | TCP 监听与 HTTP 200 都只是可达性；500 才是明确的“没就绪”。 |
| “趋势/历史” | 单次采集没有历史；趋势必须来自真实留存，缺口必须报出来，不能插值。 |

---

## 8. 变更控制

本文件的端点表与指标表是**契约的一部分**，与
`backend/observability/sources.py` 的 `STRATA_HOST_METRICS` /
`HOST_HEALTH_TARGETS` 逐字对应。任一处改动必须同一次提交里改另一处，并更新
`tests/observability/test_v1_host_source_contract.py` 的 pin。
