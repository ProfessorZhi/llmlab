/* ==========================================================================
   settings -- 设置。

   这一页回答的是：「这个控制台**实际上**是怎么配的，以及哪些开关属于**这个
   浏览器**」。

   一条纪律贯穿全篇：**凭据只有两个事实可以出现**——`key_present`（布尔）与
   `key_sha256_12`（12 位摘要）。密钥值、`secrets.env` 的内容、SSH 私钥的字节
   一律不出现在这里，也不出现在任何地方。`node.ssh_key` 是**路径**，显示路径是
   有用的；它的内容不是，也从没被读过。

   数据来自 `/api/v2/settings`。该端点会跑几个子进程（node/git/ssh/powershell）
   做环境自检，所以它慢——用 90 秒超时，并配一条 honest 的错误态。
   ========================================================================== */

import { esc } from '../core/dom.js';
import { DASH, int } from '../core/format.js';
import { get, isAborted, makeScope } from '../core/api.js';
import { toast, skeleton, errorState, segmented as wireSegmented, notice } from '../core/ui.js';
import { theme, setTheme, onTheme } from '../core/theme.js';
import { card, band, grid, kv, row, table, badge, flag, hint, src,
         segmented as segmentedMarkup } from './_parts.js';

/* ---------------------------------------------------------------- 主题控制 */

/* core/theme.js 把 localStorage 的键名当作私有常量，没有导出。要在「跟随系统」
   时真正清掉用户的显式选择（否则 initTheme 的 OS 跟随永远不会接管），只能镜像
   这个键名——这是全篇唯一一处对私有常量的重复，写在这里以便一起改。 */
const STORE_KEY = 'llmlab.theme';
const readStored = () => { try { return localStorage.getItem(STORE_KEY); } catch { return null; } };
const clearStored = () => { try { localStorage.removeItem(STORE_KEY); } catch { /* 隐私模式 */ } };

const MODES = [
  { value: 'dark', label: '深色' },
  { value: 'light', label: '浅色' },
  { value: 'auto', label: '跟随系统' },
];

const seriesSwatches = [
  ['--series-1', '系列 1'], ['--series-2', '系列 2'],
  ['--series-3', '系列 3'], ['--series-4', '系列 4'],
];
// 状态词汇必须**带词**出现在屏幕上：颜色本身不承载状态，约每十二个读者里就有
// 一个分不开「好」与「严重」两种色相。
const statusSwatches = [
  ['--good', '正常 good'], ['--warning', '警告 warning'],
  ['--critical', '严重 critical'], ['--serious', '降级 serious'],
  ['--info', '信息 info'], ['--neutral', '中性 neutral'],
];

const swatch = (t, word) => `<div style="display:flex;align-items:center;gap:var(--sp-3);min-width:0">`
  + `<span aria-hidden="true" style="width:16px;height:16px;flex:none;border-radius:var(--r-xs);`
  + `border:1px solid var(--hair);background:var(${t})"></span>`
  + `<span class="mono" style="font-size:var(--fs-xs);overflow:hidden;text-overflow:ellipsis">${esc(t)}</span>`
  + `<span style="font-size:var(--fs-xs);color:var(--ink-muted)">${esc(word)}</span></div>`;

/* --------------------------------------------------------------- 各段渲染 */

function appearanceCard() {
  const active = Math.max(0, MODES.findIndex((m) => m.value === (readStored() || 'auto')));
  const body = segmentedMarkup('settingTheme', MODES, active)
    + `<div style="margin-top:var(--sp-5);font-size:var(--fs-sm);color:var(--ink-2)">`
      + `当前：<b id="themeNow">${esc(MODES[active].label)}</b>`
      + `<span style="color:var(--ink-muted)">（正在使用 <span id="themeEffective">${esc(theme() === 'light' ? '浅色' : '深色')}</span>）</span>`
    + `</div>`
    + src('core/theme.js · setTheme() 持久化；「跟随系统」清除该选择，交给 prefers-color-scheme');
  return card({
    title: '主题',
    note: '只影响这个浏览器',
    actions: flag('ok', '已持久化'),
    body,
  });
}

function vocabularyCard() {
  const col = (items) => `<div style="display:flex;flex-direction:column;gap:var(--sp-3)">`
    + items.map(([t, w]) => swatch(t, w)).join('') + '</div>';
  return card({
    title: '色板与状态词',
    note: '状态永远是「点 + 词」',
    body: `<div class="grid grid-2" style="gap:var(--sp-5)">`
      + `<div><div style="font-size:var(--fs-xs);color:var(--ink-muted);margin-bottom:var(--sp-3)">分类系列</div>${col(seriesSwatches)}</div>`
      + `<div><div style="font-size:var(--fs-xs);color:var(--ink-muted);margin-bottom:var(--sp-3)">状态语义</div>${col(statusSwatches)}</div>`
    + `</div><div style="margin-top:var(--sp-5)">${src('styles/tokens.css · --series-1..4 与 --good/--warning/--critical 等')}</div>`,
  });
}

