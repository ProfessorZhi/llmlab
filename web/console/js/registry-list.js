import { h, put, clear } from './dom.js';
import { emptyState } from './ui.js';
import { readOps } from './page-data.js';
import { createRefresher } from './live-refresh.js';

export const LIST_PAGE_SIZE = 50;

/** Keep controls/forms mounted; cap row DOM and replace only changed visible rows. */
export function registryList(ctx, { project, render, empty, demoRows = [] }) {
  const root = h('div', { class: 'registry-list' });
  let rows = null, page = 0, signature = null, busy = false, refreshAgain = false, pendingDraw = false;
  const status = h('span', { class: 'muted', role: 'status' }, '正在读取…');
  const refreshButton = h('button', { type: 'button', class: 'btn', onclick: () => refresh(true) }, '刷新');
  const content = h('div', { class: 'registry-content' }, h('p', { class: 'muted' }, '正在读取…'));
  const count = h('span', { class: 'muted tnum' });
  const previous = h('button', { type: 'button', class: 'btn', onclick: () => { page--; draw(); } }, '上一页');
  const next = h('button', { type: 'button', class: 'btn', onclick: () => { page++; draw(); } }, '下一页');
  const pager = h('div', { class: 'list-pagination', 'aria-label': '列表分页' }, count, previous, next);
  put(root, h('div', { class: 'list-toolbar' }, status, refreshButton), content, pager);
  const synced = () => ctx.demo ? `${rows.length} 项 · 演示数据` : `${rows.length} 项 · 已同步 · 每 15 秒更新`;
  function draw() {
    const total = rows?.length ?? 0;
    page = Math.max(0, Math.min(page, Math.max(0, Math.ceil(total / LIST_PAGE_SIZE) - 1)));
    const visible = rows?.slice(page * LIST_PAGE_SIZE, (page + 1) * LIST_PAGE_SIZE) ?? [];
    const key = JSON.stringify(visible);
    if (signature !== key) {
      if (signature !== null && content.contains?.(document.activeElement)) { pendingDraw = true; return false; }
      put(clear(content), total ? render(visible) : empty()); signature = key;
    }
    pendingDraw = false;
    pager.hidden = total <= LIST_PAGE_SIZE;
    count.textContent = `共 ${total} 项 · ${page + 1} / ${Math.max(1, Math.ceil(total / LIST_PAGE_SIZE))} 页`;
    previous.disabled = page === 0; next.disabled = (page + 1) * LIST_PAGE_SIZE >= total;
    return true;
  }
  async function refresh(force = false) {
    if (ctx.signal?.aborted) return;
    if (busy) { refreshAgain ||= force; return; }
    busy = true; refreshButton.disabled = true;
    status.textContent = rows ? '正在更新，保留上次结果…' : '正在读取…';
    const r = ctx.demo ? { state: 'ready' } : await readOps(ctx, { force });
    if (ctx.signal?.aborted) return;
    busy = false; refreshButton.disabled = false;
    if (r.state === 'ready') {
      rows = ctx.demo ? demoRows : project(r.value);
      if (rows) { status.textContent = draw() ? synced() : '新数据已就绪，离开当前行后更新'; }
      else { signature = null; pager.hidden = true; put(clear(content), emptyState('not_configured')); status.textContent = '注册中心未接入'; }
    } else {
      if (!rows || ['signed_out', 'denied'].includes(r.state)) { rows = null; signature = null; pager.hidden = true; put(clear(content), emptyState(r.state)); }
      status.textContent = rows ? '更新失败，保留上次结果' : '暂时无法读取';
    }
    if (refreshAgain) { refreshAgain = false; await refresh(true); }
  }
  content.addEventListener('focusout', () => queueMicrotask(() => {
    if (pendingDraw && rows && !ctx.signal?.aborted && !content.contains?.(document.activeElement) && draw()) {
      status.textContent = synced();
    }
  }));
  const cached = ctx.opsCache?.peek();
  if (!ctx.demo && cached) { rows = project(cached.value); if (rows) draw(); }
  pager.hidden = !rows || rows.length <= LIST_PAGE_SIZE;
  refresh();
  const ticker = createRefresher({ intervalMs: 15000, isAttached: () => root.isConnected,
    run: () => refresh(true) });
  if (!ctx.demo) ticker.start();
  ctx.onDispose?.(() => ticker.stop());
  return { root, refresh: () => refresh(true), setRows(value) { rows = value; draw(); } };
}
