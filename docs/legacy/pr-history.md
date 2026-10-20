# Legacy PR dispositions

The private `llm-lab` repository was retired with several long-lived branches.
This note records what happened to each one during consolidation so nobody has
to re-open the old repository to find out.

**No pull-request body, prompt, request log or review comment is reproduced
here.** The entries are design summaries only.

| Old PR | Subject | Destination in this repository | Kept? |
|---|---|---|---|
| #2 | Single gateway: key store, per-key limits, telemetry | `legacy/gateway/pr-02-single-gateway/` | Code kept as reference |
| #12 | CPU-only vision request contract and mock end-to-end tests | `legacy/vision/` | Code kept as reference |
| #16 | Concurrency-2 candidate profile | `legacy/gateway/pr-16-concurrency-2/` | Code kept as reference; never a production default |
| #17 | Request-body capture for content logging | — | **Excluded.** See below. |
| #20 | Multi-engine routing, endpoint ACL, owner scoping | `legacy/gateway/pr-20-multi-engine-routing/` plus `docs/legacy/dell-migration/phase4–6` | Code and notes kept as reference |

## PR #17 — why it is excluded

PR #17 implemented optional capture of request bodies (including user prompts and
model responses) for content-level logging. It was never merged into the default
path in the old repository, and it is **not imported here in any form**: no code,
no recorded request bodies, no captured samples.

The constraint that survives is a design rule, not an implementation:

* The portal must not persist prompt or response content by default.
* Any future content-level observability feature needs an explicit opt-in, a
  retention bound, and a documented redaction path before it is written.
* Logs and receipts in this repository must not contain request text.

`legacy/integrations/` and `legacy/gateway/` were checked to confirm they do not
carry request-body logging code.

## Consolidation note

This page preserves historical PR dispositions and is not a statement of the current development target. The old repository branches remain read-only evidence. Both `ProfessorZhi/llm-lab` and `ProfessorZhi/llm-lab-portal` are **Private + Archived**; their historical PR/Issue references are not new work queues.

New source development happens exclusively through branches and pull requests in [`ProfessorZhi/llmlab`](https://github.com/ProfessorZhi/llmlab), with the Dell RTX A6000 as the designated long-term development environment. This supersedes the former instruction to develop on `llm-lab-portal`, without rewriting the historical decisions above.