function refreshPolicyCard() {
  // 这些数字是 core/sources.js 里实际写着的间隔，不是可调旋钮——本页不提供
  // 改变它们的控件，所以把它们如实读出来即可。
  const guards = [
    ['单飞', '一次请求在途时，再触发只记一个「待跑」标志，最多在本次结束后补跑一次——不会叠出第二份请求。'],
    ['最小间隔', '距上次网络命中不足最小间隔的手动刷新被合并（live 2 s / overview 5 s）：连按十次刷新只发一次。'],
    ['失败退避', '连续失败把下次间隔按 ×2 放大（上限 8×），成功即复位。节点宕了以后轮询频率降到 1/8。'],
    ['隐藏标签页暂停', '文档隐藏时跳过 tick，重新可见的第一拍立即补一次。开一夜的标签页不会变成后台负载发生器。'],
    ['内容不变则不重绘', '载荷做 FNV 哈希，订阅者被告知是否**真的**变了；稳态轮询不重绘，也就不会打断你正在进行的操作。'],
  ];
  return card({
    title: '刷新策略',
    note: '只读 · 属性来自 core/store.js 与 core/sources.js',
    actions: flag('ok', '只读'),
    body:
      kv([
        ['实时轮询', '/api/v2/live · 20 s'],
        ['总览轮询', '/api/v2/overview · 60 s'],
        ['配置轮询', '/api/lab · 按需（interval 0）'],
      ])
      + notice('info', '这里的旋钮是只读的',
          '以下五条是 core/store.js 的实际行为，不是可配置项——本页不提供改变它们的控件。')
      + `<div class="rows" style="margin-top:var(--sp-2)">`
        + guards.map(([name, sub]) => row({ name: esc(name), sub: esc(sub) })).join('')
      + `</div>`,
  });
}

function endpointsCard(data) {
  const rows = (data.endpoints || []).map((e) => {
    const urlCell = e.base_url
      ? { __raw: true, value: `<span class="mono truncate" style="display:inline-block;max-width:32ch;vertical-align:bottom">${esc(e.base_url)}</span>` }
      : DASH;
    return [
      { __raw: true, value: `<div style="font-weight:var(--fw-medium)">${esc(e.label || e.key)}</div>`
        + `<div class="src" style="margin-top:0">${esc(e.key)}</div>` },
      badge(e.kind || DASH),
      urlCell,
      { __raw: true, value: e.enabled ? badge('启用', 'on') : badge('停用', 'off') },
      { __raw: true, value: e.key_present ? badge('已配置', 'on') : badge('未找到', 'off') },
      { __raw: true, value: e.key_sha256_12
          ? `<span class="mono">${esc(e.key_sha256_12)}</span>` : DASH },
    ];
  });
  if (!rows.length) {
    return card({ title: '端点', body: `<div class="state"><div class="state-msg">配置中没有 endpoints 块。</div></div>` });
  }
  return card({
    title: '端点',
    note: '密钥值永不出现',
    body: table(['端点', '类型', 'base_url', '启用', '凭据', '指纹'], rows)
      + `<div style="margin-top:var(--sp-4);display:flex;align-items:flex-start;gap:var(--sp-3);font-size:var(--fs-xs);color:var(--ink-muted)">`
        + hint('指纹 = sha256(密钥)[:12]。它足以回答「加载的是哪一把钥匙」，但答不出钥匙本身。')
        + `<span>「指纹」是 <span class="mono">sha256(key)[:12]</span>：它标识加载的是<strong>哪一把</strong>密钥，`
        + `而不泄露它。<span class="mono">—</span> 表示没有可用的凭据事实（而不是「密钥为空」）。</span>`
      + `</div>`,
  });
}

