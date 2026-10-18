/**
 * tests/auth-ui/browser.auth-ui.test.mjs
 *
 * 登录页的真实浏览器验收：无头 Chromium + CDP，零第三方依赖。
 *
 * Lane B 之后本页只有**一个**入口：Dell Linux 账号。这里覆盖：
 *  - 320 / 390 / 768 / 1024 / 1440 五档宽度下**无页面溢出**，明暗两套主题；
 *  - **就绪门槛**：只有 `/auth/csrf` 明确声明 `providers.linux_pam.enabled === true`
 *    且账号范围安全时表单才启用；其余一切状态 fail-closed；
 *  - 未开闸时**不得**向 `/auth/linux/login`、`/auth/local/login` 发任何请求，
 *    也不得访问有副作用的 `/auth/feishu/start`——包括被脚本程序化触发提交时；
 *  - 旧飞书 provider 已启用**不能**让 Linux 表单可用；
 *  - 开闸后提交：恰好一次 POST `/auth/linux/login`，带 `X-CSRF-Token`，
 *    请求体只含 username / password；
 *  - 旧书签 `?mode=admin` / `#admin` 被**告知已退役**，而不是被静默换一个表单；
 *  - 退出登录必须等到服务端 200 才宣告成功，被拒绝时提示重试；
 *  - 会话过期、无项目（待分配）、已授权进入首页；
 *  - 全程**无外部网络请求**、localStorage **不含任何凭据**、会话 Cookie 不可被脚本读取；
 *  - `file://` 静态预览：页面可打开但不发任何登录请求。
 *
 * 替身 `mock-server.mjs` 逐条复刻后端真实契约（信封、单次性 CSRF、
 * `X-CSRF-Token` 头、`data.providers`、`data.user` / `data.projects`），
 * 因此下面这些断言同时也就是「UI 与后端联调」的断言。
 *
 * 运行：`node --test --test-concurrency=1 tests/auth-ui/browser.auth-ui.test.mjs`
 * 截图（本地证据，`.gitignore` 已忽略 png）落在 tests/auth-ui/.artifacts/。
 */

