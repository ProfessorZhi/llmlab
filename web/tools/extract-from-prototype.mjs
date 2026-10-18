/*! web/tools/extract-from-prototype.mjs
 *  Provenance tool: derives web/index.html + web/css/*.css from the frozen
 *  source prototype, WITHOUT editing it. The prototype stays byte-identical;
 *  this script only reads it and writes new files under web/.
 *
 *  Run:  node web/tools/extract-from-prototype.mjs [--check]
 *        --check  verifies the generated files on disk still match the source
 *                 (no writes; exits non-zero on drift)
 *
 *  The source is now **Atelier 06**, which is itself derived: 06 is 05 plus the
 *  Portal-module additions, computed by prototypes/v6/derive-from-v5.mjs, which
 *  hashes 05 before reading it. So the chain is
 *
 *      prototypes/v5/index.html   (frozen design authority, never edited)
 *        -> prototypes/v6/index.html   (05 + authored additions)
 *          -> web/                     (this script)
 *
 *  and every link in it is checked rather than asserted. Editing 05 breaks the
 *  derivation; editing 06 breaks this script; editing web/ breaks --check.
 *
 *  Why a script instead of hand-copying: the prototype is 145 kB. Slicing the
 *  original bytes removes transcription risk and makes the derivation
 *  reviewable. Everything the script writes is reproducible from the source.
 */
