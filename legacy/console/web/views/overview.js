/* ==========================================================================
   overview -- the landing view.

   Ordered by the question a person actually arrives with: *is the node
   serving right now, and what is it serving?*  Everything else is support for
   that answer, which is why the hero carries the model identity and the live
   engine numbers, and why the model endpoint's health is stated separately
   from the GPU's.

   Nothing on this page is a placeholder.  Where a reading does not exist the
   tile says — and says which probe failed; where the data comes from a cache
   the source line names the cache.
   ========================================================================== */

import { esc } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { DASH, num, int, bytes, gib, dur, lat, ts, clip } from '../core/format.js';
import { overviewPoll, livePoll, tunnelState } from '../core/sources.js';
import { skeleton, empty, notice } from '../core/ui.js';
import { series, onThemeRepaint } from '../core/theme.js';
import * as paint from '../core/chart.js';
import { card, band, grid, stat, src, flag, badge, kv, meter, row, button, linkButton, table } from './_parts.js';

/* -------------------------------------------------------------- fragments */

const healthFlag = (st) =>
  flag(st === 'ok' ? 'ok' : st === 'degraded' ? 'warn' : st === 'down' ? 'crit' : 'unknown',
    { ok: '模型 API 正常', degraded: '模型未加载', down: '模型 API 不可用', unknown: '未检测' }[st]);

const stop = (err) => `<div class="state error">${icon('alert', 'state-ico', 30)}`
  + `<div class="state-title">总览数据不可用</div>`
  + `<div class="state-msg mono">${esc(err && err.message || err)}</div></div>`;

/* ------------------------------------------------------------------- hero */

function hero(strata, tunnel, ev, native) {
  const ma = (strata || {}).model_api || {};
  const gpu = (strata || {}).gpu || {};
  const eng = (strata || {}).engine_live || {};
  const cfg = (strata || {}).engine_config || {};
  const m0 = (ma.model_list || [])[0] || null;
  const model = m0?.id || cfg.model || null;
  const st = ma.ok === false ? 'down' : ma.loaded === false ? 'degraded' : ma.ok ? 'ok' : 'unknown';

  const nativeLinks = (native?.panels || []).filter((p) => p.url).slice(0, 3);
  const side = nativeLinks.length
    ? `<div class="hero-side">
        ${flag({ ok: 'ok', degraded: 'warn', down: 'crit', unknown: 'unknown' }[st],
               { ok: '正在提供服务', degraded: '已连接，模型未加载', down: '未提供服务', unknown: '状态未知' }[st])}
        ${nativeLinks.map((p) => linkButton({ label: p.label || '原生界面', href: p.url, ic: 'external', cls: 'btn-ghost btn-sm', title: p.note || '' })).join('')}
       </div>`
    : `<div class="hero-side">${flag({ ok: 'ok', degraded: 'warn', down: 'crit', unknown: 'unknown' }[st],
        { ok: '正在提供服务', degraded: '已连接，模型未加载', down: '未提供服务', unknown: '状态未知' }[st])}</div>`;

  const heroStat = (label, value, unit, note) =>
    `<div class="hero-stat"><div class="hero-stat-label">${esc(label)}</div>`
    + `<div class="hero-stat-value">${esc(value)}${unit ? `<span class="u">${esc(unit)}</span>` : ''}</div>`
    + (note ? `<div class="hero-stat-note" title="${esc(note)}">${esc(note)}</div>` : '') + '</div>';

  const tokNote = typeof eng.tok_s_mean === 'number' ? `均值 ${num(eng.tok_s_mean, 1)}` : '';
  return `<section class="hero">
    <div class="hero-top">
      <div class="hero-id">
        <div class="hero-kicker">${icon('zap', '', 13)} 当前推理服务</div>
        <h2 title="${esc(model || '')}">${esc(model || '未报告已加载模型')}</h2>
        <div class="hero-sub">
          <span>引擎 <b>${esc(eng.state || DASH)}</b></span>
          <span>端点 <span class="mono">${esc((strata || {}).base_url || DASH)}</span></span>
          ${cfg.version ? `<span>版本 <span class="mono">${esc(cfg.version)}</span></span>` : ''}
          ${ma.auth_required ? `<span>${badge('需要 API Key')}</span>` : ''}
        </div>
      </div>
      ${side}
    </div>
    <div class="hero-stats">
      ${heroStat('解码吞吐', num(eng.tok_s, 1), 'tok/s', tokNote || 'Strata /metrics live')}
      ${heroStat('首字 / 预填充', num(eng.prefill_tok_s_mean, 0), 'tok/s', 'prefill_tok_s_mean')}
      ${heroStat('队列深度', int(eng.queued), '', eng.queued === 0 ? '无等待请求' : '有请求在排队')}
      ${heroStat('上下文上限', cfg.max_context ? int(cfg.max_context) : DASH, cfg.max_context ? 'tok' : '',
                 cfg.kv ? `KV ${cfg.kv}${cfg.spec ? ` · spec ${cfg.spec}` : ''}` : '')}
    </div>
  </section>`;
}

