/*! tests/tracked-assets.test.js — release integrity for web/.
 *
 *  Why this file exists. The first cut of web/ imported four files that were
 *  never committed: `.gitignore` carries `data/` (to keep real usage dumps out)
 *  and `*token*` (to keep keys out), and the Atlas source happened to match
 *  both. Every test passed anyway, because the tests read the *working tree* —
 *  where the files existed as ignored, untracked bystanders. `git ls-files`
 *  said 35 files; the front-end needed 39. A fresh clone was a blank page, and
 *  tests/atlas-data.test.js, which imports the same modules, could not even run.
 *
 *  So nothing here reads the working tree. Every check runs against
 *  `git archive HEAD` — literally the contents of a fresh clone — and a file
 *  that only exists on someone's disk cannot satisfy it.
 *
 *  JS module edges are resolved by Node's own linker (importing each module),
 *  not by scanning text. Two regex scanners were tried first and each invented
 *  a defect: a doc comment describing an import, and a string literal
 *  containing one. Only a parser can tell those apart, and Node ships one.
 *
 *  If this fails with "which is NOT tracked by git", you probably added a
 *  module and forgot that .gitignore needs an exception for it.
 */
import test, { before, after } from 'node:test';
import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';

import { extractRefs, kindOf } from './lib/static-refs.mjs';

const REPO_ROOT = resolve(fileURLToPath(new URL('../', import.meta.url)));
const git = (...args) => execFileSync('git', args, { cwd: REPO_ROOT, encoding: 'utf8', maxBuffer: 1 << 28 });

/** The files the front-end cannot boot without. Kept explicit so the intent
 *  survives any refactor of the scanner. */
const REQUIRED = [
  'web/index.html',
  'web/css/tokens.css',
  'web/css/base.css',
  'web/css/atlas.css',
  'web/css/portal.css',
  'web/js/app.js',
  'web/js/data/contract.js',
  'web/js/data/demo-fixture.js',
  'web/js/data/remote-client.js',
  'web/js/calls/controller.js',
  'web/js/modules/controller.js',
];

/** A missing file, a directory import or an unparsable module all mean the
 *  same thing to a browser: the page does not load. Anything else thrown by
 *  `import()` happened *after* linking succeeded, so the graph is fine. */
const isLoadFailure = (err) => {
  const code = err?.code ?? '';
  return code.startsWith('ERR_MODULE')
    || code === 'ERR_UNSUPPORTED_DIR_IMPORT'
    || err?.name === 'SyntaxError';
};

let material = null;

before(() => {
  const tracked = new Set(git('ls-files', '-z').split('\0').filter(Boolean));
  const dir = mkdtempSync(join(tmpdir(), 'llmlab-tracked-'));
  // `git archive HEAD | tar -x` — the literal contents of a fresh clone. No
  // working-tree file can leak in, which is the entire point of this file.
  const tar = execFileSync('git', ['archive', '--format=tar', 'HEAD'], {
    cwd: REPO_ROOT, maxBuffer: 1 << 28,
  });
  execFileSync('tar', ['-xf', '-', '-C', dir], { input: tar, maxBuffer: 1 << 28 });
  material = { dir, tracked };
});

after(() => { if (material) rmSync(material.dir, { recursive: true, force: true }); });

test('every module the front-end imports resolves in a fresh clone', async () => {
  const modules = [...material.tracked].filter((p) => /^web\/.*\.m?js$/.test(p)).sort();
  assert.ok(modules.length >= 20, `only ${modules.length} modules under web/; the walk is broken`);

  // Node names the temp directory in its messages; that is noise here, and
  // `web/js/…` is the part a reader can act on.
  const tidy = (s) => String(s).split(material.dir).join('');

  const failures = [];
  const linked = new Set();
  for (const rel of modules) {
    try {
      await import(pathToFileURL(join(material.dir, rel)).href);
      linked.add(rel);
    } catch (err) {
      if (isLoadFailure(err)) failures.push(`${rel}: ${err.code ?? err.name} — ${tidy(err.message).split('\n')[0]}`);
      else linked.add(rel); // linked fine; it just fell over trying to run without a DOM
    }
  }

  // Non-vacuity: linked is normally 31/31, but asserting a count would fire on
  // the very defect this test exists to report and hide the useful message.
  // Assert a known dependency-free leaf instead, then let `failures` speak.
  assert.ok(linked.has('web/js/dom.js'), 'web/js/dom.js did not link; the walk is broken');
  assert.deepEqual(failures, [],
    `a fresh clone cannot load these modules:\n  ${failures.join('\n  ')}`);
});

