/* ==========================================================================
   models -- 模型与服务.

   The question this page answers is narrower than the landing view's: *what is
   being served right now, by which engine, on which endpoint* -- and, second,
   which endpoints exist in configuration but were never probed here.

   That second half is why the page keeps two lists apart.  This console probes
   exactly one endpoint for liveness (the node's Strata API, via
   /api/v2/overview); every other endpoint in config is a *configuration fact*,
   not a health fact, and is drawn as one.  A card that said "运行中" for a
   gateway nobody queried would be the single most misleading thing on the page.

   Nothing is seeded: a series with too few points renders an honest empty, and
   a fast but empty model list says why it is empty.
   ========================================================================== */

import { esc } from '../core/dom.js';
import { DASH, num, int, gib, ts, clip } from '../core/format.js';
import { overviewPoll, labPoll } from '../core/sources.js';
import { skeleton, empty, notice } from '../core/ui.js';
import { series, onThemeRepaint } from '../core/theme.js';
import { lineChart, sparkline } from '../core/chart.js';
import { card, band, grid, src, flag, badge, kv, stat, table } from './_parts.js';

/* -------------------------------------------------------------- fragments */

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** A credential statement, never the credential.  `key_present` is a boolean
 *  the server computes; a false is stated in words, not left as a blank. */
const keyChip = (present) =>
  present === true ? badge('凭据已就绪', 'on')
    : present === false ? badge('未找到凭据', 'warn')
      : badge('凭据状态未知');

/** Whether the node demands a key is its own fact (model_api.auth_required). */
const authChip = (required) =>
  required === true ? badge('需要 API Key', 'warn')
    : required === false ? badge('无需 API Key')
      : badge('鉴权未知');

/** The row of badges that heads every service card. */
const chipsRow = (parts) => `<div class="chips">${parts.filter(Boolean).join(' ')}</div>`;

/** The node's endpoint entry from /api/lab, matched by key -- the live console
 *  payload's base_url is the tunnel URL when the tunnel is up, so it does not
 *  equal the configured one and cannot be the join key. */
function nodeEndpoint(lab) {
  const eps = lab?.endpoints;
  if (!Array.isArray(eps)) return null;
  return eps.find((e) => e && e.key === 'strata-local')
    || eps.find((e) => e && typeof e.key === 'string' && /strata/i.test(e.key))
    || null;
}

/* ---------------------------------------------------------- service cards */

/** The one endpoint this console actually queries, drawn from live evidence. */
function servingCard(strata, ep) {
  const ma = strata.model_api || {};
  const eng = strata.engine_live || {};
  const models = Array.isArray(ma.model_list) ? ma.model_list : [];
  const primary = models[0] || null;
  const modelId = primary?.id || ma.model || null;

  // Three independent facts, kept apart: reachability (ok), load state
  // (loaded), and the key requirement (auth_required).  Folding them into one
  // word is how a credential problem gets read as an outage.
  const down = ma.ok === false;
  const state = down ? 'crit' : ma.loaded === false ? 'warn' : ma.ok ? 'ok' : 'unknown';
  const stateWord = {
    crit: '未能提供服务', warn: '已连接，但模型未加载', ok: '正在提供服务', unknown: '状态未知',
  }[state];

  const chips = chipsRow([
    flag(state, stateWord),
    ep ? badge(ep.enabled ? '配置为启用' : '配置为停用', ep.enabled ? 'on' : 'off') : '',
    keyChip(ep ? ep.key_present : strata.key_present),
    authChip(ma.auth_required),
  ]);

  const ctxLine = primary
    ? `<div class="hint-row">状态 <b>${esc(primary.status || DASH)}</b>`
      + ` · 上下文 ${primary.context != null ? `${int(primary.context)} tok` : DASH}</div>`
    : '';

  return card({
    title: ep ? (ep.label || ep.key) : '推理服务',
    note: (ep && ep.kind) || 'Strata 节点端点',
    body: chips
      // The model id is the headline, so it gets the big readout -- in a
      // truncating wrapper, because a quantised id is long enough to push a
      // phone layout sideways.
      + `<div class="stat-value" style="margin-top:var(--sp-5)">`
      + `<span class="truncate" title="${esc(modelId || '')}">${esc(modelId || '未报告已加载模型')}</span></div>`
      + ctxLine
      + `<div style="height:var(--sp-5)"></div>`
      + grid(3,
        stat({ label: '解码吞吐', value: num(eng.tok_s, 1), unit: 'tok/s', source: 'Strata /metrics live' })
        + stat({ label: '队列深度', value: int(eng.queued), sub: eng.queued === 0 ? '无等待请求' : '', source: 'Strata /metrics live' })
        + stat({ label: '已加载模型', value: models.length ? int(models.length) : DASH, source: 'Strata /v1/models' }))
      + `<div style="height:var(--sp-5)"></div>`
      + kv([
        ['端点', strata.base_url || DASH],
        ['引擎状态', eng.state || DASH],
        ['密钥指纹', strata.key_sha256_12 || DASH],
      ])
      + `<div style="margin-top:var(--sp-4)">${src('Strata /health + /v1/models + /metrics live')}</div>`,
  });
}

