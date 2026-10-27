import { loadOps, readRevision } from './data.js';
import { createReadCache } from './read-cache.js';

export const createOpsCache = () => createReadCache(loadOps, { revision: readRevision });
export const readOps = (ctx, options = {}) => ctx.opsCache
  ? ctx.opsCache.read({ signal: ctx.signal, ...options }) : loadOps({ signal: ctx.signal, ...options });

/** Dispose also works for detached pages and while their first read is still pending. */
export function pageScope(ctx, root) {
  const controller = new AbortController();
  const cleanups = [];
  const dispose = () => {
    if (controller.signal.aborted) return;
    controller.abort(); ctx.signal?.removeEventListener('abort', dispose);
    for (const cleanup of cleanups) cleanup();
  };
  root.dispose = dispose;
  if (ctx.signal?.aborted) dispose(); else ctx.signal?.addEventListener('abort', dispose, { once: true });
  return { ...ctx, signal: controller.signal, onDispose: fn => controller.signal.aborted ? fn() : cleanups.push(fn) };
}
