/*! web/js/events.js — cross-module event names, so producers and consumers do
 *  not have to import each other. */

/** Fired on `document` after the shell switches light/dark theme.
 *  Charts paint literal colours read from :root, so they must redraw. */
export const THEME_EVENT = 'llm-lab:theme-change';