/**
 * An endpoint that exists in configuration but is not probed by this console.
 * It is deliberately *not* given a health colour: the only true statement here
 * is that the portal has no measurement, and "未探测" is that statement.
 */
function configCard(ep) {
  return card({
    title: ep.label || ep.key,
    note: ep.kind || '',
    body: chipsRow([
      badge(ep.enabled ? '已启用' : '已停用', ep.enabled ? 'on' : 'off'),
      keyChip(ep.key_present),
      flag('unknown', '未探测实时健康'),
    ])
      + `<div style="height:var(--sp-4)"></div>`
      + kv([
        ['地址', ep.base_url || DASH],
        ['部署', ep.deployment || DASH],
        ['说明', ep.note || DASH],
      ])
      + `<div style="margin-top:var(--sp-3)"><span class="src">本控制台只对节点端点做实时探测；该端点的可用性需在其自身界面确认。</span></div>`,
  });
}

function serviceBlock(strata, lab, err) {
  if (!strata) {
    return err
      ? notice('warning', '服务数据不可用', err.message)
      : grid(2, skeleton(5, { block: true }) + skeleton(5, { block: true }));
  }
  const node = nodeEndpoint(lab);
  const others = (lab?.endpoints || []).filter((e) => e && e !== node && e.enabled);
  return grid(2, servingCard(strata, node) + others.map(configCard).join(''));
}

/* ------------------------------------------------------------ model list */

/** A node-reported status shown as dot + word.  Green only for the one exact
 *  value we know means "loaded"; anything else stays muted rather than being
 *  guessed at. */
function statusFlag(s) {
  if (s === null || s === undefined || s === '') return DASH;
  const t = String(s).toLowerCase();
  const loaded = t === 'loaded' || t === 'ready' || t === 'ok';
  return flag(loaded ? 'ok' : 'unknown', String(s));
}

function modelsBlock(strata) {
  if (!strata) return skeleton(4);
  const ma = strata.model_api || {};
  const list = Array.isArray(ma.model_list) ? ma.model_list : null;

  // An empty list is a measurement, not a rendering slip: say which probe
  // produced it and the two reasons it is usually empty, rather than printing
  // a header row over nothing.
  if (!list || !list.length) {
    return notice('warning', '未返回已加载模型列表',
      (ma.error ? `Strata 报告：${ma.error}。` : '')
      + '该块由 Strata /v1/models 提供。端点要求 API Key 而本地未配置凭据时，此处也会为空。')
      + `<div style="margin-top:var(--sp-3)">${src('Strata /v1/models · model_list')}</div>`;
  }

  const rows = list.map((m) => [
    { __raw: true, value: `<span class="mono truncate" style="display:inline-block;max-width:42ch">${esc(m.id || DASH)}</span>` },
    { __raw: true, value: statusFlag(m.status) },
    m.context != null ? `${int(m.context)} tok` : DASH,
    ma.source || 'Strata /v1/models',
  ]);
  return table(['模型 id', '状态', '上下文', '来源'], rows, { num: [2] });
}

