/**
 * tests/integration_auth/browser.preview.test.mjs
 *
 * 真实浏览器 × 真实 Python 后端的最小端到端验收。
 *
 * 与 `tests/auth-ui/browser.auth-ui.test.mjs` 的区别在于**对面是谁**：
 * 那边对面是逐条复刻契约的 Node 替身，这边对面是 `dev/preview_auth.py`
 * 启动的**真 WSGI 服务**——真 `backend.auth.routes.Application`、真 sqlite、
 * 真 Cookie、真 CSRF 校验、真 Feishu 浏览器绑定 Cookie（离线 mock）。
 * 因此这里能验到替身验不到的东西：
 *
 *  - 无头 Chromium 在一个真实域上完成一次真实管理员登录（`X-CSRF-Token` 头
 *    必须被后端接受，否则后端会 403 —— 这条断言由后端把关，而不是由替身自说自话）；
 *  - 会话 Cookie 真的被服务端设为 `HttpOnly`，页面脚本读不到；
 *  - 匿名书签式访问 `/atlas` / `/` 在**真实浏览器**里被服务端 302 到登录页，
 *    DOM 里不存在任何 DEMO 痕迹；
 *  - 离线 mock 飞书走完**浏览器绑定 Cookie** 全流程；换一个浏览器拿同一个回调
 *    URL 是不可能的（那条由 Python 集成测试把关），而**授权与否由服务端裁决**：
 *    0 项目的成员落到拒绝页，有授权的成员落到 DEMO；
 *  - 退出的成功文案必须等真实 200，随后 `/atlas` 重新不可读；
 *  - 未配置飞书时 `/auth/feishu/start` 从浏览器里就是 503，页面如实显示不可用；
 *  - 预览不暴露任何调试/源码入口，全程只有同源请求，无外部请求。
 *
 * 运行：`node --test --test-concurrency=1 tests/integration_auth/browser.preview.test.mjs`
 * 需要本机 Chrome/Chromium（与 auth-ui 那套共用 `helpers/cdp.mjs`）。
 */

import test, { before, after, afterEach } from 'node:test';
import assert from 'node:assert/strict';
import { spawn } from 'node:child_process';
import { randomBytes } from 'node:crypto';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { Browser } from '../auth-ui/helpers/cdp.mjs';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = join(HERE, '..', '..');
const ARTIFACTS = join(HERE, '.artifacts');
const HARNESS = 'tests.integration_auth.preview_e2e_server';

const DEMO_BANNER_ID = 'llm-lab-demo-banner';

/* --------------------------------------------------------------------------
   子进程：真 Python 预览服务
   -------------------------------------------------------------------------- */

/**
 * 启动一个真预览服务，返回 `{origin, close}`。
 *
 * 口令在这里进程内随机生成，经 **stdin 单行 JSON** 交给子进程，
 * 不出现在命令行、不出现在文件、也不打印。子进程 stdout 只回一行 JSON。
 */
