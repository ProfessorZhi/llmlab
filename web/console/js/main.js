/*! web/console/js/main.js — 控制台入口：会话、边栏导航与路由。 */
import { h, clear, put } from './dom.js';
import { PATHS, loadMe, loadIntegrations, login, safeJumpUrl, send, rememberCsrf } from './data.js';
import { DEMO_ME } from './demo.js';
import { navIcon, brandMark, emptyState } from './ui.js';
import * as pages from './pages.js';
import * as gateway from './gateway.js';
import { detail as serviceDetail } from './service.js';
import { resolveDataMode, DATA_MODES } from '../../js/demo-mode.js';
import { isQaBuild } from '../../js/build-profile.js';

// Same gate as the shell: samples need the QA artifact (qa.html) AND an explicit ?demo=1.
const DEMO = resolveDataMode(location.search, { qa: isQaBuild(document) }) === DATA_MODES.DEMO;

const NAV = [
  { group: null, items: [
    { route: '#/overview', label: '概览', icon: 'overview', color: '#007AFF', page: pages.overview },
    { route: '#/usage', label: '资源监控', icon: 'usage', color: '#30B0C7', page: pages.usage },
    { route: '#/models', label: '模型库', icon: 'models', color: '#34C759', page: pages.models },
    { route: '#/services', label: '推理服务', icon: 'services', color: '#5856D6', page: pages.services },
    { route: '#/evaluation', label: '模型评测', icon: 'evaluation', color: '#FF2D55', page: pages.evaluation },
    { route: '#/plugins', label: '插件', icon: 'extensions', color: '#636366', page: pages.extensions },
  ] },
  { group: '网关', items: [
    { route: '#/gateway', label: '网关概览', icon: 'gateway', color: '#007AFF', page: gateway.overview },
    { route: '#/gateway/models', label: '模型路由', icon: 'route', color: '#AF52DE', page: gateway.models },
    { route: '#/gateway/keys', label: 'API 密钥', icon: 'keys', color: '#FF9500', page: gateway.keys },
    { route: '#/gateway/logs', label: '调用日志', icon: 'requests', color: '#34C759', page: gateway.logs },
    { route: '#/gateway/playground', label: '在线体验', icon: 'chat', color: '#FF2D55', page: gateway.playground },
  ] },
  { group: '管理', admin: true, items: [
    { route: '#/admin/users', label: '用户与权限', icon: 'users', color: '#8E8E93', page: pages.users },
  ] },
];
const EXTRA_ROUTES = {
  '#/services/register': { parent: '#/services', page: pages.register, label: '登记推理服务' },
  '#/gateway/logs/full': { parent: '#/gateway/logs', page: gateway.logsFull, label: '全量日志' },
  '#/account': { parent: '#/account', page: pages.account, label: '我的账户' },
  // 旧地址：API 密钥与请求日志已并入「网关」。
  '#/keys': { redirect: '#/gateway/keys' },
  '#/requests': { redirect: '#/gateway/logs' },
};

const ctx = { demo: DEMO, me: null, navigate: (r) => { location.hash = r; }, isAdmin: () => isAdmin(), invalidateLaunch: () => { launchLinks = null; }, rerender: () => render() };
const root = document.getElementById('app');

function isAdmin() {
  const me = ctx.me;
  return me?.is_platform_admin === true || me?.role === 'admin' || me?.role === 'platform_admin' || (me?.roles ?? []).includes('admin');
}

function sidebar(active) {
  const name = ctx.me?.display_name || ctx.me?.username || ctx.me?.subject || '—';
  return h('nav', { class: 'sidebar', 'aria-label': '主导航' },
    h('div', { class: 'brand' }, brandMark(), h('div', {}, h('div', { class: 'brand-name' }, 'LLM Lab'), h('div', { class: 'brand-sub' }, 'RTX A6000 工作站'))),
    NAV.filter((g) => !g.admin || isAdmin()).map((g) => [
      g.group ? h('div', { class: 'nav-group' }, g.group) : null,
      g.items.map((it) => h('a', { class: 'nav-item', href: it.route, 'aria-current': it.route === active ? 'page' : null },
        navIcon(it.icon, it.color), it.label)),
    ]),
    quickLaunch(),
    h('div', { class: 'sidebar-foot' },
      h('span', { class: 'avatar', 'aria-hidden': 'true' }, name.slice(0, 1)),
      h('div', { class: 'who' }, h('a', { href: '#/account', class: 'who-name', 'aria-current': active === '#/account' ? 'page' : null }, name),
        h('div', {}, [ctx.me?.username ?? ctx.me?.linux_user, isAdmin() ? '管理员' : '成员'].filter(Boolean).join(' · ')),
        h('button', { type: 'button', class: 'linkbtn', onclick: logout }, '退出登录'))));
}

