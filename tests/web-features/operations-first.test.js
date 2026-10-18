/*! tests/web-features/operations-first.test.js
 *  Live is the default. These pin the two halves of that: the mode resolver
 *  refuses every opt-in except the exact one, and the shipped HTML paints no
 *  fabricated KPI — the samples are parked, not shown.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { resolveDataMode, DATA_MODES } from '../../web/js/demo-mode.js';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const WEB = readFileSync(join(REPO, 'web', 'index.html'), 'utf8').replace(/\r\n/g, '\n');

/** Parked sample values. `02` is deliberately only checked as "parked": a bare
 *  "02" is a legitimate editorial index elsewhere in the shell. */
const PARKED = ['1,248', '99.4%', '1.42 s', '482k', '8,412', '02'];
const DISTINCTIVE = ['1,248', '99.4%', '1.42 s', '482k', '8,412'];

test('the exact ?demo=1 opt-in only leaves live mode on the QA build', () => {
  for (const search of ['', '?utm_source=chatgpt.com', '?source=demo', '?demo=0', '?demo=yes', '?demo=']) {
    assert.equal(resolveDataMode(search, { qa: true }), DATA_MODES.LIVE, `"${search}" must stay live`);
  }
  for (const search of ['?demo=1', '?demo=true', '?x=1&demo=1']) {
    assert.equal(resolveDataMode(search, { qa: true }), DATA_MODES.DEMO, `${search} on the QA build`);
    assert.equal(resolveDataMode(search), DATA_MODES.LIVE, `${search} is inert on production`);
  }
});

test('no fabricated KPI is painted in the shipped HTML', () => {
  for (const value of PARKED) {
    assert.ok(WEB.includes(`data-demo-value="${value}"`), `${value} must be parked as a sample value`);
  }
  for (const value of DISTINCTIVE) {
    const painted = new RegExp(`>\\s*${value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*<`);
    assert.doesNotMatch(WEB, painted, `${value} must not be literal text between tags`);
  }
  assert.match(WEB, /data-demo-only/, 'the sample blocks must carry the mode gate');
  assert.match(WEB, /data-live-only/, 'and a gated stand-in must exist');
});

test('the first screen is gated, not merely re-badged', () => {
  assert.match(WEB, /调用与用量 <em class="heading-en">\/ GATEWAY BFF<\/em>/);
  assert.match(WEB, /待授权 · NOT CONNECTED/);
  assert.match(WEB, /需要 Linux PAM 会话 \+ Gateway usage BFF 的服务端授权/);
});

test('the preview session wording replaces every signed-in claim', () => {
  assert.match(WEB, /预览访问 · 未建立 Linux 会话/);
  assert.match(WEB, /data-account-state="preview"/);
  assert.doesNotMatch(WEB, /飞书|Feishu|OAuth/, 'the retired sign-in wording must not return');
});

test('the shell no longer advertises the 05 study or an all-synthetic page', () => {
  for (const retired of ['所有运行数据均为示意', 'ATELIER 05 · 视觉与动效原型', 'OBSERVATORY Nº 05']) {
    assert.ok(!WEB.includes(retired), `retired copy still ships: ${retired}`);
  }
  assert.match(WEB, /ATELIER OBSERVATORY Nº 06/);
  assert.match(WEB, /HOST TELEMETRY · USER DATA GATED/);
});