async function startPreview({ mockFeishu = false, grantMockMember = false } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'llm-lab-preview-e2e-'));
  const stateDir = join(root, 'state');
  const password = `preview-${randomBytes(24).toString('base64url')}`;

  const child = spawn(process.env.PYTHON ?? 'python', ['-m', HARNESS], {
    cwd: REPO_ROOT,
    stdio: ['pipe', 'pipe', 'pipe'],
    env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUNBUFFERED: '1' },
  });

  let stderr = '';
  child.stderr.setEncoding('utf8');
  child.stderr.on('data', (chunk) => {
    // wsgiref 的访问日志走 stderr；只留作失败时的诊断，绝不外泄到 stdout。
    stderr += chunk;
    if (stderr.length > 64_000) stderr = stderr.slice(-32_000);
  });

  const ready = new Promise((resolve, reject) => {
    let buffer = '';
    let settled = false;
    const timer = setTimeout(() => {
      if (!settled) reject(new Error(`等待预览服务端口超时；stderr:\n${stderr.slice(-2000)}`));
    }, 30_000);
    child.stdout.setEncoding('utf8');
    child.stdout.on('data', (chunk) => {
      if (settled) return;
      buffer += chunk;
      const newline = buffer.indexOf('\n');
      if (newline === -1) return;
      settled = true;
      clearTimeout(timer);
      try {
        resolve(JSON.parse(buffer.slice(0, newline)));
      } catch (error) {
        reject(new Error(`预览服务返回了非 JSON 的就绪行：${buffer.slice(0, 200)}`));
      }
    });
    child.on('exit', (code) => {
      if (!settled) {
        settled = true;
        clearTimeout(timer);
        reject(new Error(`预览服务提前退出（code=${code}）；stderr:\n${stderr.slice(-2000)}`));
      }
    });
  });

  child.stdin.write(
    `${JSON.stringify({
      state_dir: stateDir,
      password,
      mock_feishu: Boolean(mockFeishu),
      grant_mock_member: Boolean(grantMockMember),
    })}\n`,
  );

  const info = await ready;

  return {
    origin: info.origin,
    port: info.port,
    sessionCookie: info.session_cookie,
    password,
    async close() {
      const exited = new Promise((resolve) => child.once('exit', resolve));
      child.stdin.end(); // EOF = 关停信号
      const timer = setTimeout(() => child.kill('SIGKILL'), 8000);
      await exited.catch(() => {});
      clearTimeout(timer);
      rmSync(root, { recursive: true, force: true });
    },
  };
}

/* --------------------------------------------------------------------------
   生命周期
   -------------------------------------------------------------------------- */

/** @type {Awaited<ReturnType<typeof startPreview>>} */
let plain; // 未配置飞书
/** @type {Awaited<ReturnType<typeof startPreview>>} */
let feishu; // 离线 mock 飞书，成员 0 项目
/** @type {Awaited<ReturnType<typeof startPreview>>} */
let granted; // 离线 mock 飞书，成员已获项目授权
/** @type {Browser} */
let browser;

before(async () => {
  [plain, feishu, granted] = await Promise.all([
    startPreview({ mockFeishu: false }),
    startPreview({ mockFeishu: true, grantMockMember: false }),
    startPreview({ mockFeishu: true, grantMockMember: true }),
  ]);
  browser = await Browser.launch();
});

after(async () => {
  await browser?.close();
  await Promise.all([plain, feishu, granted].map((server) => server?.close()));
});

const livePages = new Set();
afterEach(async () => {
  for (const page of livePages) await page.close();
  livePages.clear();
});

/* --------------------------------------------------------------------------
   辅助
   -------------------------------------------------------------------------- */

async function openPage(server, path, { width = 1024, height = 768, dpr = 1 } = {}) {
  const page = await browser.newPage();
  livePages.add(page);
  await page.setViewport(width, height, { dpr, mobile: width < 768 });
  await page.goto(`${server.origin}${path}`);
  return page;
}

/** 登录页就绪：文档已提交且已脱离 boot。 */
async function openLogin(server, query = '') {
  const page = await openPage(server, `/login${query}`);
  await page.waitFor('!!document.getElementById("auth-main")', { label: '登录页文档' });
  await page.waitFor('document.documentElement.dataset.authState !== "boot"', {
    label: '脱离 boot',
  });
  return page;
}

/** 断言：页面里不存在任何 DEMO 痕迹（未授权时绝不允许泄漏）。 */
async function assertNoDemo(page, label) {
  const state = await page.eval(
    `return {
       hasBanner: !!document.getElementById(${JSON.stringify(DEMO_BANNER_ID)}),
       hasAtlas: !!document.getElementById('atlas-title'),
       path: location.pathname,
     };`,
  );
  assert.equal(state.hasBanner, false, `${label}：页面出现了 DEMO 横幅`);
  assert.equal(state.hasAtlas, false, `${label}：页面出现了 DEMO 正文`);
  return state;
}

/** 断言：页面没有横向溢出。 */
async function assertNoOverflow(page, label) {
  const metrics = await page.eval(
    `return { inner: window.innerWidth,
              doc: document.documentElement.scrollWidth,
              body: document.body.scrollWidth };`,
  );
  assert.ok(
    metrics.doc <= metrics.inner && metrics.body <= metrics.inner,
    `${label} 横向溢出：viewport=${metrics.inner} doc=${metrics.doc} body=${metrics.body}`,
  );
}

