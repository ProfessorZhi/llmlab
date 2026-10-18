/* ==========================================================================
   bus -- the app's few global signals.

   Only three things are genuinely app-wide: "refresh everything", "the theme
   changed", and "a view wants to be told when it becomes visible again".
   Anything more specific belongs to the view that owns it.
   ========================================================================== */

const target = new EventTarget();

export function emit(type, detail) {
  target.dispatchEvent(new CustomEvent(type, { detail }));
}

export function on(type, fn) {
  const handler = (ev) => fn(ev.detail);
  target.addEventListener(type, handler);
  return () => target.removeEventListener(type, handler);
}

/** The user asked for fresh data everywhere. */
export const requestRefresh = (scope = 'all') => emit('refresh', { scope });
export const onRefresh = (fn) => on('refresh', fn);
