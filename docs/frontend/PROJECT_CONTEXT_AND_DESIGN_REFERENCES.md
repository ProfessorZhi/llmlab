# Atelier V6 · Frontend redesign handoff / project background

**Prepared for:** Claude Code working on the frontend, not the production machine.  
**Date:** 2026-10-16. **Repository:** `ProfessorZhi/llm-lab-portal` (private); the older `ProfessorZhi/llm-lab` is a separate active codebase.  
**Read first:** [Architecture](../architecture.md) · [Nine-page acceptance #56](https://github.com/ProfessorZhi/llm-lab-portal/issues/56) · [Design-reference hierarchy](references/README.md) · [Screenshot gallery](SCREENSHOT_GALLERY.md).

## 1. Why the project exists

A shared Dell Linux workstation with an NVIDIA RTX A6000 (48 GB) is used as an LLM laboratory. Several Linux accounts need independent identity and model-resource ownership while sharing selected inference endpoints. Existing components include Strata/Qwen inference, a Gateway, Prometheus/Grafana, EvalScope and the legacy LAN Portal; the V6 web console should unify them without replacing their system-level ownership or granting root-like control through a browser.

This is **a single-node, multi-user LLM Laboratory Control Plane**, not a GPU cluster scheduler, a generic SaaS dashboard, or an image of a dashboard. A successful product lets each approved Linux user log in, see their own and explicitly shared resources, manage and invoke permitted endpoints, inspect their own usage and authorized evaluations, and trust every rendered source/permission/readiness state. An administrator's visibility is separate from system-root privileges.

The two user interfaces are:
- **Web:** Atelier V6, 8 primary pages plus the Compute subpage, all under a single protected origin and Python BFF.
- **CLI:** `llmlab`, with scope-bound credentials issued server-side, the same Registry and access rules.

The **essential product journey** is: A owns a model service → verifies it → creates a private Endpoint → shares it with B → B invokes it via its own credentials → B's request appears in attributable call metadata → A revokes access → B's next invocation fails → every operation is auditable.

## 2. Actual status: distinguish product vision from running code

| Layer | Code / evidence today | Still NOT done |
| --- | --- | --- |
| Visual shell | Atelier V5-derived V6 static shell, nine page routes and partial operations modules, real host-only readonly telemetry in prior preview | Business depth is incomplete; many workspaces remain gated/not configured |
| Frontend safety | [UI Draft PR #54](https://github.com/ProfessorZhi/llm-lab-portal/pull/54), newer SHA `a33860e` has host source/freshness/timeouts corrections | No verified full Chrome matrix; latest fix is newer than the Phase B candidate snapshot |
| Identity | Linux PAM broker contract and [scoped Token PR #52](https://github.com/ProfessorZhi/llm-lab-portal/pull/52) | Actual host PAM Broker, real Linux user logins, final Web/CLI integration |
| Registry / CLI | [PR #58](https://github.com/ProfessorZhi/llm-lab-portal/pull/58), per-Owner ACL, endpoint/share/revoke/repoint/ACK, CPU loopback tests | Production Gateway route and real key/ACL E2E |
| BFF | [PR #57](https://github.com/ProfessorZhi/llm-lab-portal/pull/57) provides Host/Origin/source guards + authenticated route map | Some same-origin data endpoints not wired: `/api/v2/registry/*`, `/api/atlas/calls`, `/api/atlas/modules`, and BFF `/api/infra/summary` |
| Data and evaluation | [Data PR #53](https://github.com/ProfessorZhi/llm-lab-portal/pull/53) + [Evaluation PR #55](https://github.com/ProfessorZhi/llm-lab-portal/pull/55) | Trusted real Gateway usage; real EvalScope and legacy report ownership/contents |
| Phase B integration | [Draft PR #60](https://github.com/ProfessorZhi/llm-lab-portal/pull/60) at `c941cd3` merged seven lane commits in an isolated candidate; local Python 3320 tests (6 skip) and Node 470 pass (1 skip) | It is not deployed or merged; current #54 latest UI patch must be re-integrated/re-tested |
| CI / browser / production | Release checklist and fallback CPU-only tests exist | GitHub Actions runners currently fail at 0 steps; Chrome 9-page × width/theme QA not completed; real PAM/Gateway/production E2E unverified |

**Do not assume a PR or a successful mock test makes a feature live.** Existing `:8443` preview may use a shared Basic Auth wall; that is not a Linux per-user Session. Preserve running Strata/Qwen, Gateway, Caddy/systemd and the legacy Portal.

## 3. Visual identity, exact references and priority

**The primary design authority is Atelier V5**: [`prototypes/v5/index.html`](../../prototypes/v5/index.html), SHA-256 `04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf`, with [V5 notes](../../prototypes/v5/DATA-ATLAS-NOTES.md). Keep the restrained editorial technical character: light cream/off-white and dark surfaces; strong ink typography; rust, sage and blue measured accents; data-dense yet airy sections; disciplined baselines, hierarchies and chart legibility; small but purposeful interaction transitions. Preserve both light and dark modes. **Never modify the V5 source.**

**Secondary reference: [2027 国考招录观测台 interactive HTML](references/guokao-2027-observatory-interactive.html).** This is a user-provided, self-contained, offline visualization prototype. Transfer only the UX mechanisms—linked sunburst, treemap, dot matrix, ranking, proportions, scatter/Pareto, hover-preview / click-pinning, shared filters, comparisons, keyboard focus, provenance captions and exports—to relevant LLM-usage semantics. The recruiting numbers are a session snapshot not verified as official and must NEVER become LLM Lab data or be described as official.

**Tertiary reference: [Atelier V3 readability HTML](references/atelier-v3-readability-reference.html).** Use lessons around consistent sans typography, readable body ≈15px, tables ≈13px, balanced grouping/whitespace, appropriate contrast, not excessive serif/italic layers or decorative meta labels. V3's old login concepts, including Feishu, are superseded by Linux authentication. If V3 and V5 disagree, V5 wins.

**Current visual evidence:** [Screenshot gallery](SCREENSHOT_GALLERY.md) contains **nine authentic historical 2026-10-15 V6 PNG captures** from branch `preview/atelier-v6-captures-20261015`. They document the shell's earlier state, not current runtime truth. Compute is not among those images. Their existence doesn't waive the need to capture new screenshots from the implementation.

## 4. Nine product workspaces and what to make complete

| Workspace | Mandatory content / interactions |
| --- | --- |
| Overview | host and service health, authorized models/endpoints, personal usage, real time range/source, no fake numbers or fixed running-model story |
| Compute (subpage) | GPU/VRAM/CPU/RAM/disks, temperature/power, sample_at, staleness, engine usage and safe capacity explanations; no automatic scheduler from `gpu_util=0` |
| Models & Chat | real Engine/Model/Deployment/Endpoint status, search/detail, Owner/visibility, sharing/readiness history, streaming chat with cancellation only when backend contract is genuinely verified |
| API Management | Endpoint alias, scope, key issue/one-time reveal/mask/rotate/revoke, real URL and curl/Python snippet, safe Playground with proven CSRF/timeout; otherwise informative disabled state |
| My Calls | exactly self-scoped metadata (time, request_id, model, endpoint, status, tokens, latency), filters/keyset pagination, privacy-aware export; no prompt/completion bodies |
| Observability | host-level vs Gateway-level vs personal usage separated, units/source/sample age, historical charts, explicit Unknown and authorized Grafana entry |
| Traffic Atlas | real ACL-filtered aggregation feeding V5-inspired linked charts, search, compare, drilldowns and correctly scoped CSV/JSON export; mock only with explicit Demo mode |
| Evaluation | EvalScope vs legacy Benchmark separated, owned/authorized report directory, real measured vs missing dimensions, task state; no actual GPU dispatch until policy/runner verified |
| Administration | real identity/session/scope, limited admin mode, projects/share audit, integrations/settings provenance, never mistaken for Linux root control |

**Design responsibility:** preserve V5 refinement while increasing functionality, not simply inserting more bordered cards. Make actions visible and understandable; show useful tables, drilldown/detail drawers, filtered states and compact actionable empty states. Improve information density without shrinking labels to unreadable sizes.

## 5. Runtime and security invariants

1. Default `LIVE` means **no synthetic data**, not that every source has a fresh verified measurement. A missing source produces a meaningful Not Configured/Unknown state, **never a made-up zero or sample plot**.
2. Offline `?demo=1` is opt-in, unmistakably marked `SYNTHETIC / DEMO` and must not send real model calls. It should exercise real UI interactions with synthetic fixtures.
3. Do not expose/guess Linux usernames, UID, Owner, Role or source from user-controlled request values. Backend ACL is authoritative.
4. `Registered ≠ Verified ≠ Ready ≠ Router Applied ≠ Authorized To Invoke`. Endpoint share, revoke and repoint need accurate ownership and route state.
5. Per-user calls/Atlas/evaluation require valid principal-bound authorization. Unattributed data is not public; no sensitive request/response bodies or real tokens/keys in commits or screenshots.
6. All browser requests remain same-origin; no raw upstream `:8080`/`:8200`/`:9000` APIs or old anonymous report endpoints. Do not relax Host/Origin/CSRF checks or default disabled write controls.
7. New UI actions must use real reviewed existing contracts or remain disabled with a precise precondition. Do not manufacture backend responses, fake source headers, or “ready” flags to satisfy a screenshot.
8. No production PAM, Caddy, systemd, Strata, Gateway, GPU model, another user's files, or old LAN Portal changes in a frontend PR.

## 6. Frontend source-of-truth and file ownership

```text
prototypes/v5/index.html       # immutable V5 design authority
  + prototypes/v6/additions.{html,css}
    -> prototypes/v6/derive-from-v5.mjs
       -> prototypes/v6/index.html
          -> web/tools/extract-from-prototype.mjs
             -> web/index.html and web/css/*.css
web/js/**                      # runtime UI/controllers/data adapters
tests/web-features/**          # logic, states, flows
tests/browser/**               # real screenshot/browser regression
```

Do not hand-edit generated HTML/CSS. A V6 change must preserve V5's exact SHA and update/revalidate the *independent reviewed* V6 derivation digest (release pin), without weakening the check or auto-self-pinning it. [Phase B #60](https://github.com/ProfessorZhi/llm-lab-portal/pull/60) is a review candidate, not a production checkout; use a **new, isolated feature branch/worktree**, not someone else's active worktree.

## 7. Deliverables & QA

- Produce a **page-by-page audit against the above 9 workspaces** and refs, identifying the exact gap, fix and state transition. Prioritize default-LIVE integrity, Models/API/Calls and Atlas before cosmetic extras.
- Implement real V6 UX in isolated frontend-owned paths; compose from existing components and contracts, avoid introducing an unrelated design system.
- Run `npm test`, derivation check and extraction check. Where possible run existing browser tests and add interaction tests. Keep breakages visible rather than removing assertions.
- Capture **actual browser PNGs**: all 9 routes in 1440px desktop and 390px mobile, light/dark for key views, plus 320/768/1024 responsive checks, with documented URL, source mode and exact SHA. If Chrome unavailable, mark browser QA `SKIPPED` and do not call it passed.
- Write/update an accessible screenshot gallery and reference-to-implementation map; do not present historical screenshots as current.
- **Push to GitHub**, verify remote HEAD, create a **Draft PR** against the appropriate integration candidate, document tests/failed gates/remaining blockers; never merge to main or deploy. Cross-lane backend needs should become explicitly linked issues, not unsanctioned changes.

## 8. Pointers

[Architecture and release states](../architecture.md) · [Atelier Phase B PR #60](https://github.com/ProfessorZhi/llm-lab-portal/pull/60) · [UI PR #54](https://github.com/ProfessorZhi/llm-lab-portal/pull/54) · [Issue #56](https://github.com/ProfessorZhi/llm-lab-portal/issues/56) · [Issue #43](https://github.com/ProfessorZhi/llm-lab-portal/issues/43) · [Reference README](references/README.md) · [Claude prompt](CLAUDE_FRONTEND_IMPLEMENTATION_PROMPT.md).
