/* ==========================================================================
   compute -- 计算与监控.

   This page's job is GPU + host telemetry, and -- just as much -- *where each
   number came from*.  Two probes answer for the GPU (Strata /metrics and
   /v1/status) and disagreeing by a few percent is normal; a chart that shows
   the gap is more useful than one that silently picks a side.  The host half
   is a different machine's answer entirely (an SSH probe of the Windows node),
   so the two are never mixed into one "load" figure.

   The one trap this page exists to avoid: a GPU with dead NVML is not a GPU
   with no data.  nvidia-smi fails here, and the node's own /metrics hardware
   section reads the card through the CUDA runtime and reports everything --
   so the unavailability branch says that out loud instead of leaving a reader
   to conclude the hardware is unreadable.
   ========================================================================== */

import { esc } from '../core/dom.js';
import { DASH, num, int, gib, dur, lat, clip } from '../core/format.js';
import { overviewPoll, livePoll, labPoll, SERVICE_LABEL, SERVICE_TONE,
         gpuState, vramFrac, pressureTone } from '../core/sources.js';
import { skeleton, notice } from '../core/ui.js';
import { series, onThemeRepaint } from '../core/theme.js';
import { gauge, lineChart } from '../core/chart.js';
import { card, band, grid, stat, src, flag, badge, kv, meter, table } from './_parts.js';

const isNum = (v) => typeof v === 'number' && Number.isFinite(v);

/** Temperature and power are not utilisation, so they get their own bands
 *  rather than borrowing pressureTone's 78/92% thresholds. */
const tempTone = (c) => !isNum(c) ? '' : c >= 85 ? 'crit' : c >= 75 ? 'warn' : '';
const powerTone = (w, lim) => (isNum(w) && isNum(lim) && lim && w / lim >= 0.95) ? 'warn' : '';

/** The fact a reader would otherwise get wrong, stated once and reused. */
const NVML_NOTE = '本节点 nvidia-smi 因 NVML 驱动 / 库版本不匹配而失败，但这不是故障本身：'
  + 'GPU 实时数据由 Strata /metrics hardware 经 CUDA 运行时提供（利用率、温度、显存、功耗齐全）。'
  + '因此 GPU 并非不可读 —— 请以每处标注的 source 为准。';

/* --------------------------------------------------------------- GPU */

function crossCheck(cross) {
  if (!cross) return '';
  const d = cross.delta_pct;
  const warn = isNum(d) && d > 5;
  return `<div style="margin-top:var(--sp-4);display:flex;align-items:center;gap:var(--sp-3);flex-wrap:wrap">`
    + `<span class="src" style="margin:0;color:${warn ? 'var(--warning)' : 'var(--ink-muted)'}">`
    + `双源核对 · /metrics ${esc(num(cross.metrics_used_mib, 0))} MiB vs `
    + `/v1/status ${esc(num(cross.status_used_mib, 0))} MiB，偏差 ${esc(num(d, 1))}%</span>`
    + (warn ? flag('warn', '偏差超过 5%') : '')
    + `</div>`;
}

function gpuBlock(strata, err) {
  if (!strata) {
    return err ? notice('warning', 'GPU 数据不可用', err.message)
      : skeleton(4, { block: true });
  }
  const gpu = strata.gpu || {};

  // Unavailable means "not measured".  A gauge drawn from a missing reading
  // would show 0% and claim a measurement that was never taken.
  if (gpu.available === false) {
    return notice('warning', 'GPU 遥测不可用', gpu.reason || 'gpu.available = false')
      + `<div class="hint-row">来源 · ${esc(gpu.source || '无可用来源')}</div>`
      + `<div style="margin-top:var(--sp-4)">${notice('info', '为什么 GPU 仍然可读', NVML_NOTE)}</div>`;
  }

  const used = gpu.used_mib;
  const total = gpu.total_mib;
  const frac = vramFrac(gpu);
  const utilFrac = isNum(gpu.util_pct) ? gpu.util_pct / 100 : null;
  const stateWord = { ok: '可用', offline: '不可用', unknown: DASH }[gpuState(gpu)];

  return card({
    title: 'GPU 遥测',
    note: gpu.name || gpu.source || '',
    body: `<div class="grid grid-2" style="align-items:center">`
      + `<div><div class="chart-host" data-gauge="vram"></div>`
      + `<div class="hint-row">已用 ${gib(used, 1)} / ${gib(total, 0)} GiB`
      + (frac !== null ? ` · ${num(frac * 100, 0)}%` : '') + `</div></div>`
      + grid(2,
        stat({ label: '利用率', value: num(gpu.util_pct, 0), unit: isNum(gpu.util_pct) ? '%' : '',
               tone: pressureTone(utilFrac), source: 'Strata /metrics hardware' })
        + stat({ label: '温度', value: num(gpu.temp_c, 0), unit: isNum(gpu.temp_c) ? '°C' : '',
                 tone: tempTone(gpu.temp_c), source: 'Strata /metrics hardware' })
        + stat({ label: '功耗', value: num(gpu.power_w, 0), unit: isNum(gpu.power_w) ? 'W' : '',
                 sub: isNum(gpu.power_limit_w) ? `上限 ${num(gpu.power_limit_w, 0)} W` : '',
                 tone: powerTone(gpu.power_w, gpu.power_limit_w), source: 'Strata /metrics hardware' })
        + stat({ label: 'GPU 状态', value: stateWord, sub: gpu.source || '',
                 source: 'Strata /metrics hardware' }))
      + `</div>`
      + crossCheck(gpu.cross_check)
      + `<div style="margin-top:var(--sp-4)">${src(gpu.source || 'Strata /metrics hardware')}</div>`
      + `<div style="margin-top:var(--sp-4)">${notice('info', '为什么 nvidia-smi 不是这里的来源', NVML_NOTE)}</div>`,
  });
}

