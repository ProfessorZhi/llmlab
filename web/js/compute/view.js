/*! Atelier V6 Compute: genuine route/content without changing generated HTML/CSS.
 *
 * Nothing here reads privileged data, performs network IO, or controls a GPU.
 * The ops controller may pass an already-validated portal-ops/1 snapshot.
 * In all other cases, numbers remain UNKNOWN. In particular, synthetic
 * fixtures never count as real telemetry.
 */

export const COMPUTE_METRIC_NAMES = Object.freeze([
  'gpu_utilization_pct',
  'gpu_vram_used_mib',
  'gpu_vram_total_mib',
]);

const VERIFIED_GPU_SOURCE = 'nvml-verified';

const unknownMetric = () => ({
  value: 'UNKNOWN',
  source: null,
  updatedAt: null,
});

/** Only an authenticated live aggregate with NVML provenance may yield GPU values. */
export function projectComputeMetrics({ ops = null, mode = 'unavailable', state = 'unknown' } = {}) {
  const live = mode === 'live'
    && state === 'ready'
    && ops?.synthetic === false
    && ops?.transport?.mode === 'live'
    && ops?.transport?.gateway_attached === true;

  const metrics = {};
  for (const name of COMPUTE_METRIC_NAMES) {
    const item = live && Array.isArray(ops?.metrics?.items)
      ? ops.metrics.items.find((candidate) => candidate.name === name)
      : null;
    const valid = item
      && item.source === VERIFIED_GPU_SOURCE
      && item.state === 'available'
      && typeof item.updated_at === 'string'
      && item.updated_at.trim().length > 0
      && typeof item.value === 'number'
      && Number.isFinite(item.value)
      && item.value >= 0;

    metrics[name] = valid
      ? {
        value: Number(item.value).toLocaleString('zh-CN') +
          (name === 'gpu_utilization_pct' ? '%' : ' MiB'),
        source: VERIFIED_GPU_SOURCE,
        updatedAt: item.updated_at,
      }
      : unknownMetric();
  }

  const firstTrusted = COMPUTE_METRIC_NAMES
    .map((name) => metrics[name])
    .find((entry) => entry.updatedAt !== null);

  return {
    mode: live ? 'live' : 'unverified',
    badge: live
      ? 'LIVE · 只显示已验证指标'
      : mode === 'mock'
        ? 'SYNTHETIC · 尚无实时 GPU 指标'
        : 'UNKNOWN · 需要已授权的数据源',
    updatedAt: firstTrusted?.updatedAt ?? null,
    metrics,
  };
}

/** The verified host contract, mirrored from web/tools/verified-infra.mjs.
 *  A payload that does not match it exactly is not a host reading. */
export const HOST_CONTRACT = Object.freeze({ version: 1, source: 'prometheus-readonly', maxAgeS: 90 });

/** Only a complete, non-synthetic, read-only, known projection is a host source. */
export function isVerifiedHostSummary(summary) {
  return !!summary
    && summary.version === HOST_CONTRACT.version
    && summary.source === HOST_CONTRACT.source
    && summary.synthetic === false
    && summary.readonly === true
    && summary.state === 'known';
}

/** One metric is usable only when it is known, finite, non-negative, fresh and
 *  carries its OWN sample time. The request's `observed_at` is never substituted
 *  for a sample time — that would be a fabricated timestamp. */
function hostMetric(summary, key) {
  const m = summary?.metrics?.[key];
  if (!m || m.state !== 'known') return null;
  if (typeof m.value !== 'number' || !Number.isFinite(m.value) || m.value < 0) return null;
  if (typeof m.age_s !== 'number' || !Number.isFinite(m.age_s) || m.age_s > HOST_CONTRACT.maxAgeS) return null;
  if (typeof m.sample_at !== 'string' || !m.sample_at.trim()) return null;
  return { value: m.value, sample_at: m.sample_at, age_s: m.age_s };
}

const hostUnknown = () => ({ value: 'UNKNOWN', source: null, updatedAt: null });

/**
 * Project the verified host summary onto the four hero cells. Returns null when
 * the summary is not a host source at all, so the caller keeps its own source.
 * Cross-checks that a broken projection must not pass: a ratio over 1 and a
 * "used" larger than "total" are contract violations, not readings.
 */
