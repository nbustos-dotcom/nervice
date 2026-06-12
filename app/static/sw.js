/* Nervice service worker — served at /sw.js (top-level scope covers /v3).
   __VER__ is substituted with the server's git short hash at serve time, so every commit
   gets a fresh cache name and old caches are swept on activate.

   STRATEGY (deliberately minimal — the stale-page trap must not get worse):
   - HTML documents: NETWORK-FIRST, never cached. Offline -> a minimal inline status page.
   - Icons + manifest ONLY: cache-first (immutable per version).
   - EVERYTHING else (API routes, /health, WS upgrades, anything authed): NOT intercepted —
     the request falls through to the network exactly as if no SW existed. Auth headers and
     responses never touch a cache. */
const VER = '__VER__';
const CACHE = 'nervice-static-' + VER;
const CACHEABLE = [
  '/static/icons/icon-180.png',
  '/static/icons/icon-192.png',
  '/static/icons/icon-512.png',
  '/static/icons/icon-512-maskable.png',
  '/static/manifest.webmanifest',
];

const OFFLINE_HTML = `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>NERVICE · UNREACHABLE</title>
<style>body{margin:0;min-height:100dvh;display:flex;align-items:center;justify-content:center;
background:#0a0a12;color:#8aa6ac;font-family:ui-monospace,Consolas,monospace;text-align:center;padding:24px}
b{color:#b07cff;letter-spacing:.2em}p{font-size:13px;line-height:1.6}</style></head>
<body><p><b>NERVICE UNREACHABLE</b><br><br>is the laptop on?<br>
(server + Tailscale must both be up — pull down to retry)</p></body></html>`;

self.addEventListener('install', (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(CACHEABLE)).catch(() => {}).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const req = e.request;
  if (req.method !== 'GET') return;                       // never touch non-GET
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;        // never touch cross-origin

  // HTML documents: network-first, NEVER cached — a stale index is worse than no index.
  if (req.mode === 'navigate' || req.destination === 'document') {
    e.respondWith(
      fetch(req).catch(() =>
        new Response(OFFLINE_HTML, { status: 503, headers: { 'Content-Type': 'text/html; charset=utf-8' } }))
    );
    return;
  }

  // Icons + manifest only: cache-first (these are immutable for a given VER).
  if (CACHEABLE.includes(url.pathname)) {
    e.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(req, copy));
        }
        return res;
      }))
    );
    return;
  }
  // anything else (API, /health, authed fetches): fall through untouched — no respondWith.
});
