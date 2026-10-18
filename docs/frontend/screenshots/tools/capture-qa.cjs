/*! docs/frontend/screenshots/tools/capture-qa.cjs — real-browser QA captures.
 *
 *  Usage: node capture-qa.cjs <outDir> <origin> <commitSha>
 *  Drives a real headless Chromium (Playwright) against the loopback static
 *  server (`npm run serve`), switches theme by clicking the shell's own theme
 *  button, and writes full-page PNGs plus manifest.json recording commit, URL,
 *  viewport, theme, data mode, capture time and measured horizontal overflow.
 *  It never fabricates an image: a failed navigation is recorded as an error.
 */
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const { mkdirSync, writeFileSync } = require('node:fs');
const { join } = require('node:path');

const [,, outDir, origin, sha] = process.argv;
const PAGES = ['overview', 'compute', 'models', 'api', 'calls', 'observability', 'atlas', 'evaluation', 'admin'];
const MATRIX = [
  { width: 1440, theme: 'light', mode: 'live', pages: PAGES },
  { width: 1440, theme: 'light', mode: 'demo', pages: PAGES },
  { width: 1440, theme: 'dark', mode: 'demo', pages: PAGES },
  { width: 390, theme: 'light', mode: 'demo', pages: PAGES },
  { width: 390, theme: 'dark', mode: 'live', pages: PAGES },
];
const CHECK_WIDTHS = [320, 768, 1024];

(async () => {
  mkdirSync(outDir, { recursive: true });
  const browser = await chromium.launch();
  const manifest = {
    commit: sha, origin, browser: `Chromium ${browser.version()}`, captured_at: new Date().toISOString(),
    note: 'Real headless Chromium captures of web/ served by web/tools/serve.mjs on loopback. live = bare URL (no backend: honest NOT CONFIGURED / UNKNOWN states); demo = ?demo=1 SYNTHETIC fixtures.',
    shots: [], overflow: [],
  };
  for (const set of MATRIX) {
    const ctx = await browser.newContext({ viewport: { width: set.width, height: 900 }, reducedMotion: 'reduce' });
    const page = await ctx.newPage();
    for (const name of set.pages) {
      const url = `${origin}/index.html${set.mode === 'demo' ? '?demo=1' : ''}#${name}`;
      const file = `${name}-${set.width}-${set.theme}-${set.mode}.png`;
      try {
        await page.goto(url);
        await page.waitForFunction(() => window.__ATELIER__ && window.__WORKSPACE__);
        await page.waitForTimeout(900);
        if (set.theme === 'dark') { await page.click('#themeButton'); await page.waitForTimeout(400); }
        const sw = await page.evaluate(() => document.documentElement.scrollWidth);
        await page.screenshot({ path: join(outDir, file), fullPage: true });
        manifest.shots.push({ file, url, viewport: `${set.width}x900`, theme: set.theme, mode: set.mode, page: name, scroll_width: sw });
      } catch (e) {
        manifest.shots.push({ file: null, url, error: String(e).slice(0, 200) });
      }
    }
    await ctx.close();
  }
  for (const width of CHECK_WIDTHS) {
    const ctx = await browser.newContext({ viewport: { width, height: 900 } });
    const page = await ctx.newPage();
    for (const mode of ['live', 'demo']) {
      for (const name of PAGES) {
        await page.goto(`${origin}/index.html${mode === 'demo' ? '?demo=1' : ''}#${name}`);
        await page.waitForFunction(() => window.__ATELIER__ && window.__WORKSPACE__);
        await page.waitForTimeout(500);
        const sw = await page.evaluate(() => document.documentElement.scrollWidth);
        manifest.overflow.push({ width, mode, page: name, scroll_width: sw, overflows: sw > width });
      }
    }
    await ctx.close();
  }
  await browser.close();
  writeFileSync(join(outDir, 'manifest.json'), `${JSON.stringify(manifest, null, 2)}\n`);
  console.log(`shots=${manifest.shots.filter((s) => s.file).length} errors=${manifest.shots.filter((s) => !s.file).length} overflow=${manifest.overflow.filter((o) => o.overflows).length}`);
})();