export function projectHostMetrics(summary) {
  if (!isVerifiedHostSummary(summary)) return null;

  let util = hostMetric(summary, 'gpu_utilization_ratio');
  let used = hostMetric(summary, 'gpu_memory_used_mib');
  let total = hostMetric(summary, 'gpu_memory_total_mib');
  if (util && util.value > 1) util = null;
  if (used && total && used.value > total.value) { used = null; total = null; }

  const sample = [util, used, total].filter(Boolean)
    .sort((a, b) => a.age_s - b.age_s)[0] ?? null;

  const metrics = {
    gpu_utilization_pct: util
      ? { value: `${Math.round(util.value * 1000) / 10}%`, source: HOST_CONTRACT.source, updatedAt: util.sample_at }
      : hostUnknown(),
    gpu_vram_used_mib: used
      ? { value: `${Math.round(used.value * 10) / 10} MiB`, source: HOST_CONTRACT.source, updatedAt: used.sample_at }
      : hostUnknown(),
    gpu_vram_total_mib: total
      ? { value: `${Math.round(total.value * 10) / 10} MiB`, source: HOST_CONTRACT.source, updatedAt: total.sample_at }
      : hostUnknown(),
  };
  const anyKnown = Object.values(metrics).some((m) => m.updatedAt !== null);

  return {
    mode: anyKnown ? 'host-live' : 'unverified',
    badge: anyKnown ? 'LIVE · Prometheus（主机级）' : 'UNKNOWN · 主机遥测未验证',
    updatedAt: sample?.sample_at ?? null,
    metrics,
  };
}

const COMPUTE_MARKUP = [
  '<div class="editorial-index"><span><b>02</b> / COMPUTE</span><span class="index-rule" aria-hidden="true"></span><span>PHYSICAL RESOURCES · VERIFIED SOURCES ONLY</span></div>',
  '<div class="breadcrumb"><span>LLM LAB</span><span>/</span><b>COMPUTE</b></div>',
  '<div class="hero"><div><h1 id="compute-title">算力中心 <em class="title-whisper">Compute.</em></h1>',
  '<p>统一查看主机与推理资源的已验证状态。登记模型不代表已加载，队列与显存只有真实观测才能称为可用。</p></div>',
  '<div class="hero-right"><span class="badge warn" data-compute-provenance>SYNTHETIC · 尚无实时 GPU 指标</span><button type="button" class="secondary" data-go="overview">返回实验室概览 ↩</button></div></div>',
  '<div class="stats" aria-label="GPU 观测指标">',
  '<div class="stat"><span>GPU 利用率</span><strong data-compute-metric="gpu_utilization_pct">UNKNOWN</strong><small>NVML · 待接线</small></div>',
  '<div class="stat"><span>显存占用</span><strong data-compute-metric="gpu_vram_used_mib">UNKNOWN</strong><small>NVML · 待接线</small></div>',
  '<div class="stat"><span>显存总量 · 观测</span><strong data-compute-metric="gpu_vram_total_mib">UNKNOWN</strong><small>不等于板卡规格</small></div>',
  '<div class="stat"><span>采样时间</span><strong style="font-size:15px" data-compute-observed>UNKNOWN</strong><small>不可猜测</small></div>',
  '</div>',
  '<div class="columns">',
  '<section class="flatpanel"><div class="editorial-index"><b>01</b><span>HARDWARE PROFILE</span><span class="index-rule"></span></div>',
  '<h3>RTX A6000 · 48 GB 板卡规格</h3>',
  '<p>这是已知的硬件标称规格，不是 NVML 实测可用显存，也不意味着当前有额外推理容量。</p>',
  '<div class="statusrows">',
  '<div class="statusitem"><div class="tiny">GPU 资源准入</div><strong>待可信预检</strong></div>',
  '<div class="statusitem"><div class="tiny">模型加载/停止</div><strong>不在本预览执行</strong></div>',
  '<div class="statusitem"><div class="tiny">可用显存</div><strong>UNKNOWN</strong></div>',
  '<div class="statusitem"><div class="tiny">进程归属</div><strong>由 Linux owner 验证</strong></div>',
  '</div>',
  '<div class="guard warn">请勿把板卡规格当作当前显存余量。真正的 GPU 指标必须来自经过验证且附采样时间的 NVML 数据。</div>',
  '</section>',
  '<aside class="flatpanel"><div class="editorial-index"><b>02</b><span>SAFETY & SCHEDULING</span><span class="index-rule"></span></div>',
  '<h3>受控模型运行</h3>',
  '<ol class="steps">',
  '<li><span class="count">01</span><div><b>Model / Engine Registry</b><p>区分模型记录、部署实例与可调用的 Endpoint。</p></div></li>',
  '<li><span class="count">02</span><div><b>Owner 与显存预算</b><p>由受信 Linux 身份确认所有权；不得通过 Web 直接执行 root 命令。</p></div></li>',
  '<li><span class="count">03</span><div><b>健康检查与灰度路由</b><p>后端切换须 Gateway 完成 revision 同步与回滚验收。</p></div></li>',
  '<li><span class="count">04</span><div><b>保护正在执行的任务</b><p>不抢占现有 Strata/Qwen 或其他用户的 GPU 作业。</p></div></li>',
  '</ol></aside></div>',
  '<div class="group-title"><h2>管理工作区</h2><span class="smallmeta">VERIFIED ROUTES / 03</span></div>',
  '<div class="module-grid">',
  '<div class="module-card"><h3>模型与部署</h3><p class="module-card-sub">查看受信资源登记、部署状态和 Endpoint 权限。</p><button type="button" class="secondary" data-go="models">进入模型管理 ↗</button></div>',
  '<div class="module-card"><h3>监控与调用</h3><p class="module-card-sub">按已授权范围显示指标来源、异常和更新时间。</p><button type="button" class="secondary" data-go="observability">进入监控中心 ↗</button></div>',
  '<div class="module-card"><h3>评测队列</h3><p class="module-card-sub">评测与 GPU 推理属于独立的受控资源工作流。</p><button type="button" class="secondary" data-go="evaluation">查看评测中心 ↗</button></div>',
  '</div>',
].join('\n');

