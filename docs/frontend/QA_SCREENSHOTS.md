# Atelier V6.2 — real-browser QA captures (Apple design layer)

> **Look:** these captures show the **Apple design layer** (V6.2), which the product owner chose to replace the V5 visual identity: SF type stack, frosted header, pill controls, borderless cards, system blue accent, light and dark. SF Pro is not installed in the Linux capture container, so text renders in the fallback sans-serif; on macOS and iOS the system font is used. The previous V6.1 set (V5 look, source commit `d169c1ede2c10228b44c3225f03d1f320d40135c`) is still in git history at that commit.

> **Current branch evidence, not production.** Captured by a real headless Chromium 141.0.7390.37 at `2026-10-16T15:29:27.951Z` from source commit **`2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9`** on branch `claude/marketops-architecture-audit-u71oo7`. The pages were served by `web/tools/serve.mjs` on loopback (`http://127.0.0.1:8123/index.html`), with no backend attached. **live** = bare URL: every unwired source shows its honest NOT CONFIGURED / UNKNOWN / ERROR state. **demo** = `?demo=1`: SYNTHETIC fixtures, visibly labelled. Script: [`screenshots/tools/capture-qa.cjs`](screenshots/tools/capture-qa.cjs). Per-image metadata (URL, viewport, theme, mode, scroll width): [`manifest.json`](screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/manifest.json).

For the earlier 2026-10-15 preview images, see the historical [SCREENSHOT_GALLERY.md](SCREENSHOT_GALLERY.md). Those are *before* evidence. The design authority remains the frozen [V5 prototype](../../prototypes/v5/index.html).

**Responsive check:** all 9 views were checked in both modes at 320, 768 and 1024 px. 54 measurements, **0 horizontal overflows**. The full-page captures below are 1440 px desktop and 390 px mobile.

## Overview

| 1440 · light · live | 1440 · light · demo | 1440 · dark · demo |
| --- | --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/overview-1440-light-live.png" width="300" alt="Overview, 1440px, light theme, live mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/overview-1440-light-demo.png" width="300" alt="Overview, 1440px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/overview-1440-dark-demo.png" width="300" alt="Overview, 1440px, dark theme, demo mode"> |

| 390 · light · demo | 390 · dark · live |
| --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/overview-390-light-demo.png" width="200" alt="Overview, 390px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/overview-390-dark-live.png" width="200" alt="Overview, 390px, dark theme, live mode"> |

## Compute

| 1440 · light · live | 1440 · light · demo | 1440 · dark · demo |
| --- | --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/compute-1440-light-live.png" width="300" alt="Compute, 1440px, light theme, live mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/compute-1440-light-demo.png" width="300" alt="Compute, 1440px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/compute-1440-dark-demo.png" width="300" alt="Compute, 1440px, dark theme, demo mode"> |

| 390 · light · demo | 390 · dark · live |
| --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/compute-390-light-demo.png" width="200" alt="Compute, 390px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/compute-390-dark-live.png" width="200" alt="Compute, 390px, dark theme, live mode"> |

## Models & Chat

| 1440 · light · live | 1440 · light · demo | 1440 · dark · demo |
| --- | --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/models-1440-light-live.png" width="300" alt="Models & Chat, 1440px, light theme, live mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/models-1440-light-demo.png" width="300" alt="Models & Chat, 1440px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/models-1440-dark-demo.png" width="300" alt="Models & Chat, 1440px, dark theme, demo mode"> |

| 390 · light · demo | 390 · dark · live |
| --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/models-390-light-demo.png" width="200" alt="Models & Chat, 390px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/models-390-dark-live.png" width="200" alt="Models & Chat, 390px, dark theme, live mode"> |

## API Management

| 1440 · light · live | 1440 · light · demo | 1440 · dark · demo |
| --- | --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/api-1440-light-live.png" width="300" alt="API Management, 1440px, light theme, live mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/api-1440-light-demo.png" width="300" alt="API Management, 1440px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/api-1440-dark-demo.png" width="300" alt="API Management, 1440px, dark theme, demo mode"> |

| 390 · light · demo | 390 · dark · live |
| --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/api-390-light-demo.png" width="200" alt="API Management, 390px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/api-390-dark-live.png" width="200" alt="API Management, 390px, dark theme, live mode"> |

## My Calls