/* -------------------------------------------------------------- host */

function hostBlock(lv, strata, err) {
  const ssh = lv?.ssh;
  if (!ssh) return err ? notice('warning', '主机探针不可用', err.message) : skeleton(4, { block: true });
  if (ssh.ok === false) {
    return notice('warning', 'SSH 只读探针失败', ssh.error || 'unknown')
      + `<div class="hint-row">来源 · ${esc('probe_ssh()（固定只读命令，无插值）')}</div>`;
  }
  const mem = ssh.mem || {};
  const hm = strata?.host_metrics || {};
  const load = Array.isArray(ssh.loadavg) ? ssh.loadavg[0] : null;
  const cpuFrac = (isNum(load) && isNum(ssh.cpu_count) && ssh.cpu_count) ? load / ssh.cpu_count : null;
  const usedMib = mem.used_mib != null ? mem.used_mib
    : (isNum(mem.total_mib) && isNum(mem.avail_mib) ? mem.total_mib - mem.avail_mib : null);

  return `<div class="grid grid-2">`
    + `<div style="display:flex;flex-direction:column;gap:var(--sp-5)">`
    + meter({ name: `内存占用 · ${gib(usedMib, 1)} / ${gib(mem.total_mib, 0)} GiB`,
              value: isNum(mem.used_pct) ? mem.used_pct : null })
    + meter({ name: `1 分钟负载 · ${num(load, 2)} / ${int(ssh.cpu_count)} 核`,
              value: cpuFrac !== null ? cpuFrac * 100 : null,
              display: cpuFrac !== null ? `${num(cpuFrac * 100, 0)}%` : '' })
    + (ssh.disks || []).map((dk) => meter({
        name: `磁盘 ${dk.mount} · ${num(dk.used_gib, 0)} / ${num(dk.total_gib, 0)} GiB`,
        value: isNum(dk.used_pct) ? dk.used_pct : null })).join('')
    + `</div>`
    + `<div>`
    + kv([
      ['主机名', ssh.hostname || DASH],
      ['运行时长', dur(ssh.uptime_s)],
      ['CPU', hm.cpu_name ? `${hm.cpu_name} · ${hm.threads || ssh.cpu_count || DASH} 线程`
        : (ssh.cpu_count != null ? `${int(ssh.cpu_count)} 核` : DASH)],
      ['负载均值 1/5/15', Array.isArray(ssh.loadavg) && ssh.loadavg.length
        ? ssh.loadavg.map((v) => num(v, 2)).join(' / ') : DASH],
      ['节点 CPU', isNum(hm.cpu_pct) ? `${num(hm.cpu_pct, 0)}%` : DASH],
      ['磁盘读写', (hm.disk_read_mb != null || hm.disk_write_mb != null)
        ? `读 ${num(hm.disk_read_mb, 0)} / 写 ${num(hm.disk_write_mb, 0)} MB/s` : DASH],
      ['探针耗时', ssh.elapsed_s != null ? lat(ssh.elapsed_s, 2) : DASH],
    ])
    + src('SSH 只读探针 · 固定命令，无插值；节点侧 CPU / 磁盘来自 Strata /metrics hardware')
    + `</div></div>`;
}

/* ------------------------------------------------------------- trends */

function histSeries(strata, key) {
  const h = (strata?.history || {})[key];
  return Array.isArray(h) ? h.filter(isNum) : [];
}
const sampleSeries = (lv, key) => (lv?.samples || []).map((s) => s[key]).filter(isNum);

