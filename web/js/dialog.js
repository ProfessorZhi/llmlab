/*! web/js/dialog.js
 *  The one modal used by the shell. Every trigger in this build explains what
 *  the real platform *would* do; none of them performs the action. Keeping the
 *  copy in one place is what makes that claim auditable — there is no code path
 *  here that opens an OAuth flow, mints a key, or calls an endpoint.
 */
import { $ } from './dom.js';

/** Verbatim from the prototype: a placeholder, not a working key. */
export const CURL_EXAMPLE = "curl https://lab.example/v1/chat/completions -H 'Authorization: Bearer <YOUR_SCOPED_KEY>' -H 'Content-Type: application/json' -d '{\"model\":\"qwen3.8-flash-next-iq2_xs\",\"messages\":[]}'";

export const EXPLAIN_LINUX = {
  title: '预览访问 · 未建立 Linux 会话',
  body: '<p>当前是<strong>受 Basic Auth 保护的只读预览</strong>：主机遥测为真实只读数据，<strong>个人调用 / 评测 / 密钥数据不可访问</strong>。</p><p><strong>Linux PAM 登录尚未启用。</strong>本页不索取 Linux 密码，不向 /auth/linux/login 提交任何凭据，也不伪造登录状态；客户端参数与 Basic Auth 都不构成 Linux 主体。</p><p>正式登录需服务端 PAM 会话（<span class="mono">GET /auth/me</span> 返回 authenticated:true 才显示用户名 / UID），并配置 Cookie / CSRF 与信任边界，另行立项。</p>',
};

export const EXPLAIN_ENDPOINT = {
  title: '申请 Endpoint',
  body: '<p>正式流程为：<strong>选择模型 → 配置 Deployment Profile → Dry-run → 项目审批 → 发布私有 Endpoint</strong>。</p><p>此演示不创建任何 Endpoint、不占用 GPU、不开放端口。生产发布必须额外授权。</p>',
};

export const EXPLAIN_COPIED = {
  title: '示例已复制',
  body: '<p>复制的是<strong>占位符请求</strong>，无实际 API Key，也不指向真实服务器。</p>',
};

export const EXPLAIN_COPY_BLOCKED = {
  title: '复制提示',
  body: '<p>浏览器未授予剪贴板权限，可从代码块手动复制；不需要任何真实密钥。</p>',
};

export function createDialog({ doc = document, clipboard = globalThis.navigator?.clipboard } = {}) {
  const dlg = $('#explainDialog', doc);

  function explain(title, html) {
    $('#modalTitle', doc).textContent = title;
    $('#modalBody', doc).innerHTML = html;
    if (!dlg.open) dlg.showModal();
  }

  function open(spec) { explain(spec.title, spec.body); }
  function close() { if (dlg.open) dlg.close(); }

  $('#closeDialog', doc).addEventListener('click', close);
  $('#confirmDialog', doc).addEventListener('click', close);

  const accountExplainer = () => open(EXPLAIN_LINUX);
  $('#accountButton', doc).addEventListener('click', accountExplainer);
  $('#previewAuth', doc).addEventListener('click', accountExplainer);
  $('#newEndpoint', doc).addEventListener('click', () => open(EXPLAIN_ENDPOINT));

  $('#copyCurl', doc).addEventListener('click', async () => {
    try {
      if (!clipboard?.writeText) throw new Error('clipboard unavailable');
      await clipboard.writeText(CURL_EXAMPLE);
      open(EXPLAIN_COPIED);
    } catch {
      open(EXPLAIN_COPY_BLOCKED);
    }
  });

  return { explain, open, close, dialog: dlg };
}
