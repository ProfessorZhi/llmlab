/*! web/js/build-profile.js — the build profile, and the demo gate built on it.
 *
 *  This console ships as two artifacts, and the profile is a property of the
 *  artifact — not of the session, the URL, or anything a page can set:
 *
 *    production  the only profile that is deployed. The design samples are not
 *                merely hidden here: this bundle has no path that can put them
 *                on screen. `?demo=1` is inert, the sample-mode entry is never
 *                built into the DOM, and nothing in a URL, query or hash can
 *                turn any of that back on.
 *    qa          an isolated preview (web/qa.html) generated for reviewing the
 *                design samples. Only here does the explicit `?demo=1` opt-in
 *                reach the synthetic fixture.
 *
 *  The profile is written into the generated HTML by
 *  `web/tools/extract-from-prototype.mjs`, which emits `data-build-profile` on
 *  `<html>`. Because it is baked into the file the browser is running, a script,
 *  a query parameter or a third party cannot promote a production page to qa.
 *  Promoting a page is a *deploy* decision, which is the only place it belongs.
 *
 *  Everything here fails closed. A missing attribute, an empty string, a typo,
 *  a value someone invented at runtime, a document that is not a document — all
 *  resolve to `production`.
 */

export const PROFILES = Object.freeze({ PRODUCTION: 'production', QA: 'qa' });

/**
 * The profile this document was built as.
 *
 * Read-only by construction: it is read off the element, never written back.
 * The only accepted value is the exact string `qa`; every other input — however
 * plausible — is production.
 *
 * @param {{documentElement?: {dataset?: Record<string,string>}}} [doc]
 * @returns {'production'|'qa'}
 */
export function buildProfile(doc = globalThis.document) {
  const declared = doc?.documentElement?.dataset?.buildProfile;
  return declared === PROFILES.QA ? PROFILES.QA : PROFILES.PRODUCTION;
}

/**
 * Is this the isolated QA preview? The one and only key to the design samples.
 *
 * Every demo path in the front-end calls this rather than re-reading an
 * attribute, so "where can a sample come from?" has exactly one answer.
 */
export const isQaBuild = (doc) => buildProfile(doc) === PROFILES.QA;