/** 断言：本次页面只发起过同源请求。 */
function assertOnlyLocalRequests(page, origin, label) {
  for (const entry of page.network) {
    if (entry.url.startsWith('data:') || entry.url.startsWith('about:')) continue;
    assert.ok(entry.url.startsWith(origin), `${label}：出现外部请求 ${entry.url}`);
  }
}

/** 断言：没有 CSP 违规（说明我们给出的策略与页面实际行为一致）。 */
function assertNoCspViolations(page, label) {
  const violations = page.errors.filter((text) => /Content Security Policy/i.test(text));
  assert.deepEqual(violations, [], `${label}：出现 CSP 违规`);
}

/** 断言：localStorage / sessionStorage 里没有任何凭据类内容。 */
async function assertNoCredentialStorage(page, label) {
  const storage = await page.readLocalStorage();
  const forbidden = /token|session|csrf|password|passwd|secret|jwt|bearer|credential|access_key/i;
  for (const [key, value] of Object.entries(storage)) {
    assert.ok(!forbidden.test(key), `${label}：localStorage 出现疑似凭据键 ${key}`);
    assert.ok(!forbidden.test(String(value)), `${label}：localStorage 值疑似凭据 ${key}`);
  }
  assert.deepEqual(await page.eval('return Object.keys(sessionStorage);'), [], `${label}：sessionStorage 非空`);
}

/**
 * 最近一次请求的响应状态；没有该请求时返回 `null`。
 *
 * 按**路径**匹配而不是后缀匹配，因为带 query 的请求（回调、授权页）也属于同一条路由。
 * 注意：CDP 的 `Network.responseReceived` 不会为 302 这一跳单独发出事件
 * （跳转信息挂在下一个请求的 `redirectResponse` 上），因此**重定向本身的状态码
 * 在这里读不到**。需要重定向证据的地方，断言的是「最终落在哪个文档」——
 * 那才是浏览器与用户真正看到的结果；裸 302 由 Python 集成测试把关。
 */
function statusOf(page, method, path) {
  const hit = [...page.network]
    .reverse()
    .find((r) => r.method === method && urlPath(r.url) === path);
  return hit ? hit.status : null;
}

function urlPath(url) {
  try {
    return new URL(url).pathname;
  } catch {
    return url;
  }
}

/** 轮询等待某条请求拿到响应（CDP 记录状态是异步的）。 */
async function waitForStatus(page, method, path, { timeout = 20000 } = {}) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    const status = statusOf(page, method, path);
    if (status !== null) return status;
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  throw new Error(`等待 ${method} ${path} 的响应超时（${timeout}ms）；已记录：${
    page.network.map((r) => `${r.method} ${r.status} ${urlPath(r.url)}`).join(' | ')}`);
}

const REQUEST_LOGOUT = (page) => statusOf(page, 'POST', '/auth/logout');
const REQUEST_LOGIN = (page) => statusOf(page, 'POST', '/auth/local/login');

/**
 * 走一遍离线 mock 飞书的「点击 → 本机授权页 → 回调」这一段。
 *
 * 关键是先等飞书入口按**只读** `/auth/csrf` 的结论变成可用：链接在拿到结论前
 * 处于 `aria-disabled="true"`，此时点击会被页面的守卫吞掉（这正是它该有的行为，
 * 但会让用例停在登录页）。回调之后落到哪里由各用例自己断言——
 * 那是服务端裁决的结果，不该由这个辅助函数预设。
 */
