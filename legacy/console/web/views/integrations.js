/* ==========================================================================
   integrations -- 外部集成。

   这一页存在的理由只有一句：**「已连接」不是自证的事实。**

   每一条状态都必须带着它的判定依据一起出现——一个没有依据的状态词只是一句
   断言。所以每张卡片底部都逐字保留 `evidence`，它来自「配置项 + 真实探测」的
   组合，可以在机器上复核。

   五段：汇总 → 系统清单 → Strata 原生界面 → EvalScope → 未集成的系统。
   最后一段是必需的：只展示「什么能用」的页面读起来像「全都覆盖了」，而这里
   有六个系统，其中一半并没有接进来。

   数据来自三处：`/api/v2/integrations`（清单 + 依据）、`/api/strata`（节点的
   原生界面与隧道）、`/api/healthz`（控制台自身）。EvalScope 的实时状态则来自
   shell 拥有的 `livePoll`——本视图只订阅，绝不 start/stop。
   ========================================================================== */

import { esc } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { DASH, int } from '../core/format.js';
import { get, post, isAborted, makeScope } from '../core/api.js';
import { livePoll, tunnelState, SERVICE_LABEL, SERVICE_TONE } from '../core/sources.js';
import { toast, reportError, skeleton, empty, errorState } from '../core/ui.js';
import { card, band, grid, stat, flag, badge, kv, button, linkButton, hint } from './_parts.js';

/* ---------------------------------------------------------- status -> flag */

/* flag() 的 tone 查表里没有名为 info / muted 的「状态」——它认识的是
   planned(→info) 和一批默认落到 muted 的名字。所以先把语义色落到它认识的
   状态名上：可用与规划都用 planned（蓝色 info），停用/未安装落默认（muted）。
   这条映射就是规范里那张表，只是落在了 flag() 实际接受的名字上。 */
const FLAG_STATE = {
  connected: 'ok', available: 'planned', planned: 'planned',
  disabled: 'muted', not_installed: 'muted', error: 'crit',
};

/** 一个布尔事实渲染成一个带词的徽标：颜色只做提示，词才承载状态。 */
const boolBadge = (label, v, invert = false) => {
  const ok = invert ? !v : !!v;
  return badge(`${label} = ${v === true ? 'true' : v === false ? 'false' : DASH}`, ok ? 'on' : 'off');
};

/* ------------------------------------------------------------------ 汇总 */

function summary(items, healthz, healthzErr) {
  if (items === undefined) return grid(3, skeleton(1));
  // 清单探测失败时，标签页不能编一个「0 个系统」出来——那是把「读不到」说成
  // 「没有」。所以这里如实给出一条错误横幅。
  if (items === null) {
    return `<div class="notice error">${icon('alert', 'notice-ico', 17)}<div class="notice-body">`
      + `<div class="notice-title">清单探测失败</div>`
      + `<div class="notice-msg">无法读取 /api/v2/integrations，因此本页没有任何计数可报。</div>`
      + `</div></div>`;
  }
  const count = (s) => items.filter((i) => i.status === s).length;
  const notIntegrated = count('disabled') + count('not_installed');
  const plannedish = count('planned') + count('available');
  const errors = count('error');

  return grid(3,
    stat({ label: '外部系统', ic: 'plug', value: int(items.length),
           sub: '清单中的条目总数', source: '/api/v2/integrations' })
    + stat({ label: '已连接', ic: 'check', value: int(count('connected')),
             sub: 'status = connected', source: '逐条来自真实探测' })
    + stat({ label: '可用 / 规划中', ic: 'clock', value: int(plannedish),
             sub: 'status = available · planned', source: '可启用但尚未接通' })
    + stat({ label: '未集成', ic: 'offline', value: int(notIntegrated), tone: 'muted',
             sub: 'status = disabled · not_installed', source: '明确未接入的系统' })
    + stat({ label: '异常', ic: 'alert', value: int(errors), tone: errors ? 'crit' : '',
             sub: errors ? '有系统的探测报告了错误' : '没有系统报告错误', source: 'status = error' })
    + stat({ label: '控制台自身', ic: 'server',
             value: healthzErr ? DASH : (healthz?.version || DASH),
             sub: healthzErr ? '健康检查失败' : '本控制台 /api/healthz', source: '/api/healthz' }));
}

