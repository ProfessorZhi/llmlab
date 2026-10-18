/*! tests/web-features/live-chart-and-freshness.test.js
 *
 *  Two "the page said something it could not back up" bugs, and the fixes:
 *
 *   1. The overview chart drew a 24-point synthetic curve on **every** load,
 *      including a bare URL whose page text said the call data was not
 *      connected. Live mode now draws an unavailable state instead, and the
 *      metric/range switches are disabled with the precondition named.
 *   2. The chat composer and the model picker looked operable outside the
 *      design-sample mode, where no transport exists.
 *
 *  The host read-out's freshness contract moved to
 *  `tests/web-features/host-freshness-contract.test.js` when it became a ported
 *  state machine with its own shared fixtures: an UNKNOWN verdict, a future
 *  timestamp and a hung request are all fail-closed cases that deserve to sit
 *  next to the fixture set they are measured against.
 *
 *  No jsdom: the doubles below model only what these two modules actually use.
 */
import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DATA_MODES,
} from '../../web/js/demo-mode.js';
import {
  DEFAULT_RANGE,
  LIVE_CHART_TITLE,
  RANGE_WINDOWS,
  TIMELINE_SERIES,
  deriveSeries,
  initTimeline,
} from '../../web/js/timeline.js';
import { COMPOSER_GATE_REASON, OFFLINE_REPLY, initChat } from '../../web/js/chat.js';

// --- shared double helpers --------------------------------------------------

/** `dom.js` asks the media query directly; Node has no `matchMedia`. Reporting
 *  reduced motion keeps the demo chart off its animation path, which needs
 *  SVG measurement the double does not model. */
globalThis.matchMedia ??= () => ({ matches: true, addEventListener() {} });

const element = (extra = {}) => {
  const node = {
    dataset: {},
    attributes: {},
    style: {},
    hidden: false,
    value: '',
    textContent: '',
    innerHTML: '',
    placeholder: '',
    disabled: false,
    children: [],
    parentElement: null,
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    setAttribute(name, value) { node.attributes[name] = String(value); },
    getAttribute(name) { return node.attributes[name] ?? null; },
    hasAttribute: (name) => name in node.attributes,
    removeAttribute(name) { delete node.attributes[name]; },
    addEventListener(type, handler) { (node._listeners[type] ??= []).push(handler); },
    removeEventListener() {},
    dispatch(type, event = {}) { for (const h of node._listeners[type] ?? []) h({ ...event, currentTarget: node }); },
    appendChild(child) { child.parentElement = node; node.children.push(child); return child; },
    append(...nodes) { for (const child of nodes) node.appendChild(child); },
    prepend(child) { child.parentElement = node; node.children.unshift(child); return child; },
    insertBefore(child, before) {
      child.parentElement = node;
      const at = node.children.indexOf(before);
      if (at < 0) node.children.push(child); else node.children.splice(at, 0, child);
      return child;
    },
    after() {},
    querySelector: () => null,
    querySelectorAll: () => [],
    scrollIntoView() {},
    _listeners: {},
    ...extra,
  };
  return node;
};

/** A document whose `querySelector`/`querySelectorAll` answer by exact selector
 *  key — enough for modules that ask for a fixed, known set of nodes. */
const keyedDoc = (map = {}) => {
  const doc = element({
    documentElement: element(),
    createElement: (tag) => element({ tagName: tag }),
    querySelector: (sel) => map[sel] ?? null,
    querySelectorAll: (sel) => (Array.isArray(map[sel]) ? map[sel] : (map[sel] ? [map[sel]] : [])),
  });
  return doc;
};

// --- 1. the overview chart --------------------------------------------------

function chartDoc() {
  const svg = element({ id: 'timelineChart' });
  const plot = element();
  plot.append = (child) => plot.appendChild(child);
  plot.appendChild(svg);
  const legend = element();
  const metricButtons = ['calls', 'latency', 'tokens'].map((kind) => element({ dataset: { chart: kind } }));
  const rangeButtons = Object.keys(RANGE_WINDOWS).map((value) => element({ dataset: { range: value } }));
  const doc = keyedDoc({
    '#timelineChart': svg,
    '[data-chart]': metricButtons,
    '[data-range]': rangeButtons,
    '.legendline .mono': legend,
  });
  return { doc, svg, legend, metricButtons, rangeButtons };
}

test('a live URL draws no synthetic curve, and says why it cannot', () => {
  const { doc, svg, legend, metricButtons, rangeButtons } = chartDoc();
  const chart = initTimeline({ doc, mode: DATA_MODES.LIVE });

  assert.equal(chart.mode, DATA_MODES.LIVE);
  assert.deepEqual(chart.series, [], 'no authored point may reach a live chart');
  assert.equal(svg.dataset.chartState, 'unavailable');
  assert.match(svg.innerHTML, new RegExp(LIVE_CHART_TITLE));
  assert.match(svg.innerHTML, /等待 Gateway usage BFF 授权/);
  assert.match(svg.innerHTML, /不绘制合成曲线/);
  assert.doesNotMatch(svg.innerHTML, /data-series-line/, 'no plot path may be drawn');
  assert.equal(legend.textContent, 'NO SOURCE · 0 POINTS');
  assert.equal(metricButtons.every((b) => b.disabled), true);
  assert.equal(rangeButtons.every((b) => b.disabled), true);
  assert.match(metricButtons[0].title, /无可授权数据源/);
});