/* -------------------------------------------------------- engine config */

function engineConfigBlock(strata) {
  if (!strata) return skeleton(4);
  const cfg = strata.engine_config;
  if (!cfg) {
    return notice('warning', '引擎配置不可用',
      'Strata /metrics 未返回 engine 段。该块需要节点侧 /metrics 提供 engine 配置；缺失时不会用默认值填充任何一栏。');
  }
  // kv() already turns null / '' into —, so raw values can be passed for the
  // free-text fields; the numeric ones are given a unit only when present.
  return kv([
    ['model', cfg.model],
    ['version', cfg.version],
    ['max_context', cfg.max_context != null ? `${int(cfg.max_context)} tok` : DASH],
    ['kv', cfg.kv],
    ['spec', cfg.spec],
    ['mtp_max', cfg.mtp_max != null ? int(cfg.mtp_max) : DASH],
    ['lookup', cfg.lookup],
    ['expert_slots', cfg.expert_slots != null ? int(cfg.expert_slots) : DASH],
    ['arena_mib', cfg.arena_mib != null ? `${int(cfg.arena_mib)} MiB` : DASH],
    ['pool_workers', cfg.pool_workers != null ? int(cfg.pool_workers) : DASH],
    ['vram_free_mib', cfg.vram_free_mib != null ? `${gib(cfg.vram_free_mib, 1)} GiB` : DASH],
  ])
    + `<div style="margin-top:var(--sp-4)">${src('Strata /metrics engine')}</div>`;
}

/* ---------------------------------------------------------- throughput */

function fmtAt(at) {
  if (at === null || at === undefined || at === '') return DASH;
  if (typeof at === 'number') return ts(at > 1e12 ? at / 1000 : at);
  return clip(String(at), 24);
}

const reusedWord = (v) => {
  if (v === null || v === undefined) return DASH;
  if (v === true) return '已复用';
  if (v === false) return '未复用';
  return Number(v) > 0 ? '已复用' : '未复用';
};

function requestsTable(strata) {
  const reqs = Array.isArray(strata?.requests) ? strata.requests : [];
  if (!reqs.length) {
    return `<div class="hint-row">strata.requests 为空 —— 节点尚未记录到完成的请求，或 /metrics 未提供 requests 段。</div>`;
  }
  const rows = reqs.slice(0, 10).map((r) => [
    fmtAt(r.at),
    r.decode_tok_s != null ? num(r.decode_tok_s, 1) : DASH,
    r.output_tokens != null ? int(r.output_tokens) : DASH,
    reusedWord(r.reused),
  ]);
  return table(['时间', '解码速度 (tok/s)', '输出 tokens', 'prompt 复用'], rows, { num: [1, 2] });
}

/** The chart is chosen by how much series there is, not by what looks best:
 *  a line needs more than two points, a sparkline needs two, and neither is
 *  faked when the ring buffer is still filling. */
function paintThroughput(host, strata) {
  const tok = ((strata?.history || {}).tok_s || []).filter(isNum);
  if (tok.length > 2) {
    lineChart(host, {
      categories: tok.map((_, i) => `#${i + 1}`),
      series: [{ name: '解码吞吐', values: tok, color: series()[0] }],
      unit: 'tok/s',
      aria: '解码吞吐 tok/s 时间序列',
      fmtVal: (v) => num(v, 1),
      fmtAxis: (v) => num(v, 0),
      note: `${tok.length} 个样本`,
    });
    return;
  }
  if (tok.length >= 2) {
    sparkline(host, tok, { color: series()[0], height: 90, unit: 'tok/s', fmt: (v) => num(v, 1) });
    return;
  }
  host.className = 'chart-host';
  host.innerHTML = empty('Strata /metrics 的 history.tok_s 还没有累积到足够的采样点（需要多于 2 个）。该序列由节点侧环形缓冲提供。',
    { title: '吞吐曲线暂无样本', icon: 'activity' });
}

