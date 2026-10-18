/*! tests/build-profile.test.js — the demo gate belongs to the artifact.
 *
 *  The console ships as two generated files. `web/index.html` is the artifact
 *  that gets deployed; `web/qa.html` is an isolated preview, and the only
 *  difference between them is the `data-build-profile` attribute the generator
 *  writes onto `<html>`. Everything that can put a design sample on screen is
 *  keyed to that attribute — so "where can a sample come from?" is answered by
 *  reading a file, not by reasoning about a session.
 *
 *  This file pins the half that must never soften: production. A missing
 *  attribute, a typo, a value invented at runtime — all of them are production,
 *  and production has no path to the samples.
 */
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { PROFILES, buildProfile, isQaBuild } from '../web/js/build-profile.js';

const REPO = join(dirname(fileURLToPath(import.meta.url)), '..');
const read = (p) => readFileSync(join(REPO, p), 'utf8').replace(/\r\n/g, '\n');
const doc = (declared) => ({ documentElement: { dataset: { buildProfile: declared } } });

test('the profile fails closed: only the exact string "qa" is QA', () => {
  const notQa = [undefined, null, '', 'production', 'QA', 'Qa', 'qa ', ' qa', 'demo', 'true', '1', 1, {}, []];
  for (const declared of notQa) {
    assert.equal(buildProfile(doc(declared)), PROFILES.PRODUCTION,
      `${JSON.stringify(declared)} must resolve to production`);
    assert.equal(isQaBuild(doc(declared)), false);
  }
  assert.equal(buildProfile(doc('qa')), PROFILES.QA);
  assert.equal(isQaBuild(doc('qa')), true);
});

test('a document that cannot answer at all is production', () => {
  for (const broken of [undefined, null, {}, { documentElement: null }, { documentElement: {} }, { documentElement: { dataset: {} } }]) {
    assert.equal(buildProfile(broken), PROFILES.PRODUCTION);
    assert.equal(isQaBuild(broken), false);
  }
});

test('the deployed artifact declares production and the preview declares qa', () => {
  assert.match(read('web/index.html'),
    /<html lang="zh-CN" data-theme="light" data-build-profile="production">/);
  assert.match(read('web/qa.html'),
    /<html lang="zh-CN" data-theme="light" data-build-profile="qa">/);
});

test('no generated artifact carries a sample-mode entry in its markup', () => {
  // The switch is built at runtime and only on QA. In the shipped HTML there is
  // no node at all — absent, not hidden — so no stylesheet, script or query
  // parameter can reveal one.
  for (const file of ['web/index.html', 'web/qa.html', 'prototypes/v6/index.html']) {
    assert.doesNotMatch(read(file), /data-demo-switch/, `${file} must not carry a sample-mode entry`);
  }
});

test('the QA preview is the deployed page plus the profile, and nothing else', () => {
  const norm = (html) => html
    .replace(/ data-build-profile="(?:production|qa)"/, '')
    .replace(/<title>[\s\S]*?<\/title>/, '<title></title>')
    .replace(/<!--[\s\S]*?-->\n?/g, '');
  assert.equal(norm(read('web/qa.html')), norm(read('web/index.html')),
    'the QA artifact must not carry a sample block the deployed one lacks');
});