| 1440 · light · live | 1440 · light · demo | 1440 · dark · demo |
| --- | --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/calls-1440-light-live.png" width="300" alt="My Calls, 1440px, light theme, live mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/calls-1440-light-demo.png" width="300" alt="My Calls, 1440px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/calls-1440-dark-demo.png" width="300" alt="My Calls, 1440px, dark theme, demo mode"> |

| 390 · light · demo | 390 · dark · live |
| --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/calls-390-light-demo.png" width="200" alt="My Calls, 390px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/calls-390-dark-live.png" width="200" alt="My Calls, 390px, dark theme, live mode"> |

## Observability

| 1440 · light · live | 1440 · light · demo | 1440 · dark · demo |
| --- | --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/observability-1440-light-live.png" width="300" alt="Observability, 1440px, light theme, live mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/observability-1440-light-demo.png" width="300" alt="Observability, 1440px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/observability-1440-dark-demo.png" width="300" alt="Observability, 1440px, dark theme, demo mode"> |

| 390 · light · demo | 390 · dark · live |
| --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/observability-390-light-demo.png" width="200" alt="Observability, 390px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/observability-390-dark-live.png" width="200" alt="Observability, 390px, dark theme, live mode"> |

## Traffic Atlas

| 1440 · light · live | 1440 · light · demo | 1440 · dark · demo |
| --- | --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/atlas-1440-light-live.png" width="300" alt="Traffic Atlas, 1440px, light theme, live mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/atlas-1440-light-demo.png" width="300" alt="Traffic Atlas, 1440px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/atlas-1440-dark-demo.png" width="300" alt="Traffic Atlas, 1440px, dark theme, demo mode"> |

| 390 · light · demo | 390 · dark · live |
| --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/atlas-390-light-demo.png" width="200" alt="Traffic Atlas, 390px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/atlas-390-dark-live.png" width="200" alt="Traffic Atlas, 390px, dark theme, live mode"> |

## Evaluation

| 1440 · light · live | 1440 · light · demo | 1440 · dark · demo |
| --- | --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/evaluation-1440-light-live.png" width="300" alt="Evaluation, 1440px, light theme, live mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/evaluation-1440-light-demo.png" width="300" alt="Evaluation, 1440px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/evaluation-1440-dark-demo.png" width="300" alt="Evaluation, 1440px, dark theme, demo mode"> |

| 390 · light · demo | 390 · dark · live |
| --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/evaluation-390-light-demo.png" width="200" alt="Evaluation, 390px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/evaluation-390-dark-live.png" width="200" alt="Evaluation, 390px, dark theme, live mode"> |

## Administration

| 1440 · light · live | 1440 · light · demo | 1440 · dark · demo |
| --- | --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/admin-1440-light-live.png" width="300" alt="Administration, 1440px, light theme, live mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/admin-1440-light-demo.png" width="300" alt="Administration, 1440px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/admin-1440-dark-demo.png" width="300" alt="Administration, 1440px, dark theme, demo mode"> |

| 390 · light · demo | 390 · dark · live |
| --- | --- |
| <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/admin-390-light-demo.png" width="200" alt="Administration, 390px, light theme, demo mode"> | <img src="screenshots/qa/2cd10adca1ebfd9216ebc4715f9d0dc1d22606e9/admin-390-dark-live.png" width="200" alt="Administration, 390px, dark theme, live mode"> |

## Comparison with the historical gallery and V5

| View | 2026-10-15 historical preview | This capture |
| --- | --- | --- |
| Overview | V5 hero plus authored sample KPIs | Readiness board (9 views × route × live state), verified-host panel, all sample regions gated in live mode |
| My Calls | Single-row filter bar; demo could not load (UNKNOWN) | Wrapped filters with presets, demo rows via the real contract, insight band, detail drawer, CSV/JSON export, NOT CONFIGURED for a missing route |
| API | V5 sample endpoint/key tables | Scoped-token list (read-only, writes disabled with reason), Endpoint lifecycle with `llmlab` commands, placeholder SDK tabs |
| Administration | V5 identity and trust copy | Plus the integration matrix that mirrors `backend/bff/route_map.py` and lists the routes the BFF does not yet declare |
| Atlas | V5 linked charts (synthetic) | Unchanged design; live mode gated; demo mode keeps the V5 visual vocabulary |

Visual identity: the V5 cream/ink editorial look has been replaced by the Apple design layer at the product owner's request. The V5 page structure, the Atlas chart geometry (checked by a browser test) and every data-honesty state are unchanged.