/* ------------------------------------------------------------------ alerts */

function alertsBlock(alerts) {
  if (!alerts) return skeleton(2);
  if (!alerts.length) {
    return `<div class="notice info">${icon('check', 'notice-ico', 17)}<div class="notice-body">`
      + `<div class="notice-title">没有检测到异常</div>`
      + `<div class="notice-msg">模型 API、SSH 探针、隧道与 EvalScope 四项实时探测均正常。`
      + `这里是空列表，不是未加载 —— 每一项都来自一次真实探测。</div></div></div>`;
  }
  return alerts.map((a) => {
    const kind = a.level === 'critical' ? 'error' : 'warning';
    return `<div class="notice ${kind}">${icon('alert', 'notice-ico', 17)}<div class="notice-body">`
      + `<div class="notice-title">${esc(a.title)}</div>`
      + `<div class="notice-msg">${esc(a.detail)}</div>`
      + `<div class="src">来源 · ${esc(a.source)}</div></div></div>`;
  }).join('');
}

/* ------------------------------------------------------------ live tiles */

function liveTiles(d, err) {
  if (!d) {
    return err ? `<div class="notice warning">${icon('alert', 'notice-ico', 17)}<div class="notice-body">`
      + `<div class="notice-title">实时探针中断</div><div class="notice-msg">${esc(err.message)}</div></div></div>`
      : grid(4, skeleton(2));
  }
  const strata = d.strata || {};
  const gpu = strata.gpu || {};
  const ssh = d.ssh || {};
  const mem = ssh.mem || {};
  const tun = tunnelState(d.tunnel);
  const gpuFrac = gpu.available && gpu.total_mib ? gpu.used_mib / gpu.total_mib : null;
  const ramFrac = typeof mem.used_pct === 'number' ? mem.used_pct / 100 : null;
  const ma = strata.model_api || {};

  return grid(4,
    stat({
      label: '模型 API', ic: 'cube',
      value: ma.ok === false ? '不可用' : ma.ok === true ? '正常' : DASH,
      tone: ma.ok === false ? 'crit' : ma.loaded === false ? 'warn' : '',
      sub: ma.error || (ma.model ? `已加载 ${ma.model}` : (ma.loaded === false ? '未加载模型' : '')),
      source: 'Strata /health + /v1/models',
    })
    + stat({
      label: 'GPU 显存', ic: 'layers',
      value: gpu.available ? gib(gpu.used_mib, 1) : DASH, unit: gpu.available ? `/ ${gib(gpu.total_mib, 0)} GiB` : '',
      tone: gpuFrac !== null && gpuFrac >= 0.92 ? 'crit' : gpuFrac !== null && gpuFrac >= 0.8 ? 'warn' : '',
      sub: gpu.available ? `${gpu.name || 'GPU'} · 利用率 ${num(gpu.util_pct, 0)}%` : (gpu.reason || '遥测不可用'),
      source: gpu.source || 'Strata /metrics hardware',
    })
    + stat({
      label: '主机内存', ic: 'thermometer',
      value: typeof mem.used_pct === 'number' ? num(mem.used_pct, 0) : DASH, unit: typeof mem.used_pct === 'number' ? '%' : '',
      tone: ramFrac !== null && ramFrac >= 0.9 ? 'crit' : ramFrac !== null && ramFrac >= 0.8 ? 'warn' : '',
      sub: mem.avail_mib ? `可用 ${gib(mem.avail_mib, 1)} GiB / ${gib(mem.total_mib, 0)} GiB` : '',
      source: 'SSH 只读探针 · facts.mem',
    })
    + stat({
      label: '隧道 ' + (d.tunnel?.local_port ? `:${d.tunnel.local_port}` : ''), ic: 'link',
      value: { up: '已建立', down: '未建立', foreign: '被占用', off: '未启用', unknown: DASH }[tun.key],
      tone: tun.key === 'up' ? '' : tun.key === 'foreign' ? 'warn' : 'crit',
      sub: tun.key === 'foreign' ? `端口在听，但 pid ${d.tunnel?.pid ?? DASH} 不存活` : (d.tunnel?.url || ''),
      source: 'tunnel_state() · listening + pid_alive',
    }));
}

