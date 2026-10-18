/*! web/js/workspace/lifecycle.js — the Endpoint journey and its SDK examples.
 *
 *  The product's essential journey is: own a deployment → verify it → register a
 *  private Endpoint → share it → the grantee invokes it with their own
 *  credential → revoke → the next call fails. Every step already has a reviewed
 *  executor: the `llmlab` CLI (Registry/CLI lane #46, `cli/llmlab/entry.py`).
 *  Only *one* step has a reviewed browser route — the share write,
 *  `PUT /api/v2/ops/deployments/{id}/share` (`ops.share` in
 *  `backend/bff/route_map.py`) — and it is exercised from the Models page.
 *
 *  So this panel does not pretend the browser can do the rest. Each step shows
 *  the exact CLI command and, separately, whether a web action exists and why
 *  not. The SDK tabs template a request with **placeholders only**: no host, no
 *  key, nothing that could be mistaken for a working credential.
 */

import { escapeHtml } from '../dom.js';

/** Steps, in journey order. `cli` must name a real `llmlab` subcommand. */
export const LIFECYCLE_STEPS = Object.freeze([
  {
    id: 'deploy', title: '登记部署', cli: 'llmlab deployments register …\nllmlab deployments verify <deployment-id>',
    body: '在你自己的 Linux 账户下运行引擎，Registry 只接受本机 loopback + 端口白名单的目标地址。',
    web: null, why: 'Registry 写入没有 BFF 路由；由 CLI 以你的 scoped token 执行。',
  },
  {
    id: 'register', title: '登记 Endpoint（默认私有）', cli: 'llmlab endpoints register --name <alias> --deployment <deployment-id>\nllmlab endpoints verify <endpoint-id>',
    body: 'Registered ≠ Verified ≠ Ready：登记之后必须验证，验证通过也不等于路由已生效。',
    web: null, why: '没有 BFF 写路由；CLI 执行并记审计。',
  },
  {
    id: 'share', title: '共享给指定用户', cli: 'llmlab endpoints share <endpoint-id> --mode users --grantee <linux-user>',
    body: '只有已验证的 Endpoint 能共享。可见性由服务端按 principal × 项目判定。',
    web: 'models', why: 'Web：PUT /api/v2/ops/deployments/{id}/share（ops.share，需 CSRF 与 portal:endpoints:share）。在「模型与对话」页操作；live 写入待会话验证后启用。',
  },
  {
    id: 'invoke', title: '经授权网关调用', cli: 'llmlab invoke <endpoint-id> --prompt "hello"',
    body: '被授权者用自己的凭据调用；调用元数据出现在其本人的「我的调用」中，不含正文。',
    web: null, why: 'Playground 发送需要已验证的同源 Gateway 写契约（取消 / 超时 / 回执）；当前未登记。',
  },
  {
    id: 'revoke', title: '撤销共享', cli: 'llmlab endpoints unshare <endpoint-id> [--grantee <linux-user>]',
    body: '撤销立即生效：被撤销者的下一次调用必须失败，并可在审计中追溯。',
    web: 'models', why: 'Web：同一条 ops.share 路由把范围改回 Private。',
  },
  {
    id: 'repoint', title: '重指向 / 回滚', cli: 'llmlab endpoints repoint <endpoint-id> --deployment <deployment-id>\nllmlab endpoints rollback <endpoint-id>',
    body: '稳定 alias 切换到新部署；Router ACK 之前不视为生效（Router ACK Applied ≠ Callable）。',
    web: null, why: '没有 BFF 写路由；CLI 执行，ACK 由路由同步回写。',
  },
]);

/** Placeholder-only request templates. `alias` is user text and is escaped by
 *  the renderer; quotes are stripped so a pasted alias cannot break the shell
 *  quoting of the example. */
