/* ==========================================================================
   sources -- the named data sources, and the selectors that read them.

   Both the shell and the views need the same live payload, and a view must
   not import app.js to get it (that is a cycle, and it makes the shell's
   bootstrap order load-bearing for no reason).  So the sources live here and
   the shell is just another subscriber.

   The selectors below exist because the server's payload is honest about
   *which* system answered, and the UI has to keep that distinction: model API
   health and GPU telemetry fail independently, and a single "status" word
   would merge the two cases that matter most (a node serving fine with dead
   telemetry, and a live GPU number sitting on top of a dead endpoint).
   ========================================================================== */

import { get } from './api.js';
import { poller } from './store.js';

/* ------------------------------------------------------------ the sources */

/**
 * The light poll: the shell's health pill and every "current value" tile.
 *
 * The 30s timeout is deliberate even though the poll interval is 20s.  "Light"
 * describes what the *page* does with the payload, not how fast the server can
 * produce it: /api/v2/live carries an SSH probe of the node, and a node under
 * load has been measured answering that probe in anywhere from 31s to 459s.
 * A 15s budget was optimistic and made a slow-but-working node look like a
 * failed request.  30s lets a slow tick land as a *stale reading* -- which the
 * UI already labels with its age -- rather than as an error with no value at
 * all.  Overlap is impossible: the store's single-flight guard holds the next
 * tick while one is outstanding.
 */
export const livePoll = poller(() => get('/api/v2/live', { timeout: 30000 }),
                              { interval: 20000, minInterval: 2000 });

/** The heavy one: full console + evaluation summary.  Slower by design. */
export const overviewPoll = poller(() => get('/api/v2/overview', { timeout: 45000 }),
                                   { interval: 60000, minInterval: 5000 });

/** Static-ish: config, endpoint list, external links. */
export const labPoll = poller(() => get('/api/lab', { timeout: 15000 }),
                              { interval: 0, minInterval: 3000, immediate: true });

/* -------------------------------------------------------------- selectors */

/** ok | degraded | down | unknown -- for the model endpoint alone. */
export function modelHealth(strata) {
  const ma = (strata || {}).model_api;
  if (!ma) return 'unknown';
  if (ma.ok === false) return 'down';
  if (ma.loaded === false) return 'degraded';
  return 'ok';
}

export const MODEL_TONE = { ok: 'ok', degraded: 'warn', down: 'crit', unknown: 'unknown' };

/** GPU telemetry availability is its own axis, never folded into model health. */
export function gpuState(gpu) {
  if (!gpu) return 'unknown';
  return gpu.available ? 'ok' : 'offline';
}

export const firstModel = (strata) => ((strata || {}).model_api || {}).model_list?.[0] || null;

/** Total VRAM usage as a fraction, or null when there is no reading. */
export function vramFrac(gpu) {
  if (!gpu || !gpu.available) return null;
  const used = gpu.used_mib, total = gpu.total_mib;
  if (!Number.isFinite(used) || !Number.isFinite(total) || !total) return null;
  return used / total;
}

/** The hottest thing in the payload that is worth colouring. */
export function pressureTone(frac) {
  if (frac === null || frac === undefined) return '';
  if (frac >= 0.92) return 'crit';
  if (frac >= 0.78) return 'warn';
  return '';
}

/**
 * A tunnel's state as one word, including the case that a boolean hides:
 * **foreign** -- the port is listening but not from the pid we started, so
 * something else owns it and the data behind it is not necessarily ours.
 */
export function tunnelState(t) {
  if (!t) return { key: 'unknown', label: '未知' };
  if (t.foreign) return { key: 'foreign', label: '被其他进程占用' };
  if (!t.enabled) return { key: 'off', label: '未启用' };
  if (t.listening) return { key: 'up', label: '已建立' };
  return { key: 'down', label: '未建立' };
}

/** The three-way service state; the middle one is not a boolean's business. */
export const SERVICE_LABEL = { up: '运行中', starting: '启动中', down: '未运行' };
export const SERVICE_TONE = { up: 'ok', starting: 'warn', down: 'offline' };

/** Worst-of over a list of tones, for a summary pill. */
export function worst(tones) {
  const rank = { crit: 3, error: 3, warn: 2, serious: 2, offline: 2, unknown: 1, ok: 0 };
  return tones.reduce((a, b) => (rank[b] ?? 1) > (rank[a] ?? 1) ? b : a, 'ok');
}