function nodePortalCards(data) {
  const n = data.node || {};
  const nodeCard = card({
    title: '节点',
    note: '经 SSH 只读访问',
    body: kv([
      ['label', n.label],
      ['host', n.host],
      ['hostname', n.hostname],
      ['ssh_user', n.ssh_user],
      // 这是**路径**，不是密钥材料。显示路径有用；内容从不读取、从不渲染。
      ['ssh_key', n.ssh_key],
      ['ssh_known_hosts', n.ssh_known_hosts],
      ['ssh_fingerprint', n.ssh_fingerprint],
      ['ssh_timeout_s', n.ssh_timeout_s != null ? `${int(n.ssh_timeout_s)} s` : DASH],
      ['ssh_probe_timeout_s', n.ssh_probe_timeout_s != null ? `${int(n.ssh_probe_timeout_s)} s` : DASH],
      ['probe_cache_ttl_s', n.probe_cache_ttl_s != null ? `${int(n.probe_cache_ttl_s)} s` : DASH],
    ]) + `<div style="margin-top:var(--sp-4)">${src('ssh_key 是路径，不是密钥；私钥字节从不进入浏览器')}</div>`,
  });

  const p = data.portal || {};
  const portalCard = card({
    title: '门户',
    note: '本控制台自身',
    body: kv([
      ['host', p.host],
      ['port', p.port != null ? String(p.port) : DASH],
      ['startup_timeout_s', p.startup_timeout_s != null ? `${int(p.startup_timeout_s)} s` : DASH],
    ])
      + notice('info', '只绑定 localhost',
          'start.ps1 在解析配置时拒绝非 loopback 的 host——这个控制台没有鉴权，绑到网络可达的地址等于把节点的管理面交出去。'),
  });

  return grid(2, nodeCard + portalCard);
}

function pathsPortsCards(data) {
  const paths = data.paths || {};
  const pathsCard = card({
    title: '路径',
    note: '项目内的相对路径',
    body: kv([
      ['reports', paths.reports],
      ['benchmarks', paths.benchmarks],
      ['logs', paths.logs],
      ['metrics', paths.metrics],
    ]),
  });

  // 端口的**含义**来自配置（门户/服务/隧道各自声明的端口），而 /api/env 的
  // 探测只回答「有没有被占用」。两件事分开，谁也不冒充谁。
  const purpose = new Map();
  const add = (port, why) => { if (port != null && !purpose.has(String(port))) purpose.set(String(port), why); };
  add((data.portal || {}).port, '门户控制台');
  for (const [k, s] of Object.entries(data.services || {})) add(s?.port, `服务 · ${s?.label || k}`);
  for (const [k, t] of Object.entries(data.tunnels || {})) add(t?.local_port, `隧道 · ${t?.label || k}`);
  for (const port of (data.ports?.must_be_free || [])) add(port, '必须空闲（未在别处命名）');
  for (const port of (data.ports?.forwarded || [])) add(port, '端口转发');

  const mustFree = new Set((data.ports?.must_be_free || []).map(String));
  const probes = Array.isArray(data.env?.ports) ? data.env.ports : [];
  const rows = probes.map((pr) => {
    const inUse = pr.in_use === true;
    // 一个「必须空闲」的端口被占用 = 问题；其余被占用 = 预期（门户/服务本就该在听）。
    const tone = mustFree.has(String(pr.port)) ? (inUse ? 'warn' : 'on') : (inUse ? 'on' : 'off');
    return [
      pr.port != null ? String(pr.port) : DASH,
      purpose.get(String(pr.port)) || '未在配置中命名',
      { __raw: true, value: badge(inUse ? '占用中' : '空闲', tone) },
    ];
  });

  const portsCard = card({
    title: '端口',
    note: '占用来自 /api/env 探测',
    body:
      (rows.length
        ? table(['端口', '用途', '探测'], rows)
        : `<div class="state"><div class="state-msg">环境自检没有返回端口探测结果。</div></div>`)
      + `<div class="chips" style="margin-top:var(--sp-5)">`
        + badge(`必须空闲 ${(data.ports?.must_be_free || []).join(' · ') || DASH}`)
        + badge(`端口转发 ${(data.ports?.forwarded || []).join(' · ') || DASH}`)
      + `</div>`
      + `<div style="margin-top:var(--sp-4)">${src('/api/env · probe_local_ports() 只报告 {port, in_use}；含义取自配置')}</div>`,
  });

  return grid(2, pathsCard + portsCard);
}