/* -------------------------------------------------------------- 系统清单 */

function itemCard(it) {
  // kind 是这台系统的类型，可能为空（例如未安装的生产 API）——空就说不清，
  // 而不是编一个类型出来。
  const kind = it.kind ? badge(it.kind) : badge('未标注类型', 'off');
  const managed = it.managed ? badge('门户托管', 'on') : '';
  // 有地址才是可点的链接；没有地址时给一个「看起来就不能点」的徽标，而不是
  // 一个 href="" 的按钮——空链接点击会静默跳到本页顶部。
  const url = it.url
    ? linkButton({ label: '打开', href: it.url, ic: 'external', cls: 'btn-ghost btn-sm', title: it.url })
    : badge('未提供地址', 'off');

  const body = `<div class="chips" style="margin-bottom:var(--sp-4)">${kind}${managed}</div>`
    + `<p style="margin:0 0 var(--sp-4);color:var(--ink-2);overflow-wrap:anywhere">`
      + esc(it.detail || DASH) + `</p>`
    + `<div class="chips" style="margin-bottom:var(--sp-4)">${url}</div>`
    // 判定依据逐字保留：它是这一页的全部意义。
    + `<div class="src" style="overflow-wrap:anywhere">判定依据 · ${esc(it.evidence || DASH)}</div>`;

  return card({
    title: it.label || it.key || DASH,
    actions: flag(FLAG_STATE[it.status] || 'unknown', it.status_label || it.status || DASH),
    body,
  });
}

function itemList(items, err, retry) {
  if (err) return errorState(err, { retry });
  if (items === undefined) return grid(2, skeleton(3, { block: true }));
  if (!items.length) {
    return empty('该控制台没有报告任何外部系统。这是一次真实探测的空结果，不是未加载。',
      { title: '暂无外部系统', icon: 'plug' });
  }
  return grid(2, items.map(itemCard).join(''));
}

/* --------------------------------------------------------- Strata 原生界面 */