function throughputBlock(strata) {
  if (!strata) return skeleton(3, { block: true });
  const tok = ((strata.history || {}).tok_s || []).filter(isNum);
  const note = tok.length > 2 ? 'Strata /metrics history · 节点侧环形缓冲'
    : tok.length === 2 ? '样本少于 3 个，改画走势线'
      : '尚无足够样本';
  return card({
    title: '解码吞吐',
    note,
    body: `<div class="chart-host" data-chart="tok"></div>`,
  })
    + `<div style="height:var(--sp-6)"></div>`
    + `<h4 style="font-size:var(--fs-md);margin-bottom:var(--sp-3)">最近请求</h4>`
    + requestsTable(strata);
}

/* ---------------------------------------------------------- endpoints */

function endpointsBlock(lab) {
  if (!lab) return skeleton(4);
  const eps = lab.endpoints || [];
  if (!eps.length) {
    return empty('配置中没有任何 [endpoints.*] 段。', { title: '没有配置端点', icon: 'plug' });
  }
  const rows = eps.map((e) => [
    { __raw: true, value: `<span class="truncate" style="display:inline-block;max-width:22ch" title="${esc(e.key || '')}">${esc(e.label || e.key || DASH)}</span>`
      + `<div class="mono" style="font-size:var(--fs-2xs);color:var(--ink-faint)">${esc(e.key || DASH)}</div>` },
    e.kind || DASH,
    { __raw: true, value: `<span class="mono truncate" style="display:inline-block;max-width:34ch">${esc(e.base_url || DASH)}</span>` },
    { __raw: true, value: badge(e.enabled ? '启用' : '停用', e.enabled ? 'on' : 'off') },
    { __raw: true, value: keyChip(e.key_present) },
    e.note ? clip(e.note, 60) : DASH,
  ]);
  return table(['端点', '类型', '地址', '启用', '凭据', '说明'], rows);
}

/* --------------------------------------------------------------- render */

export async function render(host) {
  let offOv = null;
  let offLab = null;
  let offTheme = null;
  let ov = overviewPoll.value;
  let ovErr = overviewPoll.error;
  let lab = labPoll.value;

  const draw = () => {
    const strata = ov?.strata;
    host.innerHTML =
      band({
        title: '服务端点',
        sub: '本控制台实时探测的端点排在前面；其余是配置事实，未做探测，不表示其运行状态',
        body: serviceBlock(strata, lab, ovErr),
      })
      + band({
        title: '已加载模型',
        sub: '来自 Strata /v1/models 的 model_list，逐条标注节点报告的状态',
        body: modelsBlock(strata),
      })
      + band({
        title: '引擎配置',
        sub: 'Strata /metrics 的 engine 段；缺失字段显示 —，不用默认值填充',
        body: engineConfigBlock(strata),
      })
      + band({
        title: '实时吞吐',
        sub: '优先使用节点侧环形缓冲；样本不足时降级为走势线，再不足则如实说明',
        body: throughputBlock(strata),
      })
      + band({
        title: '端点清单',
        sub: '/api/lab 的 endpoints；credential 只显示有无与指纹，绝不显示内容',
        body: endpointsBlock(lab),
      })
      + `<footer class="page-foot">
           <span>数据来源：<span class="mono">/api/v2/overview</span> · <span class="mono">/api/lab</span></span>
           <span>${esc(strata?.at ? `节点快照 ${strata.at}` : '')}</span>
         </footer>`;

    // Charts are drawn after the markup exists -- SVG needs a layout box.
    const tokHost = host.querySelector('[data-chart="tok"]');
    if (tokHost && strata) paintThroughput(tokHost, strata);
  };

  draw();

  offOv = overviewPoll.subscribe((d, meta) => { ov = d; ovErr = meta.error; draw(); });
  offLab = labPoll.subscribe((d) => { lab = d; draw(); });
  // A theme switch re-reads the palette from the stylesheet, so the SVG has to
  // be redrawn rather than restyled.
  offTheme = onThemeRepaint(() => draw());

  return {
    // The pollers are owned by the shell (see app.js); this view only detaches
    // its own subscriptions.
    destroy() { offOv?.(); offLab?.(); offTheme?.(); },
  };
}
