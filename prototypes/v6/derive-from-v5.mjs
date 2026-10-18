/*! prototypes/v6/derive-from-v5.mjs
 *  Derive the Atelier Observatory 06 shell from the frozen 05 prototype.
 *
 *  Run:  node prototypes/v6/derive-from-v5.mjs [--check]
 *
 *  Why a script instead of a hand-edited 135 kB file: the 05 prototype is the
 *  design authority and stays byte-identical, so the *only* honest way to state
 *  "06 is 05 plus these additions" is to actually compute it. The script proves
 *  the claim on every run — it hashes 05 before reading it, and `--check` fails
 *  if the committed 06 file has drifted from what the additions produce.
 *
 *  It is deliberately a one-way transform: 05 -> 06. Regenerating 05 is not a
 *  thing this script can do, and nothing here writes to prototypes/v5/.
 *
 *  The additions themselves are plain HTML/CSS in this directory
 *  (`additions.html`, `additions.css`) rather than string literals in here, so
 *  the authored design can be reviewed as design.
 */
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO = resolve(HERE, '..', '..');

const V5 = resolve(REPO, 'prototypes', 'v5', 'index.html');
const ADD_HTML = resolve(HERE, 'additions.html');
const ADD_CSS = resolve(HERE, 'additions.css');
const OUT = resolve(HERE, 'index.html');

/** sha256 of prototypes/v5/index.html, normalised to LF.
 *
 *  Pinned here as well as in web/tools/extract-from-prototype.mjs on purpose:
 *  if someone edits the 05 authority source, BOTH this derivation and the web
 *  generator have to be told, and neither can happen by accident. */
export const V5_SHA256 =
  '04003f5c4788a1a52849afd14266941b0e5458f28e059cb602dc53adae95c7bf';

const readLf = (p) => readFileSync(p, 'utf8').replace(/\r\n/g, '\n');
const sha256 = (s) => createHash('sha256').update(s, 'utf8').digest('hex');

/** Read every `<!--#name--> … <!--/#name-->` fragment out of additions.html. */
export function readFragments(text) {
  const out = new Map();
  const re = /<!--#([a-z0-9-]+)-->\n([\s\S]*?)<!--\/#\1-->/g;
  let m;
  while ((m = re.exec(text)) !== null) out.set(m[1], m[2].replace(/\n$/, ''));
  return out;
}

/** Splice `fragment` in immediately before the `</section>` that closes
 *  `id="view-<viewId>"`, counting nested `<section>` tags so a view that
 *  contains sub-sections still closes where it really closes.
 *
 *  Anchoring on the opening id and walking to the real close is the only
 *  approach that survives the fact that every `</section>` in this file is
 *  identical — a plain string search for the closing tag would silently land in
 *  the wrong view. */
export function appendInsideView(html, viewId, fragment) {
  const openTag = `<section class="view`;
  const needle = `id="view-${viewId}"`;
  const idAt = html.indexOf(needle);
  if (idAt < 0) throw new Error(`view not found: ${viewId}`);
  // Walk back to the `<section` that owns this id, then forward, tracking depth.
  let cursor = html.lastIndexOf(openTag, idAt);
  if (cursor < 0) throw new Error(`opening <section> not found for: ${viewId}`);
  let depth = 0;
  while (cursor < html.length) {
    const nextOpen = html.indexOf('<section', cursor);
    const nextClose = html.indexOf('</section>', cursor);
    if (nextClose < 0) throw new Error(`unbalanced <section> for view: ${viewId}`);
    if (nextOpen >= 0 && nextOpen < nextClose) {
      depth += 1;
      cursor = nextOpen + '<section'.length;
      continue;
    }
    depth -= 1;
    if (depth === 0) {
      return html.slice(0, nextClose) + fragment + '\n' + html.slice(nextClose);
    }
    cursor = nextClose + '</section>'.length;
  }
  throw new Error(`unbalanced <section> for view: ${viewId}`);
}

/** Insert `fragment` immediately after the `</section>` that closes the view. */
export function insertAfterView(html, viewId, fragment) {
  const needle = `id="view-${viewId}"`;
  const idAt = html.indexOf(needle);
  if (idAt < 0) throw new Error(`view not found: ${viewId}`);
  let cursor = html.lastIndexOf('<section', idAt);
  let depth = 0;
  while (cursor < html.length) {
    const nextOpen = html.indexOf('<section', cursor);
    const nextClose = html.indexOf('</section>', cursor);
    if (nextClose < 0) throw new Error(`unbalanced <section> for view: ${viewId}`);
    if (nextOpen >= 0 && nextOpen < nextClose) { depth += 1; cursor = nextOpen + 8; continue; }
    depth -= 1;
    const end = nextClose + '</section>'.length;
    if (depth === 0) return html.slice(0, end) + '\n' + fragment + html.slice(end);
    cursor = end;
  }
  throw new Error(`unbalanced <section> for view: ${viewId}`);
}

