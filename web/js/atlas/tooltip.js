/*! web/js/atlas/tooltip.js — the single floating readout shared by every chart.
 *  Suppressed on touch pointers and narrow viewports, where hover has no
 *  meaning and the same values are available in the detail panel. */

/** Below this width the pointer tooltip is pointless; the side panel carries it. */
export const TOOLTIP_MIN_WIDTH = 541;

export function createTooltip(tip) {
  function showTip(html, event) {
    if (!event || event.pointerType === 'touch' || innerWidth < TOOLTIP_MIN_WIDTH) return;
    tip.hidden = false;
    tip.innerHTML = html;
    const b = tip.getBoundingClientRect();
    const x = Math.max(8, Math.min(innerWidth - b.width - 10, event.clientX + 15));
    const y = Math.max(8, Math.min(innerHeight - b.height - 10, event.clientY + 14));
    tip.style.left = `${x}px`;
    tip.style.top = `${y}px`;
    tip.dataset.open = 'true';
  }
  function hideTip() {
    tip.dataset.open = 'false';
    tip.hidden = true;
  }
  return { showTip, hideTip };
}
