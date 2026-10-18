/*! web/js/demo-mode.js — live by default, samples only on request.
 *
 *  The preview ships real host telemetry and *nothing else* that looks like a
 *  measurement. Everything fabricated — the call/usage KPIs, the endpoint and
 *  key tables, the synthetic Atlas workloads, the demo evaluation runs, the
 *  overview's 24-point call curve and its "active route" model card — is a design
 *  sample. Two things must both be true before one reaches the screen: the page
 *  must be the isolated QA artifact (`data-build-profile="qa"`, see
 *  web/js/build-profile.js), and the reader must ask for it with an explicit
 *  `?demo=1`. On the production artifact neither the samples nor the entry that
 *  offers them exist at all.
 *
 *  Three rules this file exists to keep honest:
 *
 *   * the build decides first. Unless the artifact is the QA one, the sample mode
 *     is not reachable — a query parameter cannot grant it;
 *   * the default is LIVE. A bare URL, a bare URL with `?utm_source=…`, a typo —
 *     none of them open the sample mode. Only the exact opt-in does;
 *   * it never *hides* a real measurement. It only ever touches nodes that are
 *     marked as samples (`data-demo-value`, or the regions listed below), and the
 *     host read-out is not one of them.
 *
 *  The static HTML already paints `—` where a number would go, so nothing has to
 *  be hidden after the fact: this only decides whether the parked sample values
 *  are swapped back in, and which whole regions are sample-only.
 *
 *  Every selector in `SAMPLE_REGIONS` is asserted against the shipped
 *  `web/index.html` by `tests/web-features/operations-first.test.js`. Two
 *  entries here used to name tables that no longer exist (`#view-models
 *  .tablewrap`, `#view-evaluation .tablewrap`), which meant the overview's model
 *  roster and its active-route card were the only authored samples still
 *  painted on a bare URL. A rule that matches nothing is worse than no rule: it
 *  reads like coverage. The test now fails if a selector goes dead.
 */

import { isQaBuild } from './build-profile.js';

export const DATA_MODES = Object.freeze({ LIVE: 'live', DEMO: 'demo' });

/**
 * The one opt-in. `?demo=1` / `?demo=true`; anything else is live.
 *
 * The query string is not sufficient on its own — it only *asks*. The build has
 * to be the isolated QA artifact before the ask is honoured, so a production URL
 * cannot re-enable the samples by editing itself. `qa` therefore defaults to
 * false: a caller that forgets to pass it gets the production answer.
 */
export function resolveDataMode(search = '', { qa = false } = {}) {
  if (!qa) return DATA_MODES.LIVE;
  const params = new URLSearchParams(search.startsWith('?') ? search.slice(1) : search);
  const demo = params.get('demo');
  return demo === '1' || demo === 'true' ? DATA_MODES.DEMO : DATA_MODES.LIVE;
}

/**
 * Regions that are entirely authored samples: hidden unless demo is on, each
 * replaced by one quiet line saying what it would need.
 *
 * `collapse` names an ancestor whose layout must lose a column once this region
 * is hidden — a two-column grid with one hidden child otherwise leaves a blank
 * half-page, which reads as a broken design rather than a deliberate gate.
 */
export const SAMPLE_REGIONS = Object.freeze([
  {
    sel: '#view-overview .explorer .side-side',
    label: '当前路由模型',
    why: '需要 Gateway 的模型登记与加载状态；「已登记」不等于「已加载」。',
    collapse: '#view-overview .explorer',
    // The stand-in goes *before* the grid, not inside it: `.explorer` is a
    // two-column grid, and a third child would be laid out as a column rather
    // than as a replacement for the column that was gated away.
    noticeBefore: '#view-overview .explorer',
  },
  {
    sel: '#view-overview .tablewrap',
    label: '模型服务一览',
    why: '需要 Registry 的服务端登记与项目授权；未接线前不列举任何模型条目。',
  },
  {
    sel: '#view-api .api-keys',
    label: '个人 API Keys',
    why: '需要 Linux PAM 会话与 Endpoint 授权；本预览不签发、不展示任何密钥。',
  },
  {
    sel: '#view-api .tablewrap',
    label: 'Endpoints 与调用协议',
    why: '需要服务端的 Endpoint 登记与项目授权；未接线前不展示示例条目。',
  },
  {
    // Covers the key table *and* the SDK aside beside it — the curl block with
    // its `lab.example` placeholder lives inside `.api-keys`, so a separate rule
    // for it would have been a second notice for one hidden region.
    sel: '#view-models .columns',
    label: '对话工作台与模型选择',
    why: '需要一个经 Linux PAM 身份授权的 Gateway 模型路由；未接线前不发送、也不模拟模型回复。',
  },
]);

/** The Atlas is one design, so it gets one notice rather than a per-region one. */
const ATLAS_NOTICE = Object.freeze({
  label: '调用结构图谱 · 数据面',
  why: '需要一个按 Linux 主体授权的调用数据源；未接线前不绘制任何合成工作负载、分布或排名。',
});

/** The class a collapsed grid carries while its sample column is hidden. */
export const COLLAPSED_CLASS = 'region-collapsed';

/** Attribute marking a stand-in. Its *presence* is the whole signal.
 *
 *  It used to be written as an empty attribute (`data-gated=""`) while every
 *  guard tested for `data-gated="1"`, so not one of the three idempotency checks
 *  could ever match: the Atlas notice was re-inserted on every call, and
 *  `applyRegions` never recognised the stand-in it had just placed. The value
 *  carries no information, so the guards now test presence — which is also what
 *  the static stand-in in the generated markup uses. */
export const GATED_SELECTOR = '[data-gated]';

