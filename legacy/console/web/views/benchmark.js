/* ==========================================================================
   benchmark -- Benchmark Lab.

   The question this page answers is not "how fast was it" but *what was
   measured, on which model, and which parts of the intended measurement were
   never taken*.  A benchmark page that lists only its successes reads as full
   coverage, so the coverage figure and the missing dimensions are given the
   same weight as the throughput numbers.

   Nothing here is invented.  Where an artifact is absent the block names the
   file that is missing and where it was looked for; a value the run document
   does not carry renders as an em dash (core/format.js), never as a zero.

   Three endpoints feed it, in two speeds: the run *list* and the evaluation
   summary load once per visit, while the per-run document reloads on every
   change of the picker.  The picker is an in-view <select>, not a navigation,
   so switching runs never spins the whole page back up.
   ========================================================================== */

import { esc } from '../core/dom.js';
import { DASH, num, int, bytes, gib, lat, ts, clip } from '../core/format.js';
import { get, isAborted, makeScope } from '../core/api.js';
import { skeleton, empty, errorState, notice } from '../core/ui.js';
import { onThemeRepaint } from '../core/theme.js';
import { icon } from '../core/icons.js';
import * as paint from '../core/chart.js';
import { card, band, grid, stat, src, kv, table, button } from './_parts.js';

/* --------------------------------------------------------------- constants */

/* The CSV kinds are exactly the tables _export_csv() will serve for a run:
   "bench", "concurrency", "quality", "files".  Anything else is a 404. */
const CSV_KINDS = ['bench', 'concurrency', 'quality', 'files'];
const KIND_LABELS = {
  bench: '吞吐扫描',
  concurrency: '串行 / 并发',
  quality: '能力评分',
  files: '文件清单',
};

/* ------------------------------------------------------------- url helpers */

/** The `run` query of `#/benchmark?run=…`, or null.  The router ignores the
 *  query, so the picker owns it -- but a shared link still selects a run. */
function hashRun() {
  const h = String(window.location.hash || '');
  const i = h.indexOf('?');
  if (i < 0) return null;
  try { return new URLSearchParams(h.slice(i + 1)).get('run') || null; } catch { return null; }
}

/** Keep the hash in sync without firing hashchange, so the router does not
 *  re-render (and refetch everything) just because the picker moved. */
function setHashRun(run) {
  if (!run) return;
  try { history.replaceState(null, '', `#/benchmark?run=${encodeURIComponent(run)}`); }
  catch { /* file: origin has no history API -- selection still works */ }
}

/* ---------------------------------------------------------------- selectors */

const listRun = (st) => (st.list?.runs || []).find((r) => r.run === st.run) || null;
const evalRun = (st) => (st.evals?.runs || []).find((r) => r.run === st.run) || null;

/** A metric the run document did not carry is a gap, not a zero. */
const numOrNull = (v) => (typeof v === 'number' && Number.isFinite(v)) ? v : null;
const values = (rows, key) => rows.map((r) => numOrNull(r[key]));

/* -------------------------------------------------------------- fragments */

const loading = () => skeleton(3, { block: true });

/** The picker is band tools, not page chrome: one <select> and a refresh. */
function picker(runs, current) {
  const opts = runs.map((r) =>
    `<option value="${esc(r.run)}"${r.run === current ? ' selected' : ''}>`
    + `${esc(r.run)} · ${esc(ts(r.mtime))}</option>`).join('');
  return `<label class="field" style="min-width:0"><span>选择运行</span>`
    + `<select class="select" id="benchRun" aria-label="选择评测运行">${opts}</select></label>`
    + button({ label: '刷新', ic: 'refresh', cls: 'btn-ghost btn-sm', id: 'benchRefresh' });
}

/* ------------------------------------------------------- section 2: headline */