async function signInThroughMockFeishu(page) {
  await page.waitFor('document.documentElement.dataset.authState === "member"', {
    label: '成员入口',
  });
  await page.waitFor(
    'document.getElementById("feishu-start").getAttribute("aria-disabled") !== "true"',
    { label: '飞书入口按 /auth/csrf 如实显示为可用' },
  );
  await page.click('#feishu-start');
  await waitForUrl(page, '/dev/mock/feishu/authorize', '本机 mock 授权页');
  const consent = await page.eval('return document.body.innerText;');
  assert.match(consent, /Mock 飞书授权页/, 'mock 授权页必须是本机页面');
  await page.click('a[href^="/auth/feishu/callback"]');
}
async function waitForUrl(page, pathname, label, { timeout = 20000 } = {}) {
  try {
    await page.waitFor(`location.pathname === ${JSON.stringify(pathname)}`, { timeout, label });
  } catch (error) {
    const where = await page.eval('return location.href;').catch(() => '(页面上下文不可用)');
    const recent = page.network.slice(-8).map((r) => `${r.method} ${r.status} ${urlPath(r.url)}`);
    throw new Error(
      `${error.message}\n  当前 URL：${where}\n  页面错误：${page.errors.join(' | ') || '(无)'}\n`
      + `  最近请求：${recent.join(' | ')}`,
    );
  }
}

/* ==========================================================================
   1. 匿名访问：真实浏览器里也读不到 DEMO
   ========================================================================== */

test('匿名直接用浏览器打开 /atlas 与 /：被服务端送回登录页，DOM 里没有任何 DEMO', async () => {
  for (const path of ['/atlas', '/']) {
    const page = await openPage(plain, path, { width: 1024, height: 768 });
    await page.waitFor('!!document.getElementById("auth-main")', {
      label: `${path} → 登录页`,
    });

    const state = await assertNoDemo(page, `匿名 ${path}`);
    assert.equal(state.path, '/login', `${path} 应落在登录页`);
    const search = await page.eval('return location.search;');
    assert.match(search, /next=/, `${path} 应携带 next`);

    assertOnlyLocalRequests(page, plain.origin, `匿名 ${path}`);
    await page.close();
  }
});

test('匿名 fetch("/atlas") 也一样：跟随跳转后落在登录页，拿不到 DEMO', async () => {
  const page = await openLogin(plain);
  const result = await page.eval(
    `return fetch('/atlas').then((r) => r.text().then((text) => ({
       status: r.status,
       url: r.url,
       hasBanner: text.includes(${JSON.stringify(DEMO_BANNER_ID)}),
     })));`,
  );
  assert.equal(result.hasBanner, false, '匿名 fetch 拿到了 DEMO');
  assert.match(result.url, /\/login/, `跟随跳转后应落在登录页，实际 ${result.url}`);
});

/* ==========================================================================
   2. 真实管理员登录：CSRF 头 + 会话 Cookie + 落地受保护首页
   ========================================================================== */

test('真实管理员登录：后端接受了 X-CSRF-Token（否则会 403），落地受保护首页', async () => {
  const page = await openLogin(plain, '?mode=admin');
  await page.waitFor('document.documentElement.dataset.authState === "admin"', {
    label: '管理员入口',
  });

  await page.fill('#username', 'admin');
  await page.fill('#password', plain.password);
  await page.click('#admin-submit');

  await page.waitFor('location.pathname === "/"', { timeout: 20000, label: '登录后导航' });
  await page.waitFor(`!!document.getElementById(${JSON.stringify(DEMO_BANNER_ID)})`, {
    label: 'DEMO 页就绪',
  });

  // 后端只有在同源 Origin + 有效 X-CSRF-Token 同时具备时才 200；
  // 这个 200 就是「请求头真的被后端收到并校验通过」的证据。
  assert.equal(REQUEST_LOGIN(page), 200, 'POST /auth/local/login 应 200');
  assert.equal(statusOf(page, 'GET', '/auth/csrf'), 200, '写请求前应先取 CSRF');

  assertOnlyLocalRequests(page, plain.origin, '管理员登录');
  assertNoCspViolations(page, 'DEMO 页');
  await assertNoCredentialStorage(page, '管理员登录');
});

test('会话 Cookie 由服务端设为 HttpOnly：页面脚本读不到，也不落任何存储', async () => {
  const page = await openLogin(plain, '?mode=admin');
  await page.fill('#username', 'admin');
  await page.fill('#password', plain.password);
  await page.click('#admin-submit');
  await page.waitFor('location.pathname === "/"', { timeout: 20000, label: '登录完成' });

  const cookie = await page.readCookies();
  assert.ok(
    !cookie.includes(plain.sessionCookie),
    `document.cookie 不应出现会话 Cookie：${cookie}`,
  );
  assert.equal(cookie.includes('llm_lab_session'), false, 'document.cookie 不应出现会话名');
  await assertNoCredentialStorage(page, '会话 Cookie');
});

