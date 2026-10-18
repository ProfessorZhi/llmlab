/*! tests/lib/static-refs.mjs
 *  Resolve the *markup and stylesheet* references a fresh clone must be able to
 *  follow: <link href> · <script src> · <img src> · <source src> · @import · url().
 *
 *  JavaScript module edges are deliberately NOT handled here. Reading them with
 *  regexes does not work: a doc comment that *describes* an import, or a string
 *  literal that merely *contains* one, is indistinguishable from a real edge
 *  without an actual parser. This scanner reported both — its own header, and
 *  its own unit test — as missing files before that was understood.
 *  tests/tracked-assets.test.js resolves the JS graph with Node's own linker
 *  instead, which is authoritative and costs nothing.
 *
 *  Comments are masked before matching, with string state tracked so the `//`
 *  inside "https://lab.example/…" is not mistaken for a comment. Masking
 *  preserves every offset and newline, so reported line numbers stay true.
 *
 *  Pure string work: no filesystem, no git. The caller supplies the tree.
 */
import { posix } from 'node:path';

/** `https://…`, `data:…`, `//cdn…` — anything not a repo-local path. */
const EXTERNAL = /^(?:[a-z][a-z0-9+.-]*:|\/\/)/i;
export const isExternalSpecifier = (spec) => EXTERNAL.test(spec.trim());

const HTML_TAG = /<(link|script|img|source)\b[^>]*>/gi;
const HTML_ATTR = /\b(?:href|src)\s*=\s*["']([^"']+)["']/i;

const CSS_AT_IMPORT = /@import\s+(?:url\(\s*)?["']([^"'\n]+)["']/g;
const CSS_URL = /\burl\(\s*["']?([^"')\s]+)["']?\s*\)/g;

export const kindOf = (relPath) => {
  if (/\.html?$/i.test(relPath)) return 'html';
  if (/\.css$/i.test(relPath)) return 'css';
  return null;
};

/**
 * Blank out comments, preserving every offset and newline.
 * @param {'html'|'css'} kind
 */
export function maskComments(text, kind) {
  if (kind === 'html') {
    return text.replace(/<!--[\s\S]*?-->/g, (m) => m.replace(/[^\n]/g, ' '));
  }
  const out = [];
  let i = 0;
  let quote = null;
  let block = false;
  while (i < text.length) {
    const c = text[i];
    if (block) {
      if (c === '*' && text[i + 1] === '/') { out.push('  '); i += 2; block = false; continue; }
      out.push(c === '\n' ? '\n' : ' ');
      i++;
      continue;
    }
    if (quote) {
      // Keep string bodies: `url("…")` is where the reference lives.
      if (c === '\\') { out.push(c, text[i + 1] ?? ''); i += 2; continue; }
      if (c === quote) quote = null;
      out.push(c);
      i++;
      continue;
    }
    if (c === '/' && text[i + 1] === '*') { out.push('  '); i += 2; block = true; continue; }
    if (c === '"' || c === "'") { quote = c; out.push(c); i++; continue; }
    out.push(c);
    i++;
  }
  return out.join('');
}

/**
 * Resolve a specifier against the file that contains it.
 * @returns {string|null} repo-relative posix path, or null when the specifier
 *   is external (i.e. not a file this repository is expected to own).
 */
export function resolveRef(fromRel, spec) {
  const raw = spec.trim();
  if (!raw || isExternalSpecifier(raw)) return null;
  const clean = raw.split('?')[0].split('#')[0];
  if (!clean) return null; // pure fragment, e.g. href="#view-atlas"
  if (clean.startsWith('/')) return clean.replace(/^\/+/, ''); // server-root relative
  if (!clean.startsWith('.')) return null;
  return posix.normalize(posix.join(posix.dirname(fromRel), clean));
}

const lineOf = (text, spec) => text.slice(0, text.indexOf(spec)).split('\n').length;

/**
 * @returns {Array<{spec:string, target:string|null, external:boolean, line:number}>}
 */
export function extractRefs(relPath, text) {
  const kind = kindOf(relPath);
  if (!kind) return [];
  const code = maskComments(text, kind);
  const found = [];
  const push = (spec) => {
    if (found.some((f) => f.spec === spec)) return;
    found.push({
      spec,
      target: resolveRef(relPath, spec),
      external: isExternalSpecifier(spec),
      line: lineOf(code, spec),
    });
  };

  if (kind === 'css') {
    for (const re of [CSS_AT_IMPORT, CSS_URL]) {
      re.lastIndex = 0;
      for (const m of code.matchAll(re)) push(m[1]);
    }
  } else {
    for (const tag of code.matchAll(HTML_TAG)) {
      const attr = HTML_ATTR.exec(tag[0]);
      if (attr) push(attr[1]);
    }
  }
  return found;
}
