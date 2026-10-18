/*! tests/infra/verified-infra.test.js
 *  The read-only host projection. No socket is ever opened: every case injects
 *  a fetch that either returns a crafted Prometheus body or throws, so these
 *  run on any machine, network or not.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  collectInfraSummary,
  offlineSummary,
  projectVector,
  infraEnabled,
  METRICS,
  PROMETHEUS_ORIGIN,
  MAX_SAMPLE_AGE_S,
  SOURCE,
} from '../../web/tools/verified-infra.mjs';

const NOW = () => 1_700_000_000_000;
const confirmed = METRICS.filter((m) => m.metric).map((m) => m.metric);

/** A fetch that answers Prometheus style and records every URL it was given. */
function promFetch(handler) {
  const seen = [];
  const impl = async (url, init) => {
    seen.push(String(url));
    if (init?.signal?.aborted) throw Object.assign(new Error('aborted'), { name: 'TimeoutError' });
    const vector = handler(String(url));
    return {
      ok: true,
      status: 200,
      json: async () => vector,
      arrayBuffer: async () => new ArrayBuffer(0),
    };
  };
  impl.seen = seen;
  return impl;
}

const vector = (samples) => ({ status: 'success', data: { resultType: 'vector', result: samples } });
const sample = (value, tSec = NOW() / 1000, metric = {}) => ({ metric, value: [tSec, String(value)] });

test('the route is off unless the operator sets the exact flag', () => {
  assert.equal(infraEnabled({}), false);
  assert.equal(infraEnabled({ ATELIER_INFRA_READONLY: '0' }), false);
  assert.equal(infraEnabled({ ATELIER_INFRA_READONLY: 'true' }), false);
  assert.equal(infraEnabled({ ATELIER_INFRA_READONLY: '1' }), true);
});

test('every query is a fixed whitelisted metric name, over loopback only', async () => {
  const fetchImpl = promFetch(() => vector([sample(1)]));
  await collectInfraSummary({ fetchImpl, now: NOW });
  const promotOnly = fetchImpl.seen.filter((u) => u.includes('/api/v1/query'));
  assert.ok(promotOnly.length > 0, 'it must actually ask Prometheus');
  for (const url of promotOnly) {
    assert.ok(url.startsWith(`${PROMETHEUS_ORIGIN}/api/v1/query?query=`), `unexpected target: ${url}`);
    const name = decodeURIComponent(new URL(url).searchParams.get('query'));
    assert.ok(confirmed.includes(name), `query is not on the whitelist: ${name}`);
    assert.equal(new URL(url).hostname, '127.0.0.1', 'only loopback may be addressed');
  }
  // GPU VRAM names were independently verified from the Dell metadata endpoint.
  for (const name of ['llmlab_strata_gpu_memory_used_bytes', 'llmlab_strata_gpu_memory_total_bytes']) {
    assert.ok(fetchImpl.seen.some((u) => u.includes(name)), `verified gauge was not queried: ${name}`);
  }
});

test('Dell-verified GPU memory bytes are projected to MiB', async () => {
  const values = {
    llmlab_strata_gpu_memory_used_bytes: 51258900000,
    llmlab_strata_gpu_memory_total_bytes: 51527000000,
  };
  const fetchImpl = promFetch((url) => {
    const name = new URL(url).searchParams.get('query');
    return vector([sample(values[name] ?? 1)]);
  });
  const out = await collectInfraSummary({ fetchImpl, now: NOW });
  for (const [key, gauge] of [
    ['gpu_memory_used_mib', 'llmlab_strata_gpu_memory_used_bytes'],
    ['gpu_memory_total_mib', 'llmlab_strata_gpu_memory_total_bytes'],
  ]) {
    assert.equal(out.metrics[key].state, 'known');
    assert.equal(out.metrics[key].metric, gauge);
    assert.equal(out.metrics[key].unit, 'mib');
    assert.equal(out.metrics[key].source, SOURCE);
    assert.equal(out.metrics[key].value, values[gauge] / 1048576);
    assert.equal(out.metrics[key].sample_at, new Date(NOW()).toISOString());
  }
});

test('a finite, fresh sample becomes a known reading with its unit and age', async () => {
  const fetchImpl = promFetch((url) => {
    const name = decodeURIComponent(new URL(url).searchParams.get('query'));
    return vector([sample(name === 'llmlab_strata_gpu_utilization_ratio' ? 0.42 : 7)]);
  });
  const out = await collectInfraSummary({ fetchImpl, now: NOW });
  const util = out.metrics.gpu_utilization_ratio;
  assert.equal(util.state, 'known');
  assert.equal(util.value, 0.42);
  assert.equal(util.unit, 'ratio');
  assert.equal(util.source, SOURCE);
  assert.equal(util.series, 1);
  assert.ok(util.age_s <= 1, 'a just-taken sample has a near-zero age');
  assert.equal(util.sample_at, new Date(NOW()).toISOString());
});

test('no sample is UNKNOWN, never 0', async () => {
  const fetchImpl = promFetch(() => vector([]));
  const out = await collectInfraSummary({ fetchImpl, now: NOW });
  assert.equal(out.metrics.gpu_utilization_ratio.state, 'unknown');
  assert.equal(out.metrics.gpu_utilization_ratio.reason, 'no-sample');
  assert.equal(out.metrics.gpu_utilization_ratio.value, null);
});