export function sdkSnippets(alias = '<endpoint-alias>') {
  const a = String(alias || '<endpoint-alias>').replace(/["'`\\$]/g, '').slice(0, 80) || '<endpoint-alias>';
  return {
    curl:
`curl "$LLMLAB_GATEWAY_URL/v1/chat/completions" \\
  -H "Authorization: Bearer $LLMLAB_KEY" \\
  -H "Content-Type: application/json" \\
  -d '{"model": "${a}", "messages": [{"role": "user", "content": "hello"}], "stream": false}'`,
    python:
`import os
from openai import OpenAI  # OpenAI 兼容客户端

client = OpenAI(
    base_url=os.environ["LLMLAB_GATEWAY_URL"] + "/v1",
    api_key=os.environ["LLMLAB_KEY"],  # 你自己的 scoped key，切勿写进代码
)
resp = client.chat.completions.create(
    model="${a}",
    messages=[{"role": "user", "content": "hello"}],
    timeout=60,
)
print(resp.choices[0].message.content)`,
    javascript:
`// Node 20+，在服务端运行；不要把 key 放进浏览器
const res = await fetch(\`\${process.env.LLMLAB_GATEWAY_URL}/v1/chat/completions\`, {
  method: "POST",
  headers: {
    Authorization: \`Bearer \${process.env.LLMLAB_KEY}\`,
    "Content-Type": "application/json",
  },
  body: JSON.stringify({ model: "${a}", messages: [{ role: "user", content: "hello" }] }),
  signal: AbortSignal.timeout(60_000),
});
if (!res.ok) throw new Error(\`gateway \${res.status}\`);
console.log((await res.json()).choices[0].message.content);`,
    cli:
`# 已登录的 llmlab CLI（scoped token 由服务端签发）
llmlab whoami
llmlab endpoints list
llmlab invoke ${a} --prompt "hello"`,
  };
}

const TABS = [['curl', 'curl'], ['python', 'Python'], ['javascript', 'JavaScript'], ['cli', 'llmlab CLI']];

export function renderLifecycle() {
  const steps = LIFECYCLE_STEPS.map((s) => (
    `<li class="ws-step" data-step="${s.id}"><h3>${escapeHtml(s.title)}</h3>` +
    `<p>${escapeHtml(s.body)}</p><code>${escapeHtml(s.cli)}</code>` +
    `<div class="ws-foot">` +
    (s.web
      ? `<span class="ws-state" data-state="not_configured">WEB · 有路由</span><a class="secondary" href="#${s.web}">前往</a>`
      : `<span class="ws-state" data-state="not_configured">WEB · 无路由</span>`) +
    `<span class="tiny" style="width:100%">${escapeHtml(s.why)}</span></div></li>`
  )).join('');
  return `<ol class="ws-steps">${steps}</ol>`;
}

export function renderSdk(alias, active = 'curl') {
  const snippets = sdkSnippets(alias);
  const tabs = TABS.map(([id, label]) => (
    `<button type="button" class="secondary" role="tab" id="ws-sdk-tab-${id}" aria-controls="ws-sdk-panel-${id}" ` +
    `aria-selected="${id === active}" tabindex="${id === active ? 0 : -1}" data-sdk-tab="${id}">${label}</button>`
  )).join('');
  const panels = TABS.map(([id]) => (
    `<div class="ws-tabpanel" role="tabpanel" id="ws-sdk-panel-${id}" aria-labelledby="ws-sdk-tab-${id}" data-sdk-panel="${id}"${id === active ? '' : ' hidden'}>` +
    `<pre class="codearea" data-sdk-code="${id}">${escapeHtml(snippets[id])}</pre></div>`
  )).join('');
  return `<div class="ws-tabs" role="tablist" aria-label="接入示例语言">${tabs}</div>${panels}`;
}

/** Mount the panel into `#view-api`, after the token panel if present. */
export function mountLifecycle({ doc = globalThis.document, dialog = null, clipboard = globalThis.navigator?.clipboard } = {}) {
  const view = doc?.getElementById?.('view-api');
  if (!view) return null;
  const section = doc.createElement('section');
  section.className = 'page-block ws-panel';
  section.dataset.ws = 'lifecycle';
  section.setAttribute('aria-labelledby', 'ws-lifecycle-title');
  section.innerHTML =
    '<div class="group-title"><h2 class="section-h" id="ws-lifecycle-title">Endpoint 生命周期与接入 <em class="heading-en">/ OWN → SHARE → INVOKE → REVOKE</em></h2>' +
    '<span class="smallmeta">CLI-FIRST · WEB WHERE ROUTED</span></div>' +
    '<p class="ws-lede">每一步都有经评审的执行者。Web 只在存在同源 BFF 路由的那一步提供入口；其余由 <span class="mono">llmlab</span> CLI 以你的 scoped token 执行。</p>' +
    `<div data-ws-steps>${renderLifecycle()}</div>` +
    '<div class="ws-split" style="margin-top:22px"><div>' +
    '<div class="group-title" style="margin-top:0"><h3 class="section-h" style="font-size:15px">接入示例</h3>' +
    '<label class="field" style="flex-direction:row;align-items:center;gap:8px"><span class="tiny">Endpoint alias</span>' +
    '<input type="text" data-sdk-alias placeholder="&lt;endpoint-alias&gt;" maxlength="80" style="width:180px"></label></div>' +
    '<div data-sdk-host></div>' +
    '<div class="actions"><button type="button" class="secondary" data-sdk-copy>复制当前示例</button>' +
    '<span class="tiny">地址与 key 都是环境变量占位符；本页不提供可用的网关地址或凭据。</span></div>' +
    '</div><aside class="flatpanel"><h3>为什么没有「直接调用」按钮</h3>' +
    '<p>浏览器端发起推理需要一条经评审的同源 Gateway 写契约：CSRF、流式取消、超时与结果回执，并以 Linux PAM 会话主体计量。这条契约尚未登记到 BFF 路由表，所以 Playground 的发送保持禁用。</p>' +
    '<div class="guard warn">不要在浏览器或前端代码里保存 key；SDK 示例应在服务端或本地脚本中运行。</div></aside></div>';
  const tokens = view.querySelector('[data-ws="tokens"]');
  const hero = view.querySelector(':scope > .hero');
  if (tokens) tokens.after(section); else if (hero) hero.after(section); else view.prepend(section);

  const host = section.querySelector('[data-sdk-host]');
  const aliasInput = section.querySelector('[data-sdk-alias]');
  let active = 'curl';
  const paint = () => {
    host.innerHTML = renderSdk(aliasInput.value, active);
  };
  const select = (id, focus = false) => {
    active = id;
    paint();
    if (focus) host.querySelector(`[data-sdk-tab="${id}"]`)?.focus();
  };
  host.addEventListener('click', (e) => {
    const tab = e.target.closest?.('[data-sdk-tab]');
    if (tab) select(tab.dataset.sdkTab);
  });
  host.addEventListener('keydown', (e) => {
    if (!e.target.closest?.('[data-sdk-tab]') || !['ArrowRight', 'ArrowLeft'].includes(e.key)) return;
    const ids = TABS.map(([id]) => id);
    const i = ids.indexOf(active) + (e.key === 'ArrowRight' ? 1 : -1);
    select(ids[(i + ids.length) % ids.length], true);
    e.preventDefault();
  });
  aliasInput.addEventListener('input', paint);
  section.querySelector('[data-sdk-copy]').addEventListener('click', async () => {
    const text = sdkSnippets(aliasInput.value)[active];
    try {
      if (!clipboard?.writeText) throw new Error('clipboard unavailable');
      await clipboard.writeText(text);
      dialog?.explain?.('示例已复制', '<p>复制的是<strong>占位符请求</strong>：网关地址与 key 都是环境变量，不含任何真实凭据。</p>');
    } catch {
      dialog?.explain?.('复制提示', '<p>浏览器未授予剪贴板权限，可从代码块手动复制；示例不含任何真实凭据。</p>');
    }
  });
  paint();
  return { select, get active() { return active; }, section };
}