function nativeBlock(strata, err, retry) {
  if (err) {
    return errorState(err, { retry });
  }
  if (strata === undefined) return skeleton(3, { block: true });
  const n = strata && strata.native;
  if (!n) {
    return empty('节点控制台没有返回 native 块，因此无法列出它的原生界面。',
      { title: '无原生界面数据', icon: 'plug' });
  }

  const t = n.tunnel || {};
  const ts = tunnelState(t);
  const tunnelFlag = flag(
    { up: 'ok', foreign: 'warn', off: 'unknown', down: 'crit' }[ts.key] || 'unknown',
    ts.label,
    { title: 'tunnelState() 的结论：listening + pid_alive + owned + foreign 四者的合成' });

  const reachedVia = n.kind === 'tunnel' ? '经隧道' : n.kind === 'direct' ? '直连' : (n.kind || DASH);
  const panels = (n.panels || []).filter((p) => p && p.url);
  const panelLinks = panels.length
    ? panels.map((p) => linkButton({
        label: p.label || '原生界面', href: p.url, ic: 'external', cls: 'btn-ghost btn-sm',
        title: p.note || p.url,
      })).join('')
    : badge('节点未报告任何面板', 'off');

  const fallback = n.fallback_url
    ? `<div class="chips">${linkButton({ label: '回退直连地址', href: n.fallback_url, ic: 'external',
        cls: 'btn-ghost btn-sm', title: n.fallback_url })}</div>`
      + `<div class="src">回退地址可达 · ${esc(n.fallback_reachable === true ? 'true' : n.fallback_reachable === false ? 'false' : DASH)}</div>`
    : badge('未配置回退地址', 'off');

  return card({
    title: 'Strata 原生界面',
    note: n.base_url || '',
    actions: tunnelFlag,
    body:
      kv([
        ['base_url', n.base_url || DASH],
        ['接入方式', reachedVia],
        ['可达', n.reachable === true ? 'true' : n.reachable === false ? 'false' : DASH],
      ])
      // 隧道四事实各自成词。tunnelState() 是这四者的合成，但它不该掩盖分量。
      + `<div class="chips" style="margin:var(--sp-5) 0">`
        + boolBadge('listening', t.listening)
        + boolBadge('pid_alive', t.pid_alive)
        + boolBadge('owned', t.owned)
        + boolBadge('foreign', t.foreign, true)
      + `</div>`
      + `<div style="font-size:var(--fs-sm);color:var(--ink-muted);margin-bottom:var(--sp-3)">面板（在新标签页打开节点的自有界面）</div>`
      + `<div class="chips" style="margin-bottom:var(--sp-5)">${panelLinks}</div>`
      + `<div style="font-size:var(--fs-sm);color:var(--ink-muted);margin-bottom:var(--sp-3)">回退</div>`
      + fallback
      // 两条必须显式说出的边界：不嵌入、不代替用户注入密钥。
      + `<div class="notice info" style="margin-top:var(--sp-6)">${icon('info', 'notice-ico', 17)}`
        + `<div class="notice-body"><div class="notice-title">这里不做任何嵌入</div>`
        + `<div class="notice-msg">这些按钮是<strong>链接</strong>，不是 iframe——没有任何东西被嵌进本页。`
        + `API Key 由你在那个界面里自己输入，本控制台<strong>从不</strong>替你注入。`
        + `之所以不需要 iframe 也能跨源直达：Chrome 把 `
        + `<span class="mono">http://localhost:&lt;port&gt;</span> 视为安全上下文（secure context），`
        + `所以这些 <span class="mono">localhost</span> 链接可以正常打开。</div></div></div>`
      + `<div class="src" style="overflow-wrap:anywhere;margin-top:var(--sp-5)">`
        + `native.note · ${esc(n.note || DASH)}</div>`,
  });
}

/* -------------------------------------------------------------- EvalScope */

/* EvalScope 是三态而不是布尔：导入要跑几分钟，期间进程活着但端口还没绑定。
   `starting`（pid 活着、端口没起）与 `down`（进程也没了）需要完全不同的处理，
   一个布尔会把这两件事合并成「没起来」——那既不对，也无从修复。 */
function evalscopeBody(lv, lvErr, starting) {
  const ev = lv && lv.evalscope;
  if (!ev) {
    if (lvErr) return errorState(lvErr);
    return skeleton(2, { block: true });
  }
  const state = ev.state;
  const tone = SERVICE_TONE[state] || 'unknown';
  const label = SERVICE_LABEL[state] || DASH;

  // state !== 'up' 时才提供启动按钮：运行中再点没有意义，服务端也会回
  // 「already listening」。
  const startBtn = state === 'up' ? '' :
    `<button class="btn btn-primary btn-sm" type="button" id="startEvalscope"`
    + (starting ? ' disabled aria-busy="true"' : '')
    + `>${starting ? '<span class="spinner"></span>' : icon('play', '', 15)}启动 EvalScope</button>`;

  const dashboard = state === 'up' && ev.url
    ? linkButton({ label: '打开 Dashboard', href: ev.url, ic: 'external', cls: 'btn-ghost btn-sm', title: ev.url })
    : '';

  return card({
    title: 'EvalScope',
    actions: flag(tone, label),
    body:
      `<div class="chips" style="margin-bottom:var(--sp-5)">`
        + `<span class="badge ${tone === 'ok' ? 'on' : tone === 'warn' ? 'warn' : ''}">state = ${esc(state || DASH)}</span>`
        + (ev.managed ? badge('门户托管', 'on') : badge('非托管', 'off'))
        + (ev.enabled ? badge('enabled', 'on') : badge('disabled', 'off'))
      + `</div>`
      + kv([
        ['端口', ev.port != null ? String(ev.port) : DASH],
        ['PID', ev.pid != null ? String(ev.pid) : DASH],
        ['进程存活', ev.pid_alive === true ? 'true' : ev.pid_alive === false ? 'false' : DASH],
        ['地址', ev.url || DASH],
      ])
      + `<div class="chips" style="margin-top:var(--sp-6)">${startBtn}${dashboard}</div>`
      + `<div style="margin-top:var(--sp-4);font-size:var(--fs-xs);color:var(--ink-muted);display:flex;align-items:center;gap:var(--sp-2)">`
        + `状态来自 <span class="mono">/api/v2/live</span> 的 evalscope 块，每 20 秒刷新`
        + hint('三态模型：up（端口已绑定）/ starting（进程活着但还没绑定）/ down（进程也没了）。')
      + `</div>`,
  });
}