function servicesTunnelsCards(data) {
  const services = data.services || {};
  const serviceCards = Object.entries(services).map(([key, s]) => {
    const launch = Array.isArray(s.launch) ? s.launch.join(' ') : '';
    return card({
      title: s.label || key,
      note: key,
      actions: s.enabled ? badge('启用', 'on') : badge('停用', 'off'),
      body: kv([
        ['port', s.port != null ? String(s.port) : DASH],
        ['url', s.url],
        ['enabled', s.enabled === true ? 'true' : s.enabled === false ? 'false' : DASH],
        ['managed', s.managed === true ? 'true' : s.managed === false ? 'false' : DASH],
        ['cwd', s.cwd],
        ['start_policy', s.start_policy],
        ['note', s.note],
      ])
        + (launch
          ? `<div class="src" style="margin-top:var(--sp-5);margin-bottom:var(--sp-2)">`
              + `launch · 门户唯一能运行的命令，来自配置文件，不来自浏览器</div>`
            + `<code class="mono" style="display:block;padding:var(--sp-4);border:1px solid var(--hair);`
              + `border-radius:var(--r-sm);background:var(--surface-sunken);white-space:pre-wrap;overflow-wrap:anywhere">${esc(launch)}</code>`
          : ''),
    });
  });

  const tunnels = data.tunnels || {};
  const tunnelCards = Object.entries(tunnels).map(([key, t]) => card({
    title: t.label || key,
    note: key,
    actions: t.enabled ? badge('启用', 'on') : badge('停用', 'off'),
    body: kv([
      ['local_port', t.local_port != null ? String(t.local_port) : DASH],
      ['remote', (t.remote_host && t.remote_port) ? `${t.remote_host}:${t.remote_port}` : (t.remote || DASH)],
      ['fallback_url', t.fallback_url],
    ]),
  }));

  const left = serviceCards.length ? serviceCards : [`<div class="state"><div class="state-msg">配置中没有 services 块。</div></div>`];
  const right = tunnelCards.length ? tunnelCards : [`<div class="state"><div class="state-msg">配置中没有 tunnels 块。</div></div>`];

  return grid(2,
    `<div style="display:flex;flex-direction:column;gap:var(--sp-5)">`
      + `<div style="font-size:var(--fs-sm);color:var(--ink-muted)">服务定义</div>`
      + left.join('')
      + `<div style="margin-top:var(--sp-2)">${src('launch 的 argv 由服务端从 services.*.launch 读出并替换 {port}/{host}，绝不来自请求')}</div>`
    + `</div>`
    + `<div style="display:flex;flex-direction:column;gap:var(--sp-5)">`
      + `<div style="font-size:var(--fs-sm);color:var(--ink-muted)">隧道定义</div>`
      + right.join('')
    + `</div>`);
}

function securityCard() {
  const items = [
    '门户只绑定 <span class="mono">localhost</span>：<span class="mono">start.ps1</span> 解析配置时拒绝非 loopback 的 host。',
    '改变状态的请求必须带请求头 <span class="mono">X-Requested-With: llm-lab-portal</span>。跨源 <span class="mono">fetch</span> 一旦带上自定义头，浏览器会先发 preflight，而本服务不应答 preflight——于是这个请求在到达之前就被挡下。',
    '全项目只有一个改变状态的端点：<span class="mono">POST /api/service/start</span>。它的 argv 来自配置白名单（<span class="mono">services.*.launch</span>），从不来自请求内容；非白名单的服务名一律 404。',
    '资源与报告路径中的 <span class="mono">..</span> 一律拒绝（<span class="mono">_resolve_public()</span> 的两道闸门：denylist + 前缀 allowlist）。',
    '凭据：只有 <span class="mono">key_present</span> 与 <span class="mono">key_sha256_12</span> 会被送到浏览器；密钥值、<span class="mono">secrets.env</span> 的内容一律不出现在响应体、日志、报告或截图中。',
    '没有任意 shell 执行能力：全项目只有一条硬编码的只读远程探针（<span class="mono">REMOTE_PROBE</span>），读 <span class="mono">/proc</span> / <span class="mono">df</span> / <span class="mono">ss</span> / <span class="mono">nvidia-smi</span> 之类。',
    '节点原生 UI 的 API Key 存在它自己的 <span class="mono">localStorage</span>（按源隔离），由用户手输；本控制台从不代替用户注入。',
  ];
  return card({
    title: '安全边界',
    note: '静态事实卡 · 与 docs/security.md 一致',
    actions: flag('ok', '与安全文档一致'),
    body: `<ul style="margin:0;padding-left:var(--sp-7);line-height:1.8;color:var(--ink-2)">`
      + items.map((t) => `<li style="margin-bottom:var(--sp-3)">${t}</li>`).join('')
      + `</ul>`
      + `<div style="margin-top:var(--sp-5)">${src('docs/security.md · scripts/selftest.py 的 13 道安全闸门 + 2 道阳性对照')}</div>`,
  });
}

/* --------------------------------------------------------------- render */

