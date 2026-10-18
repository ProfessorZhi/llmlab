/**
 * tests/auth-ui/helpers/cdp.mjs
 *
 * 极简 Chrome DevTools Protocol 客户端：**零第三方依赖**。
 *
 * 只用 Node 24 自带的能力：
 *  - `node:child_process` 启动本机 Chrome/Edge 的无头实例；
 *  - 全局 `WebSocket` 承载 CDP 会话；
 *  - 全局 `fetch` 读取 `/json/version`。
 *
 * 因此测试无需 `npm install`、无需 Playwright，也不会因为联网下载浏览器而失败。
 */

import { spawn } from 'node:child_process';
import { mkdtempSync, readFileSync, existsSync, mkdirSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';

/** 常见浏览器安装位置。按顺序取第一个存在的。 */
const CANDIDATES = [
  process.env.CHROME_PATH,
  'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
  'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
  join(process.env.LOCALAPPDATA ?? '', 'Google/Chrome/Application/chrome.exe'),
  'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
  'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
  '/usr/bin/google-chrome',
  '/usr/bin/chromium',
  '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
].filter(Boolean);

/**
 * 找到可用的 Chromium 可执行文件。
 * @returns {string} 可执行文件绝对路径
 * @throws {Error} 找不到时抛出，并给出可设置的 CHROME_PATH 提示
 */
export function findBrowser() {
  for (const candidate of CANDIDATES) {
    if (candidate && existsSync(candidate)) return candidate;
  }
  throw new Error(
    '未找到 Chrome/Edge。请安装浏览器，或通过环境变量 CHROME_PATH 指定可执行文件路径。',
  );
}

/** 等待若干毫秒。 */
export const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * 单条 CDP 命令的默认超时（毫秒）。
 * 见 {@link Browser.send} 的说明：宁可等久一点，也不要把「机器忙」误判成「浏览器死了」。
 */
export const CDP_TIMEOUT_MS = 60000;

/** 页面导航等待 load 事件的默认超时（毫秒）。 */
export const NAV_TIMEOUT_MS = 30000;

/** 一个 CDP 页面（标签页）会话。 */
export class Page {
  /**
   * @param {Browser} browser
   * @param {string} sessionId
   * @param {string} targetId
   */
  constructor(browser, sessionId, targetId) {
    this.browser = browser;
    this.sessionId = sessionId;
    this.targetId = targetId;
    /** @type {Array<{type: string, text: string}>} 控制台消息 */
    this.console = [];
    /** @type {Array<{method: string, url: string, status: number|null}>} 网络请求 */
    this.network = [];
    /** @type {string[]} 应用级错误：未捕获异常 / console.error / CSP 违规 */
    this.errors = [];
    /**
     * @type {Array<{url: string, status: number}>} 被浏览器记录为「加载失败」的响应。
     * 认证契约里 401 / 403 / 423 / 429 / 503 都是**正常业务信令**，
     * 不能算作页面缺陷，因此单独归类，不混进 {@link Page.errors}。
     */
    this.benignResponseErrors = [];
    this._loadResolvers = [];
    this._closed = false;
    /**
     * 主框架导航计数器。新建 target 时浏览器会先加载 about:blank，
     * 它的 load 事件可能晚于 `Page.enable` 才到达。只用「本次 goto 之后
     * 新发生的导航」触发的 load 事件来判定加载完成，避免在文档提交前就返回。
     */
    this._navCounter = 0;
  }

  /** @private */
  _onEvent(method, params) {
    switch (method) {
      case 'Page.frameNavigated': {
        // 只关心主框架（无 parentId）
        if (params.frame && params.frame.parentId === undefined) this._navCounter += 1;
        break;
      }
      case 'Page.loadEventFired': {
        // 只放行「在发起导航之后才开始的」那些加载，忽略 about:blank 的陈旧事件
        const ready = this._loadResolvers.filter((w) => w.afterSeq < this._navCounter);
        if (ready.length === 0) break;
        this._loadResolvers = this._loadResolvers.filter((w) => w.afterSeq >= this._navCounter);
        for (const waiter of ready) waiter.resolve('load');
        break;
      }
      case 'Runtime.consoleAPICalled': {
        const text = (params.args ?? [])
          .map((a) => a.value ?? a.description ?? a.unserializableValue ?? '')
          .join(' ');
        this.console.push({ type: params.type, text });
        if (params.type === 'error') this.errors.push(`console.error: ${text}`);
        break;
      }
      case 'Runtime.exceptionThrown': {
        const d = params.exceptionDetails ?? {};
        this.errors.push(`exception: ${d.exception?.description ?? d.text ?? 'unknown'}`);
        break;
      }
      case 'Log.entryAdded': {
        const entry = params.entry ?? {};
        this.console.push({ type: entry.level ?? 'log', text: entry.text ?? '' });

        // 浏览器把 4xx/5xx 统一记成 source==='network' 的 error 级日志。
        // 这些是契约内的业务信令（未登录 = 401 等），单独收集，不当缺陷。
        if (entry.source === 'network') {
          this.benignResponseErrors.push({ url: entry.url ?? '', status: Number(entry.status ?? 0) });
          break;
        }
        if (entry.level === 'error') {
          this.errors.push(`log: ${entry.text ?? ''} (${entry.url ?? ''})`);
        }
        break;
      }
      case 'Network.requestWillBeSent': {
        this.network.push({ method: params.request?.method, url: params.request?.url ?? '', status: null });
        break;
      }
      case 'Network.responseReceived': {
        const hit = this.network.find((r) => r.url === params.response?.url && r.status === null);
        if (hit) hit.status = params.response?.status ?? null;
        break;
      }
      case 'Network.loadingFailed': {
        // 主动 abort 不算错误（页面跳转时常见）
        if (!params.canceled) {
          this.errors.push(`network: ${params.errorText} (${params.requestId})`);
        }
        break;
      }
      default:
        break;
    }
  }

  /** 发送一条 CDP 命令。 */
  send(method, params = {}) {
    return this.browser.send(method, params, this.sessionId);
  }

  /**
   * 导航并等待**这一次**导航的 load 事件。
   * @param {string} url
   */
  async goto(url, { timeout = NAV_TIMEOUT_MS } = {}) {
    const afterSeq = this._navCounter;
    // 丢弃上一轮遗留的等待者，避免被陈旧 load 事件唤醒
    this._loadResolvers.length = 0;
    const loaded = this.waitForLoad(timeout, afterSeq);
    await this.send('Page.navigate', { url });
    await loaded;
    // load 之后 defer 脚本已执行完毕，但内联的异步初始化可能仍在进行；
    // 调用方可继续用 waitFor 等待业务就绪条件。
  }

  /**
   * 等待一次「发起于指定导航序号之后」的 load 事件。
   * @param {number} timeout
   * @param {number} afterSeq 只有导航序号大于该值的 load 才会唤醒
   */
  waitForLoad(timeout = NAV_TIMEOUT_MS, afterSeq = this._navCounter) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(`等待页面加载超时（${timeout}ms）`)), timeout);
      this._loadResolvers.push({
        afterSeq,
        resolve: (value) => { clearTimeout(timer); resolve(value); },
      });
    });
  }

  /**
   * 在页面上下文求值。
   * @param {string} expression 同步表达式；返回可 JSON 序列化的值
   */
  async eval(expression) {
    const result = await this.send('Runtime.evaluate', {
      expression: `(() => { ${expression} })()`,
      returnByValue: true,
      awaitPromise: true,
    });
    if (result.exceptionDetails) {
      throw new Error(`页面求值失败：${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text}`);
    }
    return result.result?.value;
  }

  /** 设置视口尺寸与像素比。 */
  async setViewport(width, height, { dpr = 1, mobile = false, scale = 1 } = {}) {
    await this.send('Emulation.setDeviceMetricsOverride', {
      width, height, deviceScaleFactor: dpr, mobile, screenWidth: width, screenHeight: height,
    });
    await this.send('Emulation.setPageScaleFactor', { pageScaleFactor: scale }).catch(() => {});
  }

  /** 模拟 `prefers-reduced-motion`。 */
  async setReducedMotion(enabled) {
    await this.send('Emulation.setEmulatedMedia', {
      features: [{ name: 'prefers-reduced-motion', value: enabled ? 'reduce' : 'no-preference' }],
    });
  }

  /** 截图并写入文件。 @param {string} filePath @returns {Promise<number>} 字节数 */
  async screenshot(filePath, { fullPage = true } = {}) {
    const shot = await this.send('Page.captureScreenshot', {
      format: 'png', captureBeyondViewport: fullPage,
    });
    const buffer = Buffer.from(shot.data, 'base64');
    mkdirSync(dirname(filePath), { recursive: true });
    writeFileSync(filePath, buffer);
    return buffer.length;
  }

  /**
   * 轮询直到表达式返回真值。
   * @param {string} expression 返回布尔/真值的表达式
   */
  async waitFor(expression, { timeout = 20000, interval = 50, label = expression } = {}) {
    const deadline = Date.now() + timeout;
    let last;
    while (Date.now() < deadline) {
      last = await this.eval(`return (${expression});`).catch(() => undefined);
      if (last) return last;
      await sleep(interval);
    }
    throw new Error(`等待条件超时（${timeout}ms）：${label}`);
  }

  /** 点击元素。 */
  async click(selector) {
    const ok = await this.eval(
      `const n = document.querySelector(${JSON.stringify(selector)});
       if (!n) return false; n.click(); return true;`,
    );
    if (!ok) throw new Error(`点击失败，未找到元素：${selector}`);
  }

  /** 填入输入框并派发 input 事件（与真实键入触发相同的事件）。 */
  async fill(selector, value) {
    const ok = await this.eval(
      `const n = document.querySelector(${JSON.stringify(selector)});
       if (!n) return false;
       n.focus();
       n.value = ${JSON.stringify(value)};
       n.dispatchEvent(new Event('input', { bubbles: true }));
       return true;`,
    );
    if (!ok) throw new Error(`填充失败，未找到元素：${selector}`);
  }

  /** 读取 localStorage 全部条目。 */
  readLocalStorage() {
    return this.eval(
      `const out = {};
       for (let i = 0; i < localStorage.length; i += 1) {
         const k = localStorage.key(i); out[k] = localStorage.getItem(k);
       }
       return out;`,
    );
  }

  /** 读取全部 Cookie（不含 HttpOnly 项——那些本就不可见）。 */
  readCookies() {
    return this.eval('return document.cookie;');
  }

  /**
   * 关闭该标签页，并**释放它独占的浏览上下文**。
   * 不释放的话，一个用例文件里累积几十个上下文会让浏览器明显变慢甚至假死。
   */
  async close() {
    if (this._closed) return;
    this._closed = true;
    this.browser._sessionHandlers.delete(this.sessionId);
    // 收尾用短超时：这里失败只意味着资源晚一点被浏览器回收，不值得让整个用例陪等
    await this.browser
      .send('Target.closeTarget', { targetId: this.targetId }, undefined, { timeout: 10000 })
      .catch(() => {});
    if (this.browserContextId) {
      await this.browser
        .send('Target.disposeBrowserContext', { browserContextId: this.browserContextId }, undefined, { timeout: 10000 })
        .catch(() => {});
      this.browserContextId = null;
    }
  }
}

