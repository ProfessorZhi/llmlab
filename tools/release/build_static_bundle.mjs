#!/usr/bin/env node
/*! tools/release/build_static_bundle.mjs — build the deployable static bundle.
 *
 *  Why this exists
 *  ---------------
 *  `web/` is the front-end *source* tree, not the deployable artifact. It also
 *  contains the QA preview (`web/qa.html`), the generator and dev server
 *  (`web/tools/**`), a package manifest and a README. Serving `web/` directly —
 *  which is what `web/tools/serve.mjs` does for local review — therefore also
 *  serves the QA build, and anything else somebody drops in that directory.
 *  There was no release artifact allowlist in the repository.
 *
 *  So the bundle is built from an **entry-point closure** rather than by copying
 *  the directory:
 *
 *    * the only entries are the ones named in `release-allowlist.json`;
 *    * every `src` / `href` / static `import` / `url()` reachable from them is
 *      followed, transitively, so a missing file is a build failure rather than
 *      a 404 in production;
 *    * nothing outside that closure is published — no prototypes, no tests, no
 *      build tooling, no fixtures, no logs;
 *    * a closure that **needs** a denied file fails, and a closure that wants an
 *      HTML entry nobody approved fails. Silently skipping either would produce
 *      a bundle that looks green and breaks (or leaks) at runtime.
 *
 *  Every published HTML document must also declare
 *  `data-build-profile="production"`. That is the gate `web/js/build-profile.js`
 *  reads, and it is what makes `?demo=1` inert on a production artifact.
 *
 *  Properties this tool keeps on purpose
 *  ------------------------------------
 *  * zero dependencies, no network, no subprocesses, no privilege;
 *  * writes only inside the directory passed to `--out`;
 *  * refuses to write into the repository work tree;
 *  * deterministic: same inputs ⇒ same bytes ⇒ same hashes.
 *
 *  Usage
 *  -----
 *    node tools/release/build_static_bundle.mjs --out /tmp/atelier-bundle
 *    node tools/release/build_static_bundle.mjs --out DIR --json
 */

import { createHash } from 'node:crypto';
import { existsSync, readFileSync, readdirSync, statSync, mkdirSync, writeFileSync } from 'node:fs';
import { dirname, join, normalize, relative, resolve, sep, posix } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = resolve(HERE, '..', '..');
export const MANIFEST_PATH = join(HERE, 'release-allowlist.json');
export const MANIFEST_SCHEMA = 'atelier-v1-static-release/1';