export async function render(host) {
  const scope = makeScope();
  let destroyed = false;
  let offTheme = null;
  let segCtrl = null;

  /* 本页七段里，三段是纯本地事实（外观／刷新策略／安全边界），随时可以立刻
     画出来；另外四段要走 /api/v2/settings —— 它要顺序跑几个各 20 s 上限的
     子进程，实测 7–19 s，门户忙时更久。所以首屏先画本地三段、只给服务端四段
     放骨架，而不是整页一堵骨架墙：等满 25 s 也只看到骨架的那一格截图，就是
     这么来的。
       data 为真 = 已到货；data 为 null 且无 err = 还在读；带 err = 读取失败。 */
  function draw(data, err) {
    if (destroyed) return;
    const pending = (fn) => (data ? fn(data) : skeleton(3, { block: true }));
    // 四段共用同一个请求，所以失败时只报一次，不逐段重复同一张错误卡。
    const remote = err
      ? band({ title: '服务端配置', sub: '无法读取', body: errorState(err, { retry: () => load() }) })
      : band({ title: '端点', sub: '来自配置；只暴露凭据的存在性与指纹', body: pending(endpointsCard) })
        + band({ title: '节点与门户', sub: '经 SSH 只读；门户只绑 loopback', body: pending(nodePortalCards) })
        + band({ title: '路径与端口', sub: '项目内相对路径与端口占用探测', body: pending(pathsPortsCards) })
        + band({ title: '服务与隧道定义', sub: '来自配置文件；argv 由服务端持有', body: pending(servicesTunnelsCards) });
    host.innerHTML =
      band({ title: '外观', sub: '只改变这个浏览器的显示，不改服务器', body: grid(2, appearanceCard() + vocabularyCard()) })
      + band({ title: '刷新策略', sub: 'shell 的轮询行为；只读', body: refreshPolicyCard() })
      + remote
      + band({ title: '安全边界', sub: '这个控制台实际做的事与不做的事', body: securityCard() })
      + `<footer class="page-foot">
           <span>数据来源：<span class="mono">/api/v2/settings</span> · <span class="mono">/api/env</span> · <span class="mono">core/store.js</span></span>
           <span>${esc(data?.lab?.name || '')}</span>
         </footer>`;
    wireTheme();
  }

  /** 主题控件与「当前值」的同步。只改两个小节点，不重绘整页。 */
  function syncThemeUI() {
    if (destroyed) return;
    const mode = readStored() || 'auto';
    const label = (MODES.find((m) => m.value === mode) || MODES[2]).label;
    const now = host.querySelector('#themeNow');
    if (now) now.textContent = label;
    const eff = host.querySelector('#themeEffective');
    if (eff) eff.textContent = theme() === 'light' ? '浅色' : '深色';
    const seg = host.querySelector('#settingTheme');
    const idx = MODES.findIndex((m) => m.value === mode);
    if (seg && segCtrl) segCtrl.select(idx < 0 ? 2 : idx);
  }

  function onThemeChoice(value) {
    if (value === 'auto') {
      // 跟随系统 = 清掉显式选择，然后立刻套用当前 OS 偏好（不持久化）。此后
      // initTheme 的 OS 监听会继续跟随，因为 localStorage 里已经没有选择了。
      clearStored();
      const mq = window.matchMedia('(prefers-color-scheme: dark)');
      setTheme(mq.matches ? 'dark' : 'light', { persist: false });
    } else {
      setTheme(value);            // 默认持久化
    }
    // setTheme 在值未变时会提前返回、不触发 onTheme，所以这里主动同步一次。
    syncThemeUI();
    const label = (MODES.find((m) => m.value === value) || MODES[2]).label;
    toast(`外观已切换为「${label}」`, { kind: 'good', ms: 1800 });
  }

  function wireTheme() {
    const seg = host.querySelector('#settingTheme');
    if (seg) {
      segCtrl = wireSegmented(seg, onThemeChoice);
    }
    syncThemeUI();
    // 顶栏的切换按钮也会改主题；onTheme 让我们把这个变化反映到「当前值」上。
    offTheme?.();
    offTheme = onTheme(() => syncThemeUI());
  }

  async function load() {
    try {
      // env_report() 会顺序跑几个子进程（各 20 s 上限），所以给它足够的时间。
      const data = await get('/api/v2/settings', { timeout: 90000, signal: scope.signal });
      if (destroyed) return;
      draw(data);
    } catch (err) {
      if (destroyed || isAborted(err)) return;
      draw(null, err);
    }
  }

  draw(null);   // 本地三段立即是真内容，服务端四段是骨架
  load();

  return {
    // settings 没有自己的轮询：只挂一个 onTheme 订阅和一个一次性请求的作用域。
    destroy() {
      destroyed = true;
      offTheme?.();
      scope.abort();
    },
  };
}
