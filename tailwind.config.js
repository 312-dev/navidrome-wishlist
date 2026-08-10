/* Tailwind v4 is CSS-first, so this file carries only what CSS cannot say.
 *
 * The content globs are the reason it exists. Class names reach the browser from
 * three places that do not look like each other: Jinja templates, the SSE client
 * that builds plate markup from an event payload, and Basecoat's own component
 * CSS. Miss one and the class is compiled away, which shows up as an unstyled
 * plate only after a live update, never in a page load.
 *
 * Referenced from libwish/web/input.css via `@config`. */
module.exports = {
  content: [
    './libwish/web/templates/**/*.html',
    './libwish/web/static/js/libwish.js',
    './libwish/web/views.py',
    './tools/vendor/basecoat/**/*.css',
  ],

  /* Class names the compiler cannot see because they are assembled at runtime
   * from an event field: `plate--working`, `row--new` and friends are written
   * by libwish.js, and the phase names come off the wire. Listed rather than
   * built by string concatenation in JS so this stays greppable. */
  safelist: [
    'is-working', 'is-refused', 'is-owned', 'is-wanted', 'is-no-store',
    'row--new', 'row--leaving',
  ],
};