function headlineBlock(st) {
  const lr = listRun(st) || {};
  const er = evalRun(st);
  const cfg = st.detail?.configuration || {};
  const model = er?.model || cfg.model?.name || null;
  const at = typeof lr.mtime === 'number' ? ts(lr.mtime) : null;
  const cov = er ? `${int(er.present)}/${int(er.expected)}` : DASH;
  const dims = er?.dimensions || {};
  const missing = Object.entries(dims).filter(([, v]) => !v.present);

  const top = grid(4,
    stat({
      label: '模型', value: model || DASH, ic: 'cube',
      sub: cfg.model?.quant || '', source: 'configuration.json · model.name',
    })
    + stat({ label: '运行', value: st.run || DASH, ic: 'flask', source: 'benchmarks/ 下的目录名' })
    + stat({ label: '时间', value: at || DASH, ic: 'clock', source: '运行目录内文件最新 mtime' })
    + stat({
      label: '口径覆盖', value: cov, unit: er ? '个维度' : '', ic: 'chart',
      tone: er && missing.length ? 'warn' : '',
      sub: er
        ? (missing.length ? `缺 ${missing.map(([, v]) => v.label).join('、')}` : '全部维度已产出')
        : (st.evalErr ? '未取到 /api/evaluation' : '加载中'),
      source: 'GET /api/evaluation · dimensions',
    }));

  const head = er?.headline
    ? `<div class="hint-row" style="margin-top:var(--sp-5)">${icon('check', '', 14)} ${esc(er.headline)}</div>`
    : '';

  let dimsBlock;
  if (!er) {
    dimsBlock = notice('warning', '覆盖口径未知',
      `未能读取 /api/evaluation（${st.evalErr?.message || '尚无运行'}），无法判断这次运行缺哪些维度。`);
  } else {
    const rows = Object.entries(dims).map(([k, v]) => [
      v.label || k,
      { __raw: true, value: v.present
          ? '<span style="color:var(--good)">✓ 已产出</span>'
          : '<span style="color:var(--warning)">✗ 缺失</span>' },
      v.summary || DASH,
    ]);
    dimsBlock = (missing.length
      ? notice('warning', '评测口径不完整', `缺 ${missing.map(([, v]) => v.label).join('、')}。下方逐维度核对。`)
      : notice('info', '评测口径完整',
          `${int(er.expected)} 个维度均已在本次运行的目录中产出，摘要见下表。`))
      + table(['评测维度', '产出', '摘要'], rows)
      + src('GET /api/evaluation · dimensions[].present / summary');
  }

  return top + head + `<div style="margin-top:var(--sp-6)">${dimsBlock}</div>`;
}

/* ------------------------------------------------- section 3: throughput */

function throughputBlock(st) {
  if (st.detailErr) return errorState(st.detailErr, { retry: st.retryDetail });
  if (!st.detail) return loading();
  const b = st.detail.bench;
  if (!b || !Array.isArray(b.rows) || !b.rows.length) {
    return empty(`本次运行没有读到 bench_summary.json 的 per_size`
      + `（查找位置：data/benchmarks/${st.run}/bench_summary.json）。吞吐 / 延迟扫描未产出。`,
      { title: '吞吐扫描缺失', icon: 'chart' });
  }
  return grid(2,
    card({
      title: '解码 / 预填充吞吐', note: 'tok/s · 同一 y 轴',
      body: `<div class="chart-host" data-chart="tp-throughput"></div>`,
    })
    + card({
      title: '首字延迟 TTFT', note: '秒 · 越低越好',
      body: `<div class="chart-host" data-chart="tp-ttft"></div>`,
    }))
    + `<div class="hint-row">每次 ${esc(int(b.max_tokens))} 输出 token · ${esc(int(b.repeats))} 次重复`
    + ` · 来源 bench_summary.json</div>`;
}

/* ------------------------------------------------ section 4: seq vs concurrent */