/* ------------------------------------------------------- 未集成的系统明说 */

function gapsBlock(items) {
  if (!items) return '';
  const gaps = items.filter((i) => ['planned', 'not_installed', 'disabled'].includes(i.status));
  if (!gaps.length) {
    return `<div class="notice info">${icon('check', 'notice-ico', 17)}<div class="notice-body">`
      + `<div class="notice-title">没有未集成的系统</div>`
      + `<div class="notice-msg">清单中的每一条都已接入。</div></div></div>`;
  }
  const list = gaps.map((g) => `<li>${esc(g.label || g.key)} <span class="muted">（${esc(g.status_label || g.status)}）</span></li>`).join('');
  return `<div class="notice warning">${icon('alert', 'notice-ico', 17)}<div class="notice-body">`
    + `<div class="notice-title">以下系统<strong>没有</strong>集成</div>`
    + `<div class="notice-msg">它们只出现在清单里，不提供任何数据面：</div>`
    + `<ul style="margin:var(--sp-3) 0 0;padding-left:var(--sp-7);line-height:1.7">${list}</ul>`
    + `</div></div>`;
}

/* --------------------------------------------------------------- render */

export async function render(host) {
  const scope = makeScope();
  let destroyed = false;
  let offLive = null;

  // 一次性数据（清单 / 原生界面 / 健康检查）。
  let items, itemsErr = null;
  let strata, strataErr = null;
  let healthz = null, healthzErr = null;

  // 启动按钮的在途状态。它决定按钮是否 disabled + 转圈，所以必须活在闭包里，
  // 而不是从 DOM 里读回来——每次 draw() 都会重建 DOM。
  let starting = false;
  let followUps = [];   // 启动后追看的定时器，destroy 时要清掉

  const foot = () => `<footer class="page-foot">
      <span>数据来源：<span class="mono">/api/v2/integrations</span> · <span class="mono">/api/strata</span> · <span class="mono">/api/v2/live</span> · <span class="mono">/api/healthz</span></span>
      <span>${items ? esc(`${items.length} 个外部系统`) : ''}</span>
    </footer>`;

  function draw() {
    if (destroyed) return;
    const lv = livePoll.value, lvErr = livePoll.error;
    host.innerHTML =
      band({
        title: '汇总',
        sub: '按状态计数的清单条目；全部由 /api/v2/integrations 的 items 派生，没有硬编码',
        tools: button({ label: '重新探测', ic: 'refresh', cls: 'btn-ghost btn-sm', id: 'intRefresh' }),
        body: summary(items, healthz, healthzErr),
      })
      + band({
        title: '系统清单',
        sub: '每张卡片一条外部系统；底部逐字保留它的判定依据（evidence）',
        body: itemList(items, itemsErr, () => load()),
      })
      + band({
        title: 'Strata 原生界面',
        sub: '节点自己的 UI，以链接方式在新标签页打开；不做任何嵌入',
        body: nativeBlock(strata, strataErr, () => load()),
      })
      + band({
        title: 'EvalScope',
        sub: '按需拉起的托管服务；状态是三态，不是布尔',
        body: evalscopeBody(lv, lvErr, starting),
      })
      + gapsBlock(items)
      + foot();

    wire();
  }

  function wire() {
    host.querySelector('#intRefresh')?.addEventListener('click', () => load());
    host.querySelector('#startEvalscope')?.addEventListener('click', onStart);
  }

  async function load() {
    const results = await Promise.allSettled([
      get('/api/v2/integrations', { timeout: 30000, signal: scope.signal }),
      get('/api/strata', { timeout: 30000, signal: scope.signal }),
      get('/api/healthz', { timeout: 15000, signal: scope.signal }),
    ]);
    if (destroyed) return;

    const [a, b, c] = results;
    if (a.status === 'fulfilled') { items = Array.isArray(a.value?.items) ? a.value.items : []; itemsErr = null; }
    else if (!isAborted(a.reason)) { items = null; itemsErr = a.reason; }

    if (b.status === 'fulfilled') { strata = b.value; strataErr = null; }
    else if (!isAborted(b.reason)) { strata = null; strataErr = b.reason; }

    if (c.status === 'fulfilled') { healthz = c.value; healthzErr = null; }
    else if (!isAborted(c.reason)) { healthz = null; healthzErr = c.reason; }

    draw();
  }

  /**
   * 启动 EvalScope。
   *
   * 这里只发一个 POST；它不带任何命令、argv 或路径。服务名取服务端自己
   * payload 里的 `evalscope.key`（回退到常量 'evalscope'）——不是用户输入，
   * 服务端也会用 managed_services() 白名单拒掉任何别的值（非白名单 → 404，
   * 见 docs/security.md §"唯一一个会做事的路由"）。
   *
   * 端点文档写作「无参数」，但运行中的服务端是从 `?key=` 读服务名的；实测
   * 无参数会 404。因此这里带上这个来自配置的常量键，它是唯一一个被允许的取值。
   */
  async function onStart() {
    if (starting || destroyed) return;
    starting = true;
    draw();                                   // 立刻显示 disabled + spinner
    try {
      const key = livePoll.value?.evalscope?.key || 'evalscope';
      const res = await post(`/api/service/start?key=${encodeURIComponent(key)}`);
      if (res && res.ok) {
        const st = res.state;
        toast(
          res.started ? '已发出启动指令，EvalScope 正在导入'
            : st === 'up' ? 'EvalScope 已在运行' : st === 'starting' ? 'EvalScope 正在启动中' : (res.note || '未启动'),
          { kind: 'good', title: '启动 EvalScope',
            ms: 6000 });
      } else {
        toast((res && (res.error || res.note)) || '启动未成功',
          { kind: 'warning', title: '启动 EvalScope' });
      }
    } catch (err) {
      reportError(err, '启动 EvalScope');
    } finally {
      starting = false;
      if (!destroyed) { draw(); scheduleFollowUps(); }
    }
  }

  /**
   * 启动后追看三态转变。EvalScope 从 down → starting → up 要走完导入过程，
   * 一次刷新看不到转变，所以在一段窗口内多次强制刷新 livePoll。
   */
  function scheduleFollowUps() {
    followUps.forEach(clearTimeout);
    followUps = [0, 1500, 3000, 6000, 10000, 15000, 20000].map((ms) =>
      setTimeout(() => { if (!destroyed) livePoll.refresh({ force: true }); }, ms));
  }

  // 骨架屏：一次性数据到达前先给形状，EvalScope 一段则立刻用 livePoll 的缓存。
  host.innerHTML = band({ title: '汇总', body: grid(3, skeleton(1)) })
    + band({ title: '系统清单', body: grid(2, skeleton(3, { block: true })) })
    + band({ title: 'Strata 原生界面', body: skeleton(3, { block: true }) })
    + band({ title: 'EvalScope', body: evalscopeBody(livePoll.value, livePoll.error, starting) });
  wire();

  // 订阅 shell 的 live poll：它每 20 秒落一次，只有它更新时 EvalScope 段才跟着动。
  offLive = livePoll.subscribe(() => draw());

  load();

  return {
    // 只解自己的订阅与自己的定时器；livePoll/overviewPoll 归 shell 所有，
    // 离开本页不该让顶部健康灯静音。
    destroy() {
      destroyed = true;
      followUps.forEach(clearTimeout);
      offLive?.();
      scope.abort();
    },
  };
}