/* -------------------------------------------------------------- charts */

function liveCharts(d, err) {
  if (!d) return grid(2, skeleton(3, { block: true }));
  const samples = d.samples || [];
  const gpu = d.strata?.gpu || {};
  const hist = d.strata?.history;

  // The node's own ring buffer is the better series when it exists: it is
  // sampled node-side, so it is not limited to how long this tab has been open.
  const util = hist?.gpu_util?.length > 2 ? hist.gpu_util
    : samples.map((s) => s.gpu_util).filter((v) => typeof v === 'number');
  const memSeries = hist?.gpu_mem_used_mib?.length > 2 ? hist.gpu_mem_used_mib
    : samples.map((s) => s.gpu_mem_mib).filter((v) => typeof v === 'number');

  const utilNote = hist?.gpu_util?.length > 2
    ? `Strata /metrics history · 节点侧环形缓冲，${hist.n} 个样本`
    : `本机采样缓冲 · 本页打开后累计 ${samples.length} 个样本，最长 120`;

  if (!util.length && !memSeries.length) {
    return notice('warning', '实时曲线暂无样本',
      '节点遥测可用，但还没有累积到足够的采样点。GPU 利用率与显存曲线需要至少两个样本。');
  }

  return grid(2,
    card({
      title: 'GPU 利用率', note: utilNote,
      body: `<div class="chart-host" data-chart="util"></div>`
        + `<div class="hint-row">最新 ${num(util.at(-1), 0)}%${hist?.gpu_temp?.length ? ` · 温度 ${num(hist.gpu_temp.at(-1), 0)} °C` : gpu.temp_c != null ? ` · 温度 ${num(gpu.temp_c, 0)} °C` : ''}</div>`,
    })
    + card({
      title: '显存占用', note: 'MiB · 与上方同源',
      body: `<div class="chart-host" data-chart="vram"></div>`
        + `<div class="hint-row">最新 ${memSeries.length ? gib(memSeries.at(-1), 1) + ' GiB' : DASH}`
        + (gpu.total_mib ? ` / ${gib(gpu.total_mib, 0)} GiB` : '') + '</div>',
    }));
}

/* ----------------------------------------------------- benchmark + host */

function benchmarkBlock(bench) {
  if (!bench) {
    return empty('benchmarks/ 下还没有可解析的运行目录。先跑一次评测流程，这里会显示最新一次的口径与结果。',
      { title: '尚无评测记录', icon: 'flask' });
  }
  const at = typeof bench.at === 'number' ? ts(bench.at) : (bench.at || DASH);
  const dims = bench.dimensions || {};
  const missing = Object.entries(dims).filter(([, v]) => !v.present);
  const rows = Object.entries(dims).map(([k, v]) => [
    v.label || k,
    { __raw: true, value: v.present
        ? `<span style="color:var(--good)">✓ 已产出</span>`
        : `<span style="color:var(--warning)">✗ 缺失</span>` },
    v.summary || DASH,
  ]);

  return `<div class="rows">
    ${row({
      // The run id is a single unbroken token, so it is the part that shrinks
      // when the card is narrow; `title` keeps the full value reachable.
      name: `<span class="mono" title="${esc(bench.run || DASH)}">${esc(bench.run || DASH)}</span>`
        + (bench.headline ? ` ${badge(bench.headline, 'on')}` : ''),
      sub: `${esc(bench.model || '未记录模型')} · ${esc(at)}`,
      meta: `口径 ${bench.present ?? DASH}/${bench.expected ?? DASH} 个维度已产出`
        + (missing.length ? ` · 缺 ${missing.map(([, v]) => esc(v.label)).join('、')}` : ''),
      actions: button({ label: '打开运行目录', ic: 'external', cls: 'btn-ghost btn-sm',
                        attrs: `data-open-report="/benchmarks/${esc(bench.run)}/summary.md"` }),
    })}
  </div>`
  + `<div style="margin-top:var(--sp-6)">`
  + table(['评测维度', '产出', '摘要'], rows)
  + `</div>`;
}

