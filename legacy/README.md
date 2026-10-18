# Legacy `llm-lab` material

This directory preserves the parts of the retired private `llm-lab` repository
that are still useful as reference while the new portal is built. **Nothing in
`legacy/` is a running entry point** and nothing here is imported by the new
application.

The import was sanitized: host names, addresses, ports, absolute paths, account
names, group names and credentials were replaced with `{{PLACEHOLDER}}` values or
example values. Host identity material (`config/ssh/*`) was not imported, no
request or prompt content was imported, and no raw benchmark sample is included.

## Contents

| Directory | Origin | Purpose |
|---|---|---|
| `console/` | `portal/server.py`, `portal/web/*` | The old read-only operations console. Kept as a UX and implementation reference; not an entry point. |
| `integrations/` | `integrations/*.py` | Strata, OpenAI-compatible and Feishu adapters. The new backend owns the supported adapters; these are reference implementations. |
| `gateway/pr-02-single-gateway/` | legacy PR #2 | Single-gateway, key-store and rate-limit implementation. |
| `gateway/pr-20-multi-engine-routing/` | legacy PR #20 | Multi-engine routing, endpoint ACL and routing self-tests. |
| `gateway/pr-16-concurrency-2/` | legacy PR #16 | The concurrency-2 candidate profile and its self-test. Not a production default. |
| `vision/` | legacy PR #12 | CPU-only vision request contract and mock end-to-end tests. |
| `evaluation/` | `evaluation/` | EvalScope integration notes. |
| `monitoring/` | `monitoring/` | Read-only collection design notes. |
| `windows-tools/` | `scripts/*.ps1`, `scripts/*.py` | Windows start/stop/self-test/bootstrap tools. |

## Deliberately excluded

| Item | Reason |
|---|---|
| `config/ssh/*` | SSH host fingerprints and public keys are host identity material. |
| legacy PR #17 | It recorded request bodies (including prompts) for content logging. High privacy risk; not merged into any default path. Its design constraints are summarized in `docs/legacy/pr-history.md`. |
| `data/benchmarks/*` raw samples | Per-request samples, prompts and raw measurement rows are not published. Only summarized conclusions are kept under `docs/benchmarks/`. |
| `reports/deployment/*` | The original deployment report contains internal topology and access instructions. Not published. |
| `docs/screenshots/01-portal-home.png`, `08-evalscope-dashboard.png` | Contain a node address/account or a workstation-local path. Not published. |

## Placeholders

See `deploy/templates/README.md` for the placeholder table. The same conventions
apply here.
