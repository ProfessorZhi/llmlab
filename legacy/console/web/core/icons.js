/* ==========================================================================
   icons -- inline SVG, no sprite file and no font.

   A font-based icon set costs an extra request, renders as tofu when it fails,
   and cannot be recoloured per-instance without extra rules.  These are 24x24
   stroked paths on `currentColor`, which means an icon inherits the colour of
   whatever it is placed inside (including a :hover or a status class) with no
   extra CSS, and they scale with `font-size` contexts.

   Geometry is deliberately uniform: 24 box, 1.8 stroke, round caps and joins.
   ========================================================================== */

const PATHS = {
  // -- navigation ---------------------------------------------------------
  overview:   'M4 13h7V4H4v9Zm0 7h7v-6H4v6Zm9 0h7V11h-7v9Zm0-16v6h7V4h-7Z',
  cube:       'M12 2.8 20.5 7v10L12 21.2 3.5 17V7L12 2.8Zm0 0v18.4M3.5 7 12 11.5 20.5 7',
  flask:      'M9 3h6M10 3v6.2L5.2 17.4A2 2 0 0 0 7 20.5h10a2 2 0 0 0 1.8-3.1L14 9.2V3M7.4 14.6h9.2',
  cpu:        'M6.5 6.5h11v11h-11zM9.5 9.5h5v5h-5zM10 3v3.5M14 3v3.5M10 17.5V21M14 17.5V21M3 10h3.5M3 14h3.5M17.5 10H21M17.5 14H21',
  file:       'M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8l-5-5Zm0 0v5h5M8.5 13h7M8.5 17h4.5',
  plug:       'M9 3v6M15 3v6M6.5 9h11v3a5.5 5.5 0 0 1-11 0V9ZM12 17.5V21',
  gear:       'M12 15.2a3.2 3.2 0 1 0 0-6.4 3.2 3.2 0 0 0 0 6.4Z'
            + 'M19.4 14.5a1.6 1.6 0 0 0 .32 1.77l.06.06a1.94 1.94 0 1 1-2.75 2.75l-.06-.06a1.6 1.6 0 0 0-1.77-.32 1.6 1.6 0 0 0-.97 1.47v.17a1.94 1.94 0 1 1-3.88 0v-.09a1.6 1.6 0 0 0-1.05-1.47 1.6 1.6 0 0 0-1.77.32l-.06.06a1.94 1.94 0 1 1-2.75-2.75l.06-.06a1.6 1.6 0 0 0 .32-1.77 1.6 1.6 0 0 0-1.47-.97H3.4a1.94 1.94 0 1 1 0-3.88h.09a1.6 1.6 0 0 0 1.47-1.05 1.6 1.6 0 0 0-.32-1.77l-.06-.06A1.94 1.94 0 1 1 7.33 4.3l.06.06a1.6 1.6 0 0 0 1.77.32h.08a1.6 1.6 0 0 0 .97-1.47V3.4a1.94 1.94 0 1 1 3.88 0v.09a1.6 1.6 0 0 0 .97 1.47 1.6 1.6 0 0 0 1.77-.32l.06-.06a1.94 1.94 0 1 1 2.75 2.75l-.06.06a1.6 1.6 0 0 0-.32 1.77v.08a1.6 1.6 0 0 0 1.47.97h.17a1.94 1.94 0 1 1 0 3.88h-.09a1.6 1.6 0 0 0-1.47.97Z',

  // -- actions ------------------------------------------------------------
  refresh:    'M20.4 12a8.4 8.4 0 1 1-2.46-5.94M20.4 4.2v5.4h-5.4',
  external:   'M14 4h6v6M20 4l-8.5 8.5M18 14.5V19a1.5 1.5 0 0 1-1.5 1.5h-11A1.5 1.5 0 0 1 4 19V8a1.5 1.5 0 0 1 1.5-1.5H10',
  download:   'M12 3.5v11M7.5 10 12 14.5 16.5 10M4.5 19.5h15',
  play:       'M7 4.8 19 12 7 19.2V4.8Z',
  search:     'M11 18.5a7.5 7.5 0 1 0 0-15 7.5 7.5 0 0 0 0 15ZM16.4 16.4 20.5 20.5',
  close:      'M6 6l12 12M18 6 6 18',
  chevron:    'M9 6l6 6-6 6',
  chevronDown:'M6 9l6 6 6-6',
  arrowRight: 'M4 12h15M13 6l6 6-6 6',
  menu:       'M4 7h16M4 12h16M4 17h16',
  copy:       'M9 9h9.5v11H9zM6.5 15H5V4h9.5v1.5',

  // -- theme --------------------------------------------------------------
  sun:        'M12 16.6a4.6 4.6 0 1 0 0-9.2 4.6 4.6 0 0 0 0 9.2ZM12 2v2.2M12 19.8V22M4.2 4.2l1.6 1.6M18.2 18.2l1.6 1.6M2 12h2.2M19.8 12H22M4.2 19.8l1.6-1.6M18.2 5.8l1.6-1.6',
  moon:       'M20.4 14.6A8.6 8.6 0 0 1 9.4 3.6a8.6 8.6 0 1 0 11 11Z',

  // -- status -------------------------------------------------------------
  check:      'M4.5 12.5 9.5 17.5 19.5 6.5',
  alert:      'M12 3.2 21.4 19.6H2.6L12 3.2ZM12 9.6v4.2M12 17h.01',
  info:       'M12 21.5a9.5 9.5 0 1 0 0-19 9.5 9.5 0 0 0 0 19ZM12 11v5.5M12 7.8h.01',
  offline:    'M3 3l18 18M10.6 5.1A9.5 9.5 0 0 1 21.5 12M3.2 8.4A9.5 9.5 0 0 0 12 21.5',
  shield:     'M12 3 19.5 6v6c0 4.4-3.1 8.2-7.5 9.4C7.6 20.2 4.5 16.4 4.5 12V6L12 3Z',
  clock:      'M12 21.5a9.5 9.5 0 1 0 0-19 9.5 9.5 0 0 0 0 19ZM12 7v5.3l3.4 2',
  activity:   'M3 12.5h4l3-7.5 4 15 3-7.5h4',
  zap:        'M13.5 2.5 4.5 13.8h6.2l-1.2 7.7 9-11.3h-6.2l1.2-7.7Z',
  layers:     'M12 2.8 21 7.5l-9 4.7-9-4.7 9-4.7ZM3 12.3l9 4.7 9-4.7M3 16.8l9 4.7 9-4.7',
  drive:      'M4 5.5h16v5H4zM4 13.5h16v5H4zM7 8h.01M7 16h.01',
  gauge:      'M12 21.5a9.5 9.5 0 1 0 0-19 9.5 9.5 0 0 0 0 19ZM12 12l4.2-3.2',
  thermometer:'M14 14.9V4.5a2 2 0 0 0-4 0v10.4a4 4 0 1 0 4 0Z',
  bolt:       'M12 3v6M12 15v6M3 12h6M15 12h6',
  link:       'M10 13.5a3.5 3.5 0 0 0 5 0l3-3a3.54 3.54 0 0 0-5-5l-1.5 1.5M14 10.5a3.5 3.5 0 0 0-5 0l-3 3a3.54 3.54 0 0 0 5 5l1.5-1.5',
  filter:     'M3.5 5h17l-6.5 8v6l-4 2v-8L3.5 5Z',
  table:      'M3.5 4.5h17v15h-17zM3.5 10h17M9.5 10v9.5',
  chart:      'M4 20V10M10 20V4M16 20v-7M22 20H2',
  server:     'M4 4.5h16v6H4zM4 13.5h16v6H4zM7.5 7.5h.01M7.5 16.5h.01',
  key:        'M14.5 10.5a4.5 4.5 0 1 0-4.2 4.5L12 17l1.6-.8 1 1.4 1.9-.3.3-1.9-1.4-1 1.1-1.9-1-1.4ZM16 8.5h.01',
};

/**
 * An icon as an SVG string.  Sized in `em` so it tracks the text it sits in;
 * pass `cls` to size or colour it from CSS instead.
 */
export function icon(name, cls = '', size = 18) {
  const d = PATHS[name];
  if (!d) return '';
  const c = cls ? ` class="ico ${cls}"` : ' class="ico"';
  return `<svg${c} width="${size}" height="${size}" viewBox="0 0 24 24" fill="none"`
       + ` stroke="currentColor" stroke-width="1.8" stroke-linecap="round"`
       + ` stroke-linejoin="round" aria-hidden="true" focusable="false">`
       + `<path d="${d}"/></svg>`;
}

export const hasIcon = (name) => Object.hasOwn(PATHS, name);