/** Prefer the node's own ring buffer -- it is sampled node-side, so it is not
 *  limited to how long this tab has been open. */
function sourcePick(strata, lv, hKey, sKey) {
  const h = histSeries(strata, hKey);
  if (h.length > 2) return { values: h, from: 'node', n: h.length };
  const s = sampleSeries(lv, sKey);
  return { values: s, from: 'local', n: s.length };
}

const srcLabel = (name, p) =>
  `${name} · ${p.from === 'node' ? '节点侧环形缓冲' : '本机采样缓冲'} · ${p.n} 个样本`;

/**
 * At most two charts, and a series only shares a chart with another when the
 * unit matches -- two y-scales on one frame is the thing the method rules out.
 * MiB counts (显存 and 主机内存) share; % and °C each get their own.
 */
function trendSpecs(strata, lv) {
  const util = sourcePick(strata, lv, 'gpu_util', 'gpu_util');
  const temp = sourcePick(strata, lv, 'gpu_temp', 'gpu_temp');
  const vram = sourcePick(strata, lv, 'gpu_mem_used_mib', 'gpu_mem_mib');
  const ram = sourcePick(strata, lv, 'ram_used_mib', 'ram_used');

  const specs = [];
  if (util.n >= 2) {
    specs.push({ title: 'GPU 利用率', unit: '%', fmtVal: (v) => num(v, 0),
      series: [{ name: 'GPU 利用率', values: util.values }], note: srcLabel('GPU 利用率', util) });
  }
  if (vram.n >= 2 && ram.n >= 2 && vram.from === ram.from) {
    const k = Math.min(vram.n, ram.n);
    specs.push({ title: '显存 / 内存占用', unit: 'MiB', fmtVal: (v) => num(v, 0),
      series: [{ name: '显存', values: vram.values.slice(-k) },
               { name: '主机内存', values: ram.values.slice(-k) }],
      note: `${vram.from === 'node' ? '节点侧环形缓冲' : '本机采样缓冲'} · ${k} 个样本` });
  } else {
    // Different sources cannot be aligned on an x axis, so the longer one is
    // shown alone rather than zipped into a chart that implies they line up.
    const pick = vram.n >= 2 ? vram : ram;
    const nm = pick === vram ? '显存' : '主机内存';
    if (pick.n >= 2) {
      specs.push({ title: nm, unit: 'MiB', fmtVal: (v) => num(v, 0),
        series: [{ name: nm, values: pick.values }], note: srcLabel(nm, pick) });
    }
  }
  if (temp.n >= 2) {
    specs.push({ title: 'GPU 温度', unit: '°C', fmtVal: (v) => num(v, 0),
      series: [{ name: '温度', values: temp.values }], note: srcLabel('GPU 温度', temp) });
  }
  return specs.slice(0, 2);
}

function trendBlock(specs, hasData) {
  if (!hasData) return skeleton(3, { block: true });
  if (!specs.length) {
    return notice('warning', '趋势暂无样本',
      '节点侧环形缓冲与本机采样缓冲都还没有累积到两个采样点。GPU 利用率、显存 / 内存与温度曲线都需要至少两个样本。');
  }
  return grid(2, specs.map((sp, i) => card({
    title: sp.title, note: sp.note,
    body: `<div class="chart-host" data-trend="${i}"></div>`,
  })).join(''));
}

/* ------------------------------------------------ services & ports */

function servicesBlock(lv, lab) {
  if (!lv && !lab) return skeleton(3);
  const svcs = lv?.services;
  let body;
  if (!svcs) body = skeleton(3);
  else if (!svcs.length) body = `<div class="hint-row">配置中没有 [services.*] 段。</div>`;
  else {
    const rows = svcs.map((s) => [
      { __raw: true, value: `<span class="truncate" style="display:inline-block;max-width:22ch">${esc(s.label || s.key)}</span>` },
      s.port != null ? int(s.port) : DASH,
      { __raw: true, value: s.up ? flag(SERVICE_TONE.up, SERVICE_LABEL.up)
        : s.enabled ? flag('warn', SERVICE_LABEL.down) : flag(SERVICE_TONE.down, '已停用') },
      { __raw: true, value: badge(s.managed ? '本 Portal 管理' : '外部', s.managed ? 'on' : 'off') },
      s.note ? clip(s.note, 60) : DASH,
    ]);
    body = table(['服务', '端口', '状态', '受管', '说明'], rows, { num: [1] });
  }

  const ports = lab?.nav?.ports;
  body += `<div style="height:var(--sp-6)"></div>`
    + `<h4 style="font-size:var(--fs-md);margin-bottom:var(--sp-3)">本机端口</h4>`
    + (ports
      ? kv([['面板 Portal', ports.portal ?? DASH], ['隧道', ports.tunnel ?? DASH],
            ['EvalScope', ports.evalscope ?? DASH]])
      : `<div class="hint-row">端口清单不可用（/api/lab 的 nav.ports 未返回）。</div>`);
  return body;
}