test('已登录后 /atlas 与 / 都可达，且都是同一份标注为合成的 DEMO', async () => {
  const page = await openLogin(plain, '?mode=admin');
  await page.fill('#username', 'admin');
  await page.fill('#password', plain.password);
  await page.click('#admin-submit');
  await page.waitFor('location.pathname === "/"', { timeout: 20000, label: '登录完成' });

  for (const path of ['/atlas', '/']) {
    await page.goto(`${plain.origin}${path}`);
    await page.waitFor(`!!document.getElementById(${JSON.stringify(DEMO_BANNER_ID)})`, {
      label: `${path} DEMO`,
    });
    const banner = await page.eval(
      `const el = document.getElementById(${JSON.stringify(DEMO_BANNER_ID)});
       return { text: el.textContent, top: el.getBoundingClientRect().top,
                title: document.title };`,
    );
    assert.match(banner.text, /DEMO/, `${path}：横幅必须醒目标注 DEMO`);
    assert.match(banner.text, /合成演示数据/, `${path}：横幅必须说明是合成数据`);
    assert.match(banner.text, /127\.0\.0\.1/, `${path}：横幅必须说明是本机预览`);
    assert.ok(banner.top >= 0 && banner.top < 200, `${path}：横幅必须在正文之前可见`);
  }
  assertOnlyLocalRequests(page, plain.origin, '已登录 DEMO');
});

/* ==========================================================================
   3. 离线 mock 飞书：浏览器绑定 Cookie 全流程 + 服务端裁决
   ========================================================================== */

test('离线 mock 飞书：走完浏览器绑定 Cookie 全流程，0 项目成员落到拒绝页而非 DEMO', async () => {
  const page = await openLogin(feishu);
  await signInThroughMockFeishu(page);
  await waitForUrl(page, '/', '回调落地');

  // 身份由飞书 mock 确认，但授权由服务端裁决：0 项目 ⇒ 拒绝，且不出现 DEMO。
  const title = await page.eval('return document.title;');
  assert.match(title, /项目授权/, `0 项目成员应落到拒绝页，实际标题 ${title}`);
  await assertNoDemo(page, '0 项目成员');
  assertOnlyLocalRequests(page, feishu.origin, 'mock 飞书流程');
  assertNoCspViolations(page, 'mock 飞书流程');
});

test('已授权成员：同一套 mock 飞书流程后可以进入 DEMO（服务端说了算）', async () => {
  const page = await openLogin(granted);
  await signInThroughMockFeishu(page);
  await page.waitFor(`!!document.getElementById(${JSON.stringify(DEMO_BANNER_ID)})`, {
    timeout: 20000,
    label: '已授权成员应进入 DEMO',
  });
  assertOnlyLocalRequests(page, granted.origin, '已授权成员');
  assertNoCspViolations(page, '已授权成员的 DEMO');
});

test('退出登录：等到真实 200 才宣告已退出，随后 /atlas 重新不可读', async () => {
  const page = await openLogin(feishu);
  await signInThroughMockFeishu(page);
  await waitForUrl(page, '/', '回调落地');

  // 带着会话回到登录页 ⇒ 页面自己会认出「已登录但没有项目」
  await page.goto(`${feishu.origin}/login`);
  await page.waitFor('document.documentElement.dataset.authState === "no-project"', {
    timeout: 20000,
    label: 'no-project 视图',
  });

  await page.click('#np-logout');
  await waitForStatus(page, 'POST', '/auth/logout');
  await page.waitFor('document.documentElement.dataset.authState === "logged-out"', {
    timeout: 20000,
    label: 'logged-out 视图（必须等真实 200 之后）',
  });
  assert.equal(REQUEST_LOGOUT(page), 200, 'POST /auth/logout 必须拿到真实 200');

  await page.goto(`${feishu.origin}/atlas`);
  await waitForUrl(page, '/login', '退出后被送回登录页');
  await assertNoDemo(page, '退出后访问 /atlas');
  assert.equal(statusOf(page, 'GET', '/auth/logout'), null, 'GET /auth/logout 不存在');
});