/** 一个无头浏览器实例。 */
export class Browser {
  /**
   * @param {import('node:child_process').ChildProcess} child
   * @param {WebSocket} ws
   * @param {string} profileDir
   * @param {number} port DevTools HTTP 端点端口，用于存活探测
   */
  constructor(child, ws, profileDir, port) {
    this.child = child;
    this.ws = ws;
    this.profileDir = profileDir;
    this.port = port;
    /** @type {Map<number, {resolve: Function, reject: Function}>} */
    this._pending = new Map();
    this._nextId = 0;
    this._sessionHandlers = new Map();
  }

  /**
   * 启动一个无头 Chromium。
   * @param {{headless?: boolean, extraArgs?: string[]}} [options]
   */
  static async launch(options = {}) {
    const exe = findBrowser();
    const profileDir = mkdtempSync(join(tmpdir(), 'llm-lab-authui-'));
    const args = [
      options.headless === false ? '' : '--headless=new',
      '--remote-debugging-port=0',
      `--user-data-dir=${profileDir}`,
      '--no-first-run',
      '--no-default-browser-check',
      '--disable-extensions',
      '--disable-background-networking',
      '--disable-component-update',
      '--disable-sync',
      '--disable-gpu',
      '--hide-scrollbars',
      // 无头实例在机器繁忙时容易被判为「后台」而被降频，导致定时器与截图变慢。
      // 关掉这类节流，让用例的等待时间可预期。
      '--disable-background-timer-throttling',
      '--disable-backgrounding-occluded-windows',
      '--disable-renderer-backgrounding',
      // 硬性断网：除环回外，任何域名解析一律失败 ⇒ 页面若尝试外网请求会立刻暴露
      '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1, EXCLUDE localhost',
      'about:blank',
      ...(options.extraArgs ?? []),
    ].filter(Boolean);

    const child = spawn(exe, args, { stdio: 'ignore' });

    const portFile = join(profileDir, 'DevToolsActivePort');
    const deadline = Date.now() + 20000;
    while (!existsSync(portFile) && Date.now() < deadline) await sleep(120);
    if (!existsSync(portFile)) {
      child.kill();
      throw new Error('浏览器未在 20 秒内就绪（DevToolsActivePort 未生成）。');
    }

    const [port] = readFileSync(portFile, 'utf8').split('\n');
    const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();

    const ws = new WebSocket(version.webSocketDebuggerUrl);
    await new Promise((resolve, reject) => {
      ws.addEventListener('open', resolve, { once: true });
      ws.addEventListener('error', () => reject(new Error('CDP WebSocket 连接失败')), { once: true });
    });

    const browser = new Browser(child, ws, profileDir, Number(port));

    ws.addEventListener('message', (event) => {
      let message;
      try { message = JSON.parse(event.data); } catch { return; }

      if (message.id !== undefined && browser._pending.has(message.id)) {
        const { resolve, reject } = browser._pending.get(message.id);
        browser._pending.delete(message.id);
        if (message.error) reject(new Error(`CDP 错误 ${message.error.code}: ${message.error.message}`));
        else resolve(message.result);
        return;
      }

      const sid = message.sessionId;
      const page = sid ? browser._sessionHandlers.get(sid) : null;
      if (page && message.method) page._onEvent(message.method, message.params ?? {});
    });

    // 保持进程可被显式关闭
    process.once('exit', () => { try { child.kill(); } catch { /* 忽略 */ } });

    return browser;
  }