function gatedNotice(doc, label, why, withSwitch) {
  const wrap = doc.createElement('div');
  wrap.className = 'gated-inline';
  wrap.dataset.liveOnly = '';
  wrap.dataset.gated = '';
  wrap.dataset.gatedLabel = label;
  // The sample-mode entry is *built* only on the QA artifact. On production it
  // is not hidden — it does not exist, so no stylesheet, script or URL can make
  // it reachable. See web/js/build-profile.js.
  const switchCta = withSwitch
    ? ' <button type="button" class="secondary" data-demo-switch>查看设计样例</button>'
    : '';
  wrap.innerHTML =
    '<div class="gated-row"><span class="gated-k">' + label + '</span>' +
    '<span class="badge warn">待接线</span></div>' +
    '<p class="gated-why">' + why + switchCta + '</p>';
  return wrap;
}

/**
 * Gate the whole Atlas body in one place.
 *
 * The Atlas is a single design built from the synthetic workload fixture, not a
 * list of separable regions: its metrics rail, control bar, stage grid, both
 * subsections, the compare block and the note are all authored numbers. Hiding
 * only `.explorer` — which is what this module used to do — left the rail, the
 * donut and the basis line printing `1,248` / `482k` on a bare URL, which is
 * exactly the thing this file exists to prevent. So every direct child that is
 * part of that design is marked at once, and one notice stands in for all of it
 * under the hero that gives the view its context.
 *
 * What stays: `.breadcrumb` and `.hero`, because they say what the view *is* and
 * carry no measurement. A direct child that declares `data-live-only` is also
 * left alone — that is how a real host panel would preserve itself if one is ever
 * mounted here. Everything else inside `#view-atlas` defaults to authored sample,
 * so a newly added sample block cannot leak by being forgotten.
 */
function gateAtlasBody(doc, withSwitch) {
  const view = doc.getElementById('view-atlas');
  if (!view) return;

  for (const child of [...view.children]) {
    if (child.hasAttribute('data-live-only')) continue; // 显式声明为实测，保留
    if (child.matches('.breadcrumb, .hero')) continue;  // 只定位，不含测量值
    child.dataset.demoOnly = '1';
  }

  if (view.querySelector(`:scope > ${GATED_SELECTOR}`)) return; // 已插过，保持幂等
  const notice = gatedNotice(doc, ATLAS_NOTICE.label, ATLAS_NOTICE.why, withSwitch);
  const hero = view.querySelector(':scope > .hero');
  if (hero) hero.after(notice);
  else view.prepend(notice);
}

/** Mark the sample regions and, in live mode, lead each with the gated notice. */
function applyRegions(doc, mode, withSwitch) {
  const demo = mode === DATA_MODES.DEMO;

  for (const region of SAMPLE_REGIONS) {
    const { sel, label, why, collapse, noticeBefore } = region;

    // One stand-in per region family, not one per matched node: a rule like
    // `#view-api .tablewrap` matches the key table too, and two identical
    // "待接线" lines stacked under one heading is noise, not information.
    let noticed = false;

    for (const node of doc.querySelectorAll(sel)) {
      node.dataset.demoOnly = '1';
      if (demo || noticed) continue;
      const parent = node.parentElement;
      if (!parent) continue;
      const anchor = noticeBefore ? doc.querySelector(noticeBefore) : node;
      const host = anchor?.parentElement ?? parent;
      // A second call must find the stand-in it placed, and a stand-in with a
      // different label in the same parent is a different region, not a dupe.
      const already = [...(host?.children ?? [])].some((child) => (
        child.dataset?.gated !== undefined && child.dataset?.gatedLabel === label
      ));
      if (already) { noticed = true; continue; }
      host?.insertBefore?.(gatedNotice(doc, label, why, withSwitch), anchor);
      noticed = true;
    }

    if (collapse) {
      for (const host of doc.querySelectorAll(collapse)) {
        host.classList.toggle(COLLAPSED_CLASS, !demo);
      }
    }
  }

  // In sample mode every stand-in is removed, not just the first one: a leftover
  // "待接线" line beside a visible sample block contradicts the block it guards.
  if (demo) {
    for (const node of doc.querySelectorAll(GATED_SELECTOR)) node.remove();
  }
}

/**
 * Apply the resolved mode to a document.
 * @param {{doc?:Document, search?:string}} [opts]
 */
export function applyDataMode({ doc = globalThis.document, search = globalThis.location?.search ?? '' } = {}) {
  // Whether the samples are reachable at all is decided by the artifact, before
  // the query string is even read. On production `qa` is false, so `mode` is
  // LIVE no matter what the URL says.
  const qa = isQaBuild(doc);
  const mode = resolveDataMode(search, { qa });
  doc.documentElement.dataset.mode = mode;

  for (const node of doc.querySelectorAll('[data-demo-value]')) {
    if (mode === DATA_MODES.DEMO) node.textContent = node.dataset.demoValue;
    else if (node.textContent !== '—') node.textContent = '—';
  }
  for (const note of doc.querySelectorAll('[data-stat-note]')) {
    note.textContent = mode === DATA_MODES.DEMO ? '设计样例 · 非实测' : '待授权';
  }
  // The Atlas body is marked first so its single notice exists by the time
  // `applyRegions` runs and can take part in the demo-switch cleanup.
  gateAtlasBody(doc, qa);
  applyRegions(doc, mode, qa);

  // The switch listener exists only where a switch can exist. On production
  // there is no `[data-demo-switch]` node to find, and no handler that could act
  // on one: the sample mode is not reachable from this bundle at all.
  if (qa) {
    doc.addEventListener('click', (event) => {
      if (!event.target.closest?.('[data-demo-switch]')) return;
      event.preventDefault();
      const next = new URL(globalThis.location.href);
      next.searchParams.set('demo', '1');
      globalThis.location.assign(next.toString());
    });
  }

  return mode;
}
