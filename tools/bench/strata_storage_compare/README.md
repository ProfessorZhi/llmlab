# Strata storage comparison harness

同一份权重、同一个引擎参数，在两个存储根上各冷启动一次，比较启动耗时、
TTFT、decode 速度和并发吞吐。用于回答“模型该放 NVMe 还是 HDD”。

> **这是实验工具，不是 CI 测试。** 它会停掉正在服务的引擎、清空目标目录的
> 页缓存、手动启动实例，并在结束时恢复 systemd 服务。不要在有人使用的时段跑。

## 前提

- 两个存储根下各有一份完整的模型/引擎数据（内容一致）。
- 当前用户能读这两个目录，并能执行 `posix_fadvise`（不需要 root）。
- 一张 GPU，一次只能跑一个实例；脚本会等显存释放后再启动第二个。
- 引擎是 OpenAI 兼容接口，`GET /v1/models` 可用于就绪探测。
- 凭据通过 `STRATA_API_KEY` 或 `--key-file` 提供；脚本不会把 key 写进结果。

## 用法

```bash
STRATA_API_KEY=... python3 tools/bench/strata_storage_compare/run_compare.py \
    --config /opt/strata/strata-iq2_xs.json \
    --source-root /srv/strata-data \
    --targets nvme=/srv/strata-data hdd=/data/scratch/strata-bench \
    --model qwen3.8-flash-next-iq2_xs \
    --systemd-unit strata.service \
    --suite extended \
    --out /tmp/strata-compare
```

先跑 `--dry-run`：只生成两份改写后的配置并打印路径，不碰服务。

常用参数：

| 参数 | 含义 |
|---|---|
| `--config` | 基准引擎配置；其中所有 `--source-root` 字符串会被替换成目标根 |
| `--targets NAME=DIR` | 至少两个存储根，按给出顺序执行 |
| `--suite basic` | 短问、2.4K 上下文、多样问题 |
| `--suite extended` | 上下文缩放、输出长度、并发、思考开关（默认） |
| `--systemd-unit` | 测试前停止、结束后恢复的 unit；不传则要求端口已空闲 |
| `--only nvme` | 只跑其中一个根，便于复测 |
| `--port` / `--client-host` | 引擎监听端口与客户端连接地址 |
| `--python` / `--server` | Strata 启动入口，默认指 `/opt/strata` |

## 文件

| 文件 | 作用 |
|---|---|
| `run_compare.py` | 驱逐缓存、启动/停止引擎、采集 `/proc` 与显存、调用客户端、恢复服务 |
| `client_basic.py` | 短问、长上下文、多样问题；3 次取中位数 |
| `client_extended.py` | 512–32K 上下文、128–1024 输出、1/2/4/8 并发、思考开关 |

每次请求都带随机前缀，避免前缀缓存把 TTFT 压成缓存命中；上下文套件排在
并发套件之前，原因见下。

## 输出

`--out` 目录下：

- `config-<name>.json`：改写后的引擎配置；
- `engine-<name>.log`：引擎日志；
- `result-<suite>.json`：启动耗时、启动读取量、运行期读取增量、显存、
  每组请求的聚合指标。

结果里含本机路径和 PID，**不要原样发布**。`docs/benchmarks/` 只收聚合数字。

## 解释时注意

- **启动耗时**由读取权重的吞吐决定，与模型大小一起看才有意义。
- **TTFT 随上下文线性增长是正常的**；比较存储介质时要看同一 prompt 长度下的差值。
- **decode 速度**才是随机读性能的敏感指标。HDD 上它会随专家访问局部性剧烈波动。
- **并发聚合吞吐**要区分“含 prefill 的墙钟吞吐”和“单请求 decode 速度”：
  前者可能持平，后者会随并发线性下降。
- 引擎在请求期间重启会被记为 `engine_restarted`。这类样本不是延迟数据，
  应单独排查，不要并进中位数。

## 已知引擎问题

在一轮早期测试中，4 路并发之后紧接着发送 512-token 以上的 prompt，prefill
报 `cuBLAS status 14`，监督进程拉起新实例，客户端那条请求等了约 63 秒。
把上下文套件移到并发之前后不再复现（0 次重启），但生产上多用户并发时仍可能
触发。脚本因此固定了这个顺序，并把重启单独计数。