  /**
   * 发送浏览器级或会话级 CDP 命令。
   *
   * 超时默认 {@link CDP_TIMEOUT_MS}。取值偏大是刻意的：开发和 CI 机器上常同时
   * 跑着其它浏览器/构建进程，新建浏览上下文偶尔会被抢到十几秒才返回。
   * 早先的 20 秒上限会把它误判成「浏览器已死」，让整条用例假红。
   *
   * @param {string} method
   * @param {object} [params]
   * @param {string} [sessionId]
   * @param {{timeout?: number}} [options]
   */
  send(method, params = {}, sessionId, options = {}) {
    const timeout = options.timeout ?? CDP_TIMEOUT_MS;
    return new Promise((resolve, reject) => {
      const id = ++this._nextId;
      this._pending.set(id, { resolve, reject });
      const payload = { id, method, params };
      if (sessionId) payload.sessionId = sessionId;
      try {
        this.ws.send(JSON.stringify(payload));
      } catch (error) {
        this._pending.delete(id);
        reject(error);
        return;
      }
      setTimeout(() => {
        if (this._pending.has(id)) {
          this._pending.delete(id);
          reject(new Error(`CDP 命令超时（${timeout}ms）：${method}`));
        }
      }, timeout).unref?.();
    });
  }

  /**
   * 通过 HTTP 侧信道确认浏览器进程是否仍然活着。
   * WebSocket 卡死时它通常还能应答，用来区分「慢」与「已死」。
   * @returns {Promise<boolean>}
   */
  async isAlive() {
    try {
      const response = await fetch(`http://127.0.0.1:${this.port}/json/version`, {
        signal: AbortSignal.timeout(5000),
      });
      return response.ok;
    } catch {
      return false;
    }
  }