function concurrencyBlock(st) {
  if (st.detailErr) return errorState(st.detailErr, { retry: st.retryDetail });
  if (!st.detail) return loading();
  const c = st.detail.concurrency;
  if (!c || !Array.isArray(c.rows) || !c.rows.length) {
    return empty(`本次运行没有读到 concurrency_short.json 的 rows`
      + `（查找位置：data/benchmarks/${st.run}/concurrency_short.json）。串行 / 并发对比未产出。`,
      { title: '串行 / 并发缺失', icon: 'chart' });
  }
  const ratios = c.rows.map((r) => numOrNull(r.ratio)).filter((v) => v !== null);
  const mean = ratios.length ? ratios.reduce((a, v) => a + v, 0) / ratios.length : null;
  const note = mean !== null
    ? `文档记录的 ratio 均值 ${esc(num(mean, 3))}（ratio_conc_over_seq，来自 concurrency_short.json）`
    : '文档未记录 ratio';

  return grid(2,
    card({
      title: '吞吐：串行 vs 并发', note: 'tok/s',
      body: `<div class="chart-host" data-chart="cc-throughput"></div>`,
    })
    + card({
      title: '首字延迟：串行 vs 并发', note: '秒 · 越低越好',
      body: `<div class="chart-host" data-chart="cc-ttft"></div>`,
    }))
    + `<div class="hint-row">${icon('info', '', 13)} ${note}</div>`;
}

/* ------------------------------------------------------ section 5: quality */

function qualityBlock(st) {
  if (st.detailErr) return errorState(st.detailErr, { retry: st.retryDetail });
  if (!st.detail) return loading();
  const q = st.detail.quality;
  if (!q || !Array.isArray(q.categories) || !q.categories.length) {
    return empty(`本次运行没有读到 quality/quality_summary.json 的 categories`
      + `（查找位置：data/benchmarks/${st.run}/quality/quality_summary.json）。能力评分未产出。`,
      { title: '能力评分缺失', icon: 'flask' });
  }
  return grid(4,
    stat({
      label: '总通过率', ic: 'check',
      value: typeof q.score === 'number' ? `${num(q.score * 100, 2)}%` : DASH,
      source: 'quality_summary.json · passed / n',
    })
    + stat({
      label: '通过 / 总数', ic: 'flask',
      value: `${int(q.passed)} / ${int(q.n)}`, source: 'quality_summary.json 汇总',
    })
    + stat({ label: '评分模型', value: q.model || DASH, ic: 'cube', source: 'quality_summary.json · model' })
    + stat({ label: '生成于', value: q.generated || DASH, ic: 'clock', source: 'quality_summary.json · generated' }))
    + `<div style="margin-top:var(--sp-6)"><div class="chart-host" data-chart="q-rate"></div></div>`
    + `<div class="hint-row">评分方式：${esc(q.grades || DASH)}</div>`;
}

/* --------------------------------------------- section 6: cold start / smoke */

function coldStartCard(st) {
  if (st.detailErr) return errorState(st.detailErr, { retry: st.retryDetail });
  if (!st.detail) return card({ title: '冷启动', note: 'loadtest_summary.json', body: loading() });
  const l = st.detail.cold_start;
  if (!l) {
    return card({
      title: '冷启动', note: 'loadtest_summary.json',
      body: empty(`本次运行没有 loadtest_summary.json`
        + `（查找位置：data/benchmarks/${st.run}/loadtest_summary.json）。冷启动未产出。`,
        { title: '冷启动缺失', icon: 'bolt' }),
    });
  }
  return card({
    title: '冷启动', note: 'loadtest_summary.json',
    body: kv([
      ['阶段', l.phase || DASH],
      ['首 token', lat(l.first_output_s)],
      ['整轮耗时', lat(l.wall_s)],
      ['加载标记', lat(l.load_marker_s)],
      ['引擎解码', numOrNull(l.engine_decode_tok_s) !== null ? `${num(l.engine_decode_tok_s, 1)} tok/s` : DASH],
      ['显存峰值', numOrNull(l.vram_peak_mib) !== null ? `${gib(l.vram_peak_mib, 1)} GiB` : DASH],
      ['磁盘读取', bytes(l.disk_read_bytes)],
      ['采样点', int(l.samples)],
      ['引擎退出码', l.engine_rc != null ? String(l.engine_rc) : DASH],
    ]) + src('loadtest_summary.json'),
  });
}

