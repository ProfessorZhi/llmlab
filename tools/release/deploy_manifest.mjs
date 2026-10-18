#!/usr/bin/env node
/*! tools/release/deploy_manifest.mjs — the canary/rollback manifest, and its validator.
 *
 *  `build_static_bundle.mjs` answers "what is in the artifact". This answers
 *  "what is about to happen to a host", in a form a machine can refuse.
 *
 *  Threat model (P0 hardening, release gate #69)
 *  ---------------------------------------------
 *  A manifest is operator input, and so is the bundle it describes. Both are
 *  treated as untrusted:
 *
 *    * **Deny by default on the deployment root.** The previous version only
 *      enforced `allowedRoots` when the list was non-empty, so omitting
 *      `--allow-root` silently accepted *any* `target.root`. Now an empty or
 *      missing approved-root set is a refusal, and the root must be an exact,
 *      normalised, absolute path from that set.
 *    * **Strict containment on bundle entries.** The previous version did
 *      `join(bundleDir, entry.path)` and read whatever that resolved to, so a
 *      `../` entry, or a symlink planted inside the bundle, could make the
 *      verifier hash a file outside the artifact and still report success. Now
 *      every entry path is validated as a plain relative POSIX path, resolved,
 *      required to stay inside the bundle, and every component is `lstat`ed so
 *      a symbolic link anywhere is a refusal.
 *    * **The manifest file itself must not be a link**, and `--out` must not
 *      overwrite anything inside the repository work tree.
 *
 *  Legitimate bundles are unaffected: ordinary relative filenames with dots,
 *  dashes and non-ASCII characters are accepted as long as they are NFC and
 *  contain no separator ambiguity. The 65-file production bundle is the
 *  regression fixture for that.
 *
 *  Nothing here shells out, opens a socket, or reads a host path — it reads a
 *  bundle directory and a JSON file, and that is all.
 *
 *  Usage
 *  -----
 *    node tools/release/deploy_manifest.mjs build \
 *      --bundle /tmp/atelier-bundle --commit <40-hex> \
 *      --known <64-hex>[,<64-hex>…] --out canary.json
 *
 *    node tools/release/deploy_manifest.mjs check canary.json \
 *      --allow-root /srv/atelier-v1-portal        # REQUIRED now
 */

