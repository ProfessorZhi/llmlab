/*! tests/web-features/platform-hub.test.js
 *  The Linux/PAM login rebrand and the platform/source hub are copy + structure
 *  changes to the generated shell. These assert the *shipped artefacts* — the
 *  bytes a browser loads — so a change to derive-from-v5.mjs that never reaches
 *  web/ still fails here.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const read = (p) => readFileSync(join(REPO, p), 'utf8').replace(/\r\n/g, '\n');
const WEB = read('web/index.html');
const V6 = read('prototypes/v6/index.html');

/** The `<article class="service" data-platform="…">…</article>` card. */
function cardOf(html, platform) {
  const at = html.indexOf(`data-platform="${platform}"`);
  assert.ok(at > 0, `missing platform card: ${platform}`);
  const open = html.lastIndexOf('<article', at);
  const end = html.indexOf('</article>', at);
  return html.slice(open, end);
}

test('the retired 飞书 sign-in wording is gone from every shipped shell', () => {
  for (const [name, html] of [['web/index.html', WEB], ['prototypes/v6/index.html', V6]]) {
    assert.doesNotMatch(html, /飞书/, `${name} still shows 飞书 copy`);
    assert.doesNotMatch(html, /Feishu|FEISHU/, `${name} still shows the Feishu name`);
    assert.doesNotMatch(html, /OAuth/, `${name} still shows an OAuth reference`);
  }
});

test('the visible header advertises a preview session, not a signed-in account', () => {
  assert.match(WEB, /<span class="account-label" data-account-label>预览访问 · 未建立 Linux 会话<\/span>/);
  assert.match(WEB, /id="accountButton"[^>]*aria-haspopup="dialog"/);
  assert.match(WEB, /id="accountButton"[^>]*data-account-state="preview"/);
  assert.doesNotMatch(WEB, /<span class="auth-demo">F<\/span>/, 'the F avatar must be gone');
  assert.match(WEB, /<span class="auth-demo" aria-hidden="true">L<\/span>/);
});

test('the visible document description names Linux, not 飞书', () => {
  const head = WEB.slice(0, WEB.indexOf('</head>'));
  assert.match(head, /<meta name="description" content="[^"]*Linux 账号身份（PAM 待接线）[^"]*">/);
  assert.doesNotMatch(head, /飞书/);
});

test('the overview hub replaces the decorative platform cards with real navigation', () => {
  assert.match(WEB, /平台与数据来源/);
  assert.doesNotMatch(WEB, /已连接的平台组件/, 'the old "connected platform components" claim must be gone');
  const routes = [
    ['strata', 'models'],
    ['gateway', 'api'],
    ['metrics', 'observability'],
    ['evalscope', 'evaluation'],
  ];
  for (const [platform, target] of routes) {
    const card = cardOf(WEB, platform);
    assert.match(card, new RegExp(`<button[^>]*data-go="${target}"`), `${platform} must route to ${target}`);
    assert.equal((card.match(/↗/g) || []).length, 0, `${platform} must not keep a decorative ↗`);
    assert.match(card, /未接线/, `${platform} must state it is not wired up yet`);
  }
});

test('CloudCLI and Fleet are separate control planes, never a simulated login', () => {
  for (const platform of ['cloudcli', 'fleet']) {
    const card = cardOf(WEB, platform);
    assert.match(card, /待接线/, `${platform} must be marked 待接线`);
    assert.match(card, /disabled/, `${platform} must not be an active control`);
    assert.doesNotMatch(card, /https?:\/\//, `${platform} must not carry an address`);
    assert.doesNotMatch(card, /\b(?:localhost|127\.0\.0\.1|10\.\d|\d{1,3}(?:\.\d{1,3}){3})\b/, `${platform} must not carry a host`);
  }
  for (const port of [':3000', ':9000', ':8200', ':8080']) {
    assert.ok(!WEB.includes(port), `a direct platform port (${port}) must never reach the browser`);
  }
});

test('unverified call/usage numbers are not painted; the samples are only parked', () => {
  assert.match(WEB, /data-source-badge="gated"/);
  assert.match(WEB, /待授权 · NOT CONNECTED/);
  for (const value of ['1,248', '99.4%', '1.42 s', '482k', '8,412']) {
    assert.ok(WEB.includes(`data-demo-value="${value}"`), `${value} must be parked as a demo value`);
    const painted = new RegExp(`>${value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}<`);
    assert.doesNotMatch(WEB, painted, `${value} must never be painted as a literal number`);
  }
  assert.match(WEB, /data-demo-only/, 'the sample block must carry the mode gate');
  assert.match(WEB, /data-stat-note>待 Gateway BFF/);
  assert.match(WEB, /待授权数据源/, 'the Atlas scope must not claim a workload count');
  assert.doesNotMatch(WEB, /示意已加载/, 'the model must not claim to be loaded');
  assert.match(WEB, /标称规格，非可用 VRAM/, 'the GPU figure is nominal, not available VRAM');
});

test('the retired demo wording is gone from the shipped shell', () => {
  for (const retired of ['所有运行数据均为示意', 'ATELIER 05 · 视觉与动效原型', 'OBSERVATORY Nº 05']) {
    assert.ok(!WEB.includes(retired), `the retired copy "${retired}" must not ship`);
  }
  assert.match(WEB, /ATELIER OBSERVATORY Nº 06/);
});

test('the eight primary routes and the Compute subpage are untouched', () => {
  const nav = WEB.slice(WEB.indexOf('<nav class="topnav"'));
  const navEnd = nav.slice(0, nav.indexOf('</nav>'));
  const entries = [...navEnd.matchAll(/data-page="([a-z]+)"/g)].map((m) => m[1]);
  assert.equal(entries.length, 8, 'eight primary navigation tabs must remain');
  assert.deepEqual(entries, ['overview', 'models', 'api', 'calls', 'observability', 'atlas', 'evaluation', 'admin']);
});