function smokeCard(st) {
  if (st.detailErr) return errorState(st.detailErr, { retry: st.retryDetail });
  if (!st.detail) return card({ title: '接口冒烟', note: 'chat_smoke*.json', body: loading() });
  const s = st.detail.smoke;
  if (!s) {
    return card({
      title: '接口冒烟', note: 'chat_smoke*.json',
      body: empty(`本次运行没有 chat_smoke_keyed.json 或 chat_smoke.json`
        + `（查找位置：data/benchmarks/${st.run}/）。接口冒烟未产出。`,
        { title: '接口冒烟缺失', icon: 'bolt' }),
    });
  }
  const usage = s.usage || {};
  const tim = s.engine_last_timings || {};
  const con = s.concurrency || {};
  return card({
    title: '接口冒烟', note: 'chat_smoke*.json',
    body: kv([
      ['首内容 token', lat(s.ttft_first_content_token_s)],
      ['首个 token（任意）', lat(s.ttft_first_token_any_s)],
      ['端到端', lat(s.e2e_s)],
      ['输入 / 输出 token', `${int(usage.prompt_tokens)} / ${int(usage.completion_tokens)}`],
      ['推理 / 内容 chunk', `${int(s.reason_chunks)} / ${int(s.content_chunks)}`],
      ['引擎解码', numOrNull(tim.predicted_per_second) !== null ? `${num(tim.predicted_per_second, 1)} tok/s` : DASH],
      ['并发（请求 / 服务）', con.requested != null || con.serving != null
        ? `${int(con.requested)} / ${int(con.serving)}` : DASH],
    ]) + src('chat_smoke_keyed.json · usage / engine_last_timings')
      + (s.answer
        ? `<div style="margin-top:var(--sp-5)"><div class="stat-label">模型回答（节选）</div>`
          + `<div style="font-size:var(--fs-sm);color:var(--ink-3);white-space:pre-wrap;overflow-wrap:anywhere">`
          + `${esc(clip(s.answer, 400))}</div></div>`
        : ''),
  });
}

/* ---------------------------------------------- section 7: artifacts + export */

/** One row of the run's own file listing.  Both halves of the URL come from
 *  the server (the run id from the list, the relative path from the run
 *  document) -- nothing the user typed ever reaches /report, /file or
 *  /download. */
function fileRow(run, f) {
  const full = `data/benchmarks/${run}/${f.path}`;
  const enc = encodeURIComponent(full);
  const viewLink = f.ext === 'md'
    ? `<a class="btn btn-ghost btn-sm" href="/report?p=${enc}" target="_blank" rel="noopener noreferrer">预览</a>`
    : `<a class="btn btn-ghost btn-sm" href="/file?p=${enc}" target="_blank" rel="noopener noreferrer">原文</a>`;
  return [
    { __raw: true, value: `<span class="mono truncate" style="display:inline-block;max-width:46ch">${esc(f.path)}</span>` },
    esc(bytes(f.bytes)),
    { __raw: true, value: `<span class="mono">${esc(f.sha256 ? `${f.sha256.slice(0, 12)}…` : DASH)}</span>` },
    { __raw: true, value: `<div class="row-actions">${viewLink}`
      + `<a class="btn btn-ghost btn-sm" href="/download?p=${enc}">下载</a></div>` },
  ];
}

function exportBlock(st) {
  if (!st.run) return empty('请先选择一次运行。', { title: '无可导出内容', icon: 'download' });
  const run = st.run;
  const csv = CSV_KINDS.map((k) =>
    `<a class="btn btn-ghost btn-sm" download`
    + ` href="/api/export/csv?run=${encodeURIComponent(run)}&amp;kind=${esc(k)}"`
    + ` title="导出运行 ${esc(run)} 的「${esc(KIND_LABELS[k])}」表（CSV）">`
    + `${icon('download', '', 15)}导出 CSV · ${esc(KIND_LABELS[k])}</a>`).join('');
  const zip = `<a class="btn btn-ghost btn-sm" download`
    + ` href="/api/export/zip?run=${encodeURIComponent(run)}"`
    + ` title="打包下载运行 ${esc(run)} 的整个目录（ZIP）">`
    + `${icon('download', '', 15)}打包下载 · ZIP</a>`;

  const files = st.detail?.files;
  const ftable = Array.isArray(files) && files.length
    ? `<div style="margin-top:var(--sp-6)">`
      + table(['运行内文件', '大小', 'SHA-256', '操作'], files.map((f) => fileRow(run, f)), { num: [1] })
      + `</div>`
    : (!st.detail ? loading() : '');

  return `<div class="chips" style="gap:var(--sp-3)">${csv}${zip}</div>`
    + `<p class="hint-row">导出对象仅为当前选中的运行 <span class="mono">${esc(run)}</span>；`
    + `按钮的 tooltip 写明了 run id，下载前可先核对。</p>`
    + ftable;
}

