# Atelier V6.1 — nine-page frontend audit and implementation record

**Date:** 2026-10-16 · **Branch:** `claude/marketops-architecture-audit-u71oo7` · **Status:** NOT DEPLOYED / NO-GO (Draft PR only)

**Base:** `docs/v6-product-system-architecture-20261016` @ `1ff49e4` (the Phase B candidate `c941cd3` plus the frontend handoff docs), merged with the latest UI lane head, [PR #54](https://github.com/ProfessorZhi/llm-lab-portal/pull/54) @ `a33860e` (host freshness fixes). The merge was clean. Issue: [#56](https://github.com/ProfessorZhi/llm-lab-portal/issues/56).

Design authority: `prototypes/v5/index.html` (SHA-256 `04003f5c…`, **unchanged**). Interaction reference: Guokao 2027 observatory HTML. Readability reference: Atelier V3 HTML.

---

## 1. Audit — before this branch

These findings come from real Chromium captures of the merged base (`?demo=1` and bare URL, 1440 px), the V5 prototype and the historical gallery.

| # | View | What we found | Severity |
|---|------|---------------|----------|
| 1 | **My Calls** | Even with `?demo=1`, the view showed `UNKNOWN · 暂时无法确定` because the controller always fetched `/api/atlas/calls`, and the preview server returns 404. The filters, pager and table could not be exercised at all. There was no LOADING state: the first paint said "unknown" while the request was still in flight. A 404 (route not deployed) looked the same as a broken payload. The failure reason was dropped, because `renderNotice(state, log?.reason)` received a null log. There were no latency percentiles, no row detail and no export. The filter bar was one overflowing flex row. | **P0** |
| 2 | Overview | Honest but fragmented. Six separate "待接线" notices and no single answer to "what is actually connected right now?". | P1 |
| 3 | API Management | No view of the reviewed IAM token contract (`GET /auth/tokens`, PR #52). The page's only "how to call" material was a single `lab.example` curl. No link between the UI and the reviewed `llmlab` CLI that executes the endpoint journey. | P1 |
| 4 | Administration | No statement of which same-origin routes exist, who owns them, or which routes the UI depends on that the BFF does not declare (`/api/infra/summary`, `/api/atlas/calls`, `/api/atlas/modules`). | P1 |
| 5 | Models / Evaluation module panels | A 404 from `/api/atlas/modules` rendered as the generic "暂时无法确定" (unknown), with the reason dropped. | P1 |
| 6 | Readability | Tables at 11 px, notice copy at about 12 px, footnotes at 11 px. Below the V3 guidance of about 13 px for tables and about 15 px for body text. | P2 |
| 7 | Browser QA | `npm run test:browser` could not launch Chromium as root (no `--no-sandbox`), and the harness did not know the pre-installed Playwright Chromium path. So the browser suite had silently never run in this environment. | P1 (QA) |
| 8 | Traffic Atlas | In `?demo=1` mode it already implements most Guokao mechanics: linked sunburst / treemap / dot matrix, donut, ranking with search and sort, hover preview vs click-to-pin, breadcrumb drill-down, a compare basket (max 3), Pareto, scatter, 7×24 heatmap, keyboard (`1–3`, `Esc`, `/`) and CSV/JSON export. In live mode the whole body is correctly gated. The real gap is a backend one: there is no authorized aggregation route. | P2 (backend) |
| 9 | Compute, Observability | Already honest: verified-host-only numbers, with freshness / future-timestamp / hung-request protection from #54. No change needed in this slice. | — |

## 2. What this branch implements

### 2.1 My Calls workspace (P0)
- **`?demo=1` works end to end.** `web/js/calls/demo-fixture.js` is a `fetch`-shaped, in-process SYNTHETIC answerer for the real `atlas-calls/1` contract. It parses the same query string the controller sends: window, outcome, key, model, limit and keyset cursor. A key outside the caller's scope is refused with 403, not filtered to empty. Every name carries a `demo-` prefix. `transport.mode = demo`, `gateway_attached = false` and `content.included = false`.
- **States:** `LOADING` (asked, not answered), `NOT_CONFIGURED` (404/501: the route is not deployed), `READY`, `EMPTY`, `SIGNED_OUT`, `DENIED`, `UNKNOWN`. The failure reason is now always shown (for example `http-404`).
- **Insight band** (`web/js/calls/insights.js`): outcome mix, success rate, total-time p50/p95, TTFT p50 and a per-model breakdown. Percentiles use nearest rank over individual rows. A missing value is excluded and counted, never treated as 0. All of it is scoped to "loaded rows", and the page says so.
- **Row detail drawer:** each row is a focusable control (click, Enter or Space) that opens the shell's existing dialog. It shows metadata only and is flagged SYNTHETIC for demo rows.
- **Export:** CSV and JSON of the loaded rows only. Columns are a fixed metadata list (no content, no credentials), cells are protected against formula injection, and the file name carries `DEMO` or `live`. Both buttons are disabled with a reason when no rows are loaded.
- **Filter bar:** quick windows (1 h / 24 h / 7 d), three labelled rows that wrap, 13 px controls, and a hero badge that names the source (`DEMO · 合成数据`, `未接线 · 路由不存在`, …).

### 2.2 Overview — workspace readiness board (P1)
`web/js/workspace/readiness.js`: nine rows (8 tabs + Compute), each naming its same-origin route and its **current** state, read back from the controllers that own it (no extra requests). The vocabulary is READY · LOADING · EMPTY · NOT CONFIGURED · DENIED · SIGNED OUT · STALE · ERROR · DEMO. DEMO is never READY, and stale host data is STALE. Each row has a "前往" (go to) link.

### 2.3 API Management (P1)
- **My Scoped Tokens** (`web/js/workspace/cli-access.js`): a read-only `GET /auth/tokens` list (IAM #52 `tokens_list`). A response carrying `token`, `secret`, `plaintext`, `value` or `key` is rejected outright. Lifecycle per record: active / expired / revoked / rotated. **Issue, rotate and revoke are rendered disabled** with the exact reason: the backend routes exist, but the browser CSRF + PAM session + one-time-reveal flow has not been verified end to end.
- **Endpoint lifecycle and access** (`web/js/workspace/lifecycle.js`): register deployment → register endpoint (private) → share → invoke → revoke → repoint/rollback. Each step shows the exact `llmlab` CLI command (the test checks each subcommand exists in `cli/llmlab/entry.py`) and whether a web route exists. Only `ops.share` has one. **SDK tabs** (curl / Python / JavaScript / llmlab CLI) with roving-tabindex arrow-key navigation, a live alias field (sanitised), placeholder-only `$LLMLAB_GATEWAY_URL` / `$LLMLAB_KEY`, and copy to clipboard with an honest dialog.

### 2.4 Administration — integration and route matrix (P1)
`web/js/workspace/routes.js` mirrors `backend/bff/route_map.py::ROUTES`: method, path, anonymous vs session, read vs write (CSRF), primary scope, owning lane and where the UI uses it. A unit test parses the Python file and fails on drift. A second table lists what the UI depends on that the BFF does **not** declare: the production wiring checklist.

### 2.5 Module catalogue states
A `/api/atlas/modules` 404/501 is now `NOT_CONFIGURED` and the reason is preserved.

### 2.6 Readability (V3 refinement, scoped)
Tables at 13 px with 1.55 line-height, notice copy at 13.5 px / 1.7, footnotes at 12.5 px. Headings, KPIs and charts keep their V5 proportions. No new colours: every value uses V5 tokens, so both themes work.

### 2.7 QA infrastructure
`tests/browser/harness.mjs` adds `--no-sandbox` **only when running as uid 0**, and the Playwright Chromium path as a candidate. Loopback-only host resolution is unchanged. With this, the browser suite actually runs here.

## 2.8 V6.2 — Apple design layer (product-owner decision)

After reviewing V6.1, the product owner asked for a full Apple-application look and explicitly chose it over keeping the V5 identity. The change is one CSS layer at the end of `prototypes/v6/additions.css`:

- **Tokens, both appearances.** `#f5f5f7` / `#000` grouped backgrounds, white / `#1c1c1e` cards, `#1d1d1f` ink, system blue `#0071e3` / `#0a84ff` accent, system green / orange / purple chart series, and an SF Pro / SF Mono font stack with PingFang SC for Chinese.
- **Chrome.** One frosted, translucent header (`backdrop-filter: saturate(180%) blur(20px)`). The institutional strip is removed. The brand mark is a gradient app icon, and navigation uses pill tabs.
- **Controls.** Pill buttons (filled blue primary, gray fill secondary), Apple-style segmented controls, filled input fields with a blue focus ring, and tinted status capsules.
- **Surfaces.** Borderless 18 px-radius cards with soft shadows in light mode and flat elevated surfaces in dark mode. Dialogs are rounded sheets over a blurred backdrop.
- **What does not change.** Markup, data honesty, disabled-with-reason controls, and the Atlas chart geometry, which a browser test checks against V5. Only the colours change.

The SF Pro fonts are not installed in the Linux capture container, so the screenshots render with the fallback sans-serif. On macOS and iOS the system font is used.

## 3. Source chain and pins

`prototypes/v5/index.html` stays byte-identical (`04003f5c…`). Changes went into `prototypes/v6/additions.{html,css}`. `derive-from-v5.mjs` regenerated `prototypes/v6/index.html`, and `extract-from-prototype.mjs` regenerated `web/index.html` + `web/css/*.css`. The **reviewed V6 digest** was re-pinned to
`bc14570234c846a7323ee776bce3bbe4efa312481a992c9e484465ca8d416ed5` in `web/tools/extract-from-prototype.mjs`, `.github/workflows/atelier-v6-release-gates.yml`, `tests/release/test_release_gates.py`, `web/README.md`, `docs/v6-ui-flows.md` and the hosting/rollback doc. Historical Phase B records still name `883a16c8…` and are left as history.

## 4. Remaining backend dependencies (not done here, by design)

| Need | Unblocks | Owner |
|------|----------|-------|
| BFF route for `/api/atlas/calls` (principal-bound) | My Calls live, Atlas live aggregation | Data #47 / Integration #49 |
| BFF route for `/api/atlas/modules` | module catalogue panels | Integration #49 |
| BFF `/api/infra/summary` (today only `web/tools/serve.mjs`) | Overview / Compute host telemetry in production | Data #47 |
| Verified browser token-write flow (CSRF + PAM session + one-time reveal) | Enabling issue / rotate / revoke on the API page | IAM #45 |
| Same-origin Gateway invoke contract (CSRF, cancel, timeout, receipt) | Playground send and streaming chat | Gateway / Integration |
| Real Linux PAM broker | Any session-bound state other than SIGNED OUT | IAM |
| EvalScope runner and admission verification | Evaluation submit | Evaluation #48 |

## 5. Verification (this branch)

Run in a Linux cloud container with Node 22.22 and Python 3.13. Screenshots were captured from source commit `2cd10ad` (Apple design layer). The earlier V6.1 set from `d169c1e` remains in git history.

| Check | Result |
|-------|--------|
| `npm test` (unit / contract, includes 24 new tests in `tests/web-features/workspace.test.js`) | **522 tests: 521 pass, 1 skipped, 0 fail** |
| `npm run verify:extract` and `node prototypes/v6/derive-from-v5.mjs --check` | ok. V5 `04003f5c…` unchanged, V6 `bc145702…` |
| `npm run test:browser` (real Chromium 141 over CDP, includes 5 new tests in `tests/browser/workspace.browser.test.js`) | **64 tests: 61 pass, 3 fail.** The same 3 (`compute-hero` #1 and #2: a verified host summary drives the first-screen cells and the live narrative; `portal` #53: dark-palette repaint, intermittent) also fail on the unmodified base `9a4d2ba` (56/59 there). Before this branch the suite could not launch Chromium in this environment at all. |
| `python3 -m unittest discover -s tests` | **3320 tests, 6 skipped.** Three groups did not pass, and all of them fail identically on the unmodified base. 4 errors: the host identity table expects Linux users (`alice`, `bob`, `carol`, `dave`) that do not exist in this container. 1 error: the BFF dev-server broker bind-guard test, which is environment-specific. 1 failure: `ProjectionConstantParityTest`, a parity break between UI #54's `host-status.js` and the Data lane's projection constants, inherited from the merge. The 2 release-gate failures in that run came from the run overlapping the pin update; `tests/release` passes 53/53 on HEAD. |
| Real-browser screenshots | 45 PNGs (9 views × {1440 light live, 1440 light demo, 1440 dark demo, 390 light demo, 390 dark live}). 0 capture errors. 0 horizontal overflows across 54 measurements (9 views × 2 modes × 320/768/1024). See [QA_SCREENSHOTS.md](QA_SCREENSHOTS.md). |