/** Splice a fragment in right after an exact, unique anchor string. */
function insertAfterAnchor(html, anchor, fragment, label) {
  const at = html.indexOf(anchor);
  if (at < 0) throw new Error(`anchor not found for ${label}: ${anchor.slice(0, 60)}`);
  if (html.indexOf(anchor, at + 1) >= 0) {
    throw new Error(`anchor is not unique for ${label}: ${anchor.slice(0, 60)}`);
  }
  const end = at + anchor.length;
  return html.slice(0, end) + fragment + html.slice(end);
}

/** Replace an exact source string, refusing to run unless it occurs exactly
 *  once. This is how 06 states a *copy* change to the frozen 05 shell without
 *  editing 05: the old wording is named explicitly, the new wording is authored
 *  in additions.html, and a 05 edit that removed or duplicated the string makes
 *  this throw instead of silently patching the wrong place. */
export function replaceUnique(html, from, to, label) {
  const at = html.indexOf(from);
  if (at < 0) throw new Error(`replace source not found for ${label}: ${from.slice(0, 60)}`);
  if (html.indexOf(from, at + 1) >= 0) {
    throw new Error(`replace source is not unique for ${label}: ${from.slice(0, 60)}`);
  }
  return html.slice(0, at) + to + html.slice(at + from.length);
}

/** Which existing view each new module panel belongs to. The panels are
 *  appended *inside* those views, so the seven original views keep their own
 *  markup and copy untouched — the additions land below them. */
const PANEL_VIEWS = [
  ['panel-models', 'models'],
  ['panel-api', 'api'],
  ['panel-observability', 'observability'],
  ['panel-evaluation', 'evaluation'],
  ['panel-admin', 'admin'],
];

export function derive() {
  const v5 = readLf(V5);
  const v5Sha = sha256(v5);
  if (v5Sha !== V5_SHA256) {
    throw new Error(
      `prototypes/v5/index.html changed: expected ${V5_SHA256}, got ${v5Sha}. ` +
        `Atelier 05 is frozen; do NOT regenerate — investigate first.`,
    );
  }

  const frags = readFragments(readLf(ADD_HTML));
  const additionsCss = readLf(ADD_CSS).trimEnd();
  const need = (name) => {
    const value = frags.get(name);
    if (!value) throw new Error(`missing fragment in additions.html: ${name}`);
    return value;
  };

  let html = v5;

  // 1. Navigation. Both spellings, in the same position in each: right after
  //    API 管理 / API, so the new page sits beside the other per-principal one.
  html = insertAfterAnchor(
    html,
    '<button class="navitem" data-page="api">API 管理</button>',
    need('nav-top'),
    'topnav',
  );
  html = insertAfterAnchor(
    html,
    '<button class="navitem" data-page="api">API</button>',
    need('nav-mobile'),
    'mobile-nav',
  );

  // 2. Module panels, appended inside the five existing views.
  for (const [fragmentName, viewId] of PANEL_VIEWS) {
    html = appendInsideView(html, viewId, need(fragmentName));
  }

  // 3. The new call-log view, as a sibling after the last existing view.
  html = insertAfterView(html, 'admin', need('view-calls'));

  // 4. The fourth CSS layer, immediately before </style>.
  const styleEnd = html.lastIndexOf('\n</style>');
  if (styleEnd < 0) throw new Error('</style> not found');
  html = html.slice(0, styleEnd) + '\n' + additionsCss + '\n' + html.slice(styleEnd + 1);

  // The provenance line in the <title>/comment area must name 06, not 05.
  html = html.replace(
    'DESIGN STUDY <i>№ 05 / 2026</i>',
    'DESIGN STUDY <i>№ 06 / 2026</i>',
  );

  // 5. Copy/structure patches to the 05 shell. Each names the exact 05 wording it
  //    retires and replaces it with either a short literal or an authored
  //    fragment, so the change is deterministic and reviewable and 05 stays
  //    frozen. The retired wording is the *visible* text only — no behaviour of
  //    the 05 shell is changed beyond the account explainer, which now describes
  //    the Linux/PAM sign-in that replaces the retired 飞书 design.
  for (const [from, to, label] of copyPatches(need)) html = replaceUnique(html, from, to, label);

  return { html, v5Sha };
}

/** The declared copy/structure patches, as `[from, to, label]` triples. Exported
 *  so the test suite can recompute the patched shell and prove that v6 edited
 *  05 *only* at these sites — a hand-edit still fails. `need` resolves an
 *  additions.html fragment by name. */