test('a stale sample is UNKNOWN, not a number', async () => {
  const stale = () => vector([sample(0.5, NOW() / 1000 - (MAX_SAMPLE_AGE_S + 60))]);
  const out = await collectInfraSummary({ fetchImpl: promFetch(stale), now: NOW });
  assert.equal(out.metrics.gpu_utilization_ratio.state, 'unknown');
  assert.equal(out.metrics.gpu_utilization_ratio.reason, 'stale');
  assert.ok(out.metrics.gpu_utilization_ratio.age_s > MAX_SAMPLE_AGE_S);
});

test('a non-finite sample is UNKNOWN', async () => {
  const out = await collectInfraSummary({ fetchImpl: promFetch(() => vector([sample('NaN')])), now: NOW });
  assert.equal(out.metrics.gpu_utilization_ratio.state, 'unknown');
  assert.equal(out.metrics.gpu_utilization_ratio.reason, 'non-finite-sample');
});

test('a stale high outlier cannot contaminate a fresh host gauge', () => {
  const spec = { unit: 'ratio', scale: 1, agg: 'max' };
  const out = projectVector(vector([
    sample(0.15, NOW() / 1000 - 9),
    sample(999, NOW() / 1000 - (MAX_SAMPLE_AGE_S + 400)),
  ]), spec, NOW());
  assert.equal(out.value, 0.15, 'only fresh samples may participate in aggregation');
  assert.equal(out.series, 1);
  assert.equal(out.sample_at, new Date(NOW() - 9000).toISOString());
});

test('blank and future-sample values remain UNKNOWN instead of zero', () => {
  const spec = { unit: 'mib', scale: 1, agg: 'max' };
  const blank = projectVector(vector([
    { metric: { api_key: 'untrusted' }, value: [NOW() / 1000, ''] },
  ]), spec, NOW());
  assert.equal(blank.value, undefined);
  assert.equal(blank.reason, 'non-finite-sample');
  const future = projectVector(vector([sample(2, NOW() / 1000 + 500)]), spec, NOW());
  assert.equal(future.value, undefined);
  assert.equal(future.reason, 'non-finite-sample');
});

test('an unreachable Prometheus yields a complete UNKNOWN contract, not a throw', async () => {
  const boom = async () => { throw new Error('ECONNREFUSED'); };
  const out = await collectInfraSummary({ fetchImpl: boom, now: NOW });
  assert.equal(out.state, 'unknown');
  assert.equal(out.source, SOURCE);
  assert.equal(out.synthetic, false);
  for (const key of Object.keys(out.metrics)) assert.equal(out.metrics[key].state, 'unknown');
  for (const key of ['strata', 'gateway', 'prometheus', 'grafana', 'evalscope']) {
    assert.equal(out.services[key].state, 'unknown');
  }
});

test('upstream labels are counted, never carried through', async () => {
  // A defensive Prometheus that leaks identifiers in its label set.
  const poisoned = () => vector([
    sample(1, NOW() / 1000, { api_key: 'SECRET-API-KEY-should-not-escape', uid: '1005', username: 'alice', model_name: 'qwen3.8' }),
    sample(3, NOW() / 1000, { api_key: 'ANOTHER-SECRET', uid: '1005' }),
  ]);
  const out = await collectInfraSummary({ fetchImpl: promFetch(poisoned), now: NOW });
  const json = JSON.stringify(out);
  for (const secret of ['SECRET-API-KEY-should-not-escape', 'ANOTHER-SECRET', 'alice', 'qwen3.8', '1005']) {
    assert.ok(!json.includes(secret), `a label value escaped into the projection: ${secret}`);
  }
  assert.equal(out.metrics.gpu_utilization_ratio.series, 2, 'the series count is still reported');
  assert.equal(out.metrics.gpu_utilization_ratio.value, 3, 'host-level aggregate is the max');
});

test('the response carries no key-, token- or secret-shaped string', async () => {
  const out = await collectInfraSummary({ fetchImpl: promFetch(() => vector([sample(0.1)])), now: NOW });
  const json = JSON.stringify(out);
  assert.doesNotMatch(json, /llk_[A-Za-z0-9]{4,}/);
  assert.doesNotMatch(json, /Bearer\s+[A-Za-z0-9_\-.]{12,}/);
  assert.doesNotMatch(json, /sk-[A-Za-z0-9]{8,}/);
  assert.doesNotMatch(json, /"api_key"/);
  assert.doesNotMatch(json, /password/i);
});

test('the contract carries the documented envelope', async () => {
  const out = await collectInfraSummary({ fetchImpl: promFetch(() => vector([sample(1)])), now: NOW });
  assert.equal(out.version, 1);
  assert.equal(out.source, 'prometheus-readonly');
  assert.equal(out.synthetic, false);
  assert.equal(out.readonly, true);
  assert.equal(out.observed_at, new Date(NOW()).toISOString());
  assert.equal(typeof out.metrics, 'object');
  assert.equal(typeof out.services, 'object');
});

test('projectVector applies the declared scale and aggregation', () => {
  const spec = { unit: 'mib', scale: 1 / 1048576, agg: 'max' };
  const out = projectVector(vector([sample(1048576 * 8, NOW() / 1000), sample(1048576 * 4, NOW() / 1000)]), spec, NOW());
  assert.equal(out.value, 8);
  assert.equal(out.series, 2);
});

test('offlineSummary is a complete UNKNOWN and touches no socket', async () => {
  const out = offlineSummary(NOW);
  assert.equal(out.state, 'unknown');
  assert.equal(out.synthetic, false);
  for (const m of Object.values(out.metrics)) assert.equal(m.value, null);
  assert.equal(out.observed_at, new Date(NOW()).toISOString());
});