test('every stylesheet and script tag resolves inside the tracked set', () => {
  const problems = [];
  const resolved = [];
  let scanned = 0;

  for (const rel of material.tracked) {
    if (!kindOf(rel)) continue;
    const abs = join(material.dir, rel);
    if (!existsSync(abs)) {
      problems.push(`${rel}: tracked by git but absent from the archive`);
      continue;
    }
    scanned++;
    for (const ref of extractRefs(rel, readFileSync(abs, 'utf8'))) {
      if (ref.target === null) continue;
      resolved.push(`${rel} → ${ref.spec}`);
      if (!material.tracked.has(ref.target)) {
        problems.push(`${rel}:${ref.line} references "${ref.spec}" → ${ref.target}, which is NOT tracked by git`);
      } else if (!existsSync(join(material.dir, ref.target))) {
        problems.push(`${rel}:${ref.line} references "${ref.spec}" → ${ref.target}, absent from the archive`);
      }
    }
  }

  // Non-vacuity, against references we know are there.
  assert.ok(scanned >= 4, `only ${scanned} markup/stylesheet files scanned`);
  for (const known of ['web/index.html → ./js/app.js', 'web/index.html → ./css/tokens.css']) {
    assert.ok(resolved.includes(known), `the scanner missed ${known}; found:\n  ${resolved.join('\n  ')}`);
  }
  assert.deepEqual(problems, [], `unresolvable references — a fresh clone would break:\n  ${problems.join('\n  ')}`);
});

test('the scanner reads code, not prose', () => {
  // Regression guard for a real false positive: a specifier written in a
  // comment or in a string is not a reference.
  const css = "/* url(./in-a-comment.css) */\n@import './real.css';\n.x { background: url(./bg.png); }";
  assert.deepEqual(extractRefs('web/css/x.css', css).map((r) => r.spec), ['./real.css', './bg.png']);

  const html = '<!-- <link href="./in-a-comment.css"> -->\n<link href="./real.css"><script src="./a.js"></script>';
  assert.deepEqual(extractRefs('web/x.html', html).map((r) => r.spec), ['./real.css', './a.js']);

  // A fragment is not a file, and an absolute URL is not ours to resolve.
  assert.deepEqual(extractRefs('web/x.html', '<a href="#x"></a>').map((r) => r.target), []);
  assert.deepEqual(extractRefs('web/x.html', '<link href="https://cdn.example/a.css">').map((r) => r.target), [null]);
});

test('the archive really is HEAD-shaped, not disk-shaped', () => {
  // Negative control: if the archive silently picked up the working tree, or
  // .gitignore were loosened for the wrong reason, the guard above would stop
  // being able to fail — and a guard that cannot fail is decoration.
  assert.equal(existsSync(join(material.dir, '__not-committed__.js')), false);
  assert.equal(material.tracked.has('web/js/data/contract.js'), true, 'sanity: the fix is present');
  assert.equal(material.tracked.has('web/js/data/usage.jsonl'), false);
});

test('the front-end never references an external host', () => {
  const offenders = [];
  for (const rel of material.tracked) {
    if (kindOf(rel) !== 'css' && kindOf(rel) !== 'html') continue;
    const abs = join(material.dir, rel);
    if (!existsSync(abs)) continue;
    for (const ref of extractRefs(rel, readFileSync(abs, 'utf8'))) {
      if (/^(?:https?:)?\/\//i.test(ref.spec)) offenders.push(`${rel}:${ref.line} → ${ref.spec}`);
    }
  }
  assert.deepEqual(offenders, [], `no remote fonts, styles or scripts are allowed:\n  ${offenders.join('\n  ')}`);
});

test('the required sources are tracked, and every tracked blob is LF', () => {
  for (const rel of REQUIRED) {
    assert.ok(material.tracked.has(rel), `${rel} is not tracked by git`);
  }
  // The repo is edited on Windows with core.autocrlf=true, so a CRLF blob is
  // easy to commit by accident and shows up as a whole-file diff for everyone
  // else. web/ is LF; keep it that way.
  const offenders = [];
  for (const line of git('ls-files', '--eol', '--', 'web/').split('\n')) {
    if (!line.trim()) continue;
    const [flags, path] = line.split('\t');
    const index = flags.trim().split(/\s+/)[0];
    if (index !== 'i/lf') offenders.push(`${path} (${index})`);
  }
  assert.deepEqual(offenders, [], `tracked web/ blobs must be LF:\n  ${offenders.join('\n  ')}`);
});

test('.gitignore opens exactly four front-end paths and nothing else', () => {
  const ignored = (rel) => {
    try { git('check-ignore', '-q', '--', rel); return true; } catch { return false; }
  };

  // Open: precisely the four sources the front-end imports.
  for (const rel of ['web/js/data/contract.js', 'web/js/data/demo-fixture.js',
    'web/js/data/remote-client.js', 'web/css/tokens.css']) {
    assert.equal(ignored(rel), false, `${rel} must be trackable`);
  }

  // Shut: everything the `data/` and `*token*` rules exist to keep out —
  // including, deliberately, other files that could sit in the same directory.
  for (const rel of [
    'web/js/data/usage.jsonl',
    'web/js/data/usage-2026.jsonl',
    'web/js/data/credentials.js',
    'web/js/data/keys/prod.pem',
    'web/js/data/nested/deep/thing.js',
    'web/css/other-tokens.css',
    'web/css/tokens.css.bak',
    '.env',
    'usage.jsonl',
    'data/whatever.js',
    'some/other/data/file.js',
  ]) {
    assert.equal(ignored(rel), true, `${rel} must stay ignored`);
  }
});