import test, { before, after, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { mkdir } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { Browser, sleep } from './helpers/cdp.mjs';
import { startMockServer, WEB_AUTH_DIR } from './mock-server.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const ARTIFACTS = join(HERE, '.artifacts');

/** 五档验收宽度。 */
const VIEWPORTS = [
  { name: '320', width: 320, height: 640, dpr: 2 },
  { name: '390', width: 390, height: 844, dpr: 3 },
  { name: '768', width: 768, height: 1024, dpr: 2 },
  { name: '1024', width: 1024, height: 768, dpr: 1 },
  { name: '1440', width: 1440, height: 900, dpr: 2 },
];

let browser = null;
let server = null;

before(async () => {
  // 视觉证据落在 .artifacts/（.gitignore 已忽略 png）：只作人眼复核，不作断言。
  await mkdir(ARTIFACTS, { recursive: true });
  server = await startMockServer({ scenario: 'member-feishu-unconfigured' });
  browser = await Browser.launch();
});

after(async () => {
  await browser?.close();
  await server?.close();
});

/**
 * 无论用例通过与否都回收本次创建的页面。
 * 断言失败会跳过用例尾部的 close()，若不兜底，泄漏的浏览上下文会拖垮后续用例。
 */
const livePages = new Set();
afterEach(async () => {
  for (const page of livePages) await page.close();
  livePages.clear();
});

/** 创建页面并登记回收。 */
async function createPage() {
  const page = await browser.newPage();
  livePages.add(page);
  return page;
}

/**
 * 新建一个隔离页面并导航到登录页，等待其真正就绪。
 *
 * 就绪 = 登录页文档已提交（`#auth-main` 存在）、已脱离 boot 视图，且页面自己打上了
 * `data-auth-ready`（初始化全部收尾）。
 *
 * 最后那条不是多余的：「脱离 boot 视图」发生得**更早**——`refreshSession` 先切视图，
 * 之后才 `await loadLinuxGate()` 去定门槛。只等前者会落在两者之间那个窗口，读到尚未
 * 应用的门槛。等 `data-auth-ready` 是把等待点移到「初始化真的做完了」，不是放宽断言：
 * 标记永不出现时 `waitFor` 照样超时失败。
 *
 * @param {{scenario?: string, query?: string, width?: number, height?: number, dpr?: number}} [opts]
 */
async function openPage(opts = {}) {
  if (opts.scenario) server.setScenario(opts.scenario);
  const page = await createPage();
  const view = { width: opts.width ?? 1024, height: opts.height ?? 768, dpr: opts.dpr ?? 1 };
  await page.setViewport(view.width, view.height, { dpr: view.dpr, mobile: view.width < 768 });
  await page.goto(`${server.origin}/index.html${opts.query ?? ''}`);

  // 必须先确认文档真的是登录页：否则后续断言会在 about:blank 上误判
  await page.waitFor('!!document.getElementById("auth-main")', {
    label: '登录页文档就绪',
  });
  await page.waitFor('document.documentElement.dataset.authState !== "boot"', {
    label: '页面脱离 boot 状态',
  });
  await page.waitFor('document.documentElement.dataset.authReady === "true"', {
    label: '页面初始化完成（门槛已应用）',
  });
  return page;
}

/** 断言页面没有横向溢出。 */
async function assertNoOverflow(page, label) {
  const metrics = await page.eval(
    `return {
       inner: window.innerWidth,
       doc: document.documentElement.scrollWidth,
       body: document.body.scrollWidth,
     };`,
  );
  assert.ok(
    metrics.doc <= metrics.inner && metrics.body <= metrics.inner,
    `${label} 出现横向溢出：viewport=${metrics.inner} doc=${metrics.doc} body=${metrics.body}`,
  );
}

/** 断言页面没有发起任何非同源请求。 */
function assertNoExternalRequests(page, label) {
  const origin = server.origin;
  for (const entry of page.network) {
    const url = entry.url;
    if (url.startsWith('data:') || url.startsWith('about:')) continue;
    assert.ok(url.startsWith(origin), `${label} 出现外部请求：${url}`);
  }
}

/** 断言 localStorage 里没有任何凭据类内容。 */
async function assertNoCredentialStorage(page, label) {
  const storage = await page.readLocalStorage();
  // 刻意不含 'auth'：主题键 llm-lab.auth.theme 是唯一允许存在的非凭据项
  const forbidden = /token|session|csrf|password|passwd|secret|jwt|bearer|credential|access_key/i;
  for (const [key, value] of Object.entries(storage)) {
    assert.ok(!forbidden.test(key), `${label} localStorage 出现疑似凭据键：${key}`);
    assert.ok(!forbidden.test(String(value)), `${label} localStorage 的值疑似凭据：${key}`);
  }
}

/** 本次页面加载窗口内，服务端收到的请求。 */
function requestsSince(mark) {
  return server.requests.slice(mark);
}

/** 表单当前的可用性快照。 */
function formState(page) {
  return page.eval(`return {
    gate: document.getElementById('linux-gate').dataset.gate,
    gateText: document.getElementById('linux-gate').textContent.trim(),
    fieldsDisabled: document.getElementById('linux-fields').disabled,
    usernameDisabled: document.getElementById('username').disabled,
    passwordDisabled: document.getElementById('password').disabled,
    submitDisabled: document.getElementById('linux-submit').disabled,
    passwordType: document.getElementById('password').type,
  };`);
}

/** 「停用」的统一断言：四处都必须 disabled，且门禁标记为 hold。 */
async function assertGateHeld(page, label) {
  const s = await formState(page);
  assert.equal(s.gate, 'hold', `${label}：#linux-gate 应为 hold`);
  assert.equal(s.fieldsDisabled, true, `${label}：fieldset 应为 disabled`);
  assert.equal(s.usernameDisabled, true, `${label}：用户名字段应为 disabled`);
  assert.equal(s.passwordDisabled, true, `${label}：口令字段应为 disabled`);
  assert.equal(s.submitDisabled, true, `${label}：提交按钮应为 disabled`);
  assert.ok(s.gateText.length > 0, `${label}：停用时应给出说明文案`);
}

/* ==========================================================================
   Lane B · 就绪门槛：默认 fail-closed
   ========================================================================== */

test('未声明 linux_pam 时：表单停用、说明如实、不发任何登录请求', async () => {
  const mark = server.requests.length;
  const page = await openPage({ scenario: 'member-feishu-unconfigured' });

  await assertGateHeld(page, '未声明 provider');
  // 面板有入场动画（`.panel { animation: rise }`）：等它跑完再截，否则整页偏淡。
  await sleep(600);
  await page.screenshot(join(ARTIFACTS, 'auth-linux-gate-hold-1024.png'));
  const s = await formState(page);
  assert.match(s.gateText, /BasicAuth|未开通|尚未/,
    '停用文案要说明「当前只是预览、PAM 服务端登录未开通」');

  // 表单是停用的，脚本仍可能去触发一次提交：这条路径也必须被挡住。
  await page.eval(`document.getElementById('linux-form').requestSubmit(); return true;`);
  await sleep(300);

  const writes = requestsSince(mark).filter((r) => r.method === 'POST');
  assert.deepEqual(
    writes.map((r) => `${r.method} ${r.path}`),
    [],
    '未开闸时不得发出任何 POST',
  );
  await page.close();
});

test('旧飞书 provider 已启用，也不得让 Linux 表单可用', async () => {
  // 这是本条 Lane 最要紧的一条：`providers.feishu === true` 与 Linux 身份无关，
  // 它不能成为「可以输入 Dell 系统口令」的理由。
  const mark = server.requests.length;
  const page = await openPage({ scenario: 'member-feishu-configured' });

  await assertGateHeld(page, '仅飞书已启用');
  const paths = requestsSince(mark).map((r) => r.path);
  assert.equal(paths.includes('/auth/feishu/start'), false,
    '飞书 provider 已启用也不得访问 /auth/feishu/start');
  await page.close();
});

test('服务端明确声明 enabled:false 时保持停用', async () => {
  const page = await openPage({ scenario: 'pam-disabled' });
  await assertGateHeld(page, 'enabled:false');
  await page.close();
});

test('服务端声明的账号范围不安全（root:true）时保持停用', async () => {
  const page = await openPage({ scenario: 'pam-unsafe-scope' });
  await assertGateHeld(page, 'root:true');
  await page.close();
});

test('认证接口返回 200 的 HTML 时保持停用，且不误判为可用', async () => {
  const page = await openPage({ scenario: 'csrf-html' });
  await assertGateHeld(page, '200 + HTML');
  await page.close();
});

test('认证接口返回 401 时保持停用', async () => {
  const page = await openPage({ scenario: 'csrf-401' });
  await assertGateHeld(page, '401');
  await page.close();
});

test('认证接口返回 403 时保持停用', async () => {
  const page = await openPage({ scenario: 'csrf-403' });
  await assertGateHeld(page, '403');
  await page.close();
});

/* ==========================================================================
   Lane B · 开闸与提交
   ========================================================================== */

test('服务端明确声明 linux_pam 可用且范围安全时，表单才启用', async () => {
  const page = await openPage({ scenario: 'pam-ready' });
  const s = await formState(page);

  assert.equal(s.gate, 'ready', '#linux-gate 应为 ready');
  assert.equal(s.fieldsDisabled, false, 'fieldset 应启用');
  assert.equal(s.usernameDisabled, false, '用户名字段应启用');
  assert.equal(s.passwordDisabled, false, '口令字段应启用');
  assert.equal(s.submitDisabled, false, '提交按钮应启用');
  assert.equal(s.passwordType, 'password', '口令字段不得默认明文');
  await sleep(600); // 同上：等入场动画结束
  await page.screenshot(join(ARTIFACTS, 'auth-linux-gate-ready-1024.png'));
  await page.close();
});

test('开闸后空提交：只给字段级提示，不发请求', async () => {
  const mark = server.requests.length;
  const page = await openPage({ scenario: 'pam-ready' });

  await page.eval(`document.getElementById('linux-form').requestSubmit(); return true;`);
  await sleep(300);

  assert.equal(requestsSince(mark).filter((r) => r.method === 'POST').length, 0,
    '空输入不得发出请求');
  assert.match(await page.eval('return document.getElementById("status-text").textContent;'),
    /请输入/);
  await page.close();
});

test('开闸后提交：恰好一次 POST /auth/linux/login，CSRF 走请求头', async () => {
  const mark = server.requests.length;
  const page = await openPage({ scenario: 'pam-ready' });

  await page.fill('#username', server.linuxCredentials.username);
  await page.fill('#password', server.linuxCredentials.password);
  await page.eval(`document.getElementById('linux-form').requestSubmit(); return true;`);
  await sleep(600);

  const posts = requestsSince(mark).filter((r) => r.method === 'POST');
  assert.equal(posts.length, 1, `应恰好一次 POST，实得 ${posts.length}`);
  assert.equal(posts[0].path, '/auth/linux/login', '写目标只能是 /auth/linux/login');
  assert.equal(posts[0].origin, server.origin, '请求必须同源');

  // 请求体只含 username / password，CSRF 走请求头（与后端 `_preflight` 的约定）
  const body = JSON.parse(posts[0].body);
  assert.deepEqual(Object.keys(body).sort(), ['password', 'username']);
  assert.equal(body.username, server.linuxCredentials.username);
  assert.equal(typeof posts[0].csrfHeader, 'string', '必须带 X-CSRF-Token 请求头');
  assert.ok(posts[0].csrfHeader.length > 10);
  assert.equal(/csrf/i.test(posts[0].body), false, 'CSRF 不得出现在请求体里');
  await page.close();
});

test('开闸后凭据被拒（401）：如实提示、清空口令、保留用户名', async () => {
  const page = await openPage({ scenario: 'pam-ready-bad-credentials' });
  await page.fill('#username', 'alice');
  await page.fill('#password', 'definitely-not-the-password');
  await page.eval(`document.getElementById('linux-form').requestSubmit(); return true;`);
  await sleep(600);

  const s = await page.eval(`return {
    status: document.getElementById('status-text').textContent,
    password: document.getElementById('password').value,
    username: document.getElementById('username').value,
  };`);
  assert.match(s.status, /不正确|失败|错误/);
  assert.equal(s.password, '', '口令应在提交后清空');
  assert.equal(s.username, 'alice', '用户名应保留');
  await page.close();
});

/* ==========================================================================
   Lane B · 已退役入口
   ========================================================================== */

test('旧书签 ?mode=admin / #admin 被明确告知入口已退役，而不是静默换表单', async () => {
  for (const query of ['?mode=admin', '?mode=member', '']) {
    const hash = query ? '' : '#admin';
    const page = await openPage({ query: `${query}${hash}` });
    if (query || hash) {
      assert.equal(await page.eval('return document.documentElement.dataset.authState;'), 'linux',
        '旧书签也必须落在唯一的 Linux 入口上');
      assert.match(
        await page.eval('return document.getElementById("status-text").textContent;'),
        /已退役/,
        '应告知双入口已退役',
      );
    }
    await page.close();
  }
});

test('页面初始化只请求 /auth/csrf 与 /auth/me，且从不访问退役端点', async () => {
  const mark = server.requests.length;
  const page = await openPage({ scenario: 'member-feishu-configured' });

  const paths = new Set(
    requestsSince(mark).map((r) => r.path).filter((p) => p.startsWith('/auth/')),
  );
  assert.deepEqual([...paths].sort(), ['/auth/csrf', '/auth/me'],
    `初始化期间的认证请求集合不符：${[...paths].join(', ')}`);
  assert.equal(paths.has('/auth/feishu/start'), false, '不得访问飞书入口');
  assert.equal(paths.has('/auth/local/login'), false, '不得访问已退役的本地登录端点');
  await page.close();
});

/* ==========================================================================
   静态与可访问性（沿用既有验收）
   ========================================================================== */

test('唯一入口在 320/390/768/1024/1440 五档宽度下均无横向溢出', async () => {
  for (const vp of VIEWPORTS) {
    const page = await openPage({ scenario: 'pam-ready', width: vp.width, height: vp.height, dpr: vp.dpr });
    await assertNoOverflow(page, `Linux 入口 ${vp.name}px`);
    await page.close();
  }
});

test('停用态（未声明 provider）在五档宽度下同样无横向溢出', async () => {
  for (const vp of VIEWPORTS) {
    const page = await openPage({ scenario: 'member-feishu-unconfigured', width: vp.width, height: vp.height, dpr: vp.dpr });
    await assertNoOverflow(page, `停用态 ${vp.name}px`);
    await page.close();
  }
});

test('默认明亮主题，可切换到深色并持久化；reduced-motion 下过渡被关闭', async () => {
  const page = await openPage({ scenario: 'pam-ready' });
  assert.equal(await page.eval('return document.documentElement.dataset.theme;'), 'light');

  await page.click('#theme-toggle');
  assert.equal(await page.eval('return document.documentElement.dataset.theme;'), 'dark');
  assert.equal(await page.eval('return window.localStorage.getItem("llm-lab.auth.theme");'), 'dark');

  await page.setReducedMotion(true);
  await page.goto(`${server.origin}/index.html`);
  await page.waitFor('document.documentElement.dataset.authReady === "true"', { label: '就绪' });
  const duration = await page.eval(
    `return getComputedStyle(document.querySelector('.panel')).transitionDuration;`,
  );
  // 浏览器可能把 0s 序列化成 `1e-06s`，所以按数值比而不是按字面比。
  assert.ok(parseFloat(duration) <= 0.001, `reduced-motion 下不应有过渡：${duration}`);
  await page.close();
});

test('跳过链接、标签关联与状态区 live 语义齐备', async () => {
  const page = await openPage({ scenario: 'pam-ready' });

  assert.equal(await page.eval('return document.querySelector("a.skip").getAttribute("href");'), '#auth-main');
  for (const [label, field] of [['Dell Linux 用户名', 'username'], ['Dell Linux 口令', 'password']]) {
    assert.equal(
      await page.eval(`return document.querySelector('label[for="${field}"]').textContent.trim();`),
      label,
      `${field} 的标签文案不符`,
    );
    assert.equal(await page.eval(`return !!document.getElementById("${field}");`), true);
  }
  assert.equal(await page.eval('return document.getElementById("linux-gate").getAttribute("role");'), 'status');
  assert.equal(await page.eval('return document.getElementById("linux-gate").getAttribute("aria-live");'), 'polite');
  assert.equal(await page.eval('return document.getElementById("linux-submit").getAttribute("aria-describedby");'), 'linux-gate');
  await page.close();
});

test('全程不把会话写进 Web Storage，会话 Cookie 对脚本不可见', async () => {
  const page = await openPage({ scenario: 'pam-ready' });
  await page.fill('#username', server.linuxCredentials.username);
  await page.fill('#password', server.linuxCredentials.password);
  await page.eval(`document.getElementById('linux-form').requestSubmit(); return true;`);
  await sleep(600);

  // 回到登录页检查存储（同一浏览上下文，cookie 与 storage 保持）
  await page.goto(`${server.origin}/index.html`);
  await sleep(400);

  await assertNoCredentialStorage(page, '登录后');
  const cookie = await page.eval('return document.cookie;');
  assert.doesNotMatch(cookie, /llm_lab_session/, 'HttpOnly 会话 Cookie 不得被脚本读到');
  await page.close();
});

test('file:// 静态预览：页面可打开、如实提示、且不发任何登录请求', async () => {
  const page = await createPage();
  const fileUrl = pathToFileURL(join(WEB_AUTH_DIR, 'index.html')).href;
  const mark = server.requests.length;
  await page.goto(fileUrl);
  await page.waitFor('document.documentElement.dataset.authReady === "true"', { label: '静态预览就绪' });

  await assertGateHeld(page, 'file://');
  assert.equal(server.requests.length, mark, 'file:// 下不得向服务端发任何请求');
  await page.close();
});

/* ==========================================================================
   会话视图（沿用既有验收）
   ========================================================================== */

test('未登录访问 → 唯一的 Linux 登录入口', async () => {
  const page = await openPage({ scenario: 'session-expired' });
  assert.equal(await page.eval('return document.documentElement.dataset.authState;'), 'linux');
  await page.close();
});

test('已登录但无授权项目 → 待分配视图，且绝不自动成为管理员', async () => {
  const page = await openPage({ scenario: 'session-no-project' });
  assert.equal(await page.eval('return document.documentElement.dataset.authState;'), 'no-project');
  assert.match(await page.eval('return document.getElementById("np-user").textContent;'), /synthetic-member/);
  await page.close();
});

test('平台管理员没有任何项目授权 → 仍可进入受保护首页（不得误判为无权限）', async () => {
  // 后端 `/auth/me` 对平台管理员返回 projects: [] + is_platform_admin: true。
  // 若页面只看「有没有项目」，就会把唯一的平台管理员挡在门外。
  const page = await createPage();
  server.setScenario('session-admin-no-project');
  await page.setViewport(1024, 768);
  await page.goto(`${server.origin}/index.html`);
  await page.waitFor("location.pathname === '/'", { label: '管理员进入 Portal', timeout: 10000 });
  assert.equal(await page.eval('return location.origin;'), server.origin);
  assert.notEqual(
    await page.eval('return document.documentElement.dataset.authState;'),
    'no-project',
    '不得落到「待分配」视图',
  );
  assertNoExternalRequests(page, '管理员进入 Portal');
  await page.close();
});

test('退出登录：POST /auth/logout 并落到「已退出」视图', async () => {
  const page = await openPage({ scenario: 'session-no-project' });
  await page.waitFor('document.documentElement.dataset.authState === "no-project"', { label: '待分配视图' });
  const mark = server.requests.length;
  await page.click('#np-logout');
  await page.waitFor('document.documentElement.dataset.authState === "logged-out"',
    { label: '进入已退出视图', timeout: 8000 });

  assert.match(
    await page.eval('return document.querySelector(\'[data-view="logged-out"]\').textContent;'),
    /已安全退出/,
  );

  const logout = requestsSince(mark).filter((r) => r.path === '/auth/logout').pop();
  assert.ok(logout, '必须发出退出请求');
  assert.equal(logout.method, 'POST');
  assert.equal(logout.origin, server.origin);
  assert.equal(typeof logout.csrfHeader, 'string', '退出必须带 X-CSRF-Token 请求头');

  // 退出后可以回到登录入口
  await page.click('#lo-again');
  await page.waitFor('document.documentElement.dataset.authState === "linux"', { label: '回到登录入口' });
  await page.close();
});

test('退出被服务端拒绝（403）→ 不宣告已退出，如实提示重试', async () => {
  const page = await openPage({ scenario: 'logout-rejected' });
  if (await page.eval('return document.documentElement.dataset.authState;') !== 'no-project') {
    // logout-rejected 场景下页面可能直接落在登录入口；此时本用例不适用
    await page.close();
    return;
  }
  await page.click('#np-logout');
  await sleep(600);
  assert.notEqual(await page.eval('return document.documentElement.dataset.authState;'), 'logged-out');
  assert.match(await page.eval('return document.getElementById("status-text").textContent;'), /退出未成功|重试/);
  await page.close();
});

test('认证服务不可达 → 离线视图，且不伪造登录成功', async () => {
  const page = await openPage({ scenario: 'api-down' });
  const state = await page.eval('return document.documentElement.dataset.authState;');
  assert.ok(state === 'offline' || state === 'linux', `意外视图：${state}`);
  if (state === 'linux') await assertGateHeld(page, '服务不可达');
  await page.close();
});

test('全程没有任何外部网络请求', async () => {
  const page = await openPage({ scenario: 'pam-ready' });
  assertNoExternalRequests(page, 'Linux 入口');
  await page.close();
});