export function copyPatches(need) {
  return [
    [
      '<meta name="description" content="Dell LLM Lab 统一管理后台：Strata、Gateway、Grafana、Prometheus、EvalScope、API 与飞书身份。界面原型，非生产数据。">',
      '<meta name="description" content="Dell LLM Lab 统一管理后台：Strata、Gateway、Grafana、Prometheus、EvalScope、API 与 Linux 账号身份（PAM 待接线）。界面原型，非生产数据。">',
      'meta-description',
    ],
    [
      '<button class="account" id="accountButton"><span class="auth-demo">F</span><span class="account-label">飞书登录</span><span class="account-arrow"> ↗</span></button>',
      '<button class="account" id="accountButton" aria-haspopup="dialog" data-account-state="preview"><span class="auth-demo" aria-hidden="true">L</span><span class="account-label" data-account-label>预览访问 · 未建立 Linux 会话</span><span class="account-arrow"> …</span></button>',
      'header-account',
    ],
    [
      '<p>飞书负责登录身份；Portal 控制用户、项目、模型与 API 权限。物理部署仍需独立审批。</p>',
      '<p>Linux 账号（PAM）负责登录身份；Portal 控制用户、项目、模型与 API 权限。物理部署仍需独立审批。</p>',
      'admin-hero',
    ],
    [
      '<button class="secondary" id="previewAuth">查看飞书登录方案 ↗</button>',
      '<button class="secondary" id="previewAuth">查看 Linux 登录方案</button>',
      'admin-auth-button',
    ],
    [
      '<div class="tiny">统一入口</div><strong>飞书 OAuth 2.0</strong><div class="badge warn" style="margin-top:10px">未启用的设计状态</div>',
      '<div class="tiny">统一入口</div><strong>Linux 账号 / PAM</strong><div class="badge warn" style="margin-top:10px">预览未接线 · 需服务端 PAM 会话</div>',
      'admin-identity',
    ],
    [
      '<tr><td>飞书 Session</td><td>浏览器登录 Portal</td><td>Gateway API Key</td></tr>',
      '<tr><td>Linux PAM 会话</td><td>浏览器登录 Portal</td><td>Gateway API Key</td></tr>',
      'admin-trust-row',
    ],
    [
      "$('#accountButton').onclick=()=>explain('飞书统一登录（设计方案）','<p>真实平台会通过 <strong>飞书 OAuth 2.0 / 扫码授权</strong> 建立 Portal 服务端 Session，再由 Portal RBAC 授予用户、项目及 API 权限。</p><p>当前为静态演示页，不发起 OAuth 请求，不创建用户或密钥。正式上线须配置企业应用、回调白名单、租户 allowlist、Cookie / CSRF、权限表和 Caddy 信任边界。</p>');",
      need('account-explain'),
      'account-explainer',
    ],
    [
      '<span class="unit">GB <small>GDDR6</small></span><span class="note">RTX A6000</span>',
      '<span class="unit">GB <small>GDDR6</small></span><span class="note">RTX A6000 · 标称规格，非可用 VRAM</span>',
      'gpu-nominal',
    ],
    [
      '<span class="badge"><span class="dot"></span>示意已加载</span>',
      '<span class="badge warn"><span class="dot rust"></span>示意 · 非真实加载</span>',
      'model-loaded-badge',
    ],
    [
      '<div class="stats"><div class="stat"><span>请求总量</span><strong class="number">1,248</strong><small>示例计数</small></div><div class="stat"><span>成功率</span><strong class="number">99.4%</strong><small>示例比例</small></div><div class="stat"><span>响应 p95</span><strong class="number">1.42 s</strong><small>示例延迟</small></div><div class="stat"><span>输出 Tokens</span><strong class="number">482k</strong><small>示例用量</small></div></div>',
      need('home-stats'),
      'home-stats',
    ],
    [
      '<div class="group-title"><h2>已连接的平台组件</h2><span class="smallmeta">PLATFORM SURFACES / 04</span></div>\n   <div class="service-grid"><article class="service"><div class="service-icon">S</div><h3>Strata</h3><p>模型执行引擎。对话与调用使用 Portal 自有界面，通过 Gateway 访问后端。</p><div class="service-foot"><span>API ADAPTER</span><span>01 ↗</span></div></article><article class="service"><div class="service-icon">G<span class="monogram-tail">.</span></div><h3>Gateway</h3><p>统一模型路由、请求计量、Key 与限额的可信服务端来源。</p><div class="service-foot"><span>REST · USAGE</span><span>02 ↗</span></div></article><article class="service"><div class="service-icon">P<span class="monogram-tail">.</span></div><h3>Prometheus / Grafana</h3><p>首页指标原生绘制，完整图表经同源代理进入 Grafana 深度视图。</p><div class="service-foot"><span>METRICS · DEEP LINK</span><span>03 ↗</span></div></article><article class="service"><div class="service-icon">E</div><h3>EvalScope</h3><p>评测数据、运行记录和报告汇总。完整原生工具按兼容性开放。</p><div class="service-foot"><span>REPORT ADAPTER</span><span>04 ↗</span></div></article></div>',
      need('platform-hub'),
      'overview-platform-cards',
    ],
    [
      '<p>原生完整交互图表，经可信反代与 SSO 权限映射进入。</p>',
      '<p>原生完整交互图表，经可信反代与统一权限校验进入。</p>',
      'grafana-sso-copy',
    ],
    [
      '<div class="demo-strip" role="note"><span><strong>ATELIER 05 · 视觉与动效原型</strong> · 所有运行数据均为示意，尚未连接 Dell 生产服务。</span><span class="mono">VISUAL LANGUAGE 05 / MOTION WITH RESTRAINT</span></div>',
      '<div class="demo-strip" role="note" data-demo-strip><span><strong data-strip-title>ATELIER 06 / 视觉与动效原型</strong> · <span data-demo-state>主机遥测正在连接；个人调用 / 评测数据需 Linux PAM 与 Gateway 授权后可见，未验证处显示 UNKNOWN。</span></span><span class="mono" data-strip-right>HOST TELEMETRY · USER DATA GATED</span></div>',
      'demo-strip-mixed-source',
    ],
    // The literal demo numbers in 05's own views become neutral placeholders in
    // the shipped HTML, with the demo value parked in a data attribute. So the
    // static first paint never shows a fabricated KPI, and the sample numbers
    // only reappear when a reader explicitly asks for the design-samples mode.
    [
      '<footer class="footbar"><span>DELL LLM LAB / ATELIER OBSERVATORY Nº 05</span><span>MOTION · CLARITY · RESTRAINT &nbsp; / &nbsp; 纯离线前端概念 · 不含真实密钥</span></footer>',
      '<footer class="footbar"><span>DELL LLM LAB / ATELIER OBSERVATORY Nº 06</span><span>MOTION · CLARITY · RESTRAINT &nbsp; / &nbsp; 只读预览 · 无真实密钥 · 未接入用户数据</span></footer>',
      'footbar',
    ],
    [
      '<div class="stat"><span>我的 Endpoints</span><strong class="number">02</strong><small>示例</small></div>',
      '<div class="stat"><span>我的 Endpoints</span><strong class="number" data-demo-value="02">—</strong><small data-stat-note>待 Gateway BFF</small></div>',
      'api-stat-endpoints',
    ],
    [
      '<div class="stat"><span>个人 Key</span><strong class="number">02</strong><small>均已遮蔽</small></div>',
      '<div class="stat"><span>个人 Key</span><strong class="number" data-demo-value="02">—</strong><small data-stat-note>待授权签发</small></div>',
      'api-stat-keys',
    ],
    [
      '<div class="stat"><span>本月调用</span><strong class="number">8,412</strong><small>演示计量</small></div>',
      '<div class="stat"><span>本月调用</span><strong class="number" data-demo-value="8,412">—</strong><small data-stat-note>待 Gateway BFF</small></div>',
      'api-stat-calls',
    ],
    [
      '<strong id="atlasCalls">1,248</strong>',
      '<strong id="atlasCalls" data-demo-value="1,248">—</strong>',
      'atlas-calls',
    ],
    [
      '<strong id="atlasTokens">482k</strong>',
      '<strong id="atlasTokens" data-demo-value="482k">—</strong>',
      'atlas-tokens',
    ],
    [
      '<span id="atlasScope">全部 / 3 个业务方向 / 9 类工作负载</span>',
      '<span id="atlasScope">待授权数据源</span>',
      'atlas-scope',
    ],
    [
      '<span id="atlasBasis">分母 1,248 次 · 完全合成</span>',
      '<span id="atlasBasis">无授权分母</span>',
      'atlas-basis',
    ],
  ];
}

function main() {
  const check = process.argv.includes('--check');
  const { html, v5Sha } = derive();
  if (check) {
    const current = existsSync(OUT) ? readLf(OUT) : null;
    if (current !== html) {
      console.error(`DRIFT: ${OUT} does not match what v5 + additions produce`);
      process.exit(1);
    }
    console.log('ok: prototypes/v6/index.html matches v5 + additions');
  } else {
    writeFileSync(OUT, html, 'utf8');
    console.log(`wrote ${OUT} (${Buffer.byteLength(html)} bytes)`);
  }
  const sha = sha256(html);
  console.log(`v5 source sha256: ${v5Sha}`);
  console.log(`v6 result sha256: ${sha}`);
}

if (process.argv[1] && resolve(process.argv[1]) === resolve(fileURLToPath(import.meta.url))) main();