test('a live chart cannot be driven into drawing a sample', () => {
  const { doc, svg } = chartDoc();
  const chart = initTimeline({ doc, mode: DATA_MODES.LIVE });
  chart.chart('tokens');
  chart.setRange('30d');
  assert.deepEqual(chart.series, []);
  assert.equal(svg.dataset.chartState, 'unavailable');
});

test('demo mode still draws the authored sample, and the switches change it', () => {
  const { doc, svg, legend, rangeButtons } = chartDoc();
  const chart = initTimeline({ doc, mode: DATA_MODES.DEMO });

  assert.equal(svg.dataset.chartState, 'demo');
  assert.equal(chart.series.length, RANGE_WINDOWS[DEFAULT_RANGE].count);
  assert.match(svg.innerHTML, /data-series-line/);
  assert.equal(legend.textContent, `DEMO · ${RANGE_WINDOWS[DEFAULT_RANGE].count} POINTS`);
  assert.equal(rangeButtons.every((b) => b.disabled), false);

  // The range switches used to repaint nothing but an aria-label.
  rangeButtons.find((b) => b.dataset.range === '7d').dispatch('click');
  assert.equal(chart.range, '7d');
  assert.equal(chart.series.length, RANGE_WINDOWS['7d'].count);
  assert.equal(legend.textContent, 'DEMO · 7 POINTS');
  assert.match(svg.innerHTML, /-7d/, 'the axis must follow the window');
});

test('deriveSeries is a deterministic resample of the authored shape', () => {
  const base = TIMELINE_SERIES.calls;
  assert.deepEqual(deriveSeries(base, '24h'), [...base], 'the 24h window is the sample itself');
  const week = deriveSeries(base, '7d');
  assert.equal(week.length, 7);
  assert.deepEqual(deriveSeries(base, '7d'), week, 'same input, same window, same numbers');
  assert.ok(week.every((value) => Number.isFinite(value) && value >= 0));
  // Bucket averages stay inside the range of the series they came from.
  assert.ok(Math.min(...week) >= Math.min(...base) && Math.max(...week) <= Math.max(...base));
  assert.equal(deriveSeries(base, 'nonsense').length, RANGE_WINDOWS['24h'].count,
    'an unknown window falls back to the default rather than drawing nothing');
});

// --- 2. the chat composer ---------------------------------------------------

function chatDoc() {
  const form = element();
  const input = element();
  const log = element({ querySelectorAll: () => log.children });
  const send = element();
  form.querySelector = (sel) => (sel.startsWith('button') ? send : null);
  const picker = element({ value: 'Qwen3.8 Flash Next / Strata', closest: () => selectorParent });
  const selectorParent = element({ after: (node) => selectorParent.parentElement.appendChild(node) });
  const columns = element();
  columns.appendChild(selectorParent);
  const doc = keyedDoc({
    '#chatForm': form,
    '#chatInput': input,
    '#chatLog': log,
    '#view-models select[aria-label="演示模型"]': picker,
  });
  return { doc, form, input, log, send, picker, selectorParent, columns };
}

test('outside the sample mode the composer is disabled and says what is missing', () => {
  const { doc, form, input, send, picker } = chatDoc();
  const chat = initChat({ doc, mode: DATA_MODES.LIVE });

  assert.equal(input.disabled, true);
  assert.equal(send.disabled, true);
  assert.equal(send.attributes['aria-disabled'], 'true');
  assert.equal(send.title, COMPOSER_GATE_REASON);
  assert.equal(picker.disabled, true);
  assert.match(input.placeholder, /未接线/);

  // A submit that arrives anyway (Enter on a disabled field, a future CSS
  // mistake) must not fabricate a model reply.
  form.dispatch('submit', { preventDefault() {} });
  assert.equal(chat.messages, 0);
});

test('in the sample mode the composer answers with the offline reply, and says so', () => {
  const { doc, form, input, log } = chatDoc();
  const chat = initChat({ doc, mode: DATA_MODES.DEMO });
  assert.equal(input.disabled, false);

  input.value = '你好';
  form.dispatch('submit', { preventDefault() {} });
  assert.equal(chat.messages, 2, 'the message and one reply');
  assert.equal(log.children[1].textContent, OFFLINE_REPLY);
  assert.equal(input.value, '', 'the field is cleared after sending');
});

test('the model picker is wired: choosing a route is visible and reaches nothing', () => {
  const { doc, picker, columns } = chatDoc();
  const chat = initChat({ doc, mode: DATA_MODES.DEMO });

  assert.equal(columns.children.length, 2, 'a route note is placed beside the picker');
  assert.match(columns.children[1].textContent, /样例路由/);
  assert.match(chat.modelRoute, /未连接任何推理引擎，不会发送请求/);

  picker.value = 'vLLM · 未安装';
  picker.dispatch('change');
  assert.match(chat.modelRoute, /vLLM/);
  assert.match(chat.modelRoute, /不会发送请求/);
});
