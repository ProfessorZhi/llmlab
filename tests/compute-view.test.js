/*! Tests for the dynamic Compute view. Synthetic fixtures may not become
 * authoritative GPU telemetry, and legacy #/compute must stay routable.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  COMPUTE_METRIC_NAMES,
  mountComputeView,
  projectComputeMetrics,
} from '../web/js/compute/view.js';
import { PAGES, ALL_ROUTES, pageFromHash, pageParent } from '../web/js/router.js';

const trusted = (name, value, source = 'nvml-verified', updated_at = '2026-10-16T01:00:00Z') => ({
  name, value, source, updated_at, state: 'available',
});

const liveOps = (items) => ({
  synthetic: false,
  transport: { mode: 'live', gateway_attached: true },
  metrics: { items },
});

test('Compute route exists and accepts the old /#/compute URL', () => {
  assert.equal(PAGES.includes('compute'), false);
  assert.ok(ALL_ROUTES.includes('compute'));
  assert.equal(pageParent('compute'), 'overview');
  assert.equal(pageFromHash('#/compute'), 'compute');
  assert.equal(pageFromHash('#compute'), 'compute');
  assert.equal(pageFromHash('#/unknown'), 'overview');
});

test('all three compute metric slots are present and default to UNKNOWN', () => {
  const empty = projectComputeMetrics();
  assert.equal(empty.mode, 'unverified');
  assert.equal(empty.updatedAt, null);
  assert.equal(COMPUTE_METRIC_NAMES.length, 3);
  for (const name of COMPUTE_METRIC_NAMES) {
    assert.equal(empty.metrics[name].value, 'UNKNOWN');
  }
});

test('a synthetic response may never report GPU values even if its fields are forged', () => {
  const payload = liveOps([trusted('gpu_vram_used_mib', 111)]);
  payload.synthetic = true;
  const reported = projectComputeMetrics({ ops: payload, mode: 'live', state: 'ready' });
  assert.equal(reported.metrics.gpu_vram_used_mib.value, 'UNKNOWN');
  assert.equal(reported.updatedAt, null);

  const misleadingPreview = projectComputeMetrics({
    ops: liveOps([trusted('gpu_utilization_pct', 100)]),
    mode: 'mock',
    state: 'ready',
  });
  assert.equal(misleadingPreview.metrics.gpu_utilization_pct.value, 'UNKNOWN');
  assert.match(misleadingPreview.badge, /SYNTHETIC/);
});

test('untrusted provenance, missing timestamps, and bad data remain UNKNOWN', () => {
  const cases = [
    trusted('gpu_vram_used_mib', 100, 'prometheus-readonly'),
    trusted('gpu_vram_used_mib', 100, 'nvml-verified', ''),
    trusted('gpu_vram_used_mib', -1),
    trusted('gpu_vram_used_mib', Number.NaN),
    { ...trusted('gpu_vram_used_mib', 100), state: 'unknown' },
  ];
  for (const item of cases) {
    const r = projectComputeMetrics({ ops: liveOps([item]), mode: 'live', state: 'ready' });
    assert.equal(r.metrics.gpu_vram_used_mib.value, 'UNKNOWN');
    assert.equal(r.updatedAt, null);
  }
  const noSession = projectComputeMetrics({
    ops: liveOps([trusted('gpu_vram_used_mib', 100)]),
    mode: 'live',
    state: 'signed_out',
  });
  assert.equal(noSession.metrics.gpu_vram_used_mib.value, 'UNKNOWN');
});

test('a genuine NVML-sourced observation is shown only with an observation time', () => {
  const result = projectComputeMetrics({
    ops: liveOps([
      trusted('gpu_utilization_pct', 0),
      trusted('gpu_vram_used_mib', 1234),
      trusted('gpu_vram_total_mib', 49140),
    ]),
    mode: 'live',
    state: 'ready',
  });
  assert.equal(result.mode, 'live');
  assert.equal(result.metrics.gpu_utilization_pct.value, '0%');
  assert.equal(result.metrics.gpu_vram_used_mib.value, '1,234 MiB');
  assert.equal(result.metrics.gpu_vram_total_mib.value, '49,140 MiB');
  assert.equal(result.updatedAt, '2026-10-16T01:00:00Z');
});

test('the generated shell remains unchanged and a nested Overview entry mounts once', () => {
  const elements = new Map();
  const footer = { id: 'footer' };
  const main = {
    children: [footer],
    querySelector(sel) { return sel === '.footbar' ? footer : null; },
    insertBefore(node, before) {
      const at = this.children.indexOf(before);
      this.children.splice(at >= 0 ? at : this.children.length, 0, node);
      elements.set('#view-compute', node);
    },
    appendChild(node) {
      this.children.push(node);
      elements.set('#view-compute', node);
    },
  };
  const overview = {
    children: [],
    querySelector(sel) {
      if (sel !== '[data-compute-entry]') return null;
      return this.children.find((node) => node.dataset?.computeEntry === '1') ?? null;
    },
    appendChild(node) { this.children.push(node); },
  };
  const desktop = { children: [], appendChild(node) { this.children.push(node); } };
  elements.set('#app-main', main);
  elements.set('#view-overview', overview);
  elements.set('.topnav', desktop);

  const doc = {
    querySelector(sel) { return elements.get(sel) ?? null; },
    createElement(tag) {
      if (tag === 'section') {
        const cells = new Map();
        return {
          dataset: {},
          className: '',
          innerHTML: '',
          setAttribute() {},
          querySelector(sel) {
            if (!cells.has(sel)) cells.set(sel, { textContent: null });
            return cells.get(sel);
          },
        };
      }
      return { type: tag, dataset: {}, className: '', textContent: '' };
    },
  };

  const first = mountComputeView({ doc });
  const second = mountComputeView({ doc });
  assert.ok(first);
  assert.ok(second);
  assert.equal(main.children.filter((x) => x.id === 'view-compute').length, 1);
  assert.equal(overview.children.filter((x) => x.dataset.computeEntry === '1').length, 1);
  assert.match(overview.children[0].innerHTML, /data-go="compute"/);
  assert.equal(desktop.children.length, 0, 'subpages must not create a ninth primary tab');
  assert.match(first.view.innerHTML, /id="compute-title"/);
  assert.match(first.view.innerHTML, /data-go="overview"/);
  assert.match(first.view.innerHTML, /data-go="observability"/);

  const snapshot = first.update({
    ops: liveOps([trusted('gpu_utilization_pct', 33)]),
    mode: 'live',
    state: 'ready',
  });
  assert.equal(snapshot.metrics.gpu_utilization_pct.value, '33%');
  assert.equal(first.view.querySelector('[data-compute-metric="gpu_utilization_pct"]').textContent, '33%');

  const revoked = first.update({ ops: null, mode: 'live', state: 'signed_out' });
  assert.equal(revoked.metrics.gpu_utilization_pct.value, 'UNKNOWN');
  assert.equal(first.view.querySelector('[data-compute-metric="gpu_utilization_pct"]').textContent, 'UNKNOWN');
});