  /**
   * 新建一个隔离的浏览上下文与页面（独立 cookie / localStorage）。
   *
   * 机器繁忙时 `Target.createBrowserContext` 偶发迟迟不返回。这里做有限重试：
   * 先用 HTTP 侧信道确认浏览器没死，再退避重试，避免一条用例因调度抖动整条假红。
   *
   * @param {{attempts?: number}} [options]
   * @returns {Promise<Page>}
   */
  async newPage({ attempts = 3 } = {}) {
    let lastError;
    for (let attempt = 1; attempt <= attempts; attempt += 1) {
      try {
        return await this._newPageOnce();
      } catch (error) {
        lastError = error;
        const alive = await this.isAlive();
        if (!alive) {
          throw new Error(`浏览器进程已无响应，无法新建页面：${error.message}`);
        }
        if (attempt < attempts) await sleep(1000 * attempt);
      }
    }
    throw lastError;
  }

  /**
   * @private 单次尝试新建上下文与页面。
   * 中途失败时把已经建出来的上下文销毁掉，避免重试几次就攒下一堆僵尸上下文。
   */
  async _newPageOnce() {
    /** @type {string|undefined} */
    let browserContextId;
    /** @type {string|undefined} */
    let sessionId;
    try {
      ({ browserContextId } = await this.send('Target.createBrowserContext', { disposeOnDetach: true }));
      const { targetId } = await this.send('Target.createTarget', {
        url: 'about:blank',
        browserContextId,
      });
      ({ sessionId } = await this.send('Target.attachToTarget', { targetId, flatten: true }));

      const page = new Page(this, sessionId, targetId);
      page.browserContextId = browserContextId;
      this._sessionHandlers.set(sessionId, page);

      await page.send('Page.enable');
      await page.send('Runtime.enable');
      await page.send('Log.enable');
      await page.send('Network.enable');
      return page;
    } catch (error) {
      if (sessionId) this._sessionHandlers.delete(sessionId);
      if (browserContextId) {
        await this.send('Target.disposeBrowserContext', { browserContextId }).catch(() => {});
      }
      throw error;
    }
  }

  /** 关闭浏览器进程。 */
  async close() {
    for (const page of this._sessionHandlers.values()) page._closed = true;
    this._sessionHandlers.clear();
    try { this.ws.close(); } catch { /* 忽略 */ }
    try { this.child.kill(); } catch { /* 忽略 */ }
    await sleep(150);
  }
}
