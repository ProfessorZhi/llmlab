# Atelier V6 frontend handoff · index

> **Design direction update — 2026-10-16 (V6.2).** The product owner asked for the console to look like a first-party **Apple application**: SF Pro type, system grays and accents, frosted translucent chrome, borderless cards, pill buttons and segmented controls, in both light and dark. This **replaces the V5 visual identity** as the governing look. It is implemented as a single CSS layer at the end of `prototypes/v6/additions.css` ("Apple design layer"). V5 itself is still **frozen and byte-identical** (`04003f5c…`). It remains the source of the page structure, the Atlas chart grammar and the interaction model, and deleting that layer restores the V5 look exactly. Data-honesty, security and source-chain rules are unchanged.

This directory is the **frontend-design and implementation handoff** for Claude Code. It is a review/documentation artifact, **not** proof that the V6 product is deployed.

**Production API contract-first update (2026-10-17):** Start from the [verified-source and API catalogue](../integrations/README.md). It defines real source readiness, the required nine-page data, explicit unavailable states, per-Linux-user temporary admin policy choices, and the [offline no-fabrication handoff](../integrations/CLAUDE_OFFLINE_IMPLEMENTATION_HANDOFF.md). This supersedes any earlier suggestion to populate production dashboards with synthetic sample model instances or traffic.

## Start here

1. **[Copy/paste Claude Code implementation prompt (English)](CLAUDE_FRONTEND_IMPLEMENTATION_PROMPT.md)** — fully scoped source, product, design references, QA and GitHub delivery requirements.
2. **[Project background + architecture + nine-page requirements](PROJECT_CONTEXT_AND_DESIGN_REFERENCES.md)** — why the platform exists, what is implemented vs still unwired, and what not to touch.
3. **[Historical frontend screenshots](SCREENSHOT_GALLERY.md)** — nine archived V6 preview PNGs, explicitly dated/provenanced; screenshots of newly implemented views must be captured separately.
4. **[Design reference hierarchy](references/README.md)** — V5 primary, Guokao 2027 interaction inspiration, V3 typography refinement.
5. **[V6.1 audit and implementation record](V6_1_FRONTEND_AUDIT_AND_IMPLEMENTATION.md)** — the nine-page gap audit, what was built and the remaining backend dependencies.
6. **[V6.1 real-browser QA captures](QA_SCREENSHOTS.md)** — 45 Chromium screenshots (1440/390, light/dark, live/demo) with commit SHA and manifest.

## Primary design source

- **Atelier V5 (immutable)**: [`prototypes/v5/index.html`](../../prototypes/v5/index.html) and [V5 Atlas notes](../../prototypes/v5/DATA-ATLAS-NOTES.md). **Preserve SHA-256 `04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf`.**
- Secondary: [2027 国考 Observatory offline interactive HTML](references/guokao-2027-observatory-interactive.html), for linked-chart interaction patterns, not for borrowing its dataset or political/recruitment content.
- Tertiary: [Atelier V3 readability HTML](references/atelier-v3-readability-reference.html), for typography and readability only.
- Actual current V6 implementation remains versioned in `prototypes/v6/**`, `web/js/**` and the integration PR. The design reference files are **not runtime dependencies**.

## Data and safety

- Default V6 is honest LIVE-or-UNKNOWN. No authored demo data on the plain URL.
- Demo data is only under `?demo=1`, with `SYNTHETIC` visibly labeled.
- Missing Auth/PAM, Gateway, Calls, EvalScope or BFF routes must stay clearly disabled/unconfigured.
- New work must use an isolated branch, preserve V5, keep CPU-only tests honest and be uploaded to GitHub in an independently reviewable **Draft PR**.

Related: [full architecture](../architecture.md) · [nine-page DoD #56](https://github.com/ProfessorZhi/llm-lab-portal/issues/56) · [Phase B PR #60](https://github.com/ProfessorZhi/llm-lab-portal/pull/60).