/* 快捷入口：自带网页界面的插件（管理员在「插件」页填好的跳转地址），点开即在新标签页打开。 */
let launchLinks = null;
let launchOpen = true;
try { launchOpen = localStorage.getItem('llmlab.launch.open') !== '0'; } catch { /* 无存储时默认展开 */ }

async function loadLaunchLinks() {
  if (DEMO) {
    launchLinks = [{ key: 'strata', name: 'Strata 控制台', ui_url: '/strata/' }, { key: 'grafana', name: 'Grafana', ui_url: '/grafana/' }, { key: 'evalscope', name: 'EvalScope 报告', ui_url: '/evalscope/' }];
    return;
  }
  const r = await loadIntegrations();
  launchLinks = r.state === 'ready' ? r.value.filter((row) => safeJumpUrl(row.ui_url)) : [];
}

function quickLaunch() {
  if (!launchLinks || !launchLinks.length) return null;
  const list = h('div', { class: 'launch-list', hidden: !launchOpen }, launchLinks.map((row) => h('a', {
    class: 'nav-item', href: safeJumpUrl(row.ui_url), target: '_blank', rel: 'noopener noreferrer',
    'aria-label': `打开 ${row.name}（新标签页）`,
  }, navIcon('launch', '#8E8E93'), row.name, h('span', { class: 'ext-mark', 'aria-hidden': 'true' }, '↗'))));
  const toggle = h('button', {
    type: 'button', class: 'nav-group launch-toggle', 'aria-expanded': String(launchOpen),
    onclick: () => {
      launchOpen = !launchOpen;
      try { localStorage.setItem('llmlab.launch.open', launchOpen ? '1' : '0'); } catch { /* 忽略 */ }
      toggle.setAttribute('aria-expanded', String(launchOpen)); list.hidden = !launchOpen;
    },
  }, h('span', { style: { display: 'flex', 'align-items': 'center' } }, '快捷入口', h('span', { class: 'chev', 'aria-hidden': 'true' }, '›')));
  return [toggle, list];
}

function findRoute(full) {
  const hash = full.split('?')[0];
  for (const g of NAV) for (const it of g.items) if (it.route === hash) return { ...it, adminOnly: !!g.admin };
  if (EXTRA_ROUTES[hash]) return { route: hash, ...EXTRA_ROUTES[hash] };
  const svc = /^#\/services\/([^/]+)$/.exec(hash);
  if (svc) {
    const id = decodeURIComponent(svc[1]);
    return { route: hash, parent: '#/services', label: '推理服务', page: (c) => serviceDetail(c, id) };
  }
  return null;
}

async function render() {
  const hash = location.hash || '#/overview';
  if (!DEMO && (!ctx.me || hash === '#/login')) { renderLogin(); return; }
  if (ctx.me?.must_change_password) { renderChangePassword(); return; }
  if (launchLinks === null) await loadLaunchLinks();
  const route = findRoute(hash) ?? findRoute('#/overview');
  if (route.redirect) { location.replace(route.redirect); return; }
  const active = route.parent ?? route.route;
  const main = h('main', { class: 'main', id: 'main', tabindex: -1 }, h('div', { class: 'page' }, h('p', { class: 'muted' }, '正在加载…')));
  put(clear(root), h('div', { class: 'app' }, sidebar(active), main));
  document.title = `${route.label} · LLM Lab`;
  let content;
  try {
    if (route.adminOnly && !isAdmin()) content = h('div', { class: 'page' }, emptyState('denied'));
    else content = await route.page(ctx);
  } catch (err) {
    console.error(err);
    content = h('div', { class: 'page' }, emptyState('error'));
  }
  if (DEMO) {
    content.prepend(h('div', { class: 'banner', role: 'note' }, h('strong', {}, '演示数据'), '当前页面显示的是示例数据，不代表实验室的真实状态。去掉地址中的 demo=1 即可查看真实数据。'));
  }
  put(clear(main), content);
}

