/* ==========================================================================
   reports -- 报告中心.

   Every document this project has produced, in one list, previewable and
   downloadable, with the provenance that makes the list trustworthy: where
   each file lives, how big it is, and when it last changed.

   The list is a directory scan the server already performs (/api/reports), so
   this view adds only presentation: a big-enough table, a filter that is
   deliberately *client-side* (it never reaches a URL, and it never refetches),
   and a preview that loads the server's own rendered page in a sandboxed
   iframe rather than re-rendering markdown a second time here.

   Path safety: the only paths that ever reach /report, /file or /download are
   the ones the server itself returned in the report list, each passed through
   encodeURIComponent.  The filter box is a display filter and nothing else.
   ========================================================================== */

import { esc, delegate } from '../core/dom.js';
import { DASH, bytes, ts, ago, clip, int } from '../core/format.js';
import { get, isAborted, makeScope } from '../core/api.js';
import { skeleton, empty, errorState, modal, segmented as wireSegmented } from '../core/ui.js';
import { band, grid, stat, table, segmented as segmentedControl } from './_parts.js';

/* ---------------------------------------------------------------- helpers */

/** A numeric field or 0.  Used only for sums/orderings, never for display --
 *  a displayed value goes through format.js, which renders a gap as DASH. */
const finite = (v) => (typeof v === 'number' && Number.isFinite(v)) ? v : 0;

/** A report's kind from its extension -- the item's own `kind` is always
 *  "report", so the file type is read from the path. */
const kindOf = (r) => {
  const m = /\.([a-z0-9]+)$/i.exec(r.path || '');
  const ext = m ? m[1].toLowerCase() : '';
  return ext === 'md' ? 'md' : (ext === 'html' || ext === 'htm') ? 'html' : 'other';
};

const KIND_TEXT = { md: 'Markdown', html: 'HTML', other: '' };
const KIND_OPTIONS = [
  { value: 'all', label: '全部' },
  { value: 'md', label: 'Markdown' },
  { value: 'html', label: 'HTML' },
];

/* --------------------------------------------------------------- fragments */

function statsBand(reports) {
  const total = reports.reduce((a, r) => a + finite(r.bytes), 0);
  const newest = reports.reduce((a, r) => Math.max(a, finite(r.mtime)), 0) || null;
  return band({
    title: '统计',
    sub: '来自 GET /api/reports 的一次目录扫描',
    body: grid(3,
      stat({ label: '报告数量', value: int(reports.length), unit: '份', ic: 'file', source: 'GET /api/reports' })
      + stat({ label: '合计大小', value: bytes(total), ic: 'drive', source: '各报告文件大小求和' })
      + stat({ label: '最近更新', value: ago(newest), sub: newest ? ts(newest) : '', ic: 'clock', source: '最新文件 mtime' })),
  });
}

function filtersBand() {
  return band({
    title: '筛选',
    sub: '只作用于当前已载入的列表，不会重新请求',
    body: `<div style="display:flex;flex-wrap:wrap;gap:var(--sp-4);align-items:center">`
      + segmentedControl('repKind', KIND_OPTIONS, 0)
      + `<input class="input" id="repSearch" type="search" placeholder="按标题或路径筛选…"`
      + ` aria-label="按标题或路径筛选报告" style="flex:1 1 220px;min-width:0">`
      + `</div>`,
  });
}

/** One table row.  `i` is unused for values but keeps the call site obvious. */
function rowOf(r) {
  const link = `/report?p=${encodeURIComponent(r.path)}`;
  const dl = `/download?p=${encodeURIComponent(r.path)}`;
  const kt = KIND_TEXT[kindOf(r)] || r.kind || DASH;
  return [
    // An <a> the reader may open in a new tab; the row's own click (handled by
    // delegate) is what opens the sandboxed preview modal instead.
    { __raw: true, value: `<a href="${link}" target="_blank" rel="noopener noreferrer">`
        + `${esc(clip(r.title || r.path, 58))}</a>` },
    { __raw: true, value: `<span class="mono truncate" style="display:inline-block;max-width:42ch">`
        + `${esc(r.path)}</span>` },
    esc(kt),
    esc(bytes(r.bytes)),
    esc(ts(r.mtime)),
    { __raw: true, value: `<div class="row-actions">`
        + `<button class="btn btn-ghost btn-sm" type="button">预览</button>`
        + `<a class="btn btn-ghost btn-sm" href="${dl}">下载</a></div>` },
  ];
}

/* ---------------------------------------------------------------- render */