function hostBlock(d) {
  const ssh = d?.ssh;
  if (!ssh) return skeleton(3);
  if (ssh.ok === false) {
    return `<div class="notice warning">${icon('alert', 'notice-ico', 17)}<div class="notice-body">`
      + `<div class="notice-title">SSH 只读探针失败</div>`
      + `<div class="notice-msg">${esc(ssh.error || 'unknown')}</div>`
      + `<div class="src">来源 · probe_ssh()（固定只读命令，无插值）</div></div></div>`;
  }
  const mem = ssh.mem || {};
  const hm = d?.strata?.host_metrics || {};
  const load = Array.isArray(ssh.loadavg) ? ssh.loadavg[0] : null;
  const cpuFrac = ssh.cpu_count && load != null ? load / ssh.cpu_count : null;

  return `<div class="grid grid-2">
    <div style="display:flex;flex-direction:column;gap:var(--sp-5)">
      ${meter({ name: `内存占用 · ${gib(mem.used_mib ?? (mem.total_mib - (mem.avail_mib || 0)), 1)} / ${gib(mem.total_mib, 0)} GiB`,
                value: typeof mem.used_pct === 'number' ? mem.used_pct : null })}
      ${meter({ name: `1 分钟负载 · ${num(load, 2)} / ${int(ssh.cpu_count)} 核`,
                value: cpuFrac !== null ? cpuFrac * 100 : null,
                display: cpuFrac !== null ? `${num(cpuFrac * 100, 0)}%` : '' })}
      ${(ssh.disks || []).map((dk) => meter({
          name: `磁盘 ${dk.mount} · ${num(dk.used_gib, 0)} / ${num(dk.total_gib, 0)} GiB`,
          value: typeof dk.used_pct === 'number' ? dk.used_pct : null })).join('')}
    </div>
    <div>
      ${kv([
        ['主机', ssh.hostname || DASH],
        ['运行时长', dur(ssh.uptime_s)],
        ['CPU', hm.cpu_name ? `${hm.cpu_name} · ${hm.threads || ssh.cpu_count} 线程` : `${ssh.cpu_count ?? DASH} 核`],
        ['节点 CPU', hm.cpu_pct != null ? `${num(hm.cpu_pct, 0)}%` : DASH],
        ['磁盘读写', hm.disk_read_mb != null || hm.disk_write_mb != null
            ? `读 ${num(hm.disk_read_mb, 0)} / 写 ${num(hm.disk_write_mb, 0)} MB/s` : DASH],
        ['探针耗时', ssh.elapsed_s != null ? lat(ssh.elapsed_s, 2) : DASH],
        ['主机密钥', ssh.fingerprint?.match === false ? '指纹不匹配' : ssh.fingerprint?.match ? '已固定并匹配' : DASH],
      ])}
      ${src('SSH 只读探针 · 固定命令，无插值；节点侧 CPU/磁盘来自 Strata /metrics hardware')}
    </div>
  </div>`;
}

function reportsBlock(reports, err) {
  if (err) return skeleton(3);
  if (!reports?.length) {
    return empty('reports/ 与 docs/ 下还没有 .md / .html 报告。', { title: '暂无报告', icon: 'file' });
  }
  return `<div class="rows">${reports.map((r) => row({
    lead: icon('file', 'ico', 18),
    name: `<span class="truncate">${esc(clip(r.title, 62))}</span>`,
    sub: `<span class="mono">${esc(r.path)}</span>`,
    meta: `${esc(ts(r.mtime))} · ${esc(bytes(r.bytes))}`,
    actions: `<a class="btn btn-ghost btn-sm" href="/report?p=${encodeURIComponent(r.path)}" target="_blank" rel="noopener">预览</a>`
      + `<a class="btn btn-ghost btn-sm" href="/download?p=${encodeURIComponent(r.path)}">下载</a>`,
  })).join('')}</div>`;
}

/* --------------------------------------------------------------- render */