/** Install a real hash-route target and navigation into the existing V6 shell. */
export function mountComputeView({ doc = globalThis.document } = {}) {
  if (!doc) return null;
  const main = doc.querySelector('#app-main');
  if (!main) return null;

  let view = doc.querySelector('#view-compute');
  if (!view) {
    view = doc.createElement('section');
    view.id = 'view-compute';
    view.className = 'view';
    view.setAttribute('aria-labelledby', 'compute-title');
    view.innerHTML = COMPUTE_MARKUP;
    const footer = main.querySelector('.footbar');
    if (footer) main.insertBefore(view, footer);
    else main.appendChild(view);
  }

  // Compute lives under Overview. Expose a single in-page entry, never
  // append a ninth top-level tab or rewrite the frozen generated shell.
  const overview = doc.querySelector('#view-overview');
  if (overview && !overview.querySelector('[data-compute-entry]')) {
    const entry = doc.createElement('section');
    entry.className = 'page-block module-panel';
    entry.dataset.computeEntry = '1';
    entry.innerHTML =
      '<div class="group-title"><h2>算力中心 <em class="heading-en">/ COMPUTE</em></h2>' +
      '<span class="smallmeta">OVERVIEW / COMPUTE</span></div>' +
      '<div class="surface"><p class="overview-intro">查看 GPU 资源、受信观测与部署安全状态。' +
      '未连接 NVML 的指标保持 UNKNOWN。</p>' +
      '<div class="actions"><button type="button" class="secondary" data-go="compute">' +
      '打开算力子页面 ↗</button></div></div>';
    overview.appendChild(entry);
  }

  // The two sources are kept apart and re-evaluated together on every paint, so
  // neither can silently overwrite the other.
  let lastOps = { ops: null, mode: 'unavailable', state: 'unknown' };
  let lastHost = null;

  function paint() {
    // The verified host reading wins while it is valid: it is measured and
    // carries its own sample time, where the ops path is a portal aggregate.
    // A late ops callback therefore cannot overwrite a live host read, and when
    // the host read stops being valid the host cells are cleared (or filled from
    // the ops path) rather than left showing a stale number.
    const projection = projectHostMetrics(lastHost) ?? projectComputeMetrics(lastOps);
    const badge = view.querySelector('[data-compute-provenance]');
    if (badge) badge.textContent = projection.badge;
    const observed = view.querySelector('[data-compute-observed]');
    if (observed) observed.textContent = projection.updatedAt ?? 'UNKNOWN';
    for (const name of COMPUTE_METRIC_NAMES) {
      const cell = view.querySelector('[data-compute-metric="' + name + '"]');
      if (cell) cell.textContent = projection.metrics[name].value;
    }
    return projection;
  }

  return {
    view,
    update: (input) => { lastOps = { ...lastOps, ...input }; return paint(); },
    updateHost: (summary) => { lastHost = summary ?? null; return paint(); },
    get projection() { return paint(); },
  };
}