import { createHash } from 'node:crypto';
import {
  existsSync, lstatSync, readFileSync, writeFileSync,
} from 'node:fs';
import { dirname, isAbsolute, join, posix, relative, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
export const REPO_ROOT = resolve(HERE, '..', '..');
export const ALLOWLIST_PATH = join(HERE, 'release-allowlist.json');

export const MANIFEST_SCHEMA = 'atelier-v1-deploy-manifest/1';
export const BUNDLE_SCHEMA = 'atelier-v1-static-release/1';

/** Ports this project must never be told to bind. */
const FORBIDDEN_PORTS = Object.freeze([
  8443, // external TLS for the existing production entry
  18443, // the reverse proxy's own listener
  8080, // the upstream Gateway
  8123, // the preview server's silent default
]);

/** The four approvals an operator must obtain, each from a different owner. */
export const APPROVAL_GATES = Object.freeze([
  'pam-root-uid',
  'gateway-strata',
  'caddy-systemd-8443',
  'owner-go',
]);

/** Keys whose presence would mean this file can execute something. */
const COMMAND_KEYS = ['command', 'cmd', 'exec', 'shell', 'script', 'sudo', 'systemctl', 'caddy'];

/** Characters that make a path ambiguous across platforms. */
const CONTROL_CHARS = /[\u0000-\u001F\u007F\u0080-\u009F]/;

export function sha256(text) {
  return createHash('sha256').update(text).digest('hex');
}

/** Stable JSON: keys sorted at every level, two-space indent, trailing newline. */
export function stableJson(value) {
  const sort = (v) => {
    if (Array.isArray(v)) return v.map(sort);
    if (v && typeof v === 'object') {
      return Object.fromEntries(Object.keys(v).sort().map((k) => [k, sort(v[k])]));
    }
    return v;
  };
  return JSON.stringify(sort(value), null, 2) + '\n';
}

/* ------------------------------------------------------------------ paths */

/**
 * Validate one bundle entry path as a plain relative POSIX path.
 *
 * Deliberately strict about *separators and segments*, not about names: a file
 * called `render-sunburst.js`, `V6.1_notes.txt` or `图表.css` is fine. What is
 * refused is anything that could resolve somewhere else — absolute paths,
 * `..`, backslashes, drive/stream prefixes, control characters, and text that
 * is not NFC (which can make two different byte strings look identical).
 *
 * @returns {string[]} problems; empty means the path is a safe relative entry.
 */
export function checkEntryPath(rel) {
  if (typeof rel !== 'string' || rel === '') return ['entry path must be a non-empty string'];
  const problems = [];
  if (rel.normalize('NFC') !== rel) problems.push(`entry path is not NFC-normalised: ${JSON.stringify(rel)}`);
  if (CONTROL_CHARS.test(rel)) problems.push(`entry path contains a control character: ${JSON.stringify(rel)}`);
  if (rel.includes('\\')) problems.push(`entry path contains a backslash: ${JSON.stringify(rel)}`);
  if (rel.startsWith('/')) problems.push(`entry path is absolute: ${JSON.stringify(rel)}`);
  if (/^[A-Za-z]:/.test(rel)) problems.push(`entry path carries a drive or stream prefix: ${JSON.stringify(rel)}`);
  const segments = rel.split('/');
  if (segments.some((s) => s === '' || s === '.' || s === '..')) {
    problems.push(`entry path has an empty, "." or ".." segment: ${JSON.stringify(rel)}`);
  }
  return problems;
}

/**
 * Assert that no component of `abs` is a symbolic link, including the last one.
 *
 * Symlinks are how a "contained" path escapes containment after the fact: the
 * string looks inside the bundle, `readFileSync` follows the link, and the hash
 * is computed over bytes from somewhere else entirely. `lstat` on every
 * component is the only way to know the difference.
 *
 * @throws {Error} when a component is missing or is a symbolic link
 */
export function assertNoSymlinks(abs) {
  const parts = abs.split(sep).filter(Boolean);
  let cur = sep;
  for (const part of parts) {
    cur = join(cur, part);
    let st;
    try {
      st = lstatSync(cur);
    } catch {
      throw new Error(`missing path component: ${cur}`);
    }
    if (st.isSymbolicLink()) throw new Error(`symbolic link in path: ${cur}`);
  }
  return lstatSync(abs);
}

/** Validate a deployment root: absolute, normalised, no ambiguity. */
export function checkDeploymentRoot(root) {
  if (typeof root !== 'string' || root === '') return ['target.root must be a non-empty string'];
  const problems = [];
  if (CONTROL_CHARS.test(root)) problems.push('target.root contains a control character');
  if (root.includes('\\')) problems.push('target.root contains a backslash');
  if (!root.startsWith('/')) problems.push('target.root must be absolute');
  // `path.posix.normalize` keeps a trailing separator, so it cannot catch this.
  if (root.length > 1 && root.endsWith('/')) problems.push('target.root must not end with "/"');
  if (posix.normalize(root) !== root) {
    problems.push('target.root is not normalised (no trailing slash, no "." or ".." segments)');
  }
  if (root === '/') problems.push('target.root must not be the filesystem root');
  return problems;
}

/* ----------------------------------------------------------------- bundle */

/** Re-hash a bundle directory and compare it to its own manifest. */
export function verifyBundle(bundleDir) {
  const rootGiven = resolve(String(bundleDir ?? ''));
  if (!bundleDir) throw new Error('a bundle directory is required');

  // The bundle root itself must not travel through a link.
  assertNoSymlinks(rootGiven);
  if (!lstatSync(rootGiven).isDirectory()) {
    throw new Error(`not a directory: ${rootGiven}`);
  }
  const rootReal = resolve(rootGiven);

  const manifestPath = join(rootReal, 'release-manifest.json');
  try {
    assertNoSymlinks(manifestPath);
  } catch (err) {
    throw new Error(`bundle manifest is not a plain file: ${err.message}`);
  }
  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  if (manifest.schema !== BUNDLE_SCHEMA) {
    throw new Error(`unsupported bundle schema: ${manifest.schema}`);
  }
  if (!Array.isArray(manifest.files)) throw new Error('bundle manifest has no files list');

  const problems = [];
  const seen = new Set();

  for (const entry of manifest.files) {
    const rel = entry && entry.path;
    const pathProblems = checkEntryPath(rel);
    if (pathProblems.length) {
      problems.push(...pathProblems);
      continue;
    }
    if (seen.has(rel)) {
      problems.push(`duplicate entry in the bundle manifest: ${rel}`);
      continue;
    }
    seen.add(rel);

    const abs = join(rootReal, rel);
    const relFromRoot = relative(rootReal, abs);
    if (relFromRoot === '' || relFromRoot.startsWith('..') || isAbsolute(relFromRoot)) {
      problems.push(`entry escapes the bundle: ${rel}`);
      continue;
    }
    try {
      assertNoSymlinks(abs);
    } catch (err) {
      problems.push(`${err.message} (entry: ${rel})`);
      continue;
    }
    if (!lstatSync(abs).isFile()) {
      problems.push(`entry is not a regular file: ${rel}`);
      continue;
    }
    const data = readFileSync(abs);
    if (data.length !== entry.bytes) problems.push(`byte count changed: ${rel}`);
    if (sha256(data) !== entry.sha256) problems.push(`content changed: ${rel}`);
  }

  const digest = sha256(manifest.files.map((e) => `${e.path}:${e.sha256}`).join('\n') + '\n');
  if (digest !== manifest.bundle_sha256) {
    problems.push(`bundle digest mismatch: manifest says ${manifest.bundle_sha256}, files hash ${digest}`);
  }
  if (problems.length) throw new Error(`bundle failed re-verification:\n  - ${problems.join('\n  - ')}`);
  return manifest;
}

/** Build a canary manifest from a verified bundle. Deterministic. */
export function buildManifest({
  bundleDir,
  commit,
  knownGood,
  generatedAt = null,
  root = '/srv/atelier-v1-portal',
  unit = 'atelier-v1-portal-canary.service',
  user = 'atelier-v1-portal',
  port = 18766,
  stateDir = '/var/lib/atelier-v1-portal',
} = {}) {
  if (!/^[0-9a-f]{40}$/.test(String(commit || ''))) {
    throw new Error('--commit must be a 40-character lowercase hex commit SHA');
  }
  const known = (knownGood || []).map(String);
  for (const sha of known) {
    if (!/^[0-9a-f]{64}$/.test(sha)) throw new Error(`--known entry is not a sha256: ${sha}`);
  }
  const rootProblems = checkDeploymentRoot(root);
  if (rootProblems.length) throw new Error(`deployment root refused: ${rootProblems.join('; ')}`);

  const bundle = verifyBundle(bundleDir);
  if (!known.includes(bundle.bundle_sha256)) {
    throw new Error(
      `the bundle's digest ${bundle.bundle_sha256} is not in --known; `
      + 'a candidate that cannot be named in the rollback set must not be deployed',
    );
  }
  const allowlist = readFileSync(ALLOWLIST_PATH);
  const manifest = {
    schema: MANIFEST_SCHEMA,
    kind: 'canary',
    candidate: {
      source_commit: commit,
      bundle_sha256: bundle.bundle_sha256,
      bundle_file_count: bundle.file_count,
      bundle_total_bytes: bundle.total_bytes,
      allowlist_sha256: sha256(allowlist),
    },
    target: {
      root,
      release_dir: `${root}/releases/${bundle.bundle_sha256}`,
      current_link: `${root}/current`,
      build_profile: 'production',
      non_production: true,
    },
    service: {
      unit,
      user,
      port,
      bind: '127.0.0.1',
      working_directory: `${root}/current`,
      state_dir: stateDir,
    },
    approval_gates: [...APPROVAL_GATES],
    known_good_bundles: [...new Set([...known, bundle.bundle_sha256])].sort(),
    rollback: {
      method: 'symlink-swap',
      to_bundle_sha256: null, // filled by the operator from known_good_bundles
      validated: false,
    },
    execution_authorized: false,
  };
  if (generatedAt) manifest.generated_at = generatedAt;
  return manifest;
}

/* --------------------------------------------------------------- validate */

/**
 * Validate a manifest. Returns `{ok, problems}`; never throws on bad input.
 *
 * `allowedRoots` is **required**. An empty or missing set is a refusal — the
 * previous version treated "no approved roots" as "no restriction", which is
 * exactly backwards for a gate.
 */
export function validateManifest(doc, { allowedRoots = [], requireNonProduction = false } = {}) {
  const problems = [];
  const isHex = (v, n) => typeof v === 'string' && new RegExp(`^[0-9a-f]{${n}}$`).test(v);

  if (!doc || typeof doc !== 'object') return { ok: false, problems: ['manifest is not an object'] };
  if (doc.schema !== MANIFEST_SCHEMA) problems.push(`schema must be ${MANIFEST_SCHEMA}`);
  if (doc.kind !== 'canary' && doc.kind !== 'rollback') {
    problems.push('kind must be "canary" or "rollback"');
  }

  // No field may be able to run something.
  const walk = (v, path) => {
    if (Array.isArray(v)) return v.forEach((item, i) => walk(item, `${path}[${i}]`));
    if (v && typeof v === 'object') {
      for (const [k, val] of Object.entries(v)) {
        if (COMMAND_KEYS.includes(k.toLowerCase())) {
          problems.push(`"${path}.${k}" is a command-shaped field; this file must not carry commands`);
        }
        walk(val, `${path}.${k}`);
      }
    }
  };
  walk(doc, '$');

  const candidate = doc.candidate || {};
  if (!isHex(candidate.source_commit, 40)) problems.push('candidate.source_commit must be 40 hex');
  if (!isHex(candidate.bundle_sha256, 64)) problems.push('candidate.bundle_sha256 must be 64 hex');
  if (!isHex(candidate.allowlist_sha256, 64)) problems.push('candidate.allowlist_sha256 must be 64 hex');

  const known = Array.isArray(doc.known_good_bundles) ? doc.known_good_bundles : [];
  if (!known.length) problems.push('known_good_bundles must list at least one digest');
  for (const sha of known) {
    if (!isHex(sha, 64)) problems.push(`known_good_bundles entry is not a sha256: ${sha}`);
  }
  if (candidate.bundle_sha256 && !known.includes(candidate.bundle_sha256)) {
    problems.push('candidate.bundle_sha256 is not in known_good_bundles');
  }

  const rollback = doc.rollback || {};
  if (doc.kind === 'rollback') {
    const to = rollback.to_bundle_sha256;
    if (!isHex(to, 64)) {
      problems.push('a rollback manifest must name rollback.to_bundle_sha256');
    } else if (!known.includes(to)) {
      // The point of the rule: an unrecorded digest is not a rollback target.
      problems.push(`rollback.to_bundle_sha256 ${to} is not in known_good_bundles; refusing an unknown SHA`);
    } else if (to === candidate.bundle_sha256) {
      problems.push('a rollback must target a different bundle than the candidate');
    }
    if (rollback.validated !== true) {
      problems.push('a rollback manifest must record rollback.validated = true (rehearsed)');
    }
  }

  // ---- deny-by-default on the deployment root -------------------------------
  const target = doc.target || {};
  const approved = Array.isArray(allowedRoots)
    ? allowedRoots.filter((r) => typeof r === 'string' && r !== '')
    : [];
  if (approved.length === 0) {
    problems.push(
      'no approved deployment roots were supplied; pass at least one --allow-root '
      + '(deny-by-default: an omitted allowlist refuses every target.root)',
    );
  } else {
    const normalised = [];
    for (const root of approved) {
      const rootProblems = checkDeploymentRoot(root);
      if (rootProblems.length) problems.push(...rootProblems.map((p) => `approved root: ${p}`));
      else normalised.push(posix.normalize(root));
    }
    const rootProblems = checkDeploymentRoot(target.root);
    problems.push(...rootProblems);
    if (!rootProblems.length && !normalised.includes(target.root)) {
      problems.push(`target.root ${target.root} is not an allowed root (approved: ${normalised.join(', ')})`);
    }
  }

  if (target.build_profile !== 'production') {
    problems.push('target.build_profile must be "production"');
  }
  if (requireNonProduction && target.non_production !== true) {
    problems.push('a rehearsal manifest must set target.non_production = true');
  }

  const port = doc.service && doc.service.port;
  if (!Number.isInteger(port)) problems.push('service.port must be an integer');
  else if (FORBIDDEN_PORTS.includes(port)) {
    problems.push(`service.port ${port} is a reserved production/upstream port`);
  }
  if (doc.service && doc.service.bind !== '127.0.0.1') {
    problems.push('service.bind must be 127.0.0.1; a canary does not listen publicly');
  }

  const gates = Array.isArray(doc.approval_gates) ? doc.approval_gates : [];
  for (const gate of APPROVAL_GATES) {
    if (!gates.includes(gate)) problems.push(`missing approval gate: ${gate}`);
  }

  if (doc.execution_authorized !== false) {
    problems.push('execution_authorized must be false; this file authorises nothing');
  }

  return { ok: problems.length === 0, problems };
}

/* ------------------------------------------------------------------ CLI */

function parseArgs(argv) {
  const args = { _: [], 'allow-root': [] };
  for (let i = 0; i < argv.length; i += 1) {
    const a = argv[i];
    if (a.startsWith('--')) {
      const key = a.slice(2);
      const val = argv[i + 1];
      if (val === undefined || val.startsWith('--')) { args[key] = true; }
      else {
        if (key === 'allow-root') args['allow-root'].push(val);
        else args[key] = val;
        i += 1;
      }
    } else args._.push(a);
  }
  return args;
}

/** Refuse to write anywhere inside the repository work tree. */
function assertOutsideRepo(target) {
  const abs = resolve(String(target));
  const fromRoot = relative(REPO_ROOT, abs);
  if (fromRoot === '' || (!fromRoot.startsWith('..') && !isAbsolute(fromRoot))) {
    throw new Error(`refusing to write inside the repository work tree: ${abs}`);
  }
  return abs;
}

function main() {
  const args = parseArgs(process.argv.slice(2));
  const [sub] = args._;
  try {
    if (sub === 'build') {
      const out = args.out ? assertOutsideRepo(args.out) : null;
      const manifest = buildManifest({
        bundleDir: resolve(String(args.bundle || '')),
        commit: args.commit,
        knownGood: String(args.known || '').split(',').filter(Boolean),
        generatedAt: args['generated-at'] || null,
      });
      const text = stableJson(manifest);
      if (out) writeFileSync(out, text);
      else process.stdout.write(text);
      process.stderr.write(`ok: canary manifest for ${manifest.candidate.bundle_sha256}\n`);
      return 0;
    }
    if (sub === 'check') {
      const file = args._[1];
      if (!file) { process.stderr.write('check needs a manifest path\n'); return 2; }
      const abs = resolve(file);
      // The manifest itself may not arrive through a link.
      try {
        assertNoSymlinks(abs);
      } catch (err) {
        process.stderr.write(`manifest refused: ${err.message}\n`);
        return 1;
      }
      const doc = JSON.parse(readFileSync(abs, 'utf8'));
      const verdict = validateManifest(doc, {
        allowedRoots: args['allow-root'],
        requireNonProduction: args.rehearsal === true,
      });
      if (verdict.ok) {
        process.stderr.write('ok: manifest accepted\n');
        return 0;
      }
      process.stderr.write(`manifest refused:\n  - ${verdict.problems.join('\n  - ')}\n`);
      return 1;
    }
    process.stderr.write(
      'usage: deploy_manifest.mjs build --bundle DIR --commit SHA --known SHA[,SHA] [--out FILE]\n'
      + '       deploy_manifest.mjs check FILE --allow-root PATH [--allow-root PATH] [--rehearsal]\n',
    );
    return 2;
  } catch (err) {
    process.stderr.write(`${err && err.message ? err.message : err}\n`);
    return 1;
  }
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) {
  process.exit(main());
}