/* ---------------------------------------------------------- provenance */

function probesBlock() {
  return kv([
    ['GPU 遥测', 'Strata /metrics hardware（经 CUDA 运行时）'],
    ['GPU 交叉核对', 'Strata /v1/status machine.gpu'],
    ['引擎实时', 'Strata /metrics live'],
    ['历史曲线', 'Strata /metrics history（节点侧环形缓冲），不足时用本机采样缓冲'],
    ['主机内存 / 磁盘 / 负载', 'SSH 只读探针 · facts.mem / facts.disks / facts.loadavg'],
    ['节点 CPU / 磁盘 IO', 'Strata /metrics hardware'],
    ['本地服务端口', 'probe_services() · 本机 localhost TCP 探测'],
    ['端点端口', '/api/lab nav.ports'],
  ])
    + `<div style="margin-top:var(--sp-5)">`
    + notice('info', '探针是只读的', 'SSH 探针是固定只读命令，无插值；不重启、不修改节点任何配置。')
    + `</div>`;
}

/* -------------------------------------------------------------- render */

export async function render(host) {
  let offOv = null;
  let offLive = null;
  let offLab = null;
  let offTheme = null;
  let ov = overviewPoll.value;
  let ovErr = overviewPoll.error;
  let lv = livePoll.value;
  let lvErr = livePoll.error;
  let lab = labPoll.value;

  const draw = () => {
    const strata = ov?.strata;
    const specs = trendSpecs(strata, lv);

    host.innerHTML =
      band({
        title: 'GPU',
        sub: '读数来自节点侧遥测；两个来源不一致时会并列显示，不取其一',
        body: gpuBlock(strata, ovErr),
      })
      + band({
        title: '主机',
        sub: 'Windows 侧经 SSH 只读探针取得；不重启、不修改节点任何配置',
        body: hostBlock(lv, strata, lvErr),
      })
      + band({
        title: '趋势',
        sub: '每个指标一张图；只有单位相同的序列才共用一个纵轴',
        body: trendBlock(specs, !!(strata || lv)),
      })
      + band({
        title: '本地服务与端口',
        sub: '服务状态来自本机 localhost 的 TCP 探测；端口清单来自 /api/lab',
        body: servicesBlock(lv, lab),
      })
      + band({
        title: '探针来源',
        sub: '本页每个数字出自哪一次探测',
        body: probesBlock(),
      })
      + `<footer class="page-foot">
           <span>数据来源：<span class="mono">/api/v2/overview</span> · <span class="mono">/api/v2/live</span> · <span class="mono">/api/lab</span></span>
           <span>${esc(strata?.at ? `节点快照 ${strata.at}` : '')}</span>
         </footer>`;

    // SVG needs a layout box, so charts are drawn after the markup is in place.
    const gpuHost = host.querySelector('[data-gauge="vram"]');
    if (gpuHost && strata?.gpu?.available) {
      const g = strata.gpu;
      gauge(gpuHost, {
        value: (isNum(g.used_mib) && isNum(g.total_mib) && g.total_mib) ? g.used_mib / 1024 : null,
        max: (isNum(g.total_mib) && g.total_mib) ? g.total_mib / 1024 : 100,
        label: 'GPU 显存占用', unit: 'GiB', digits: 1,
      });
    }
    host.querySelectorAll('[data-trend]').forEach((el) => {
      const sp = specs[Number(el.dataset.trend)];
      if (!sp) return;
      lineChart(el, {
        categories: sp.series[0].values.map((_, i) => `#${i + 1}`),
        series: sp.series.map((s, i) => ({ ...s, color: series()[i] })),
        unit: sp.unit,
        aria: `${sp.title} 时间序列`,
        fmtVal: sp.fmtVal,
        fmtAxis: (v) => num(v, 0),
        note: sp.note,
      });
    });
  };

  draw();

  offOv = overviewPoll.subscribe((d, meta) => { ov = d; ovErr = meta.error; draw(); });
  offLive = livePoll.subscribe((d, meta) => { lv = d; lvErr = meta.error; draw(); });
  offLab = labPoll.subscribe((d) => { lab = d; draw(); });
  // Colour is baked into the SVG at draw time, so a theme switch redraws.
  offTheme = onThemeRepaint(() => draw());

  return {
    // The pollers are owned by the shell (see app.js); this view only detaches
    // its own subscriptions.
    destroy() { offOv?.(); offLive?.(); offLab?.(); offTheme?.(); },
  };
}