export async function render(host) {
  const scope = makeScope();
  let destroyed = false;
  let reports = null;      // null = loading; [] = a real, measured nothing
  let err = null;
  let kind = 'all';
  let query = '';
  const byPath = new Map();

  const numBytes = finite;

  const matches = (r) => {
    if (kind === 'md' && kindOf(r) !== 'md') return false;
    if (kind === 'html' && kindOf(r) !== 'html') return false;
    if (query) {
      const hay = `${r.title || ''} ${r.path || ''}`.toLowerCase();
      if (!hay.includes(query)) return false;
    }
    return true;
  };

  /** Rebuild only the table body, so the filter input keeps focus. */
  function paintTable() {
    const tbl = host.querySelector('#repTable');
    if (!tbl) return;
    const list = reports.filter(matches).sort((a, b) => (numBytes(b.mtime) - numBytes(a.mtime)));
    if (!list.length) {
      tbl.innerHTML = empty('当前筛选条件下没有匹配的报告。清空关键字或切回「全部」。',
        { title: '没有匹配项', icon: 'filter' });
      return;
    }
    tbl.innerHTML = table(['标题', '路径', '类型', '大小', '时间', '操作'], list.map(rowOf), { num: [3] });
    // _parts.table() emits plain <tr>s (no per-row attributes), so the path is
    // stamped on afterwards; dataset assignment needs no HTML escaping.
    tbl.querySelectorAll('tbody tr').forEach((tr, i) => { tr.dataset.path = list[i].path; });
  }

  function view() {
    if (err) return band({ title: '报告清单', body: errorState(err, { retry: () => load() }) });
    if (!reports) return band({ title: '报告', body: skeleton(3, { block: true }) });
    if (!reports.length) {
      return band({
        title: '报告中心',
        sub: '由服务端对 reports/ 目录扫描得到',
        body: empty('本次扫描没有在 reports/ 下找到任何 .md / .html 文件。把报告放进该目录后重新载入即可。'
          + '（本列表是一次目录扫描，不是数据库查询。）', { title: '暂无报告', icon: 'file' }),
      });
    }
    return statsBand(reports)
      + filtersBand()
      + band({ title: '报告列表', sub: '按更新时间倒序', body: `<div id="repTable"></div>` })
      + `<footer class="page-foot">
           <span>数据来源：<span class="mono">/api/reports</span> · <span class="mono">/report</span> · <span class="mono">/download</span></span>
           <span>${esc(int(reports.length))} 份报告</span>
         </footer>`;
  }

  function openPreview(item) {
    if (!item) return;
    const src = `/report?p=${encodeURIComponent(item.path)}`;
    modal({
      title: `预览 · ${item.title || item.path}`,
      width: 'min(1080px,100%)',
      body: `<p class="hint-row" style="margin:0 0 var(--sp-4)">下方是服务器渲染的页面`
        + `（<span class="mono">/report?p=…</span>）；内容为只读快照，脚本已在沙箱中禁用。</p>`
        + `<iframe src="${esc(src)}" title="报告预览：${esc(item.title || item.path)}"`
        + ` sandbox="allow-same-origin"`
        + ` style="width:100%;height:min(70vh,720px);border:1px solid var(--hair);border-radius:var(--r-md);background:#fff"></iframe>`,
    });
  }

  function wire() {
    const seg = host.querySelector('#repKind');
    if (seg) wireSegmented(seg, (v) => { kind = v; paintTable(); });
    const inp = host.querySelector('#repSearch');
    if (inp) inp.addEventListener('input', () => { query = inp.value.trim().toLowerCase(); paintTable(); });
    const tbl = host.querySelector('#repTable');
    if (tbl) delegate(tbl, 'click', 'tbody tr[data-path]', (ev, tr) => {
      if (ev.target.closest('a')) return;              // 标题链接与下载各自处理
      openPreview(byPath.get(tr.dataset.path));
    });
  }

  const paint = () => {
    if (destroyed) return;
    host.innerHTML = view();
    wire();
    paintTable();
  };

  async function load() {
    err = null;
    reports = null;
    paint();
    try {
      const data = await get('/api/reports', { signal: scope.signal, timeout: 20000 });
      reports = Array.isArray(data?.reports) ? data.reports : [];
      byPath.clear();
      reports.forEach((r) => byPath.set(r.path, r));
    } catch (e) {
      if (isAborted(e) || destroyed) return;
      err = e;
    }
    if (!destroyed) paint();
  }

  host.innerHTML = skeleton(3, { block: true });
  await load();
  if (destroyed) return { destroy() {} };

  return {
    // One-shot fetch only; a late response must not paint into a container the
    // router has already replaced.
    destroy() { destroyed = true; scope.abort(); },
  };
}