/* ==========================================================================
   4. 未配置飞书：从浏览器里就是 503，页面如实显示不可用
   ========================================================================== */

test('未配置飞书：/auth/feishu/start 从浏览器里返回 503，入口如实显示不可用', async () => {
  const page = await openLogin(plain);
  await page.waitFor('document.documentElement.dataset.authState === "member"', { label: '成员入口' });
  // 可用性结论来自只读 /auth/csrf，等它落到页面上再断言
  await page.waitFor(
    'document.getElementById("feishu-start").getAttribute("aria-disabled") === "true"',
    { label: '入口被如实置为不可用' },
  );

  const probe = await page.eval(
    `return fetch('/auth/feishu/start', { redirect: 'manual' }).then((r) =>
       r.text().then((body) => ({ status: r.status, type: r.type, body })));`,
  );
  // 未配置的应用绝不重定向到任何外部地址（重定向在这里会表现为 opaque 的 status 0）。
  assert.equal(probe.status, 503, `未配置飞书应 503，实际 ${probe.status}`);
  assert.match(probe.body, /not_configured/, '错误码必须是 not_configured');

  const ui = await page.eval(
    `const a = document.getElementById('feishu-start');
     return { disabled: a.getAttribute('aria-disabled'),
              label: document.getElementById('feishu-label').textContent,
              hint: document.getElementById('feishu-hint').textContent };`,
  );
  assert.equal(ui.disabled, 'true', '未配置时入口必须是不可用状态');
  assert.doesNotMatch(ui.label, /使用飞书登录/, '不得显示成可用的飞书登录入口');
  assert.match(ui.hint, /未配置|暂不可用|尚未/, `必须给出真实的不可用说明，实际：${ui.hint}`);
});

test('页面初始化只打 /auth/csrf 与 /auth/me，绝不用有副作用的 /auth/feishu/start 当探针', async () => {
  const page = await openLogin(feishu);
  await page.waitFor('document.documentElement.dataset.authState !== "boot"', { label: '就绪' });

  // `/auth.css` / `/auth.js` / `/auth-core.js` 也以 `/auth` 开头，是静态资源而非接口
  const ASSETS = new Set(['/auth.css', '/auth.js', '/auth-core.js']);
  const paths = page.network
    .filter((entry) => entry.url.startsWith(feishu.origin))
    .map((entry) => new URL(entry.url).pathname)
    .filter((path) => path.startsWith('/auth/') && !ASSETS.has(path));

  assert.ok(!paths.includes('/auth/feishu/start'), `加载页面不得访问 start：${paths.join(', ')}`);
  assert.deepEqual([...new Set(paths)].sort(), ['/auth/csrf', '/auth/me']);
  assertOnlyLocalRequests(page, feishu.origin, '页面初始化');
});

/* ==========================================================================
   5. 预览不是调试服务器
   ========================================================================== */

test('预览不暴露源码 / 数据库 / 任意路径：一律 404', async () => {
  const page = await openLogin(plain);
  const results = await page.eval(
    `const paths = ['/dev/preview_auth.py', '/dev/__init__.py', '/package.json', '/README.md',
                     '/auth.sqlite3', '/admin_password.hash', '/backend/auth/routes.py',
                     '/docs/architecture.md', '/web/auth/auth.js.map', '/__debug__', '/debug',
                     '/../etc/passwd', '/proto-types/v5/index.html'];
     return Promise.all(paths.map((p) => fetch(p).then((r) =>
       r.text().then((text) => ({
         path: p,
         status: r.status,
         leaked: /def |class |BEGIN |PRIVATE KEY/.test(text),
       })))));`,
  );
  for (const row of results) {
    assert.equal(row.status, 404, `${row.path} 必须 404，实际 ${row.status}`);
    assert.equal(row.leaked, false, `${row.path} 泄漏了源码/密钥样本`);
  }
});

