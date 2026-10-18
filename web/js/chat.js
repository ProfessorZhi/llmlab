/*! web/js/chat.js
 *  The demo conversation box. It is deliberately a dead end: the reply is a
 *  fixed local string and nothing is sent anywhere. Making that explicit in
 *  code (rather than leaving a fetch that happens to 404) is the point —
 *  there is no transport in this module at all.
 *
 *  Because there is no transport, the composer is only operable in the explicit
 *  design-sample mode (`?demo=1`). On a live URL the send button and the text
 *  field are disabled and carry the missing precondition, so the page never
 *  offers a control that looks like it would reach a model and does not. The
 *  chat region is hidden wholesale in live mode by `demo-mode.js`; disabling
 *  here is the belt to that braces, because a control that is merely hidden can
 *  be revealed by any CSS regression.
 */
import { $, prefersReducedMotion } from './dom.js';
import { DATA_MODES } from './demo-mode.js';

export const OFFLINE_REPLY = '这是离线设计原型：消息未发送到 Gateway 或 Strata。正式页面会显示流式响应、模型来源和用量归属。';

/** Why the composer cannot act outside the sample mode. */
export const COMPOSER_GATE_REASON = '需要一个经 Linux PAM 身份授权的 Gateway 模型路由；本预览不发送、也不模拟模型回复。';

export function initChat({ doc = document, mode = DATA_MODES.LIVE } = {}) {
  const form = $('#chatForm', doc);
  const input = $('#chatInput', doc);
  const log = $('#chatLog', doc);
  if (!form || !input || !log) return null;

  const demo = mode === DATA_MODES.DEMO;
  const send = form.querySelector('button[type="submit"], button');

  if (!demo) {
    // Disabled *and* explained: `title` for the pointer, `aria-disabled` for the
    // assistive path, and the reason is also printed in the page by the gated
    // notice that stands in for this whole region.
    for (const node of [input, send]) {
      if (!node) continue;
      node.disabled = true;
      node.setAttribute('aria-disabled', 'true');
      node.title = COMPOSER_GATE_REASON;
    }
    input.placeholder = '未接线：需要 Gateway 模型路由授权后才可以发送';
  }

  function bubble(text, who) {
    const node = doc.createElement('div');
    node.className = `chatbubble ${who}`;
    node.textContent = text;
    log.append(node);
    return node;
  }

  form.addEventListener('submit', (e) => {
    e.preventDefault();
    if (!demo) return; // no transport exists; a sample must not be produced
    const value = input.value.trim();
    if (!value) return;

    bubble(value, 'me');
    const reply = bubble(OFFLINE_REPLY, 'bot');
    input.value = '';
    reply.scrollIntoView({ behavior: prefersReducedMotion() ? 'instant' : 'smooth', block: 'nearest' });
  });

  // --- the model picker -----------------------------------------------------
  //
  // The shell's model `<select>` was an enabled control with no listener at all:
  // choosing a different entry changed nothing and said nothing. It is an
  // authored sample list, so the honest fix is not to disable it in demo mode
  // but to make the choice *visible* and to keep saying that the choice reaches
  // no engine. Outside demo mode the region is hidden anyway; the picker is
  // disabled too, for the same belt-and-braces reason as the composer.
  const picker = $('#view-models select[aria-label="演示模型"]', doc);
  let routeNote = null;

  function reflectRoute() {
    if (!picker) return null;
    if (!routeNote) {
      routeNote = doc.createElement('p');
      routeNote.className = 'tiny mono';
      routeNote.dataset.modelRoute = '';
      routeNote.setAttribute('aria-live', 'polite');
      const anchor = picker.closest?.('.selector') ?? picker.parentElement;
      anchor?.after ? anchor.after(routeNote) : anchor?.parentElement?.appendChild(routeNote);
    }
    routeNote.textContent = `样例路由：${picker.value} · 未连接任何推理引擎，不会发送请求`;
    return routeNote;
  }

  if (picker) {
    if (demo) {
      reflectRoute();
      picker.addEventListener('change', reflectRoute);
    } else {
      picker.disabled = true;
      picker.setAttribute('aria-disabled', 'true');
      picker.title = COMPOSER_GATE_REASON;
    }
  }

  return {
    bubble,
    reflectRoute,
    get mode() { return demo ? DATA_MODES.DEMO : DATA_MODES.LIVE; },
    get modelRoute() { return routeNote?.textContent ?? null; },
    get messages() { return log.querySelectorAll('.chatbubble').length; },
  };
}