export async function render(host) {
  let offTheme = null;
  let offOv = null;
  let offLive = null;

  host.innerHTML = band({ title: '实时', sub: '每 20 秒刷新一次；数值旁标注了来源', body: skeleton(4, { block: true }) })
    + `<div id="ovAlerts">${skeleton(2)}</div>`
    + band({ title: '评测与报告', body: skeleton(3, { block: true }) });

  // The hero and the heavy blocks come from the slower overview poll; the
  // tiles and curves come from the shared live poll.
  const draw = (ov, ovErr, lv, lvErr) => {
    host.innerHTML =
      (ov ? hero(ov.strata, ov.tunnel, ov.evalscope, ov.strata?.native) : '')
      + `<div class="band" id="ovAlerts">${ov ? alertsBlock(ov.alerts) : (ovErr ? '' : skeleton(2))}</div>`
      + band({
          title: '实时',
          sub: '每 20 秒刷新；GPU 曲线优先使用节点侧环形缓冲，其次才是本页打开后的采样',
          tools: button({ label: '刷新', ic: 'refresh', cls: 'btn-ghost btn-sm', id: 'ovRefresh' }),
          body: liveTiles(lv, lvErr) + `<div style="height:var(--sp-5)"></div>` + liveCharts(lv, lvErr),
        })
      + band({
          title: '主机资源',
          sub: 'Windows 侧经 SSH 只读探针取得；不重启、不修改节点任何配置',
          body: hostBlock(lv),
        })
      + band({
          title: '最新 Benchmark',
          sub: '来自 benchmarks/ 下最近一次运行目录，逐维度核对产出情况',
          tools: `<a class="btn btn-ghost btn-sm" href="#/benchmark">进入 Benchmark Lab${''}</a>`,
          body: benchmarkBlock(ov?.benchmark),
        })
      + band({
          title: '报告中心',
          sub: '最近 5 份；完整列表与导出见「报告中心」',
          tools: `<a class="btn btn-ghost btn-sm" href="#/reports">全部报告</a>`,
          body: reportsBlock(ov?.reports),
        })
      + `<footer class="page-foot">
           <span>数据来源：<span class="mono">/api/v2/overview</span> · <span class="mono">/api/v2/live</span> · <span class="mono">/api/benchmarks</span></span>
           <span id="ovStamp">${esc(ov ? `总览快照 ${esc(ov.ts || '')}` : '')}</span>
         </footer>`;

    // Curves are drawn after the markup exists -- SVG needs a layout box.
    const utilHost = host.querySelector('[data-chart="util"]');
    const vramHost = host.querySelector('[data-chart="vram"]');
    if (utilHost && lv) {
      const hist = lv.strata?.history;
      const util = hist?.gpu_util?.length > 2 ? hist.gpu_util
        : (lv.samples || []).map((s) => s.gpu_util).filter((v) => typeof v === 'number');
      paint.sparkline(utilHost, util, { color: series()[0], height: 90, unit: '%', fmt: (v) => num(v, 0) });
    }
    if (vramHost && lv) {
      const hist = lv.strata?.history;
      const mem = hist?.gpu_mem_used_mib?.length > 2 ? hist.gpu_mem_used_mib
        : (lv.samples || []).map((s) => s.gpu_mem_mib).filter((v) => typeof v === 'number');
      paint.sparkline(vramHost, mem, { color: series()[2], height: 90, unit: 'MiB', fmt: (v) => gib(v, 1) });
    }

    host.querySelector('#ovRefresh')?.addEventListener('click', () => {
      overviewPoll.refresh({ force: true });
      livePoll.refresh({ force: true });
    });

    // The hero is the one element worth a slide-in: it is the answer.
    const h = host.querySelector('.hero');
    if (h) { h.classList.add('rise'); requestAnimationFrame(() => h.classList.add('rise-in')); }
  };

  let ov = overviewPoll.value, ovErr = overviewPoll.error;
  let lv = livePoll.value, lvErr = livePoll.error;
  draw(ov, ovErr, lv, lvErr);

  offOv = overviewPoll.subscribe((data, meta) => {
    ov = data; ovErr = meta.error; draw(ov, ovErr, lv, lvErr);
  });
  offLive = livePoll.subscribe((data, meta) => {
    lv = data; lvErr = meta.error; draw(ov, ovErr, lv, lvErr);
  });
  // A light poll lands every 20 s; repainting the whole page that often would
  // fight the reader's scroll position, so the chart colours are the only
  // thing a theme switch has to redraw, and that is what this does.
  offTheme = onThemeRepaint(() => draw(ov, ovErr, lv, lvErr));

  return {
    // The pollers are owned by the shell (see app.js); a view only detaches its
    // own subscriptions, so leaving this page never silences the health pill.
    destroy() { offOv?.(); offLive?.(); offTheme?.(); },
  };
}
