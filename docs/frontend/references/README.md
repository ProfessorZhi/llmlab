# Frontend design-reference hierarchy

> **Design direction update — 2026-10-16 (V6.2).** The product owner asked for the console to look like a first-party **Apple application**: SF Pro type, system grays and accents, frosted translucent chrome, borderless cards, pill buttons and segmented controls, in both light and dark. This **replaces the V5 visual identity** as the governing look. It is implemented as a single CSS layer at the end of `prototypes/v6/additions.css` ("Apple design layer"). V5 itself is still **frozen and byte-identical** (`04003f5c…`). It remains the source of the page structure, the Atlas chart grammar and the interaction model, and deleting that layer restores the V5 look exactly. Data-honesty, security and source-chain rules are unchanged.

These files are **design-only examples** for the ongoing Atelier V6 frontend improvement. None of the reference pages is a production service, an authenticated API, or a trustworthy model/usage data source.

## Priority 1 — Atelier Observatory V5 (FROZEN, primary authority)

- **Canonical repo source:** [`prototypes/v5/index.html`](../../../prototypes/v5/index.html).
- [V5 data/interaction design notes](../../../prototypes/v5/DATA-ATLAS-NOTES.md).
- This **is** the governing visual identity: typography, measured spacing, data density, surfaces, color tokens, restrained motion, overview hierarchy, responsive navigation, the Traffic Atlas visual grammar and cross-chart behavior.
- **NEVER EDIT OR REFORMAT** `prototypes/v5/index.html`. Its expected SHA-256 is `04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf`.
- V6 is a derived product layer. Extend `prototypes/v6/additions.html` and `additions.css` only through the approved generation chain; generated `web/index.html` and CSS are not primary edit targets.
- The V5 prototype uses **offline authored samples**. Preserve its aesthetic, not its fake counts, labels, Feishu login, or pretended readiness.

## Priority 2 — 2027 National Civil Service Recruitment Observatory (国考招录观测台, interaction reference)

- **Archived source:** [`guokao-2027-observatory-interactive.html`](guokao-2027-observatory-interactive.html), preserved from the user-provided `2027国考_招录观测台_交互修复完整版.html` source.
- This is a **self-contained offline HTML example** with an unrelated recruiting dataset. Its dataset is a session-level summary **not independently verified against government originals**; do not quote it as official, reuse its counts, or inject its records into the LLM Portal.
- Borrow interaction and data-literacy patterns: linked views and shared filters; multi-level sunburst, treemap, dot matrix, sortable ranking, donut, Pareto/scatter; hover previews distinct from click-to-lock selection; breadcrumbs and reset; keyboard, focus and reduced-motion; explicit units/denominators; CSV/JSON and image export where supported.
- Translate these patterns into **trusted model usage / workload / latency / token** semantics; do not reproduce the recruiting site's identity, labels or unrelated charts where data does not justify them.
- Treat archived HTML/JS as **untrusted design input**. Do not wire it directly into V6 production, bundle its dataset, fetch arbitrary URLs or preserve unknown scripts in runtime code.

## Priority 3 — Atelier Observatory V3 (readability/typography reference)

- **Archived source:** [`atelier-v3-readability-reference.html`](atelier-v3-readability-reference.html), from `dell_llm_lab_atelier_v3_readable.html`.
- V3 is a historical offline design iteration, not a second architecture or authentication authority.
- Preserve its lessons: avoid mixing multiple competing serif and italic styles; consolidate Chinese/English hierarchy, remove redundant decorative labels, use body copy around 15px and tables around 13px as legibility targets, improve line height/contrast, place whitespace in logical groups instead of giant empty blocks, provide focused chart labels.
- Historical text about Feishu sign-in is obsolete; V6 identity is Linux/PAM and scoped Token, currently fail-closed until genuinely configured.
- **If V3 conflicts with V5, V5 wins.** V3 only refines readability.

## Historical screenshots — evidence, not the authority

- [Screenshot gallery](../SCREENSHOT_GALLERY.md) holds original screenshots from `preview/atelier-v6-captures-20261015`; it is **not** the latest V6 branch render and is not proof of true LIVE model calls.
- Priorities: **V5 visual identity > Guokao's tested interaction mechanics > V3 typography lessons > historical V6 layout evidence**. Backend contract/auth truth outranks *all* visual references.

## Implementation guardrails

1. A polished panel with invented numbers is worse than a meaningful **Not Configured** state. Default V6 should be **LIVE mode with no synthetic data**, not "demo by default".
2. Explicit `?demo=1` may use clearly marked synthetic interaction fixtures, primarily for visual QA and interaction regression, but must not be confused with real usage.
3. Resource state must be unambiguous: `registered != verified != deployment-ready != router-applied != authorized-to-invoke`.
4. User-specific data can only come from a trusted BFF/Gateway-derived Principal, never from the visible HTML's owner, UID or role fields.
5. Treat missing, stale, future or contradictory readings as UNKNOWN, not measured zero.
6. Keep authentication, Gateway, Service, V5 prototype, Caddy/systemd and other people's running models untouched during the frontend-only task.

See [frontend project context](../PROJECT_CONTEXT_AND_DESIGN_REFERENCES.md) and the [English Claude implementation prompt](../CLAUDE_FRONTEND_IMPLEMENTATION_PROMPT.md).
