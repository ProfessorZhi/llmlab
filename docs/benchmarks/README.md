# Benchmark summaries

Summary-level results from benchmarks run on the retired private `llm-lab`
repository. Only conclusions and aggregate numbers are published.

| Run | Directory | Contents |
|---|---|---|
| 2026-10-10 IQ2_XS low-RAM 32K | `2026-10-10_iq2xs_lowram_32k/` | `summary.md` plus three aggregate JSON files: `bench_summary.json`, `loadtest_summary.json`, `quality_summary.json` |
| 2026-10-23 IQ2_XS NVMe vs HDD | `2026-10-23_iq2xs_nvme_vs_hdd/` | `summary.md` plus `storage_summary.json` (aggregates) |

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

The 2026-10-10 run required the specific quantization and the node's memory
ceiling; it is a historical measurement with no runner.

The 2026-10-23 NVMe/HDD comparison ships with the harness that produced it:
`tools/bench/strata_storage_compare/`. It stops the engine service, evicts page
cache and starts one manual instance per storage root, so run it in a
maintenance window and use `--dry-run` first.

Treat all numbers here as context, not as a regression gate.
