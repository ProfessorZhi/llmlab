# 评测（EvalScope）

## 现状

| 项 | 值 |
|---|---|
| EvalScope | **1.12.0**，含 `[service]` extra |
| 虚拟环境 | `evaluation/.venv`（Python 3.12） |
| CLI | `evaluation/.venv/Scripts/evalscope.exe` |
| Dashboard | `evalscope service --host localhost --port 9000` |
| 全局 Python | **未修改** |

安装用 `deploy/evalscope/install-evalscope.ps1`（走 `pypi.tuna.tsinghua.edu.cn` 镜像，可覆盖）。
镜像索引相对 pypi.org 有延迟，脚本对此有提示。

## 依赖隔离

EvalScope 的依赖量很大，所以它**独占一个 venv**；控制台则是纯标准库、不共享这个环境。
这样评测依赖的升级或冲突**不会**波及控制台，反之亦然。

## 硬约束

* **不在 {{LAB_NODE}} 上安装前端或额外的大型评测环境。** 节点只服务 token。
* **不开展大型 Benchmark、不做生产 API 压测。**
* **`start.ps1` 不自动启动 Benchmark。** 启动 Dashboard 不等于开始跑评测。
* 控制台**加载页面时绝不调用** `/v1/chat/completions`——那会产生计费形状的调用。

## 与节点交互时注意

* 端点是 `http://{{LAB_HOST}}:8080`，配置在 `config/llm-lab.example.toml` 的
  `[endpoints.strata-local]`。
* API key 在 `config/secrets.env`，**不进 Git、不进日志、不进报告**。
* 节点是 `low-ram` / 32K 上下文 / `parallel=1`。**并发不是免费变量**：`parallel=1`
  意味着并发请求会排队，把并发当成吞吐来源会得到误导性的结论。
* 已有一次实测的并发短测数据显示 `ratio_conc_over_seq = 1.1946`（并发/串行墙钟比）——
  解读时请连同 `parallel=1` 这个前提一起看。

## 数据落点

* 镜像过来的评测产物：`data/benchmarks/<date>_<profile>/`，出处记在
  `data/benchmarks/provenance.json`。
* 大规模 EvalScope 运行输出**不入库**（`.gitignore` 已排除 `evaluation/data/`、
  `evaluation/outputs/`、`evaluation/**/work_dir/`），因为它是可再生的且会失控增长；
  **作为证据保留的是小而明确的产物**。

## 未验证的部分

`evaluation/.venv` 里的 EvalScope 已确认**可导入、版本正确、CLI 存在**，
但**尚未真正跑过一次评测**，Dashboard 也**尚未成功拉起**。首次实跑应被视为一次测试，
而不是例行操作。