/* --------------------------------------------------------------- the page */

function view(st) {
  if (st.listErr) {
    return band({ title: '运行', body: errorState(st.listErr, { retry: st.retryList }) });
  }
  if (!st.list) {
    return band({ title: '运行', body: skeleton(3, { block: true }) });
  }
  const runs = st.list.runs || [];
  if (!runs.length) {
    return band({
      title: '运行',
      body: empty('benchmarks/ 下还没有可解析的运行目录。先跑一次评测流程，这里会显示最新一次的口径与结果。',
        { title: '尚无评测记录', icon: 'flask' }),
    });
  }
  return band({
      title: '本次运行',
      sub: '覆盖口径来自 /api/evaluation；吞吐 / 延迟 / 质量 / 冷启动 / 冒烟来自本次运行目录的原始产物',
      tools: picker(runs, st.run),
      body: headlineBlock(st),
    })
    + band({ title: '吞吐 / 延迟', sub: '每个图一个 y 轴、一个单位；数据来自 bench_summary.json', body: throughputBlock(st) })
    + band({ title: '串行 vs 并发', sub: '同一批输入在串行与并发下的吞吐与首字延迟', body: concurrencyBlock(st) })
    + band({ title: '能力评分', sub: '按能力分类的通过率；数据来自质量评测汇总', body: qualityBlock(st) })
    + band({ title: '冷启动与接口冒烟', sub: '两项独立产物，缺失时各自说明', body: grid(2, coldStartCard(st) + smokeCard(st)) })
    + band({ title: '运行产物与导出', sub: '逐文件预览 / 下载，以及按表导出 CSV 或整包 ZIP', body: exportBlock(st) })
    + `<footer class="page-foot">
         <span>数据来源：<span class="mono">/api/benchmarks</span> · <span class="mono">/api/benchmarks/&lt;run&gt;</span> · <span class="mono">/api/evaluation</span></span>
         <span>${esc(st.run ? `运行 ${st.run}` : '')}</span>
       </footer>`;
}

/* --------------------------------------------------------------- painting */

/** Charts are drawn after the markup exists -- SVG needs a layout box, and the
 *  table twin is emitted by the chart helpers themselves. */
function drawCharts(host, st) {
  const d = st.detail;
  if (!d) return;

  const b = d.bench;
  if (b && Array.isArray(b.rows) && b.rows.length) {
    const cats = b.rows.map((r) => `${int(r.prompt_tokens)} tok`);
    paint.barChart(host.querySelector('[data-chart="tp-throughput"]'), {
      categories: cats,
      series: [
        { name: '预填充 tok/s', values: values(b.rows, 'prefill_tok_s') },
        { name: '解码 tok/s', values: values(b.rows, 'decode_tok_s') },
      ],
      unit: 'tok/s', fmtVal: (v) => num(v, 1),
      aria: '各输入长度下的解码与预填充吞吐（tok/s）',
    });
    paint.barChart(host.querySelector('[data-chart="tp-ttft"]'), {
      categories: cats,
      series: [{ name: '首字延迟 TTFT', values: values(b.rows, 'ttft_s') }],
      unit: 's', fmtVal: (v) => num(v, 2),
      aria: '各输入长度下的首字延迟（秒）',
    });
  }

  const c = d.concurrency;
  if (c && Array.isArray(c.rows) && c.rows.length) {
    const cats = c.rows.map((r) => `${int(r.size)} tok`);
    paint.barChart(host.querySelector('[data-chart="cc-throughput"]'), {
      categories: cats,
      series: [
        { name: '串行 tok/s', values: values(c.rows, 'seq_tok_s') },
        { name: '并发 tok/s', values: values(c.rows, 'conc_tok_s') },
      ],
      unit: 'tok/s', fmtVal: (v) => num(v, 1),
      aria: '串行与并发吞吐对比（tok/s）',
    });
    paint.barChart(host.querySelector('[data-chart="cc-ttft"]'), {
      categories: cats,
      series: [
        { name: '串行首字', values: values(c.rows, 'seq_ttft_s') },
        { name: '并发首字', values: values(c.rows, 'conc_ttft_s') },
      ],
      unit: 's', fmtVal: (v) => num(v, 2),
      aria: '串行与并发首字延迟对比（秒）',
    });
  }

  const q = d.quality;
  if (q && Array.isArray(q.categories) && q.categories.length) {
    const cats = q.categories.map((x) => x.category);
    paint.barChart(host.querySelector('[data-chart="q-rate"]'), {
      categories: cats,
      series: [{
        name: '通过率',
        values: q.categories.map((x) => (typeof x.n === 'number' && x.n > 0 && typeof x.passed === 'number')
          ? (x.passed / x.n) * 100 : null),
      }],
      unit: '%', fmtVal: (v) => `${num(v, 1)}%`,
      aria: '各能力分类的通过率（%）',
    });
  }
}

