/*! web/js/atlas/render-detail.js
 *  The locked/previewed workload readout, plus the breadcrumb that says which
 *  filter and lock are currently in force. */
import { escapeHtml } from '../dom.js';
import { fmt, num, withUnit, isKnown, UNKNOWN } from '../format.js';
import { sum, groupName, hit } from './selectors.js';

export function renderDetail(node, ctx) {
  const { state, groups } = ctx;
  const d = ctx.findRow(hit(state));
  const subset = ctx.visible();
  const total = sum(subset, 'calls');
  const groupText = state.group === 'all' ? '全局工作负载' : groupName(groups, state.group);

  if (!d) {
    // No lock: summarise the current scope instead of inventing a headline row.
    node.innerHTML = `<div class="context">当前范围 · ${escapeHtml(groupText)}</div>`
      + '<h3>调用结构总览</h3>'
      + `<div class="focus-number">${fmt(total)}<small>请求 · DEMO</small></div>`
      + '<dl>'
      + `<div><dt>负载数量</dt><dd>${subset.length}</dd></div>`
      + `<div><dt>输出 Tokens</dt><dd>${fmt(sum(subset, 'tokens'))}k</dd></div>`
      + `<div><dt>业务方向</dt><dd>${state.group === 'all' ? 3 : 1}</dd></div>`
      + '<div><dt>合成 p95</dt><dd>不聚合</dd></div>'
      + '</dl>'
      + '<div class="atlas-focus-note">悬停预览、点击锁定单项。不同请求集的 p95 不能直接相加或加权平均。</div>';
    return;
  }

  const share = (d.calls / Math.max(total, 1)) * 100;
  node.innerHTML = `<div class="context">${escapeHtml(groupName(groups, d.g))} · ${state.preview ? '临时预览' : '已锁定'}</div>`
    + `<h3>${escapeHtml(d.name)}</h3>`
    + `<div class="focus-number">${fmt(d.calls)}<small>调用请求</small></div>`
    + '<dl>'
    + `<div><dt>当前范围占比</dt><dd>${num(share)}%</dd></div>`
    + `<div><dt>p95 延迟</dt><dd>${withUnit(d.p95, 'ms')}</dd></div>`
    + `<div><dt>输出 Tokens</dt><dd>${isKnown(d.tokens) ? `${fmt(d.tokens)}k` : UNKNOWN}</dd></div>`
    + '<div><dt>计量口径</dt><dd>合成样本</dd></div>'
    + '</dl>'
    + `<div class="atlas-focus-note">${escapeHtml(d.desc)}。再点一次取消锁定；可在排名区加入对比。</div>`;
}

export function renderBreadcrumb(base, ctx) {
  const { state, groups } = ctx;
  base.replaceChildren();
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.textContent = '全部工作负载';
  btn.onclick = () => ctx.actions.reset();
  btn.setAttribute('aria-label', '恢复全部负载与清除锁定');
  base.append(btn);

  const rest = document.createElement('span');
  rest.textContent = ` / ${state.group === 'all' ? '全部方向' : groupName(groups, state.group)}`;
  base.append(rest);

  if (state.selected) {
    const d = ctx.findRow(state.selected);
    if (d) {
      const s = document.createElement('span');
      s.textContent = ` / ${d.name}`;
      base.append(s);
    }
  }
}