test('/auth/* 只暴露约定的路由，其它一律 404，且不含任何调试入口', async () => {
  const page = await openLogin(plain);
  const probe = await page.eval(
    `const paths = ['/auth/debug', '/auth/users', '/auth/tokens', '/auth/__all__',
                     '/auth/local/login/', '/auth/local/register', '/auth/feishu/token',
                     '/auth/me/extra'];
     return Promise.all(paths.map((p) => fetch(p, { redirect: 'manual' })
       .then((r) => ({ path: p, status: r.status }))));`,
  );
  for (const row of probe) {
    assert.equal(row.status, 404, `${row.path} 必须 404，实际 ${row.status}`);
  }
});

/* ==========================================================================
   6. 布局：320 / 390 / 1440
   ========================================================================== */

test('登录页在 320 / 390 / 1440 三档宽度下均无横向溢出', async () => {
  for (const viewport of [
    { width: 320, height: 640, dpr: 2 },
    { width: 390, height: 844, dpr: 3 },
    { width: 1440, height: 900, dpr: 1 },
  ]) {
    const page = await openLogin(plain, '', viewport);
    await assertNoOverflow(page, `登录页 @${viewport.width}`);
    await page.goto(`${plain.origin}/login?mode=admin`);
    await page.waitFor('document.documentElement.dataset.authState === "admin"', { label: '管理员入口' });
    await assertNoOverflow(page, `管理员入口 @${viewport.width}`);
    await page.screenshot(join(ARTIFACTS, `preview-login-${viewport.width}.png`));
    await page.close();
  }
});

test('DEMO 页与拒绝页的横幅在 320px 下都不溢出视口', async () => {
  const page = await openLogin(plain, '?mode=admin', { width: 320, height: 640, dpr: 2 });
  await page.fill('#username', 'admin');
  await page.fill('#password', plain.password);
  await page.click('#admin-submit');
  await page.waitFor(`!!document.getElementById(${JSON.stringify(DEMO_BANNER_ID)})`, {
    timeout: 20000,
    label: 'DEMO 页',
  });
  const banner = await page.eval(
    `const r = document.getElementById(${JSON.stringify(DEMO_BANNER_ID)}).getBoundingClientRect();
     return { width: r.width, inner: window.innerWidth };`,
  );
  assert.ok(banner.width <= banner.inner, `DEMO 横幅溢出：${banner.width} > ${banner.inner}`);
  await page.screenshot(join(ARTIFACTS, 'preview-demo-320.png'));
});

test('拒绝页（0 项目成员）在 320px 下无横向溢出', async () => {
  // 从 /atlas 匿名进入 → 被送回登录页 → 走 mock 飞书 → 落到拒绝页
  const page = await openPage(feishu, '/atlas', { width: 320, height: 640, dpr: 2 });
  await page.waitFor('!!document.getElementById("auth-main")', { label: '登录页' });
  await signInThroughMockFeishu(page);
  await waitForUrl(page, '/', '回调落地');
  await assertNoDemo(page, '0 项目成员 @320');

  await page.setViewport(320, 640, { dpr: 2, mobile: true });
  await assertNoOverflow(page, '拒绝页 @320');
  assertNoCspViolations(page, '拒绝页 @320');
  await page.screenshot(join(ARTIFACTS, 'preview-noproject-320.png'));
});

test('减少动效偏好下页面仍可用，口令错误如实报错且不产生页面错误', async () => {
  const page = await openLogin(plain, '?mode=admin');
  await page.setReducedMotion(true);
  assert.deepEqual(page.errors, [], `出现页面错误：${page.errors.join(' | ')}`);

  await page.fill('#username', 'admin');
  await page.fill('#password', 'not-the-password');
  await page.click('#admin-submit');

  assert.equal(await waitForStatus(page, 'POST', '/auth/local/login'), 401, '错误口令应是 401');
  await page.waitFor('document.getElementById("status-text").textContent.trim() !== ""', {
    timeout: 20000,
    label: '错误提示',
  });
  assert.deepEqual(page.errors, [], `出现页面错误：${page.errors.join(' | ')}`);
});