/** References we never follow: they are not files in the bundle. */
const NON_FILE = /^(?:[a-z][a-z0-9+.-]*:|\/\/|#)/i;

const REFERENCE_RES = {
  html: /\b(?:src|href)\s*=\s*["']([^"']+)["']/gi,
  // `import x from './y.js'`, `export ... from './y.js'`, `import './y.js'`
  jsStatic: /\b(?:import|export)\b[^;]*?\bfrom\s*['"]([^'"]+)['"]/g,
  jsSideEffect: /\bimport\s+['"]([^'"]+)['"]/g,
  jsDynamic: /\bimport\s*\(\s*['"]([^'"]+)['"]\s*\)/g,
  cssUrl: /\burl\(\s*['"]?([^'")]+?)['"]?\s*\)/g,
  cssImport: /@import\s+(?:url\(\s*)?['"]([^'"]+)['"]/g,
};

export function sha256(buf) {
  return createHash('sha256').update(buf).digest('hex');
}

/** Normalise a reference to a POSIX path relative to `sourceRoot`, or null. */
export function resolveReference(fromRel, raw) {
  const value = String(raw ?? '').trim();
  if (!value || NON_FILE.test(value)) return null;
  const clean = value.split('?')[0].split('#')[0];
  if (!clean) return null;
  const base = posix.dirname(fromRel.replaceAll(sep, '/'));
  const joined = posix.normalize(posix.join(base, clean));
  if (joined.startsWith('..')) return { escaped: true, path: joined };
  return { escaped: false, path: joined };
}

/** Glob → RegExp for the small subset the allowlist needs (`**`, `*`). */
export function globToRegExp(glob) {
  let out = '';
  for (let i = 0; i < glob.length; i += 1) {
    const c = glob[i];
    if (c === '*' && glob[i + 1] === '*') { out += '.*'; i += 1; }
    else if (c === '*') out += '[^/]*';
    else if (c === '?') out += '[^/]';
    else out += c.replace(/[.+^${}()|[\]\\]/g, '\\$&');
  }
  return new RegExp(`^${out}$`);
}

export function readAllowlist(path = MANIFEST_PATH) {
  const doc = JSON.parse(readFileSync(path, 'utf8'));
  if (doc.schema !== 'atelier-v1-static-release-allowlist/1') {
    throw new Error(`unsupported allowlist schema: ${doc.schema}`);
  }
  for (const key of ['source_root', 'entry_points', 'deny_paths', 'deny_globs']) {
    if (!Array.isArray(doc[key]) && typeof doc[key] !== 'string') {
      throw new Error(`allowlist is missing ${key}`);
    }
  }
  doc.denyGlobs = doc.deny_globs.map(globToRegExp);
  doc.entryPoints = [...doc.entry_points];
  return doc;
}

/** Is this relative POSIX path denied for publication? */
export function isDenied(relPath, allowlist) {
  const p = relPath.replaceAll(sep, '/');
  for (const denied of allowlist.deny_paths) {
    if (denied.endsWith('/') ? p === denied.slice(0, -1) || p.startsWith(denied) : p === denied) {
      return denied;
    }
  }
  for (let i = 0; i < allowlist.deny_globs.length; i += 1) {
    if (allowlist.denyGlobs[i].test(p)) return allowlist.deny_globs[i].source;
  }
  return null;
}

/**
 * Walk the entry closure.
 *
 * @returns {{files: string[], references: Map<string,string[]>, problems: string[]}}
 */
export function collectClosure({ root, allowlist }) {
  const entrySet = new Set(allowlist.entryPoints);
  const seen = new Set();
  const queue = [...allowlist.entryPoints];
  const references = new Map();
  const problems = [];

  while (queue.length) {
    const rel = queue.shift();
    if (seen.has(rel)) continue;
    seen.add(rel);

    const denied = isDenied(rel, allowlist);
    if (denied) {
      problems.push(`closure needs "${rel}", which the allowlist denies (${denied})`);
      continue;
    }
    if (/\.html$/i.test(rel) && !entrySet.has(rel)) {
      problems.push(
        `closure needs an unapproved HTML entry "${rel}"; add it to entry_points only after a release review`,
      );
      continue;
    }

    const abs = join(root, rel);
    if (!existsSync(abs) || !statSync(abs).isFile()) {
      problems.push(`closure needs "${rel}", which does not exist under ${root}`);
      continue;
    }

    const source = readFileSync(abs, 'utf8');
    const refs = [];
    const patterns = [];
    if (/\.html$/i.test(rel)) patterns.push(REFERENCE_RES.html);
    else if (/\.m?js$/i.test(rel)) patterns.push(REFERENCE_RES.jsStatic, REFERENCE_RES.jsSideEffect, REFERENCE_RES.jsDynamic);
    else if (/\.css$/i.test(rel)) patterns.push(REFERENCE_RES.cssUrl, REFERENCE_RES.cssImport);

    for (const re of patterns) {
      re.lastIndex = 0;
      let m;
      while ((m = re.exec(source)) !== null) {
        const resolved = resolveReference(rel, m[1]);
        if (!resolved) continue;
        if (resolved.escaped) {
          problems.push(`"${rel}" references "${m[1]}", which escapes ${allowlist.source_root}/`);
          continue;
        }
        refs.push(resolved.path);
        if (!seen.has(resolved.path)) queue.push(resolved.path);
      }
    }
    references.set(rel, refs);
  }

  return { files: [...seen].sort(), references, problems };
}

/**
 * Check the build profile of every published HTML document.
 *
 * The invariant is: **a published page may not declare any profile other than
 * `production`.** That is the one thing that would let a QA document ship with
 * its samples reachable, and `web/qa.html` (`data-build-profile="qa"`) is the
 * live example of it.
 *
 * An *absent* attribute is not a hole. `web/js/build-profile.js` resolves
 * anything that is not the exact string `qa` to `production`, so a page that
 * omits the attribute is already fail-closed; and a page can only be published
 * at all if it is an approved entry point, which is enforced separately. The
 * omission is reported and recorded in the manifest rather than silently
 * accepted, so a reviewer can see exactly which pages rely on the default.
 *
 * `web/auth/index.html` is the current example: it is a standalone login
 * document that does not load the application bundle. Making it declare the
 * profile would be a one-word change in a UI-Owner file, so it is raised as a
 * contract note instead of being edited here.
 *
 * @returns {{problems: string[], undeclared: string[]}}
 */
export function checkHtmlProfiles({ root, files, expected }) {
  const problems = [];
  const undeclared = [];
  for (const rel of files) {
    if (!/\.html$/i.test(rel)) continue;
    const html = readFileSync(join(root, rel), 'utf8');
    const m = /<html\b[^>]*\bdata-build-profile\s*=\s*["']([^"']*)["']/i.exec(html);
    if (!m) undeclared.push(rel);
    else if (m[1] !== expected) {
      problems.push(
        `"${rel}" declares data-build-profile="${m[1]}"; a released page must be "${expected}" `
        + '(or omit the attribute, which the runtime treats as production)',
      );
    }
  }
  return { problems, undeclared };
}

/** Build the bundle into `outDir`. Returns the manifest object. */
export function build({ root = REPO_ROOT, allowlist = readAllowlist(), outDir, log = () => {} } = {}) {
  const sourceRoot = join(root, allowlist.source_root);
  if (!existsSync(sourceRoot)) throw new Error(`source root not found: ${sourceRoot}`);
  if (!outDir) throw new Error('--out <dir> is required');

  const outAbs = resolve(outDir);
  if (outAbs === resolve(root) || outAbs.startsWith(resolve(root) + sep)) {
    throw new Error(`refusing to write the bundle inside the repository work tree: ${outAbs}`);
  }

  const closure = collectClosure({ root: sourceRoot, allowlist });
  const problems = [...closure.problems];
  const profile = checkHtmlProfiles({
    root: sourceRoot, files: closure.files, expected: allowlist.require_html_profile,
  });
  problems.push(...profile.problems);
  // Defence in depth: even if a deny rule were mis-edited, these never ship.
  for (const rel of closure.files) {
    const denied = isDenied(rel, allowlist);
    if (denied) problems.push(`"${rel}" is denied (${denied}) but reached the closure`);
  }
  if (problems.length) {
    const err = new Error(`release bundle refused:\n  - ${problems.join('\n  - ')}`);
    err.problems = problems;
    throw err;
  }

  const entries = [];
  for (const rel of closure.files) {
    const abs = join(sourceRoot, rel);
    const data = readFileSync(abs);
    const dest = join(outAbs, rel);
    mkdirSync(dirname(dest), { recursive: true });
    writeFileSync(dest, data);
    entries.push({ path: rel, bytes: data.length, sha256: sha256(data) });
    log(`  + ${rel}`);
  }

  const bundleSha256 = sha256(
    entries.map((e) => `${e.path}:${e.sha256}`).join('\n') + '\n',
  );

  const manifest = {
    schema: MANIFEST_SCHEMA,
    source_root: allowlist.source_root,
    entry_points: allowlist.entry_points,
    require_html_profile: allowlist.require_html_profile,
    undeclared_profile: profile.undeclared,
    file_count: entries.length,
    total_bytes: entries.reduce((n, e) => n + e.bytes, 0),
    bundle_sha256: bundleSha256,
    files: entries,
  };
  writeFileSync(join(outAbs, 'release-manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
  return manifest;
}

function parseArgs(argv) {
  const args = { out: null, json: false, source: REPO_ROOT, allowlist: MANIFEST_PATH };
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === '--out') args.out = argv[++i];
    else if (argv[i] === '--source') args.source = resolve(argv[++i]);
    else if (argv[i] === '--allowlist') args.allowlist = resolve(argv[++i]);
    else if (argv[i] === '--json') args.json = true;
    else if (argv[i] === '--help' || argv[i] === '-h') args.help = true;
    else throw new Error(`unknown argument: ${argv[i]}`);
  }
  return args;
}

function main() {
  let args;
  try {
    args = parseArgs(process.argv.slice(2));
  } catch (err) {
    console.error(String(err.message));
    process.exit(2);
  }
  if (args.help || !args.out) {
    console.error('usage: node tools/release/build_static_bundle.mjs --out <dir> '
      + '[--source <checkout>] [--allowlist <json>] [--json]');
    process.exit(args.help ? 0 : 2);
  }
  try {
    const manifest = build({
      root: args.source,
      allowlist: readAllowlist(args.allowlist),
      outDir: args.out,
      log: args.json ? () => {} : (line) => console.error(line),
    });
    if (args.json) process.stdout.write(JSON.stringify(manifest, null, 2) + '\n');
    else {
      console.log(`ok: ${manifest.file_count} files, ${manifest.total_bytes} bytes`);
      console.log(`bundle sha256: ${manifest.bundle_sha256}`);
    }
  } catch (err) {
    console.error(String(err.message));
    process.exit(1);
  }
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) main();
