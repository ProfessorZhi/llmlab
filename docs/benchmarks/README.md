# Benchmark summaries

Summary-level results from benchmarks run on the retired private `llm-lab`
repository. Only conclusions and aggregate numbers are published.

| Run | Directory | Contents |
|---|---|---|
| 2026-10-10 IQ2_XS low-RAM 32K | `2026-10-10_iq2xs_lowram_32k/` | `summary.md` plus three aggregate JSON files: `bench_summary.json`, `loadtest_summary.json`, `quality_summary.json` |

## What is here

* Aggregate throughput, latency and quality scores.
* The model/quantization configuration under test (model name and quant
  parameters are not sensitive).
* Conclusions about whether the configuration is usable.

## What is deliberately **not** here

| Excluded | Reason |
|---|---|
| Raw per-request samples (`*.jsonl`) | They contain prompts, generated text and timestamps. Not published. |
| `provenance.json` | It records machine-local paths and the exact host configuration. |
| Hardware/configuration dumps | They describe internal node configuration in detail. |
| Quality and performance per-sample rows | Same content risk as raw samples. |
| The deployment report (`reports/deployment/`) | It contains internal topology and access instructions. |
| Screenshot `08-evalscope-dashboard.png` | It shows a workstation-local path. |

The aggregate JSON files were sanitized: host names, addresses and absolute
paths are placeholders or removed.

## Reproducing

These runs required the specific quantization and the node's memory ceiling.
They are historical measurements, not a benchmark harness — there is no runner
in this repository. Treat the numbers as context, not as a regression gate.