/* ---------------------------------------------------------------- render */

export async function render(host) {
  const scope = makeScope();
  let destroyed = false;
  let offTheme = null;

  const st = {
    list: null, listErr: null,
    evals: null, evalErr: null,
    detail: null, detailErr: null,
    run: hashRun(),
    retryList: null, retryDetail: null,
  };

  const paint = () => {
    if (destroyed) return;
    host.innerHTML = view(st);
    const sel = host.querySelector('#benchRun');
    sel?.addEventListener('change', (ev) => selectRun(ev.target.value));
    host.querySelector('#benchRefresh')?.addEventListener('click', () => reloadAll());
    drawCharts(host, st);
  };

  /** Load the run document for `st.run`; renew the scope so a rapid second
   *  pick aborts the first fetch instead of racing it into the same slots. */
  async function loadDetail() {
    st.detail = null;
    st.detailErr = null;
    if (!st.run) return;
    const sig = scope.renew();
    if (destroyed) return;
    try {
      st.detail = await get(`/api/benchmarks/${encodeURIComponent(st.run)}`, { signal: sig, timeout: 30000 });
    } catch (err) {
      if (isAborted(err) || destroyed) return;
      st.detailErr = err;
    }
  }

  async function selectRun(run) {
    if (!run || run === st.run) return;
    st.run = run;
    setHashRun(run);
    paint();                       // run-dependent bands fall back to skeletons
    await loadDetail();
    if (!destroyed) paint();
  }

  async function reloadAll() {
    st.listErr = null;
    st.evalErr = null;
    st.list = null;
    st.detail = null;
    st.detailErr = null;
    paint();
    let list;
    try {
      list = await get('/api/benchmarks', { signal: scope.signal, timeout: 20000 });
    } catch (err) {
      if (isAborted(err) || destroyed) return;
      st.listErr = err;
      paint();
      return;
    }
    if (destroyed) return;
    st.list = list;
    // The evaluation summary is a heavier probe and only supplies coverage;
    // its failure degrades section 2, it does not take the page down.
    try {
      st.evals = await get('/api/evaluation', { signal: scope.signal, timeout: 30000 });
    } catch (err) {
      if (!isAborted(err) && !destroyed) st.evalErr = err;
    }
    if (destroyed) return;

    const runs = st.list.runs || [];
    if (runs.length && (!st.run || !runs.some((r) => r.run === st.run))) {
      st.run = runs[0].run;        // newest first, per the server's ordering
      setHashRun(st.run);
    }
    if (st.run) await loadDetail();
    if (!destroyed) paint();
  }

  // Retry hooks the error states call back into.  Declared here so the first
  // paint already carries them.
  st.retryList = () => { reloadAll(); };
  st.retryDetail = async () => { paint(); await loadDetail(); if (!destroyed) paint(); };

  host.innerHTML = skeleton(3, { block: true });
  await reloadAll();
  if (destroyed) return { destroy() {} };

  offTheme = onThemeRepaint(() => paint());

  return {
    // The pollers belong to the shell; this view owns only its own one-shot
    // fetches, so leaving the page aborts them rather than letting a late
    // response write into a container that is gone.
    destroy() { destroyed = true; offTheme?.(); scope.abort(); },
  };
}
