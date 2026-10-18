# Strata + Qwen3.8-Flash-Next IQ2_XS — 基准测试摘要

- 日期：2026-10-10
- 主机：{{LAB_NODE}} / i9-10900X (10C/20T) / 30.6 GiB RAM / RTX A6000 48 GB
- 引擎：Strata v0.1.41 (`fb58e0dbc8399662c0e47c76578c6e878b14f6cf`)
- 模型：Qwen3.8-Flash-Next IQ2_XS (GSQ-RCO)，MoE 48 层 / 512 专家
- 部署：Low-RAM（专家常驻主机 RAM + GPU 专家缓存 24576 个 = 33.02 GiB）
- 运行参数：`--max-context 32768 --kv int8 --spec 4 --spec-min-p 0.5 --resident-experts`

## 加载与启动

| 项 | 实测 |
|---|---|
| 引擎冷启动（独立 `strata generate`，含专家缓存填充） | 61.6 s |
| 其中专家缓存填充 | 42.6 s @ 833 MB/s |
| 首个 token | 60.1 s |
| 经 `serve/server.py` 到 `/health` 就绪 | ~65 s |
| VRAM 峰值 | 39,350 MiB / 49,140 MiB |
| 可用内存最低 | 23.1 GiB |
| Swap 峰值 | ~460 MiB |
| 磁盘读取 | 91.0 GB |

## 单路性能（parallel=1, temperature=0, 输出 256 tokens, 3 次取中位数, 全部 cached=0）

| 目标 prompt | 实际 prompt | TTFT | Prefill | Decode | E2E |
|---|---|---|---|---|---|
| 1K | 1069 | 0.896 s | 1214.1 tok/s | **172.6 tok/s** | 2.407 s |
| 4K | 4147 | 1.988 s | 2105.3 tok/s | **164.2 tok/s** | 3.541 s |
| 16K | 16403 | 6.385 s | 2577.1 tok/s | **155.9 tok/s** | 8.051 s |

短提示词（62 tokens）实测：prefill 178.4 tok/s（固定开销占比高），decode 138.4 tok/s，
MTP 草稿接受率 80/114 = **70.2%**。

## 并发（parallel=2，与同提示词串行对照组比较）

| Prompt | 串行 wall | 并发 wall | 并发/串行 |
|---|---|---|---|
| 512 | 4.42 s | 5.29 s | 1.164 |
| 1024 | 4.87 s | 5.93 s | 1.217 |
| 4096 | 7.46 s | 8.54 s | 1.145 |
| 16384 | 16.53 s | 18.64 s | 1.128 |

并发在**每一个**尺寸上都更慢（1.10–1.22×），且第二个用户的 TTFT 约翻倍
（512: 0.68→1.52 s；4K: 2.03→4.25 s；16K: 6.4→15.7 s）。资源安全：
VRAM 峰值 40,714 MiB，可用内存最低 22.5 GiB，swap 峰值 502 MiB（与 parallel=1 持平）。
**落地配置取 `parallel: 1`。**

## 质量评测（内部第一版测试集，40 题，全部机械判分）

| 类别 | 通过 | 说明 |
|---|---|---|
| 中文能力 | 10/10 | 四选一 |
| 数学推理 | 10/10 | 数值比对（含分数） |
| Python 代码 | 10/10 | 代码在独立 venv 沙箱中**实际执行**测试 |
| 长上下文 needle | 3/3 | 16,378 token 上下文，深度 5% / 50% / 95% |
| JSON 输出 | 2/3 | `js02` 失败：要求 3 元素数组，只输出首个对象 |
| Function calling | 4/4 | 工具选择、参数类型、该不调用时不调用 |
| **合计** | **39/40 = 97.5%** | |

## 产物清单

- `hardware.txt` — 主机/CPU/GPU/内存/磁盘/软件基线全文
- `configuration.json` — 生效配置、`/proc` 中的实际引擎 argv、数据树与哈希
- `performance.jsonl` / `bench_summary.json` — 单路 9 次请求逐条与中位数
- `loadtest_summary.json` / `loadtest_raw.log` / `loadtest_samples.json` — 首次加载
- `chat_smoke.json` / `chat_smoke_keyed.json` — API 冒烟（含引擎自报计时）
- `concurrency.json` / `concurrency_short.json` / `sequential_control.json` — 并发与对照
- `quality/` — 40 题逐条 `quality_results.jsonl`、`quality_summary.json`、`eval_run.log`