import { readFileSync, writeFileSync, mkdirSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const WEB = resolve(HERE, '..');
const REPO = resolve(WEB, '..');
const SRC = join(REPO, 'prototypes', 'v6', 'index.html');

/** sha256 of prototypes/v6/index.html (LF-normalised). */
export const EXPECTED_SOURCE_SHA256 =
  'b05e754453edfb5238249957dd1e622d452bbff57cbdf951eb7d4ee5fa7f4573';

/** The frozen 05 authority source, kept here so the generated banners can name
 *  the whole chain instead of just the last hop. */
export const V5_SHA256 =
  '04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf';

/** Read the frozen prototype, normalised to LF.
 *
 *  A fresh clone on Windows gets `core.autocrlf=true`, so the worktree copy of
 *  the prototype arrives with CRLF even though the committed blob is LF.
 *  Hashing or slicing those bytes would (a) miss the pinned sha256 below and
 *  (b) leak CRLF into every generated file. The prototype is only ever read
 *  here — never written — so its bytes on disk are untouched either way. */
const readSource = () => readFileSync(SRC, 'utf8').replace(/\r\n/g, '\n');
const sha256 = (s) => createHash('sha256').update(s, 'utf8').digest('hex');

/** Slice CSS text between the two <style> tags. */
function extractStyle(css) {
  const open = css.indexOf('<style>');
  const close = css.indexOf('</style>');
  if (open < 0 || close < 0 || close < open) throw new Error('style block not found');
  return css.slice(open + '<style>'.length, close);
}

/** Slice the body markup: from just after <body> through the closing </dialog>. */
function extractBody(html) {
  const open = html.indexOf('<body>');
  const dialogEnd = html.indexOf('</dialog>');
  if (open < 0 || dialogEnd < 0) throw new Error('body/dialog markers not found');
  return html.slice(open + '<body>'.length, dialogEnd + '</dialog>'.length);
}

/** Split the inline CSS at the top-level `/* ... *\/` banner comments that mark
 *  the Atelier layers, so each layer can be routed to its own stylesheet. */
function splitLayers(css) {
  const atlasMarker = '/* Atelier Observatory 05 — Atlas visualization.';
  const portalMarker = '/* Atelier Observatory 06 — Portal modules';
  const atlasAt = css.indexOf(atlasMarker);
  if (atlasAt < 0) throw new Error('atlas layer banner not found');
  const portalAt = css.indexOf(portalMarker);
  if (portalAt < 0) throw new Error('portal layer banner not found');
  if (portalAt < atlasAt) throw new Error('portal layer must follow the atlas layer');
  return {
    shell: css.slice(0, atlasAt),
    atlas: css.slice(atlasAt, portalAt),
    portal: css.slice(portalAt),
  };
}

/** Pull every top-level `:root{...}` / `:root[data-theme=dark]{...}` block out
 *  of a CSS chunk, preserving their original relative order. Brace-matched so
 *  multi-line blocks survive intact. */
function pullRootTokens(css) {
  const tokens = [];
  let out = '';
  let i = 0;
  while (i < css.length) {
    const m = /^:root(?:\[data-theme=["']?dark["']?\])?\s*\{/m.exec(css.slice(i));
    if (m && m.index === 0) {
      // brace match from the opening { at end of match
      let depth = 0;
      let j = i + m[0].length - 1;
      const start = i;
      for (; j < css.length; j++) {
        if (css[j] === '{') depth++;
        else if (css[j] === '}') {
          depth--;
          if (depth === 0) { j++; break; }
        }
      }
      tokens.push(css.slice(start, j).trim());
      i = j;
      // swallow one trailing newline so the remainder keeps its shape
      if (css[i] === '\n') i++;
      continue;
    }
    // advance to end of current line
    const nl = css.indexOf('\n', i);
    out += nl < 0 ? css.slice(i) : css.slice(i, nl + 1);
    i = nl < 0 ? css.length : nl + 1;
  }
  return { tokens, rest: out };
}

const BANNER = (title, sourceSha) =>
  `/*! ${title}\n` +
  ` *  Derived from prototypes/v6/index.html @ sha256:${sourceSha.slice(0, 12)}…\n` +
  ` *  (v6 is itself prototypes/v5/index.html @ ${V5_SHA256.slice(0, 12)}… plus\n` +
  ` *  the authored Portal additions — see prototypes/v6/derive-from-v5.mjs.)\n` +
  ` *  Generated by web/tools/extract-from-prototype.mjs — edit the generator,\n` +
  ` *  not this file, or the next --check run will flag drift.\n` +
  ` */\n`;

export function generate() {
  const raw = readSource();
  const sourceSha = sha256(raw);
  if (sourceSha !== EXPECTED_SOURCE_SHA256) {
    throw new Error(
      `prototype changed: expected ${EXPECTED_SOURCE_SHA256}, got ${sourceSha}. ` +
      `The source is frozen; do NOT regenerate — investigate first.`,
    );
  }

  const css = extractStyle(raw);
  const { shell, atlas, portal } = splitLayers(css);
  const shellParts = pullRootTokens(shell);
  const atlasParts = pullRootTokens(atlas);
  const portalParts = pullRootTokens(portal);

  const tokensCss =
    BANNER('web/css/tokens.css — design tokens', sourceSha) +
    '\n' +
    shellParts.tokens.concat(atlasParts.tokens, portalParts.tokens).join('\n\n') +
    '\n';

  const baseCss =
    BANNER('web/css/base.css — shell, typography, components', sourceSha) +
    '\n' +
    shellParts.rest.replace(/\n{3,}/g, '\n\n').trim() +
    '\n';

  const atlasCss =
    BANNER('web/css/atlas.css — Traffic Atlas', sourceSha) +
    '\n' +
    atlasParts.rest.replace(/\n{3,}/g, '\n\n').trim() +
    '\n';

  const portalCss =
    BANNER('web/css/portal.css — Portal modules', sourceSha) +
    '\n' +
    portalParts.rest.replace(/\n{3,}/g, '\n\n').trim() +
    '\n';

  const body = extractBody(raw);

  /** The one page template. `profile` is the ONLY difference between the console
   *  that ships and the isolated QA preview — see web/js/build-profile.js for
   *  everything `data-build-profile` gates. Both artifacts are generated from
   *  the same frozen source, so neither can drift into the other's behaviour. */
  const pageHtml = ({ profile, title, note }) => `<!doctype html>
<html lang="zh-CN" data-theme="light" data-build-profile="${profile}">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light dark"><title>${title}</title>
<meta name="description" content="Dell LLM Lab 统一管理后台：Strata、Gateway、Grafana、Prometheus、EvalScope、API 与 Linux 账号身份（PAM 待接线）。界面原型，非生产数据。">
<!-- Derived from prototypes/v6/index.html @ sha256:${sourceSha.slice(0, 12)}… — see web/tools/extract-from-prototype.mjs -->
${note}<link rel="stylesheet" href="./css/tokens.css">
<link rel="stylesheet" href="./css/base.css">
<link rel="stylesheet" href="./css/atlas.css">
<link rel="stylesheet" href="./css/portal.css">
</head>
<body>${body}
<script type="module" src="./js/app.js"></script>
</body></html>
`;

  // The deployed console. The design samples are not hidden here — this bundle
  // has no path that can put them on screen, and `?demo=1` is inert.
  const indexHtml = pageHtml({
    profile: 'production',
    title: 'Dell LLM Lab — Atelier Observatory · 06',
    note: '',
  });

  // The isolated QA preview, generated for design review only. It differs by one
  // attribute, and that attribute is the whole gate. It is not linked from the
  // production page and must never be deployed.
  const qaHtml = pageHtml({
    profile: 'qa',
    title: 'Dell LLM Lab — Atelier Observatory · 06 · QA 预览（非生产）',
    note: '<!-- NON-PRODUCTION: the isolated QA preview. It differs from web/index.html\n' +
      '     by data-build-profile="qa", which is what lets ?demo=1 reach the design\n' +
      '     samples. Serve it locally for review; do not deploy this file. -->\n',
  });

  return { tokensCss, baseCss, atlasCss, portalCss, indexHtml, qaHtml, sourceSha };
}

const targets = (out) => ({
  [join(WEB, 'css', 'tokens.css')]: out.tokensCss,
  [join(WEB, 'css', 'base.css')]: out.baseCss,
  [join(WEB, 'css', 'atlas.css')]: out.atlasCss,
  [join(WEB, 'css', 'portal.css')]: out.portalCss,
  [join(WEB, 'index.html')]: out.indexHtml,
  [join(WEB, 'qa.html')]: out.qaHtml,
});

function main() {
  const check = process.argv.includes('--check');
  const out = generate();
  const files = targets(out);
  let drift = 0;
  for (const [path, text] of Object.entries(files)) {
    if (check) {
      // Normalise the same way: on an autocrlf checkout the worktree copy of a
      // generated file is CRLF too, and this compares content fidelity — not
      // line-ending style (the committed blobs are LF; see tests/tracked-assets).
      const current = existsSync(path) ? readFileSync(path, 'utf8').replace(/\r\n/g, '\n') : null;
      if (current !== text) { drift++; console.error(`DRIFT: ${path}`); }
    } else {
      mkdirSync(dirname(path), { recursive: true });
      writeFileSync(path, text, 'utf8');
      console.log(`wrote ${path} (${Buffer.byteLength(text)} bytes)`);
    }
  }
  if (check) {
    if (drift) { console.error(`${drift} file(s) drifted from generator output`); process.exit(1); }
    console.log('ok: web/ matches generator output');
  }
  console.log(`source sha256: ${out.sourceSha}`);
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) main();
