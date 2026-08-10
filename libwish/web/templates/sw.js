/* Service worker. Rendered by the app so the cache name carries the same build
 * digest the asset URLs do: a new build is a new cache, and the old one is
 * deleted on activate rather than lingering.
 *
 * What this worker does NOT do is as important as what it does. It never
 * answers a navigation, an API call or the event stream from cache. Every one
 * of those carries state that changes while the page is open, and a wishlist
 * that shows yesterday's rows because a worker had them on disk would be worse
 * than one that fails to load. Only the shell is cached: the stylesheet, the
 * scripts, the typeface and the icons, all of which are immutable at a given
 * digest.
 */

const VERSION = '{{ version }}';
const SHELL = 'libwish-shell-' + VERSION;

const PRECACHE = [
  {%- for url in precache %}
  '{{ url }}',
  {%- endfor %}
];

self.addEventListener('install', (event) => {
  // The new worker takes over as soon as it is ready. Waiting for every tab to
  // close would leave a stale shell in front of a freshly deployed app, which
  // on a single-user LAN app is the wrong trade.
  event.waitUntil(
    caches.open(SHELL).then((cache) => cache.addAll(PRECACHE)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((names) => Promise.all(names.filter((n) => n !== SHELL).map((n) => caches.delete(n))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  // The event stream is a response that never ends. Putting it through a
  // handler that resolves with a whole body would hang the page rather than
  // stream it.
  if (url.pathname === '{{ events_path }}') return;

  // Shell assets are immutable at this digest, so the cache is the answer and
  // the network is the fallback for a first visit that missed the precache.
  if (url.pathname.startsWith('/static/')) {
    event.respondWith(
      caches.match(request).then((hit) => hit || fetch(request).then((response) => {
        if (response.ok) {
          const copy = response.clone();
          caches.open(SHELL).then((cache) => cache.put(request, copy));
        }
        return response;
      }))
    );
    return;
  }

  // Everything else is live. A page that cannot be fetched says so in the
  // app's own voice instead of the browser's error page; anything that is not
  // a navigation is simply allowed to fail, because a stale answer to a
  // question about state is worse than no answer.
  if (request.mode === 'navigate') {
    event.respondWith(
      fetch(request).catch(() => caches.match('{{ offline_url }}'))
    );
  }
});
