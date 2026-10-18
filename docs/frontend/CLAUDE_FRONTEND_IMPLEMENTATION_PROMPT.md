# Claude Code — Atelier V6 frontend completion brief (English, copy/paste)

You are the frontend implementation lead for **Atelier V6**, the multi-user Dell LLM Laboratory portal. Your mission is to bring the V6 frontend from a polished but partially static/mock dashboard into a **complete, cohesive, data-honest nine-page management interface**, while preserving the best parts of its original design.

**This is an implementation request.** Inspect the real repository and reference files, make a page-by-page plan, then implement the highest-value frontend improvements, add tests, capture screenshots when a real browser is available, and **push all finished work to GitHub as a Draft PR**. Do not stop after proposing a redesign.

## A. Repository and project context

- Repository: `ProfessorZhi/llm-lab-portal` (private), independent of the older `ProfessorZhi/llm-lab`.
- Read **`docs/architecture.md`**, **`docs/frontend/PROJECT_CONTEXT_AND_DESIGN_REFERENCES.md`**, **`docs/frontend/references/README.md`**, **`docs/frontend/SCREENSHOT_GALLERY.md`**, and [Issue #56](https://github.com/ProfessorZhi/llm-lab-portal/issues/56) before writing code.
- Read the existing frontend/source contracts (`docs/v6-ui-flows.md`, `docs/atlas-data-contract.md`, `docs/portal-call-log-contract.md`, `docs/observability-contract.md`, `docs/registry-cli-contract.md`) and inspect what the backend actually serves. Do not infer an API from a button.
- The lab has a Dell RTX A6000 (48 GB), Strata/Qwen and a Gateway, Prometheus/Grafana, EvalScope, independent Linux users, and a `llmlab` CLI. The frontend must use server-verified identity, per-user/Endpoint ACL and same-origin BFF semantics.
- Seven development lanes have Draft PRs #51/#52/#53/#54/#55/#57/#58. Phase B [PR #60](https://github.com/ProfessorZhi/llm-lab-portal/pull/60) is an isolated **NOT DEPLOYED / NO-GO** integration candidate. Note that UI [PR #54](https://github.com/ProfessorZhi/llm-lab-portal/pull/54) has a newer frontend fix (`a33860e…`) than Phase B's original UI snapshot (`e0d4ff8…`); verify the latest upstream heads before choosing a base.
- Protect running production services and other engineers' work. Use an independent, non-production feature branch and worktree, do not change production and do not force-push anyone else's branch.

## B. Design references — strict priority order

**1. Atelier V5 is the PRIMARY DESIGN AUTHORITY.**
Open `prototypes/v5/index.html` and `prototypes/v5/DATA-ATLAS-NOTES.md`. Study the complete design—not just the first screen: layout rhythm, editorial headings, calibrated information density, neutral cream/ink and rust/sage/blue accents, both themes, navigation, restrained transitions and the V5 Traffic Atlas visual vocabulary. Keep this distinct visual identity and elevate V6 with real product features. **Never edit or reformat V5.** Its frozen SHA-256 is `04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf`.

**2. Guokao 2027 Observatory is the INTERACTION REFERENCE.**
Open `docs/frontend/references/guokao-2027-observatory-interactive.html`, a copy of the user-provided `2027国考_招录观测台_交互修复完整版.html`. Study synchronized multi-view data exploration: hierarchical sunburst, treemap, dot matrix, proportion/donut, ranking, Pareto and scatter views; shared metric/group filters; hover preview vs click-to-pin; drill-down breadcrumbs, comparisons, legends, contextual tooltips, keyboard access and credible exports. Translate useful mechanics to real LLM workload, Endpoint, request, token, latency and model-usage data. **Never copy its recruiting records or claim that its 2027 figures are officially verified**; do not transplant its brand, text or arbitrary JS into production.

**3. Atelier V3 is the TYPOGRAPHY / READABILITY REFERENCE.**
Open `docs/frontend/references/atelier-v3-readability-reference.html`. Use its readability lessons: consistent sans-serif hierarchy, around 15px body copy and 13px tables, contrast, comfortable line-height, useful whitespace and fewer competing ornamental labels. V3 is *not* an authentication or backend source. V5 wins if style choices conflict.

**4. Historical V6 screenshots are the BEFORE STATE, not the desired authority.**
Open `docs/frontend/SCREENSHOT_GALLERY.md` and compare the eight main-route previews (plus overview light/dark). They date from 2026-10-15, can show synthetic placeholders, and omit Compute. Never present these pictures as current live production evidence.

## C. Product and UX goals

The app has **eight top-level views plus a real Compute subpage**:

1. **Overview:** useful host and service health, only authorized models/Endpoints and personal usage, meaningful time range, alerts, source and timestamp. No constant fake throughput curve or prewritten “running model.”
2. **Compute:** GPU/VRAM/CPU/RAM/disk, temperature/power, engine presence, recency, service status and safe capacity; do not infer schedulable GPU space from 0% utilization.
3. **Models & Chat:** searchable Engine/Model/Deployment/Endpoint cards and tables, ownership, status and detail drawers, visibility/sharing, revisions. Enable real streaming chat, cancellation and errors only when a verified same-origin Gateway route exists.
4. **API Management:** real Endpoint route/alias and SDK examples, key list/mask/one-time issuance/revoke/rotate, scopes/expiry and Playground. Never enable a dead button; if no reviewed backend route exists, show exactly what is missing and keep the write action disabled.
5. **My Calls:** own authorized request metadata, time/endpoint/model/status filters, latency/tokens/request ID, keyset pagination, appropriate export and good empty/error states. No prompt/completion content.
6. **Observability:** separate host, Engine, Gateway and per-principal telemetry. Chart exact units, sampling time, source, history, staleness and errors. Add a safe Grafana entry only when configured.
7. **Traffic Atlas:** make V5's excellent visuals into an operable authorized data exploration workspace: linked views, filter/group/metric selectors, hover/pin, compare, drilldown, correct empty states, trustworthy CSV/JSON. Do not fake heatmap cells as zeros.
8. **Evaluation:** visibly separate EvalScope reports from legacy Benchmark artifacts, measured vs missing dimensions, authorized report list/details; submission disabled until real runner/GPU admission has been independently verified.
9. **Administration:** real subject/session/scopes, approved shared resources, temporary admin-mode state, ACL audit and integration/settings state; admin mode is never root.

For **every view**, design and test: Loading, Ready, Authorized Empty, Not Configured, Denied, Stale, Error/Retry, and Explicit Demo. A useful empty state should explain what is missing and the permitted next action—not become a large wall of decorative UNKNOWN cards. Responsive grids, keyboard focus, light/dark contrast and visible active/disabled states are required. Prefer functional tables, filters, detail panels, clear actions and sound information architecture over gratuitous decoration.

## D. Non-negotiable architecture and security constraints

- **Default page mode is genuine LIVE-or-UNKNOWN, not demo.** No fabricated data, usage totals, model health or evaluation scores on the normal URL. An explicitly selected `?demo=1` can use clearly labeled SYNTHETIC fixtures for complete UI/interaction QA.
- UI never determines UID, owner, role, permissions, metric source or current readiness from an untrusted header, query parameter or synthetic fixture. Authorization lives in the BFF/Registry/Gateway and must be revalidated on every call.
- Keep `Registered != Verified != Ready != Router ACK Applied != Callable` distinct. Default Endpoint visibility is private; revocation must be reflected on the next access.
- Missing, future-dated, stale or contradictory samples must fail closed to UNKNOWN; a missing value is `null`, never measured zero.
- No browser-to-`:8080`/`:8200`/`:9000` direct fetches, no bypass of Host/Origin/CSRF checks, no legacy anonymous report access. Do not store or commit actual credentials, API keys, user content, raw logs or private reports.
- Do **not** modify PAM, systemd, Caddy, Strata, Gateway runtime, other users' homes, GPU jobs, existing `:8443` preview, or the legacy LAN Portal. Do not install or restart production services.

## E. Implementation and design-source discipline

1. First inspect the selected branch, current frontend DOM and modules, source/data contracts, open PRs and existing tests. Produce a concise nine-page **gap audit**, identify P0/P1 issues, and plan implementation slices.
2. Preserve the V5-derived structure. Work in `prototypes/v6/additions.html`, `additions.css`, `web/js/**` and frontend tests; never manually edit generated `web/index.html` or `web/css/*.css`. Regenerate using `prototypes/v6/derive-from-v5.mjs` and `web/tools/extract-from-prototype.mjs`, with their checks passing.
3. Solve critical false-LIVE, broken interaction and route/state handling first; then implement more complete Models/API/Calls/Atlas workflows and improve other pages. Do not use “ready: true” to make unimplemented actions clickable.
4. Integrate **existing reviewed** BFF contracts only. For backend dependencies that are missing, provide a precise disabled / unconfigured UI and document the required endpoint in the PR. Do not edit another lane's backend worktree.
5. Keep the actual V6 design source hash independently pinned in all appropriate verification/release files. When the reviewed V6 source changes, explicitly update the independently reviewed pin without weakening tests. Leave `prototypes/v5` byte-identical.
6. Run `npm test`, derivation/extraction checks and focused state/security regressions; preserve existing test coverage. If feasible run stdlib Python contract tests on the candidate branch without GPU.
7. Use an actual Chrome/Chromium browser when available to capture PNGs in `docs/frontend/screenshots/qa/<commit-sha>/` for all nine views at **1440px desktop + 390px mobile**; verify 320, 768 and 1024 widths and light/dark themes. Document commit SHA, URL, viewport, theme, source mode and time. If browser unavailable, **mark skipped**—never fabricate screenshots or call E2E green. Update the screenshot gallery so old and new captures are visibly distinct.
8. Review every visible clickable control, including retry, selectors, exports, modal/drawer close, forms, navigation, compare/reset, keyboard and mobile interactions. No dead controls and no default synthetic series.
9. Commit and push **all completed source, tests, docs and authentic screenshots to GitHub** on the isolated frontend branch. Open a **Draft PR** against the correct latest integration branch; verify remote HEAD SHA; list exactly what works, what is simulated/disabled, screenshots, test counts, failures, unresolved backend dependencies and the explicit NOT DEPLOYED / NO-GO state. Link [Issue #56](https://github.com/ProfessorZhi/llm-lab-portal/issues/56).

## F. Definition of done for this frontend iteration

A reviewer can open the nine views at desktop and mobile size, recognize the **Atelier V5 visual identity**, interact meaningfully with the controls, distinguish trusted facts from examples, inspect responsive screenshots and keyboard behavior, and trace every enabled business action to a real reviewed backend path. Any incomplete feature is genuinely disabled with a clear reason. The branch, PR, test evidence and image captures are present on GitHub.

**Start by reading the references and writing the audit. Then implement, test and push. Do not stop at a design plan, and do not claim deployment or production integration.**