function renderLogin(message = null) {
  const st = { username: '', password: '', error: message, busy: false };
  const err = h('p', { class: 'form-error', role: 'alert' }, st.error ?? '');
  const submit = async (e) => {
    e.preventDefault();
    if (!st.username || !st.password) { err.textContent = '请输入用户名和密码。'; return; }
    btn.disabled = true; btn.textContent = '正在登录…';
    const r = await login(st.username, st.password);
    btn.disabled = false; btn.textContent = '登录';
    if (!r.ok) {
      err.textContent = r.status === 401 || r.status === 400 ? '用户名或密码不正确。'
        : r.status === 429 ? '尝试次数过多，请稍后再试。'
          : r.state === 'not_configured' ? '本部署尚未启用账户登录，请联系管理员。' : '登录失败，请稍后再试。';
      return;
    }
    if (r.data?.csrf_token) rememberCsrf(r.data.csrf_token);
    ctx.me = r.data?.user ?? null;
    location.hash = '#/overview';
    render();
  };
  const btn = h('button', { type: 'submit', class: 'btn primary' }, '登录');
  put(clear(root), h('main', { class: 'login' }, h('div', { class: 'login-card' },
    brandMark(64),
    h('div', { style: { 'text-align': 'center' } }, h('h1', { style: { margin: 0, 'font-size': '28px', 'font-weight': 600 } }, '登录 LLM Lab'),
      h('p', { class: 'muted', style: { margin: '4px 0 0', 'font-size': '15px' } }, 'RTX A6000 工作站')),
    h('form', { onsubmit: submit, novalidate: true },
      h('div', { class: 'fields' },
        h('label', {}, h('span', {}, '用户名'), h('input', { autocomplete: 'username', autofocus: true, oninput: (e) => { st.username = e.target.value.trim(); } })),
        h('label', {}, h('span', {}, '密码'), h('input', { type: 'password', autocomplete: 'current-password', oninput: (e) => { st.password = e.target.value; } }))),
      btn, err),
    h('p', { class: 'muted', style: { margin: 0, 'text-align': 'center', 'font-size': '13px' } }, '如需开通账户或重置密码，请联系实验室管理员。'))));
  document.title = '登录 · LLM Lab';
}

function renderChangePassword() {
  const st = { current: '', next: '', confirm: '' };
  const err = h('p', { class: 'form-error', role: 'alert' });
  const submit = async (e) => {
    e.preventDefault();
    if (st.next.length < 10) { err.textContent = '新密码至少 10 个字符。'; return; }
    if (st.next !== st.confirm) { err.textContent = '两次输入的新密码不一致。'; return; }
    const r = await send(PATHS.changePassword, { current_password: st.current, new_password: st.next });
    if (!r.ok) { err.textContent = r.status === 401 || r.status === 400 ? '当前密码不正确，或新密码不符合要求。' : '修改失败，请稍后再试。'; return; }
    // 改密后服务端注销了全部旧会话并签发新会话；以返回的身份为准。
    ctx.me = r.data?.user ?? { ...ctx.me, must_change_password: false };
    location.hash = '#/overview';
    render();
  };
  const input = (key, label, ac) => h('label', {}, h('span', {}, label), h('input', { type: 'password', autocomplete: ac, oninput: (e) => { st[key] = e.target.value; } }));
  put(clear(root), h('main', { class: 'login' }, h('div', { class: 'login-card' },
    brandMark(64),
    h('div', { style: { 'text-align': 'center' } }, h('h1', { style: { margin: 0, 'font-size': '28px', 'font-weight': 600 } }, '设置新密码'),
      h('p', { class: 'muted', style: { margin: '4px 0 0', 'font-size': '15px' } }, '您正在使用管理员提供的临时密码，请先设置新密码。')),
    h('form', { onsubmit: submit, novalidate: true },
      h('div', { class: 'fields' }, input('current', '临时密码', 'current-password'), input('next', '新密码（至少 10 个字符）', 'new-password'), input('confirm', '确认新密码', 'new-password')),
      h('button', { type: 'submit', class: 'btn primary' }, '保存'), err))));
}

async function logout() {
  if (!DEMO) await send(PATHS.logout, {});
  rememberCsrf(null);
  launchLinks = null;
  ctx.me = null;
  location.hash = '#/login';
  render();
}

async function boot() {
  if (DEMO) {
    ctx.me = { ...DEMO_ME };
  } else {
    const r = await loadMe();
    ctx.me = r.state === 'ready' ? r.value : null;
  }
  window.addEventListener('hashchange', render);
  render();
}

boot();
